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
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".flv", ".webm"}
SKIP_CODECS = {"hevc", "av1", "vp9"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
from nvenc_shrink import __version__

PROG = "nvenc-shrink"
SYSTEM_CONF = "/etc/nvenc-shrink.conf"
LEGACY_SYSTEM_CONF = "/etc/shrink-library.conf"  # pre-1.0 name
USER_CONF = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "nvenc-shrink.conf")
DEFAULT_CQ = {"hevc_nvenc": 27, "libx265": 24}
DEFAULT_ESTIMATE_CQ = {"hevc_nvenc": [24, 27, 30], "libx265": [22, 24, 26]}


def conf_path():
    """Config file in use: $NVENC_SHRINK_CONF, then ~/.config (non-root), then /etc."""
    if os.environ.get("NVENC_SHRINK_CONF"):
        return os.environ["NVENC_SHRINK_CONF"]
    if os.path.isfile(USER_CONF) and os.geteuid() != 0:
        return USER_CONF
    if not os.path.isfile(SYSTEM_CONF) and os.path.isfile(LEGACY_SYSTEM_CONF):
        return LEGACY_SYSTEM_CONF
    return SYSTEM_CONF


def default_db():
    legacy = os.path.expanduser("~/.shrink-library.db")  # used by pre-1.0 versions
    if os.path.exists(legacy):
        return legacy
    if os.geteuid() == 0:
        return "/var/lib/nvenc-shrink/state.db"
    state = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(state, "nvenc-shrink", "state.db")


TMP_SUFFIX = ".shrinking-tmp"   # non-video extension: Jellyfin/Sonarr/Radarr ignore it mid-encode
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
    with tempfile.TemporaryDirectory(prefix="shrink-sample-", dir=tmpdir) as d:
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
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-v", "error"]
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
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY, status TEXT, reason TEXT,
        size_before INTEGER, size_after INTEGER, updated TEXT)""")
    return db


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
                if p.suffix.lower() in VIDEO_EXTS and not f.startswith("."):
                    yield p


def deadline_from(stop_at):
    """Next occurrence of HH:MM after now (so a 01:00 start with --stop-at 07:00 stops at 07:00 today)."""
    if not stop_at:
        return None
    hh, mm = map(int, stop_at.split(":"))
    now = dt.datetime.now()
    d = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return d if d > now else d + dt.timedelta(days=1)


def cleanup_stale(roots, apply):
    for root in roots:
        # Only the exact temp form this version writes: "<name>.<video ext>.shrinking-tmp".
        stale = [p for p in Path(root).rglob(f"*{TMP_SUFFIX}")
                 if p.is_file() and Path(p.name[:-len(TMP_SUFFIX)]).suffix.lower() in MUXERS]
        for p in stale:
            log(f"stale partial from an interrupted run: {p}" + (" (removing)" if apply else ""))
            if apply:
                p.unlink()


def print_status(db_path):
    if not os.path.exists(db_path):
        print(f"no state yet ({db_path}) — nothing has been run with --apply")
        return
    db = sqlite3.connect(db_path)
    rows = db.execute("""SELECT status, COUNT(*), COALESCE(SUM(size_before),0), COALESCE(SUM(size_after),0)
                         FROM files WHERE reason NOT LIKE 'replaced by %' GROUP BY status ORDER BY status""").fetchall()
    print(f"state: {db_path}\n")
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


def select_encoder(args):
    """Resolve --encoder auto, detect NVENC features (cached), set args.nvenc_extra."""
    args.nvenc_extra = []
    if args.encoder == "libx265":
        return
    cache_path = args.db + ".nvenc.json"
    key = f"{FFMPEG}|gpu={os.environ.get('CUDA_VISIBLE_DEVICES', '')}|10bit={args.ten_bit}"
    cache = {}
    try:
        cache = json.load(open(cache_path))
    except (OSError, ValueError):
        pass
    if key in cache and not args.redetect:
        label, extra = cache[key]
        log(f"NVENC: {label or 'not available'} (cached; --redetect to re-test)")
    else:
        try:
            label, extra = detect_nvenc_features(args)
        except RuntimeError as e:
            label, extra = None, []
            log(str(e))
        cache[key] = [label, extra]
        try:
            json.dump(cache, open(cache_path, "w"))
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
    ap = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]) if sys.argv[0] and not sys.argv[0].endswith(".py") else PROG,
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*", help="library folders to scan (default: from the config file)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--apply", action="store_true", help="actually encode and replace (default is dry run)")
    ap.add_argument("--status", action="store_true", help="show progress and savings so far, then exit")
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
        words = []
        for line in open(conf):
            words += shlex.split(line, comments=True)  # per line, so a trailing "# note" is fine
        conf_args = ap.parse_args(words)
        if conf_args.apply:
            ap.error(f"--apply isn't allowed in {conf}: keep it on the command line so a bare run stays a dry run")
        conf_roots = conf_args.roots
        ap.set_defaults(**{k: v for k, v in vars(conf_args).items() if k != "roots"})
    args = ap.parse_args(argv)
    args.conf = conf
    args.roots = args.roots or conf_roots
    args.db = args.db or default_db()
    if not args.roots and not args.status:
        ap.error(f"no library folders given — pass them on the command line or list them in {conf}")
    return args


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    signal.signal(signal.SIGTERM, _raise_interrupt)  # `systemctl stop` -> same clean exit as Ctrl-C
    try:
        run(parse_args(argv))
    except KeyboardInterrupt:
        log("interrupted — any partial file was removed; originals are untouched")
        sys.exit(130)


def run(args):
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

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
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
    log(f"{PROG} {__version__} {mode}: encoder={args.encoder} cq={args.cq} 10bit={args.ten_bit} "
        f"read-cap={args.max_read_mbps or 'none'} MB/s pause={args.pause}s roots={args.roots}")

    converted = saved = candidates_n = candidate_bytes = hardlinked = hardlinked_bytes = attempted = 0
    est_totals = {}
    for src in candidates(args.roots):
        if args.limit and converted >= args.limit:
            log(f"reached --limit {args.limit}")
            break
        if args.apply and deadline and dt.datetime.now() >= deadline:
            log(f"past --stop-at {args.stop_at}, stopping")
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
        tmp = src.with_name(f"{src.stem}{out_ext}{TMP_SUFFIX}")
        final = src.with_suffix(out_ext)
        if final != src and final.exists():
            log(f"SKIP {src}: {final.name} already exists beside it")
            record(db, src, "failed", f"target {final.name} exists", size_before)
            continue

        if attempted and args.pause:
            time.sleep(args.pause)  # give other IO (Jellyfin playback, scrub, arr) breathing room
        attempted += 1
        log(f"checking {src}  [{details['codec']} {details['res']} {details['kbps']} kbps, {human(size_before)}]")
        try:
            pred, ratio = predict_size(src, info, args, args.cq)
        except Exception as e:
            log(f"  sample check failed, skipping: {e}")
            record(db, src, "failed", f"sample: {e}"[:500], size_before)
            continue
        if pred > size_before * (1 - args.min_saving):
            log(f"  skipping: samples predict {human(size_before)} -> ~{human(pred)}, not worth a full encode")
            record(db, src, "no-gain", f"predicted {pred}", size_before, pred)
            continue
        log(f"  samples predict ~{human(pred)} ({100 * (1 - pred / size_before):.0f}% smaller), encoding")
        st = src.stat()
        t0 = time.time()
        try:
            encode(src, tmp, info, args)
            verify(info, tmp, args)
        except KeyboardInterrupt:
            if tmp.exists():
                tmp.unlink()
            raise
        except Exception as e:
            if tmp.exists():
                tmp.unlink()
            log(f"  FAILED, original untouched: {e}")
            record(db, src, "failed", str(e)[:500], size_before)
            continue
        size_after = tmp.stat().st_size
        if size_after > size_before * (1 - args.min_saving):
            tmp.unlink()
            log(f"  no worthwhile gain ({human(size_before)} -> {human(size_after)}), keeping original")
            record(db, src, "no-gain", "", size_before, size_after)
            continue

        # Verified: carry over permissions/ownership/mtime, then swap in.
        try:
            shutil.copymode(src, tmp)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
            os.utime(tmp, (st.st_atime, st.st_mtime))
        except OSError as e:
            log(f"  warning: couldn't copy file attributes: {e}")

        src.unlink()
        os.replace(tmp, final)
        converted += 1
        saved += size_before - size_after
        record(db, final, "done", "", size_before, size_after)
        if final != src:
            record(db, src, "done", f"replaced by {final.name}", size_before, size_after)
        mins = (time.time() - t0) / 60
        log(f"  done: {human(size_before)} -> {human(size_after)} "
            f"({100 * (1 - size_after / size_before):.0f}% smaller, {mins:.1f} min)")

    if args.apply:
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
