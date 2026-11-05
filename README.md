<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
    <img alt="nvenc-shrink" src="docs/logo-light.svg" width="460">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/jxadhd/nvenc-shrink/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/jxadhd/nvenc-shrink/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/jxadhd/nvenc-shrink/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/jxadhd/nvenc-shrink?color=1a7f5a"></a>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-1a7f5a">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/github/license/jxadhd/nvenc-shrink?color=1a7f5a"></a>
</p>

<p align="center">
  Get disk space back from your <b>Jellyfin</b>, <b>Plex</b> or <b>Emby</b> library by re-encoding
  H.264 video to HEVC, without risking a file it can't verify.
</p>

```console
$ shrink-library --apply
checking .../Apex (2026) WEBRip-1080p.mp4  [h264 1920x872 2639 kbps, 1.8GB]
  skipping: samples predict 1.8GB -> ~2.7GB, not worth a full encode
checking .../Whisper of the Heart (1995) Bluray-1080p.mkv  [h264 1920x1038 15956 kbps, 12.4GB]
  samples predict ~5.9GB (52% smaller), encoding
```

## Why this one

Most "transcode my library" scripts loop ffmpeg over every file and hope for
the best. nvenc-shrink is built for a library you care about, running
unattended on a homelab box:

| | |
|---|---|
| 🔮 **Predicts before it encodes** | It encodes three 20-second samples in RAM. Files that won't shrink are skipped in about 10 seconds, not after a 30-minute encode. |
| ✅ **Verifies before it replaces** | Duration, every audio, subtitle, cover and attachment stream, HEVC output, and a full decode pass with zero errors. Anything less and the original stays put. |
| 🏷️ **Same filename, same metadata** | The new file takes the original's exact path, permissions, owner and modification time, so watched state and Sonarr/Radarr keep working. |
| 🐢 **Gentle on your disks** | Reads are capped (8 MB/s by default) with a pause between files, so playback, scrubs and backups keep running. |
| 🧠 **Knows what to leave alone** | Skips HDR, Dolby Vision, files already HEVC/AV1/VP9, low-bitrate files, and hardlinked files that are still seeding. |
| 🎛️ **Any NVIDIA card, or none** | Detects what your GPU's NVENC supports (even cards without HEVC B-frames, such as the T400, GTX 1650 and Pascal) and falls back to CPU x265 if there's no GPU. |
| 🔁 **Set and forget** | A systemd timer runs it nightly, and its state database means each run carries on from the last. |

## Install

**Debian, Ubuntu, Proxmox LXC.** Download the `.deb` from the
[latest release](https://github.com/jxadhd/nvenc-shrink/releases/latest), then:

```bash
sudo apt install ./nvenc-shrink_*_all.deb
```

You get the `shrink-library` command (also available as `nvenc-shrink`),
`man shrink-library`, `/etc/nvenc-shrink.conf`, and a nightly systemd timer
that stays **off until you enable it**. It installs `ffmpeg` if you don't have it.

**Any other Linux.** With Python 3.9+ and ffmpeg installed:

```bash
pipx install "nvenc-shrink @ git+https://github.com/jxadhd/nvenc-shrink"
```

## Quick start

**1. Point it at your library.** Edit `/etc/nvenc-shrink.conf` (or
`~/.config/nvenc-shrink.conf` if you don't run it as root):

```bash
/srv/media/movies /srv/media/tv
--gpu 0
```

**2. Dry run.** This shows what it would convert and changes nothing:

```bash
shrink-library
```

**3. Choose a quality setting.** This predicts sizes at several settings and converts nothing:

```bash
shrink-library --estimate --limit 10
```

Each file shows its predicted size at `cq` 24, 27 and 30 (22, 24 and 26 on x265),
followed by a projected total for each. Use the lowest `--cq` that gives you a
saving you're happy with, and put it in the config.

**4. Convert a handful and actually watch them.** Look at a dark scene, fast
motion and anything grainy on your real TV:

```bash
shrink-library --apply --limit 5
```

**5. Let it run every night:**

```bash
sudo systemctl enable --now nvenc-shrink.timer
```

It starts at 03:00. To change that, run `sudo systemctl edit nvenc-shrink.timer`.

### Day to day

```bash
shrink-library --status               # files done, space saved, recent failures
journalctl -u nvenc-shrink -f         # watch tonight's run live
sudo systemctl stop nvenc-shrink      # stop at any time, safely
```

## How it works

```
  original.mkv ──► sample 3 × 20 s in RAM ──► predicted saving < 15%? ──► skip (≈10 s)
                                                       │ no
                                                       ▼
             encode to original.mkv.shrinking-tmp (read-capped, same folder)
                                                       │
                                                       ▼
  verify: duration · stream counts · HEVC · full decode · real saving ≥ 15%
                         │ pass                                   │ fail
                         ▼                                        ▼
        copy perms/owner/mtime, swap in as original.mkv    delete temp, keep original
```

The temp file has no video extension, so media servers and *arr apps ignore
it mid-encode. If a run is interrupted, the next one cleans up the partial file.

## Tuning

| Encoder | Default quality | Notes |
|---|---|---|
| NVIDIA NVENC (`hevc_nvenc`) | `--cq 27` | 4–7× realtime at 1080p. Picked automatically when an NVIDIA GPU works. |
| CPU x265 (`libx265`) | `--cq 24` (CRF) | Much slower, but compresses noticeably better at the same quality. |

- **The scales differ.** NVENC's `cq` and x265's CRF aren't the same scale. At `cq 24`, NVENC often makes
  already-compressed files *bigger*. Use `--estimate` rather than guessing.
- **Where the savings are.** Big on Blu-ray remuxes and high-bitrate WEB-DLs (40–70%), modest on typical
  rips, and nothing on already-compressed YTS-style encodes. Those are skipped in seconds.
- **10-bit output.** `--ten-bit` reduces banding at the same size, but some older TVs and sticks
  can't direct-play 10-bit HEVC.
- **Disk IO.** `--max-read-mbps` (default 8) and `--pause` (default 30 s). A 1080p
  stream needs about 1–2 MB/s, so the defaults leave plenty of headroom.

`shrink-library --help` or `man shrink-library` lists every option.

## Good to know

> [!WARNING]
> **There's no undo.** Once a file passes verification, the original is deleted.
> If you snapshot your media, that's your safety net. If you don't, take step 4 seriously.

- **Renamed files.** `.avi`, `.wmv`, `.ts` and similar become `.mkv`, so media servers treat
  them as new items. `.mkv`, `.mp4`, `.m4v` and `.mov` keep their exact name.
- **Sonarr and Radarr** aren't affected, because paths don't change. Avoid *Rename files*
  if your naming template includes `{MediaInfo VideoCodec}`.
- **GPU in a container.** The container needs `libnvidia-encode` and `libnvcuvid`. If
  encodes are slow to start, enable persistence mode on the host with `nvidia-smi -pm 1`.
- **One run at a time.** A lock file stops the timer and a manual run from overlapping.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © Josh Cross
