"""
nvenc-shrink — safely re-encode a media library (Jellyfin, Plex, Emby, plain
folders) to HEVC to save disk space, without touching anything it can't verify.

For each candidate file:
  1. encode three short samples in RAM and predict the final size; skip files
     that wouldn't shrink enough (costs ~10 s instead of a full encode)
  2. encode to "<name>.<ext>.shrinking-tmp" in the SAME folder as the original
  3. verify the new file (duration, stream counts, full decode pass, size saving)
  4. only if every check passes: delete the original and rename the new file
     into the original's path (same name, so media servers keep watched state)

Dry run by default. Nothing is modified without --apply.

Skips files already HEVC/AV1/VP9, HDR / Dolby Vision, low-bitrate files,
hardlinked files (e.g. still seeding), and anything that won't shrink enough.
State lives in a small SQLite DB so reruns pick up where they left off.
"""

import argparse
import datetime as dt
import fcntl
import json
import os
import shlex
import shutil
import signal
import stat
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".flv", ".webm"}
SKIP_CODECS = {"hevc", "av1", "vp9"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
from nvenc_shrink import __version__

PROG = "nvenc-shrink"
SYSTEM_CONF = "/etc/nvenc-shrink.conf"
USER_CONF = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "nvenc-shrink.conf")
DEFAULT_CQ = {"hevc_nvenc": 27, "libx265": 24}
DEFAULT_ESTIMATE_CQ = {"hevc_nvenc": [24, 27, 30], "libx265": [22, 24, 26]}


def conf_path():
    """Config file in use: $NVENC_SHRINK_CONF, then ~/.config (non-root), then /etc."""
    if os.environ.get("NVENC_SHRINK_CONF"):
        return os.environ["NVENC_SHRINK_CONF"]
    if os.path.isfile(USER_CONF) and os.geteuid() != 0:
        return USER_CONF
    return SYSTEM_CONF


def default_db():
    if os.geteuid() == 0:
        return "/var/lib/nvenc-shrink/state.db"
    state = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(state, "nvenc-shrink", "state.db")


WORKDIR_PREFIX = ".nvenc-shrink-work-"  # hidden: media servers and *arr apps skip dot folders
TMP_SUFFIX = ".shrinking-tmp"   # temp files from 1.x (written beside the original); only cleaned up now
MUXERS = {".mkv": "matroska", ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov"}


def real_ext(p):
    """Container extension of an output path, ignoring the temp suffix."""
    name = p.name[:-len(TMP_SUFFIX)] if p.name.endswith(TMP_SUFFIX) else p.name
    return Path(name).suffix.lower()

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")


USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
COLORS = [  # (prefix of the message after indentation, ANSI style)
    ("done:", "32"), ("samples predict", "36"), ("skipping", "33"), ("no worthwhile gain", "33"),
    ("FAILED", "31;1"), ("sample check failed", "31"), ("interrupted", "31"), ("finished", "1"),
    ("dry run:", "1"), ("estimate cq", "1"), ("would convert", "0"), ("checking", "1"),
]


def log(msg):
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if USE_COLOR:
        body = msg.lstrip()
        style = next((c for prefix, c in COLORS if body.startswith(prefix)), None)
        if style:
            msg = f"{msg[:len(msg) - len(body)]}\033[{style}m{body}\033[0m"
        print(f"\033[2m[{ts}]\033[0m {msg}", flush=True)
    else:
        print(f"[{ts}] {msg}", flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# ---------------------------------------------------------------- probing

def probe(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    return json.loads(out.stdout)


def real_video_streams(info):
    return [s for s in info["streams"]
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]


def duration_of(info):
    d = info.get("format", {}).get("duration")
    if d:
        return float(d)
    vs = real_video_streams(info)
    if vs and vs[0].get("duration"):
        return float(vs[0]["duration"])
    return 0.0


def is_dolby_vision(stream):
    for sd in stream.get("side_data_list", []) or []:
        if "DOVI" in sd.get("side_data_type", "") or "Dolby Vision" in sd.get("side_data_type", ""):
            return True
    return stream.get("codec_tag_string", "") in {"dvh1", "dvhe", "dva1", "dvav"}


def stream_counts(info):
    counts = {}
    for s in info["streams"]:
        t = s.get("codec_type")
        if t == "video" and s.get("disposition", {}).get("attached_pic"):
            t = "cover"
        counts[t] = counts.get(t, 0) + 1
    return counts


def classify(path, info, min_kbps_per_mpx):
    """Return (skip_reason or None, details dict)."""
    vs = real_video_streams(info)
    if not vs:
        return "no video stream", {}
    v = vs[0]
    codec = v.get("codec_name", "?")
    if codec in SKIP_CODECS:
        return f"already {codec}", {}
    if v.get("color_transfer") in HDR_TRANSFERS:
        return "HDR source", {}
    if is_dolby_vision(v):
        return "Dolby Vision", {}
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    dur = duration_of(info)
    if not (w and h and dur):
        return "missing width/height/duration", {}
    size = path.stat().st_size
    total_kbps = size * 8 / dur / 1000
    mpx = w * h / 1e6
    kbps_per_mpx = total_kbps / mpx
    details = {"codec": codec, "res": f"{w}x{h}", "kbps": round(total_kbps), "kbps_per_mpx": round(kbps_per_mpx)}
    if kbps_per_mpx < min_kbps_per_mpx:
        return f"already low bitrate ({round(total_kbps)} kbps)", details
    return None, details


# ---------------------------------------------------------------- encoding

def output_container(src):
    ext = src.suffix.lower()
    if ext in {".mkv"}:
        return ".mkv"
    if ext in {".mp4", ".m4v", ".mov"}:
        return ext
    return ".mkv"  # avi/wmv/ts/etc. -> mkv (filename extension changes)


# NVENC options from best to most basic. Older NVENC generations (Pascal, and the
# TU117 in the T400 / GTX 1650) can't do HEVC B-frames, and some lack temporal AQ.
NVENC_FEATURE_LADDER = [
    ("B-frames + temporal AQ", ["-temporal-aq", "1", "-bf", "3", "-b_ref_mode", "middle"]),
    ("B-frames", ["-bf", "3", "-b_ref_mode", "middle"]),
    ("temporal AQ, no B-frames", ["-temporal-aq", "1", "-bf", "0"]),
    ("basic (no B-frames, no temporal AQ)", ["-bf", "0"]),
]


def detect_nvenc_features(args):
    """Test-encode a few frames with each option set; return the first one this GPU accepts."""
    for label, extra in NVENC_FEATURE_LADDER:
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
               "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24", "-frames:v", "12",
               "-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "24", "-b:v", "0",
               "-spatial-aq", "1", "-rc-lookahead", "32", *extra]
        if args.ten_bit:
            cmd += ["-profile:v", "main10", "-pix_fmt", "p010le"]
        cmd += ["-f", "null", "-"]
        if subprocess.run(cmd, capture_output=True, text=True).returncode == 0:
            return label, extra
    raise RuntimeError("hevc_nvenc unavailable (no NVIDIA GPU/driver visible, or ffmpeg built without NVENC)")


def video_encoder_args(args, cq, stream="v"):
    """Encoder options, each qualified with an output stream specifier (e.g. "-profile:0").

    Qualifying matters: an unqualified option like -profile also applies to streams that
    are only being copied (cover art, extra video tracks), and ffmpeg refuses to open the
    output if it can't set it there."""
    if args.encoder == "hevc_nvenc":
        pairs = ["-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", str(cq), "-b", "0",
                 "-spatial-aq", "1", "-rc-lookahead", "32", *args.nvenc_extra]
        pairs += ["-profile", "main10", "-pix_fmt", "p010le"] if args.ten_bit else ["-profile", "main"]
    elif args.encoder == "libx265":
        pairs = ["-preset", "medium", "-crf", str(cq), "-x265-params", "log-level=error",
                 "-pix_fmt", "yuv420p10le" if args.ten_bit else "yuv420p"]
    else:
        raise SystemExit(f"unsupported encoder {args.encoder}")
    out = []
    for opt, value in zip(pairs[::2], pairs[1::2]):
        out += [f"{opt.split(':')[0]}:{stream}", value]
    return out


SAMPLE_SECS = 20
SAMPLE_POINTS = (0.2, 0.5, 0.8)


def predict_size(src, info, args, cq):
    """Encode three short video-only samples and compare them with the same stretches
    of the original (stream-copied). Samples go to RAM-backed temp space, so the only
    disk IO is reading ~1 minute of the source. Returns projected output size in bytes."""
    dur = duration_of(info)
    vid = real_video_streams(info)[0]["index"]
    secs = min(SAMPLE_SECS, max(2.0, dur / 10))
    tmpdir = "/dev/shm" if os.path.isdir("/dev/shm") else None
    src_bytes = enc_bytes = 0
    with tempfile.TemporaryDirectory(prefix="nvenc-shrink-sample-", dir=tmpdir) as d:
        for n, frac in enumerate(SAMPLE_POINTS):
            start = f"{dur * frac:.2f}"
            t0, t1 = dur * frac, dur * frac + secs
            # Original: sum the video packet sizes whose timestamps fall exactly in [t0, t1).
            # (Stream-copying a clip would start at the previous keyframe and overstate it.)
            r1 = subprocess.run([FFPROBE, "-v", "error", "-select_streams", f"{vid}",
                                 "-read_intervals", f"{max(0, t0 - 30):.2f}%{t1 + 1:.2f}",
                                 "-show_entries", "packet=pts_time,dts_time,size", "-of", "csv=p=0", str(src)],
                                capture_output=True, text=True)
            for line in r1.stdout.splitlines():
                f = line.split(",")
                try:
                    ts = float(f[0] if f[0] not in ("", "N/A") else f[1])
                    if t0 <= ts < t1:
                        src_bytes += int(f[2])
                except (ValueError, IndexError):
                    continue
            # Re-encode: decode-accurate seek to t0, raw HEVC bitstream to RAM (no container overhead).
            b = Path(d) / f"{n}.hevc"
            hw = ["-hwaccel", "cuda"] if args.encoder == "hevc_nvenc" and args.hw_decode else []
            r2 = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error", *hw,
                                 "-ss", f"{t0:.3f}", "-t", f"{secs}", "-i", str(src), "-map", f"0:{vid}",
                                 "-c:v", args.encoder, *video_encoder_args(args, cq), "-f", "hevc", str(b)],
                                capture_output=True, text=True)
            if r1.returncode or r2.returncode:
                raise RuntimeError("sample encode failed: " + ((r2.stderr or r1.stderr).strip().splitlines() or ["?"])[-1])
            enc_bytes += b.stat().st_size
    if not src_bytes:
        raise RuntimeError("empty sample")
    ratio = enc_bytes / src_bytes
    size = src.stat().st_size
    video_total = min(size, src_bytes / (secs * len(SAMPLE_POINTS)) * dur)  # estimated video share of the file
    return int(size - video_total * (1 - ratio)), ratio


def readrate_args(path, duration, args):
    """ffmpeg -readrate that caps reading `path` at --max-read-mbps (MB/s)."""
    if not args.max_read_mbps or duration <= 0:
        return []
    bytes_per_s = path.stat().st_size / duration  # file's average data rate at 1x playback
    rate = max(0.1, args.max_read_mbps * 1e6 / bytes_per_s)
    return ["-readrate", f"{rate:.3f}", "-readrate_initial_burst", "2"]


def build_cmd(src, dst, info, args):
    ext = real_ext(dst)
    is_mp4 = ext in {".mp4", ".m4v", ".mov"}

    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error", "-stats"]
    if args.encoder == "hevc_nvenc" and args.hw_decode:
        cmd += ["-hwaccel", "cuda"]
    cmd += readrate_args(src, duration_of(info), args)
    cmd += ["-i", str(src)]

    # Map streams explicitly so we know exactly what goes in and can set per-stream codecs.
    out_idx = 0
    codec_args = []
    for s in info["streams"]:
        t = s.get("codec_type")
        i = s["index"]
        if t == "video":
            cmd += ["-map", f"0:{i}"]
            if s.get("disposition", {}).get("attached_pic"):
                codec_args += [f"-c:{out_idx}", "copy"]
            else:
                codec_args += [f"-c:{out_idx}", args.encoder, *video_encoder_args(args, args.cq, str(out_idx))]
                if is_mp4:
                    codec_args += [f"-tag:{out_idx}", "hvc1"]  # Apple/TV players need hvc1, not hev1
            out_idx += 1
        elif t in ("audio", "subtitle"):
            if is_mp4 and t == "subtitle" and s.get("codec_name") not in ("mov_text",):
                continue  # mp4 can't hold image/ass subs; they couldn't have been there anyway
            cmd += ["-map", f"0:{i}"]
            codec_args += [f"-c:{out_idx}", "copy"]
            out_idx += 1
        elif t == "attachment" and not is_mp4:
            cmd += ["-map", f"0:{i}"]
            codec_args += [f"-c:{out_idx}", "copy"]
            out_idx += 1
        # data streams are dropped (chapters/metadata are still carried below)

    cmd += codec_args
    cmd += ["-map_metadata", "0", "-map_chapters", "0"]


    if is_mp4:
        cmd += ["-movflags", "+faststart"]
    # Muxer is explicit: the temp name has no video extension, and ffmpeg's
    # default .m4v muxer ("ipod") can't hold HEVC anyway.
    cmd += ["-f", MUXERS[ext], "-max_muxing_queue_size", "4096", str(dst)]
    return cmd


def encode(src, dst, info, args):
    cmd = build_cmd(src, dst, info, args)
    cmd[1:1] = progress_args(args)
    r = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        if dst.exists():
            dst.unlink()
        err = [l for l in r.stderr.strip().splitlines() if not l.startswith("frame=")][-5:]
        raise RuntimeError("ffmpeg failed: " + " | ".join(err))


# ---------------------------------------------------------------- verification

def verify(src_info, dst, args):
    dst_info = probe(dst)
    is_mkv = real_ext(dst) == ".mkv"

    d_src, d_dst = duration_of(src_info), duration_of(dst_info)
    if abs(d_src - d_dst) > max(1.5, d_src * 0.005):
        raise RuntimeError(f"duration mismatch: {d_src:.1f}s vs {d_dst:.1f}s")

    c_src, c_dst = stream_counts(src_info), stream_counts(dst_info)
    for kind in ("video", "audio", "cover", "attachment"):
        if c_src.get(kind, 0) != c_dst.get(kind, 0) and not (kind == "attachment" and not is_mkv):
            raise RuntimeError(f"{kind} stream count changed: {c_src.get(kind, 0)} -> {c_dst.get(kind, 0)}")
    if c_dst.get("subtitle", 0) < (c_src.get("subtitle", 0) if is_mkv else 0):
        raise RuntimeError("subtitle streams lost")

    vs = real_video_streams(dst_info)
    if not vs or vs[0].get("codec_name") != "hevc":
        raise RuntimeError("output video is not HEVC")

    if not args.skip_decode_check:
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", *progress_args(args)]
        if args.encoder == "hevc_nvenc" and args.hw_decode:
            cmd += ["-hwaccel", "cuda"]
        cmd += readrate_args(dst, duration_of(dst_info), args)
        cmd += ["-i", str(dst), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or r.stderr.strip():
            first = (r.stderr.strip().splitlines() or ["non-zero exit"])[0]
            raise RuntimeError(f"decode check failed: {first}")
    return dst_info


# ---------------------------------------------------------------- state

def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY, status TEXT, reason TEXT,
        size_before INTEGER, size_after INTEGER, updated TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY, started TEXT, ended TEXT, pid INTEGER, version TEXT, settings TEXT,
        outcome TEXT, attempted INTEGER DEFAULT 0, converted INTEGER DEFAULT 0, saved INTEGER DEFAULT 0,
        failed INTEGER DEFAULT 0, no_gain INTEGER DEFAULT 0,
        current TEXT, current_size INTEGER, current_duration REAL, stage TEXT, stage_since TEXT)""")
    db.commit()
    return db


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


class RunLog:
    """One row per --apply run in the `runs` table, so --status can show what a run
    is doing right now (file, stage, progress) and how earlier runs ended."""

    COUNTERS = ("attempted", "converted", "saved", "failed", "no_gain")

    def __init__(self, db, settings, progress_path):
        self.db = db
        self.progress_path = progress_path
        # We hold the lock, so any run still marked "running" died without cleaning up.
        db.execute("UPDATE runs SET outcome='killed', ended=COALESCE(stage_since, started), "
                   "current=NULL, stage=NULL WHERE outcome='running'")
        cur = db.execute("INSERT INTO runs (started, pid, version, settings, outcome, stage, stage_since) "
                         "VALUES (?,?,?,?,'running','starting',?)",
                         (now_iso(), os.getpid(), __version__, settings, now_iso()))
        db.commit()
        self.id = cur.lastrowid
        self.ended = False

    def stage(self, stage, path=None, size=None, duration=None):
        if stage in ("encoding", "verifying"):
            self.clear_progress()
        if path is None:
            self.db.execute("UPDATE runs SET stage=?, stage_since=?, current=NULL, current_size=NULL, "
                            "current_duration=NULL WHERE id=?", (stage, now_iso(), self.id))
        else:
            self.db.execute("UPDATE runs SET stage=?, stage_since=?, current=?, current_size=?, "
                            "current_duration=? WHERE id=?", (stage, now_iso(), str(path), size, duration, self.id))
        self.db.commit()

    def count(self, field, n=1):
        assert field in self.COUNTERS
        self.db.execute(f"UPDATE runs SET {field} = {field} + ? WHERE id=?", (n, self.id))
        self.db.commit()

    def end(self, outcome):
        if self.ended:
            return
        self.ended = True
        self.clear_progress()
        self.db.execute("UPDATE runs SET outcome=?, ended=?, current=NULL, current_size=NULL, "
                        "current_duration=NULL, stage=NULL, stage_since=NULL WHERE id=?",
                        (outcome[:300], now_iso(), self.id))
        self.db.commit()

    def clear_progress(self):
        try:
            os.unlink(self.progress_path)
        except FileNotFoundError:
            pass


def progress_args(args):
    """ffmpeg -progress output for --status, when a run is being tracked."""
    runlog = getattr(args, "runlog", None)
    return ["-progress", runlog.progress_path] if runlog else []


def record(db, path, status, reason="", before=None, after=None):
    db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
               (str(path), status, reason, before, after, dt.datetime.now().isoformat(timespec="seconds")))
    db.commit()


def known(db, path):
    row = db.execute("SELECT status, size_before, size_after FROM files WHERE path=?", (str(path),)).fetchone()
    return row if row else (None, None, None)


# ---------------------------------------------------------------- main loop

def candidates(roots):
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("@eaDir", "#recycle")]
            for f in sorted(filenames):
                p = Path(dirpath) / f
                # Symlinked videos are skipped: replacing one would swap the link for a copy
                # (and as root, a link could point anywhere).
                if p.suffix.lower() in VIDEO_EXTS and not f.startswith(".") and not p.is_symlink():
                    yield p


def deadline_from(stop_at):
    """Next occurrence of HH:MM after now (so a 01:00 start with --stop-at 07:00 stops at 07:00 today)."""
    if not stop_at:
        return None
    hh, mm = map(int, stop_at.split(":"))
    now = dt.datetime.now()
    d = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return d if d > now else d + dt.timedelta(days=1)


def copy_attributes(st, path):
    """Give the new file the original's owner, mode and times, via a no-follow file handle."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            os.fchown(fd, st.st_uid, st.st_gid)  # before chmod: chown clears setuid/setgid bits
        except PermissionError:
            pass  # not root: the file stays ours
        os.fchmod(fd, stat.S_IMODE(st.st_mode))
        os.utime(fd, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError as e:
        log(f"  warning: couldn't copy file attributes: {e}")
    finally:
        os.close(fd)


def cleanup_stale(roots, apply):
    """Remove leftovers from interrupted runs. Never follows symlinks."""
    euid = os.geteuid()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for d in list(dirnames):
                p = Path(dirpath) / d
                if d.startswith(WORKDIR_PREFIX):
                    lst = os.lstat(p)
                    if stat.S_ISDIR(lst.st_mode) and lst.st_uid == euid:
                        log(f"stale work folder from an interrupted run: {p}" + (" (removing)" if apply else ""))
                        if apply:
                            shutil.rmtree(p, ignore_errors=True)
                    dirnames.remove(d)
                elif d.startswith("."):
                    dirnames.remove(d)
            for f in filenames:  # temp files written beside originals by 1.x
                if f.endswith(TMP_SUFFIX) and Path(f[:-len(TMP_SUFFIX)]).suffix.lower() in MUXERS:
                    p = Path(dirpath) / f
                    lst = os.lstat(p)
                    if stat.S_ISREG(lst.st_mode) and lst.st_uid == euid:
                        log(f"stale partial from an interrupted run: {p}" + (" (removing)" if apply else ""))
                        if apply:
                            os.unlink(p)


def fmt_duration(secs):
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_clock(secs):
    secs = int(max(0, secs))
    return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def read_progress(path):
    """Latest (position in seconds, speed multiplier) from an ffmpeg -progress file."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None, None
    pos = speed = None
    for line in tail.splitlines():
        key, _, value = line.partition("=")
        value = value.strip()
        try:
            if key == "out_time_us":
                pos = int(value) / 1e6
            elif key == "speed" and value.endswith("x"):
                speed = float(value[:-1])
        except ValueError:
            continue
    return pos, speed


def pid_is_running(pid):
    """True if `pid` is alive and still an nvenc-shrink process (not a reused pid)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # alive, owned by someone else (e.g. root's run seen by a normal user)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"nvenc" in f.read()
    except OSError:
        return True  # no /proc here: trust the signal check


def timer_status():
    """One line about the systemd timer, or None when there is no system timer to report."""
    if os.geteuid() != 0 or not os.path.isdir("/run/systemd/system") or not shutil.which("systemctl"):
        return None
    r = subprocess.run(["systemctl", "show", "nvenc-shrink.timer",
                        "-p", "LoadState,UnitFileState,ActiveState,NextElapseUSecRealtime"],
                       capture_output=True, text=True)
    props = dict(line.partition("=")[::2] for line in r.stdout.splitlines())
    if r.returncode or props.get("LoadState") != "loaded":
        return None
    if props.get("ActiveState") == "active" and props.get("NextElapseUSecRealtime"):
        return f"next scheduled run: {props['NextElapseUSecRealtime']}"
    if props.get("UnitFileState") == "enabled":
        return "timer is enabled but not active (systemctl start nvenc-shrink.timer)"
    return "timer is off — no nightly runs (systemctl enable --now nvenc-shrink.timer)"


OUTCOMES = {
    "finished": "finished (library done)",
    "limit": "stopped at --limit",
    "stop-at": "stopped at --stop-at",
    "interrupted": "interrupted (stopped by Ctrl-C or systemctl stop)",
    "killed": "ended unexpectedly (killed, crashed or power lost)",
}


def print_run_summary(r):
    print(f"  this run: {r['attempted']} checked, {r['converted']} converted, "
          f"{human(r['saved'])} saved, {r['no_gain']} not worth it, {r['failed']} failed")


def print_current_run(r, db_path):
    started = dt.datetime.fromisoformat(r["started"])
    print(f"Current run: running since {started:%a %d %b %H:%M} ({fmt_duration((dt.datetime.now() - started).total_seconds())}), "
          f"pid {r['pid']}, {PROG} {r['version']}")
    if r["settings"]:
        print(f"  settings: {r['settings']}")
    stage = r["stage"] or "?"
    since = (dt.datetime.now() - dt.datetime.fromisoformat(r["stage_since"])).total_seconds() if r["stage_since"] else 0
    if r["current"]:
        p = Path(r["current"])
        size = f", {human(r['current_size'])}" if r["current_size"] else ""
        print(f"  now:      {stage} {p.name}{size}")
        print(f"            in {p.parent}")
        dur = r["current_duration"] or 0
        pos, speed = read_progress(db_path + ".progress") if stage in ("encoding", "verifying") else (None, None)
        if pos is not None and dur:
            line = f"{min(100, 100 * pos / dur):.0f}% ({fmt_clock(pos)} of {fmt_clock(dur)})"
            if speed:
                line += f", {speed:g}x, ~{fmt_duration((dur - pos) / speed)} left"
            print(f"            {line}")
        else:
            print(f"            for {fmt_duration(since)}")
    else:
        print(f"  now:      {stage} (for {fmt_duration(since)})")
    print_run_summary(r)


def print_runs(rows):
    print(f"{'started':16}  {'took':>8}  {'checked':>7}  {'converted':>9}  {'saved':>9}  {'failed':>6}  outcome")
    for r in rows:
        took = ""
        if r["ended"]:
            took = fmt_duration((dt.datetime.fromisoformat(r["ended"]) - dt.datetime.fromisoformat(r["started"])).total_seconds())
        outcome = r["outcome"] if r["outcome"] in OUTCOMES else (r["outcome"] or "?")
        print(f"{r['started'][:16].replace('T', ' '):16}  {took:>8}  {r['attempted']:7}  {r['converted']:9}  "
              f"{human(r['saved']):>9}  {r['failed']:6}  {outcome}")


def print_status(db_path):
    if not os.path.exists(db_path):
        print(f"no state yet ({db_path}) — nothing has been run with --apply")
        line = timer_status()
        if line:
            print(line)
        return
    # Read-only, so --status never changes state or waits on a running encode.
    db = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(db_path))}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    print(f"state: {db_path}\n")

    has_runs = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
    runs = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5").fetchall() if has_runs else []
    current = runs[0] if runs and runs[0]["outcome"] == "running" and pid_is_running(runs[0]["pid"]) else None
    if current:
        print_current_run(current, db_path)
        runs = runs[1:]
    elif runs:
        last = runs[0]
        outcome = "killed" if last["outcome"] == "running" else last["outcome"]
        started = dt.datetime.fromisoformat(last["started"])
        when = f"{started:%a %d %b %H:%M}"
        if last["ended"]:
            ended = dt.datetime.fromisoformat(last["ended"])
            when += f" → {ended:%H:%M} ({fmt_duration((ended - started).total_seconds())})"
        print(f"No run in progress. Last run: {when}, {PROG} {last['version']}")
        print(f"  outcome:  {OUTCOMES.get(outcome, outcome)}")
        if outcome == "killed" and last["current"]:
            print(f"  was on:   {last['stage']} {last['current']}")
        print_run_summary(last)
    else:
        print("No runs recorded yet (run history starts with version 1.2.0).")
    line = timer_status()
    if line:
        print(line)
    earlier = runs if current else runs[1:]
    if earlier:
        print("\nearlier runs:")
        print_runs(earlier)
    print()

    rows = db.execute("""SELECT status, COUNT(*), COALESCE(SUM(size_before),0), COALESCE(SUM(size_after),0)
                         FROM files WHERE reason NOT LIKE 'replaced by %' GROUP BY status ORDER BY status""").fetchall()
    print(f"{'status':10} {'files':>7} {'before':>10} {'after':>10}")
    for status, n, before, after in rows:
        print(f"{status:10} {n:7} {human(before):>10} {human(after) if status == 'done' else '':>10}")
    done = next((r for r in rows if r[0] == "done"), None)
    if done:
        print(f"\nsaved so far: {human(done[2] - done[3])} ({100 * (1 - done[3] / done[2]):.0f}% of converted files)")
    fails = db.execute("SELECT path, reason FROM files WHERE status='failed' ORDER BY updated DESC LIMIT 10").fetchall()
    if fails:
        print("\nrecent failures:")
        for path, reason in fails:
            print(f"  {path}\n    {reason[:160]}")


def check_config(args):
    """Parse errors already exited via argparse; report what the config resolves to."""
    print(f"config: {args.conf}" + ("" if os.path.isfile(args.conf) else " (not found, using defaults)"))
    print("folders: " + (" ".join(args.roots) if args.roots else "none yet (add your library folders)"))
    missing = [r for r in args.roots if not os.path.isdir(r)]
    for r in missing:
        print(f"warning: folder not found: {r}")
    settings = {k: v for k, v in sorted(vars(args).items())
                if k not in ("roots", "conf", "check_config", "status", "apply") and v not in (None, False)}
    print("settings: " + " ".join(f"{k}={v}" for k, v in settings.items()))
    print("OK" if not missing else "OK, with warnings")


def select_encoder(args):
    """Resolve --encoder auto, detect NVENC features (cached), set args.nvenc_extra."""
    args.nvenc_extra = []
    if args.encoder == "libx265":
        return
    cache_path = args.db + ".nvenc.json"
    key = f"{FFMPEG}|gpu={os.environ.get('CUDA_VISIBLE_DEVICES', '')}|10bit={args.ten_bit}"
    cache = {}
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        pass
    # The cache stores only a label; the ffmpeg options always come from the built-in
    # list, so editing the cache file can't inject options. Unknown labels are re-tested.
    known = dict(NVENC_FEATURE_LADDER)
    cached = cache.get(key)
    cached_label = cached[0] if isinstance(cached, list) and cached else cached
    if key in cache and not args.redetect and (cached_label is None or cached_label in known):
        label = cached_label
        extra = known.get(label, [])
        log(f"NVENC: {label or 'not available'} (cached; --redetect to re-test)")
    else:
        try:
            label, extra = detect_nvenc_features(args)
        except RuntimeError as e:
            label, extra = None, []
            log(str(e))
        cache[key] = label
        try:
            with open(cache_path, "w") as f:
                json.dump(cache, f)
        except OSError:
            pass
        if label:
            log(f"NVENC on this GPU supports: {label}")
    if label:
        args.encoder, args.nvenc_extra = "hevc_nvenc", extra
    elif args.encoder == "hevc_nvenc":
        sys.exit("--encoder hevc_nvenc requested but NVENC isn't usable here (try --redetect, or --encoder libx265)")
    else:
        log("falling back to CPU encoding (libx265) — slower, but better compression")
        args.encoder = "libx265"


def build_parser():
    ap = argparse.ArgumentParser(prog=PROG,
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*", help="library folders to scan (default: from the config file)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--apply", action="store_true", help="actually encode and replace (default is dry run)")
    ap.add_argument("--status", action="store_true", help="show the current run (file, stage, progress) or how the last run ended, "
                         "recent runs, the next scheduled run, and savings so far, then exit")
    ap.add_argument("--check-config", action="store_true",
                    help="check the config file and show the settings it gives, then exit")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many files are converted")
    ap.add_argument("--encoder", default="auto", choices=["auto", "hevc_nvenc", "libx265"],
                    help="auto = NVENC if an NVIDIA GPU works, else CPU x265. Default auto")
    ap.add_argument("--cq", type=int, default=None,
                    help="quality: lower = better/bigger. Default 27 for NVENC (cq), 24 for x265 (crf)")
    ap.add_argument("--min-saving", type=float, default=0.15,
                    help="skip a file unless it saves at least this fraction. Default 0.15")
    ap.add_argument("--min-kbps-per-mpx", type=float, default=1200,
                    help="skip files already below this bitrate per megapixel (1080p ≈ 2.07 Mpx). Default 1200")
    ap.add_argument("--gpu", default=None, help="CUDA_VISIBLE_DEVICES value to pin NVENC to one GPU (e.g. 0)")
    ap.add_argument("--ten-bit", action="store_true",
                    help="encode HEVC Main10 (less banding; some older TVs can't direct-play it)")
    ap.add_argument("--no-hw-decode", dest="hw_decode", action="store_false", help="decode on CPU instead of NVDEC")
    ap.add_argument("--skip-decode-check", action="store_true", help="skip the full decode pass during verification")
    ap.add_argument("--stop-at", default=None, help="HH:MM — don't start new files after this time (e.g. 07:00)")
    ap.add_argument("--max-read-mbps", type=float, default=8,
                    help="cap how fast each ffmpeg run reads from disk, in MB/s (0 = no cap). Default 8")
    ap.add_argument("--pause", type=int, default=30,
                    help="seconds to wait between files so other disk users can catch up. Default 30")
    ap.add_argument("--estimate", action="store_true",
                    help="dry run only: sample-encode each candidate and predict its size (use with --limit)")
    ap.add_argument("--estimate-cq", type=lambda v: [int(x) for x in v.split(",")], default=None,
                    help="cq values to compare in --estimate, comma-separated. Default 24,27,30 (NVENC) / 22,24,26 (x265)")
    ap.add_argument("--allow-hardlinks", action="store_true",
                    help="also convert files with more than one hardlink (they're skipped by default)")
    ap.add_argument("--retry-failed", action="store_true", help="re-check files that failed or didn't shrink before")
    ap.add_argument("--redetect", action="store_true", help="re-test which NVENC features the GPU supports")
    ap.add_argument("--db", default=None, help="state database path (default: /var/lib/nvenc-shrink/state.db "
                                               "as root, else ~/.local/state/nvenc-shrink/state.db)")
    return ap


def parse_args(argv=None):
    ap = build_parser()
    conf = conf_path()
    conf_roots = []
    if os.path.isfile(conf):
        def conf_error(msg):
            sys.exit(f"{PROG}: problem in {conf}: {msg}")
        words = []
        try:
            with open(conf) as f:
                for n, line in enumerate(f, 1):
                    words += shlex.split(line, comments=True)  # per line, so a trailing "# note" is fine
        except ValueError as e:
            conf_error(f"line {n}: {e}")
        conf_parser = build_parser()
        conf_parser.error = conf_error  # name the file, and skip the usage text
        conf_args, unknown = conf_parser.parse_known_args(words)
        if unknown:
            conf_error(f"unknown option(s): {' '.join(unknown)} (see `{PROG} --help`)")
        if conf_args.apply:
            conf_error("--apply isn't allowed here; keep it on the command line so a bare run stays a dry run")
        conf_roots = conf_args.roots
        ap.set_defaults(**{k: v for k, v in vars(conf_args).items() if k != "roots"})
    args = ap.parse_args(argv)
    args.conf = conf
    args.roots = args.roots or conf_roots
    args.db = args.db or default_db()
    if not args.roots and not (args.status or args.check_config):
        ap.error(f"no library folders given — pass them on the command line or list them in {conf}")
    return args


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def end_run(args, outcome):
    runlog = getattr(args, "runlog", None)
    if runlog:
        try:
            runlog.end(outcome)
        except Exception:
            pass  # never let bookkeeping hide the real error


def main(argv=None):
    signal.signal(signal.SIGTERM, _raise_interrupt)  # `systemctl stop` -> same clean exit as Ctrl-C
    args = None
    try:
        args = parse_args(argv)
        run(args)
    except KeyboardInterrupt:
        end_run(args, "interrupted")
        log("interrupted — any partial file was removed; originals are untouched")
        sys.exit(130)
    except Exception as e:
        end_run(args, f"error: {e}")
        raise


def run(args):
    if args.check_config:
        check_config(args)
        return
    if args.status:
        print_status(args.db)
        return
    for root in args.roots:
        if not os.path.isdir(root):
            sys.exit(f"library folder not found: {root}")

    if args.gpu is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    deadline = deadline_from(args.stop_at)
    for name, binary in (("FFMPEG", FFMPEG), ("FFPROBE", FFPROBE)):
        if not shutil.which(binary):
            sys.exit(f"can't find {binary!r}. "
                     + (f"Unset ${name} (`unset {name}`) or point it at a real binary." if name in os.environ
                        else "Install ffmpeg (e.g. `apt install ffmpeg`)."))

    # State (database, lock, NVENC cache) lists the whole library: keep it private.
    os.umask(0o077)
    state_dir = os.path.dirname(os.path.abspath(args.db))
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    # makedirs' mode only applies when it creates the folder; tighten one made by 1.0/1.1.
    # Only a folder we own and that isn't a link (never chmod a shared dir like --db /tmp/x.db).
    sd = os.lstat(state_dir)
    if stat.S_ISDIR(sd.st_mode) and sd.st_uid == os.geteuid() and os.path.basename(state_dir) == "nvenc-shrink":
        os.chmod(state_dir, 0o700)
    for path in (args.db, args.db + ".lock", args.db + ".nvenc.json", args.db + ".progress"):
        try:
            if os.lstat(path).st_uid == os.geteuid():
                os.chmod(path, 0o600)  # tighten files made by older versions
        except FileNotFoundError:
            pass
    lock = open(args.db + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another run is already in progress")

    db = open_db(args.db)
    cleanup_stale(args.roots, args.apply)

    select_encoder(args)
    if args.cq is None:
        args.cq = DEFAULT_CQ[args.encoder]
    if args.estimate_cq is None:
        args.estimate_cq = DEFAULT_ESTIMATE_CQ[args.encoder]
    mode = "APPLY" if args.apply else ("ESTIMATE" if args.estimate else "DRY RUN")
    settings = (f"encoder={args.encoder} cq={args.cq} 10bit={args.ten_bit} "
                f"read-cap={args.max_read_mbps or 'none'} MB/s pause={args.pause}s")
    log(f"{PROG} {__version__} {mode}: {settings} roots={args.roots}")
    if args.apply:
        if args.stop_at:
            settings += f" stop-at={args.stop_at}"
        if args.limit:
            settings += f" limit={args.limit}"
        args.runlog = RunLog(db, settings, args.db + ".progress")
        args.runlog.stage("scanning")
    runlog = getattr(args, "runlog", None)

    converted = saved = candidates_n = candidate_bytes = hardlinked = hardlinked_bytes = attempted = 0
    est_totals = {}
    outcome = "finished"
    for src in candidates(args.roots):
        if args.limit and converted >= args.limit:
            log(f"reached --limit {args.limit}")
            outcome = "limit"
            break
        if args.apply and deadline and dt.datetime.now() >= deadline:
            log(f"past --stop-at {args.stop_at}, stopping")
            outcome = "stop-at"
            break

        state, rec_before, rec_after = known(db, src)
        try:
            size_now = src.stat().st_size
        except FileNotFoundError:
            continue  # moved/deleted (e.g. by Sonarr/Radarr) since the scan started
        # Already handled and unchanged on disk -> don't touch it again (no re-probe, no IO).
        # If Sonarr/Radarr has since replaced the file, its size differs and it's re-evaluated.
        if state == "done" and size_now == rec_after:
            continue
        if state == "skipped" and size_now == rec_before:
            continue
        if state in ("failed", "no-gain") and size_now == rec_before and not args.retry_failed:
            continue

        if src.stat().st_nlink > 1 and not args.allow_hardlinks:
            # Hardlinked (e.g. Sonarr/Radarr import of a seeding torrent). Re-encoding would
            # break the link: the old data stays on disk via the other name, so it costs space.
            if args.apply:
                record(db, src, "skipped", f"hardlinked ({src.stat().st_nlink} links)", src.stat().st_size)
            else:
                hardlinked += 1
                hardlinked_bytes += src.stat().st_size
            continue

        try:
            info = probe(src)
        except Exception as e:
            log(f"SKIP (unreadable) {src}: {e}")
            continue

        reason, details = classify(src, info, args.min_kbps_per_mpx)
        if reason:
            if args.apply:
                record(db, src, "skipped", reason, src.stat().st_size)
            continue

        size_before = src.stat().st_size
        candidates_n += 1
        candidate_bytes += size_before
        if not args.apply:
            if args.estimate:
                if args.limit and candidates_n > args.limit:
                    candidates_n -= 1
                    candidate_bytes -= size_before
                    break
                parts = []
                for cq in args.estimate_cq:
                    try:
                        pred, _ = predict_size(src, info, args, cq)
                        if pred < size_before:
                            parts.append(f"cq{cq}: {human(pred)} (-{100 * (1 - pred / size_before):.0f}%)")
                        else:
                            parts.append(f"cq{cq}: {human(pred)} (bigger)")
                        worth_it = pred <= size_before * (1 - args.min_saving)
                        est_totals[cq] = est_totals.get(cq, 0) + (pred if worth_it else size_before)
                    except Exception as e:
                        parts.append(f"cq{cq}: error ({e})")
                        est_totals[cq] = est_totals.get(cq, 0) + size_before  # unknown -> assume kept
                log(f"{src.name}  [{details['kbps']} kbps, {human(size_before)}]  " + "  ".join(parts))
            else:
                log(f"would convert {src}  [{details['codec']} {details['res']} {details['kbps']} kbps, {human(size_before)}]")
            continue

        out_ext = output_container(src)
        final = src.with_suffix(out_ext)
        if final != src and os.path.lexists(final):
            log(f"SKIP {src}: {final.name} already exists beside it")
            record(db, src, "failed", f"target {final.name} exists", size_before)
            continue

        if attempted and args.pause:
            runlog.stage("pausing between files")
            time.sleep(args.pause)  # give other IO (Jellyfin playback, scrub, arr) breathing room
        attempted += 1
        dur = duration_of(info)
        runlog.count("attempted")
        runlog.stage("sampling", src, size_before, dur)
        log(f"checking {src}  [{details['codec']} {details['res']} {details['kbps']} kbps, {human(size_before)}]")
        try:
            pred, ratio = predict_size(src, info, args, args.cq)
        except Exception as e:
            log(f"  sample check failed, skipping: {e}")
            record(db, src, "failed", f"sample: {e}"[:500], size_before)
            runlog.count("failed")
            runlog.stage("scanning")
            continue
        if pred > size_before * (1 - args.min_saving):
            log(f"  skipping: samples predict {human(size_before)} -> ~{human(pred)}, not worth a full encode")
            record(db, src, "no-gain", f"predicted {pred}", size_before, pred)
            runlog.count("no_gain")
            runlog.stage("scanning")
            continue
        log(f"  samples predict ~{human(pred)} ({100 * (1 - pred / size_before):.0f}% smaller), encoding")
        st = os.lstat(src)
        t0 = time.time()
        # Encode into a fresh private directory beside the original (same filesystem, so
        # the final rename is atomic). mkdtemp creates it 0700 with a random name, so
        # nobody else can plant a file or symlink where ffmpeg will write.
        work = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=src.parent))
        tmp = work / f"output{out_ext}"
        try:
            try:
                runlog.stage("encoding", src, size_before, dur)
                encode(src, tmp, info, args)
                runlog.stage("verifying", src, size_before, dur)
                verify(info, tmp, args)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"  FAILED, original untouched: {e}")
                record(db, src, "failed", str(e)[:500], size_before)
                runlog.count("failed")
                runlog.stage("scanning")
                continue
            size_after = tmp.stat().st_size
            if size_after > size_before * (1 - args.min_saving):
                log(f"  no worthwhile gain ({human(size_before)} -> {human(size_after)}), keeping original")
                record(db, src, "no-gain", "", size_before, size_after)
                runlog.count("no_gain")
                runlog.stage("scanning")
                continue

            runlog.stage("replacing original", src, size_before, dur)
            copy_attributes(st, tmp)
            os.replace(tmp, final)  # atomic; replaces a link itself, never what it points to
            if final != src:
                os.unlink(src)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        converted += 1
        saved += size_before - size_after
        record(db, final, "done", "", size_before, size_after)
        if final != src:
            record(db, src, "done", f"replaced by {final.name}", size_before, size_after)
        runlog.count("converted")
        runlog.count("saved", size_before - size_after)
        runlog.stage("scanning")
        mins = (time.time() - t0) / 60
        log(f"  done: {human(size_before)} -> {human(size_after)} "
            f"({100 * (1 - size_after / size_before):.0f}% smaller, {mins:.1f} min)")

    if args.apply:
        runlog.end(outcome)
        log(f"finished: {converted} converted, {human(saved)} saved this run")
    else:
        if hardlinked:
            log(f"skipped {hardlinked} hardlinked files ({human(hardlinked_bytes)}) — "
                f"re-encoding them wouldn't free space while the other link exists. --allow-hardlinks overrides.")
        for cq, tot in sorted(est_totals.items()):
            log(f"estimate cq{cq}: these {candidates_n} files {human(candidate_bytes)} -> ~{human(tot)} "
                f"(~{human(candidate_bytes - tot)} saved; files that wouldn't shrink {args.min_saving:.0%}+ counted as kept)")
        log(f"dry run: {candidates_n} files would be converted, {human(candidate_bytes)} currently. "
            f"Rerun with --apply (try --limit 5 first).")


if __name__ == "__main__":
    main()
