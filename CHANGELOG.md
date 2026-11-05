# Changelog

## 1.1.1 — 2026-11-06

- The command is now only `nvenc-shrink` (and `man nvenc-shrink`).
  The old `shrink-library` name is gone, so update any scripts or cron jobs that
  call it. The bundled systemd service is updated automatically.
- Removed fallbacks for pre-release file locations (`/etc/shrink-library.conf`,
  `~/.shrink-library.db`).

## 1.1.0 — 2026-11-06

- Signed apt repository at https://jxadhd.github.io/nvenc-shrink/apt, updated
  automatically on every release. Installing the `.deb` adds it once, so later
  versions arrive with `apt upgrade`. Delete the source file to opt out.
- Ready-made entry for Debian's extrepo (`packaging/extrepo/`)
- Project website at https://jxadhd.github.io/nvenc-shrink

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
