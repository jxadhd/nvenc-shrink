"""End-to-end tests: build a small fake library with ffmpeg, run nvenc-shrink on it.

Needs ffmpeg/ffprobe with libx265 and libx264. Runs on CPU (no GPU needed).
    python3 -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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

        cls.before = {p: md5(p) for p in lib.rglob("*") if p.is_file()}
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

    def test_rerun_is_noop(self):
        r = self.shrink("--apply", "--encoder", "libx265")
        self.assertIn("finished: 0 converted", r.stdout)
        self.assertNotIn("checking", r.stdout)

    def test_status(self):
        r = self.shrink("--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved so far", r.stdout)


class Config(unittest.TestCase):
    def test_apply_not_allowed_in_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write("/tmp  # comment\n--apply\n")
        r = subprocess.run([sys.executable, "-m", "nvenc_shrink"], capture_output=True, text=True,
                           env={**ENV, "NVENC_SHRINK_CONF": f.name})
        os.unlink(f.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--apply isn't allowed", r.stderr)

    def test_no_roots_is_a_clear_error(self):
        r = subprocess.run([sys.executable, "-m", "nvenc_shrink"], capture_output=True, text=True,
                           env={**ENV, "NVENC_SHRINK_CONF": "/nonexistent"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no library folders given", r.stderr)


if __name__ == "__main__":
    unittest.main()
