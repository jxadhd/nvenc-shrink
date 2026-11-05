# Changelog

## 1.0.1 — 2026-11-06

- Fix NVENC encodes failing on files with cover art or extra video tracks
  ("Unable to parse option value \"main\""). Encoder options now apply only to
  the stream being encoded. Originals were never affected: the encode failed
  before anything was written.

## 1.0.0 — 2026-11-06

First public release.

- Sample-based size prediction: files that won't shrink are skipped in seconds
- NVENC with automatic feature detection (handles GPUs without HEVC B-frames
  or temporal AQ, such as the T400/GTX 1650 and Pascal cards); CPU x265 fallback
- Verification before replacing: duration, stream counts, full decode pass
- Disk IO throttling (`--max-read-mbps`, `--pause`)
- Skips HDR/Dolby Vision, already-HEVC/AV1/VP9, low-bitrate and hardlinked files
- `--estimate` to compare quality settings, `--status` for progress
- Config file (`/etc/nvenc-shrink.conf`), systemd service and timer, Debian package with man page
- `shrink-library` command, also installed as `nvenc-shrink`
- Coloured output in terminals (respects `NO_COLOR`)
