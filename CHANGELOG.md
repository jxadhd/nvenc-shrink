# Changelog

## 1.1.2 — 2026-11-06

- Upgrades never replace your `/etc/nvenc-shrink.conf` and never stop to ask
  about it. If the shipped default changes, the upgrade prints a one-line notice
  and a `diff` command, and the new default is in `/usr/share/nvenc-shrink/`.
  A config you never edited follows the new default automatically.
- After each upgrade, the config is checked and you get a warning if it contains
  anything this version doesn't understand. It still isn't changed.
- New `--check-config` shows which config file is in use and the settings it gives.
- Config errors now name the file and the problem, without the full usage text.

Security (from code scanning):

- Encodes are written into a private, randomly named folder beside the
  original instead of a predictable temp path, so a symlink planted in a
  writable media folder can no longer redirect root's writes elsewhere.
- File owner, mode and times are copied through a handle that never follows
  links, and symlinked videos are skipped rather than replaced.
- The NVENC cache can only pick from the built-in option sets, so editing it
  can't inject ffmpeg arguments.
- The state database, lock and cache are private (0600 in a 0700 folder).
- The systemd service drops every capability except the four it needs to
  handle other users' media, and adds seccomp and privilege restrictions that
  also work in unprivileged LXC containers.

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
