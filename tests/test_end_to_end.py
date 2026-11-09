"""End-to-end tests: build a small fake library with ffmpeg, run nvenc-shrink on it.

Needs ffmpeg/ffprobe with libx265 and libx264. Runs on CPU (no GPU needed).
    python3 -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def make_clip(path, vcodec="libx264", size="640x360", secs=6, extra=()):
    ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24,noise=alls=10:allf=t",
       "-f", "lavfi", "-i", "sine=f=440", "-t", str(secs),
       "-c:v", vcodec, *(("-b:v", "6M") if vcodec in ("libx264", "mpeg4") else ()),
       *(("-x265-params", "log-level=error") if vcodec == "libx265" else ()),
       "-c:a", "aac" if not str(path).endswith(".avi") else "mp3", *extra, str(path))


def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"]


def md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("ffmpeg not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="nvenc-shrink-test-"))
        lib = cls.lib = cls.tmp / "lib"
        (lib / "movies").mkdir(parents=True)
        (lib / "tv").mkdir()
        (lib / "downloads").mkdir()

        # MKV with two audio tracks, subtitles and a title
        make_clip(lib / "movies" / "raw.mkv")
        srt = cls.tmp / "s.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nHello\n")
        ff("-i", str(lib / "movies" / "raw.mkv"), "-i", str(srt), "-i", str(lib / "movies" / "raw.mkv"),
           "-map", "0:v", "-map", "0:a", "-map", "2:a", "-map", "1", "-c", "copy",
           "-metadata", "title=Test Film", str(lib / "movies" / "Film (2020).mkv"))
        (lib / "movies" / "raw.mkv").unlink()
        os.utime(lib / "movies" / "Film (2020).mkv", (1577836800, 1577836800))

        # MP4 with cover art, .m4v (ipod muxer), AVI, an already-HEVC file
        make_clip(cls.tmp / "ep.mp4")
        ff("-f", "lavfi", "-i", "color=red:s=200x200", "-frames:v", "1", str(cls.tmp / "cover.jpg"))
        ff("-i", str(cls.tmp / "ep.mp4"), "-i", str(cls.tmp / "cover.jpg"), "-map", "0", "-map", "1",
           "-c", "copy", "-disposition:v:1", "attached_pic", str(lib / "tv" / "Show S01E01.mp4"))
        make_clip(lib / "tv" / "Show S01E02.m4v", extra=("-f", "ipod"))
        make_clip(lib / "tv" / "Show S01E03.avi", vcodec="mpeg4")
        make_clip(lib / "movies" / "already.mkv", vcodec="libx265")

        # Hardlinked into downloads (seeding) -> must be skipped
        make_clip(lib / "downloads" / "seed.mkv")
        os.link(lib / "downloads" / "seed.mkv", lib / "tv" / "Show S01E04.mkv")

        # Files that merely look like temp files must survive cleanup
        make_clip(lib / "movies" / "the.incredible.shrinking.woman.1981.mkv", vcodec="libx265")
        (lib / "movies" / "notes.txt.shrinking-tmp").write_text("not ours")
        # A real stale partial from an interrupted run -> removed
        (lib / "tv" / "Show S01E01.mp4.shrinking-tmp").write_bytes(b"\0" * 100)
        # ...and a stale private work folder from an interrupted run -> removed
        (lib / "tv" / ".nvenc-shrink-work-old").mkdir()
        (lib / "tv" / ".nvenc-shrink-work-old" / "output.mkv").write_bytes(b"\0" * 100)

        # Security: a file outside the library that must never be written to
        cls.victim = cls.tmp / "victim.txt"
        cls.victim.write_text("do not overwrite")
        # a symlink planted at the predictable temp path 1.x used -> must not be followed
        (lib / "movies" / "Film (2020).mkv.shrinking-tmp").symlink_to(cls.victim)
        # a symlinked "video" -> skipped entirely, target untouched
        make_clip(cls.tmp / "outside.mkv")
        (lib / "movies" / "linked.mkv").symlink_to(cls.tmp / "outside.mkv")

        cls.before = {p: md5(p) for p in [*lib.rglob("*"), cls.victim, cls.tmp / "outside.mkv"]
                      if p.is_file() and not p.is_symlink()}
        cls.db = cls.tmp / "state.db"
        cls.result = cls.shrink("--apply", "--encoder", "libx265")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def shrink(cls, *args):
        return subprocess.run(
            [sys.executable, "-m", "nvenc_shrink", str(cls.lib / "movies"), str(cls.lib / "tv"),
             "--db", str(cls.db), "--min-kbps-per-mpx", "0", "--min-saving", "0.05", "--pause", "0", *args],
            capture_output=True, text=True, env={**ENV, "NVENC_SHRINK_CONF": "/nonexistent"})

    def test_run_succeeded(self):
        self.assertEqual(self.result.returncode, 0, self.result.stdout + self.result.stderr)

    def test_mkv_keeps_streams_metadata_and_mtime(self):
        f = self.lib / "movies" / "Film (2020).mkv"
        s = streams(f)
        self.assertEqual([x["codec_name"] for x in s], ["hevc", "aac", "aac", "subrip"])
        self.assertEqual(int(f.stat().st_mtime), 1577836800)
        self.assertNotEqual(md5(f), self.before[f])

    def test_mp4_keeps_cover_and_uses_hvc1(self):
        s = streams(self.lib / "tv" / "Show S01E01.mp4")
        video = [x for x in s if x["codec_type"] == "video" and not x["disposition"]["attached_pic"]]
        cover = [x for x in s if x["disposition"]["attached_pic"]]
        self.assertEqual(video[0]["codec_name"], "hevc")
        self.assertEqual(video[0]["codec_tag_string"], "hvc1")
        self.assertEqual(len(cover), 1)

    def test_m4v_keeps_name(self):
        self.assertEqual(streams(self.lib / "tv" / "Show S01E02.m4v")[0]["codec_name"], "hevc")

    def test_avi_becomes_mkv(self):
        self.assertFalse((self.lib / "tv" / "Show S01E03.avi").exists())
        self.assertEqual(streams(self.lib / "tv" / "Show S01E03.mkv")[0]["codec_name"], "hevc")

    def test_untouched_files(self):
        for p in (self.lib / "movies" / "already.mkv",
                  self.lib / "tv" / "Show S01E04.mkv",
                  self.lib / "downloads" / "seed.mkv",
                  self.lib / "movies" / "the.incredible.shrinking.woman.1981.mkv",
                  self.lib / "movies" / "notes.txt.shrinking-tmp"):
            self.assertEqual(md5(p), self.before[p], p)

    def test_stale_partial_removed(self):
        self.assertFalse((self.lib / "tv" / "Show S01E01.mp4.shrinking-tmp").exists())
        self.assertFalse((self.lib / "tv" / ".nvenc-shrink-work-old").exists())

    def test_no_work_folders_left(self):
        self.assertEqual([p for p in self.lib.rglob(".nvenc-shrink-work-*")], [])

    def test_planted_symlink_not_followed(self):
        self.assertEqual(self.victim.read_text(), "do not overwrite")
        link = self.lib / "movies" / "Film (2020).mkv.shrinking-tmp"
        self.assertTrue(link.is_symlink(), "cleanup must not touch links it didn't create")

    def test_symlinked_video_skipped(self):
        self.assertTrue((self.lib / "movies" / "linked.mkv").is_symlink())
        self.assertEqual(md5(self.tmp / "outside.mkv"), self.before[self.tmp / "outside.mkv"])

    def test_state_is_private(self):
        self.assertEqual(self.db.stat().st_mode & 0o077, 0, oct(self.db.stat().st_mode))

    def test_rerun_is_noop(self):
        r = self.shrink("--apply", "--encoder", "libx265")
        self.assertIn("finished: 0 converted", r.stdout)
        self.assertNotIn("checking", r.stdout)

    def test_status(self):
        r = self.shrink("--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved so far", r.stdout)
        self.assertIn("No run in progress. Last run:", r.stdout)
        self.assertIn("finished (library done)", r.stdout)
        self.assertRegex(r.stdout, r"this run: \d+ checked, \d+ converted")


class LiveStatus(unittest.TestCase):
    """--status while a run is encoding, then after it's stopped or killed."""

    def setUp(self):
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("ffmpeg not installed")
        self.tmp = Path(tempfile.mkdtemp(prefix="nvenc-shrink-live-"))
        (self.tmp / "lib").mkdir()
        make_clip(self.tmp / "lib" / "Slow.mkv", secs=20)
        self.db = self.tmp / "state.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cmd(self, *args):
        return [sys.executable, "-m", "nvenc_shrink", str(self.tmp / "lib"), "--db", str(self.db),
                "--min-kbps-per-mpx", "0", "--min-saving", "0.05", "--encoder", "libx265", *args]

    def status(self):
        r = subprocess.run(self.cmd("--status"), capture_output=True, text=True,
                           env={**ENV, "NVENC_SHRINK_CONF": "/nonexistent"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def start_slow_run(self):
        # Read-capped hard so the encode takes long enough to watch.
        p = subprocess.Popen(self.cmd("--apply", "--max-read-mbps", "0.4"), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env={**ENV, "NVENC_SHRINK_CONF": "/nonexistent"},
                             start_new_session=True)
        out = ""
        for _ in range(120):
            time.sleep(0.5)
            out = self.status()
            if "%" in out and "encoding" in out:
                return p, out
        p.kill()
        self.fail("never saw the run encoding:\n" + out)

    def test_current_then_interrupted(self):
        p, out = self.start_slow_run()
        self.assertIn("Current run: running since", out)
        self.assertIn("encoding Slow.mkv", out)
        self.assertRegex(out, r"\d+% \(\d+:\d\d:\d\d of 0:00:20\)")
        self.assertIn("this run: 1 checked, 0 converted", out)
        p.terminate()  # what `systemctl stop` sends
        p.wait(30)
        out = self.status()
        self.assertIn("No run in progress", out)
        self.assertIn("interrupted", out)

    def test_killed_run_is_reported(self):
        p, _ = self.start_slow_run()
        os.killpg(p.pid, signal.SIGKILL)  # run and its ffmpeg, no chance to clean up
        p.wait(30)
        out = self.status()
        self.assertIn("ended unexpectedly", out)
        self.assertIn("was on:   encoding", out)


class EncoderOptions(unittest.TestCase):
    """Encoder options must target only the encoded stream, never copied ones (e.g. cover art).

    Regression: an unqualified `-profile:v main` also hit the copied cover-art stream on
    NVENC runs, and ffmpeg refused to open the output ("Unable to parse option value")."""

    def build(self, encoder):
        sys.path.insert(0, str(ROOT / "src"))
        from argparse import Namespace
        from nvenc_shrink import cli
        info = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "disposition": {"attached_pic": 0}},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "disposition": {}},
            {"index": 2, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        ], "format": {"duration": "10"}}
        args = Namespace(encoder=encoder, cq=27, ten_bit=False, hw_decode=False, max_read_mbps=0,
                         nvenc_extra=["-bf", "0"])
        return cli.build_cmd(Path("/x/in.mp4"), Path("/x/in.mp4.shrinking-tmp"), info, args)

    def test_options_are_stream_qualified(self):
        for encoder in ("hevc_nvenc", "libx265"):
            cmd = self.build(encoder)
            encoder_opts = {"-preset", "-tune", "-rc", "-cq", "-b", "-spatial-aq", "-rc-lookahead", "-bf",
                            "-profile", "-pix_fmt", "-crf", "-x265-params", "-temporal-aq", "-b_ref_mode"}
            for word in cmd:
                if word.startswith("-") and word.split(":")[0] in encoder_opts:
                    self.assertEqual(word.split(":", 1)[1:], ["0"], f"{encoder}: {word} isn't limited to stream 0")
            self.assertIn("-profile:0" if encoder == "hevc_nvenc" else "-crf:0", cmd)

    def test_cover_art_with_profile_encodes(self):
        """Real ffmpeg run with a -profile option and a copied cover (x265 stands in for NVENC)."""
        tmp = Path(tempfile.mkdtemp(prefix="nvenc-shrink-opt-"))
        try:
            make_clip(tmp / "v.mp4", secs=2)
            ff("-f", "lavfi", "-i", "color=red:s=100x100", "-frames:v", "1", str(tmp / "c.jpg"))
            ff("-i", str(tmp / "v.mp4"), "-i", str(tmp / "c.jpg"), "-map", "0", "-map", "1", "-c", "copy",
               "-disposition:v:1", "attached_pic", str(tmp / "in.mp4"))
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp / "in.mp4"),
                                "-map", "0:0", "-map", "0:1", "-map", "0:2",
                                "-c:0", "libx265", "-profile:0", "main", "-crf:0", "30",
                                "-x265-params:0", "log-level=error", "-c:1", "copy", "-c:2", "copy",
                                str(tmp / "out.mp4")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NvencCache(unittest.TestCase):
    """The NVENC cache may only select from built-in option sets, never supply its own."""

    def test_tampered_cache_cannot_inject_options(self):
        sys.path.insert(0, str(ROOT / "src"))
        from argparse import Namespace
        from nvenc_shrink import cli
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "state.db")
            key = f"{cli.FFMPEG}|gpu={os.environ.get('CUDA_VISIBLE_DEVICES', '')}|10bit=False"
            # old list format, with a known label but injected options
            with open(db + ".nvenc.json", "w") as f:
                json.dump({key: ["B-frames", ["-f", "null", "-y", "/etc/passwd"]]}, f)
            args = Namespace(encoder="auto", db=db, ten_bit=False, redetect=False)
            cli.select_encoder(args)
            self.assertNotIn("/etc/passwd", args.nvenc_extra)
            self.assertEqual(args.nvenc_extra, dict(cli.NVENC_FEATURE_LADDER)["B-frames"])
            # unknown label -> ignored and re-detected (no GPU here, so CPU fallback)
            with open(db + ".nvenc.json", "w") as f:
                json.dump({key: "evil"}, f)
            args = Namespace(encoder="auto", db=db, ten_bit=False, redetect=False)
            cli.select_encoder(args)
            self.assertNotIn("evil", json.dumps(args.nvenc_extra))


class Config(unittest.TestCase):
    def test_apply_not_allowed_in_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("/tmp  # comment\n--apply\n")
        r = subprocess.run([sys.executable, "-m", "nvenc_shrink"], capture_output=True, text=True,
                           env={**ENV, "NVENC_SHRINK_CONF": f.name})
        os.unlink(f.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--apply isn't allowed", r.stderr)

    def test_bad_config_names_the_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("/tmp\n--cq 27 --no-such-option\n")
        r = subprocess.run([sys.executable, "-m", "nvenc_shrink", "--check-config"], capture_output=True,
                           text=True, env={**ENV, "NVENC_SHRINK_CONF": f.name})
        os.unlink(f.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"problem in {f.name}", r.stderr)
        self.assertIn("--no-such-option", r.stderr)
        self.assertNotIn("usage:", r.stderr)

    def test_check_config_accepts_shipped_template(self):
        r = subprocess.run([sys.executable, "-m", "nvenc_shrink", "--check-config"], capture_output=True,
                           text=True, env={**ENV, "NVENC_SHRINK_CONF": str(ROOT / "etc" / "nvenc-shrink.conf")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK", r.stdout)

    def test_existing_state_folder_is_made_private(self):
        """A state folder left 0755 by 1.0 is tightened to 0700 (code scanning #5)."""
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "nvenc-shrink"
            state.mkdir(mode=0o755)
            os.chmod(state, 0o755)
            (Path(d) / "lib").mkdir()
            r = subprocess.run([sys.executable, "-m", "nvenc_shrink", str(Path(d) / "lib"), "--apply",
                                "--encoder", "libx265", "--db", str(state / "state.db")],
                               capture_output=True, text=True, env={**ENV, "NVENC_SHRINK_CONF": "/nonexistent"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)

    def test_no_roots_is_a_clear_error(self):
        r = subprocess.run([sys.executable, "-m", "nvenc_shrink"], capture_output=True, text=True,
                           env={**ENV, "NVENC_SHRINK_CONF": "/nonexistent"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no library folders given", r.stderr)


if __name__ == "__main__":
    unittest.main()
