# Contributing

Thanks for helping out. Bug reports from real libraries are the most useful
thing you can send: odd containers, unusual streams, GPUs that behave
differently.

## Reporting a bug

Open an issue and include:

- `nvenc-shrink --version`
- `ffmpeg -version | head -1`
- your GPU (`nvidia-smi -L`), or "CPU only"
- the log lines around the problem (`journalctl -u nvenc-shrink`, or the terminal output)
- for a file-specific problem, the `ffprobe -hide_banner <file>` output

## Working on the code

No dependencies beyond Python 3.9+ and ffmpeg.

```bash
git clone https://github.com/jxadhd/nvenc-shrink && cd nvenc-shrink
PYTHONPATH=src python3 -m nvenc_shrink --help        # run from source
python3 -m unittest discover -s tests -v             # end-to-end tests, CPU only
python3 packaging/build_deb.py                       # build dist/nvenc-shrink_<ver>_all.deb
```

The tests build a small fake library with ffmpeg and run the real tool
against it. Please add a test for any behaviour change, especially anything
that touches when an original file gets deleted.

## Releasing

1. Bump `__version__` in `src/nvenc_shrink/__init__.py` and add a `CHANGELOG.md` entry.
2. Commit and push to `main`.
3. Publish a GitHub release with a new `vX.Y.Z` tag (or push the tag from git).
4. CI runs the tests, attaches the `.deb`, wheel and sdist to the release, and
   adds the `.deb` to the signed apt repository at
   `https://jxadhd.github.io/nvenc-shrink/apt` (the `gh-pages` branch), so apt
   users get it on their next `apt upgrade`.

The apt repository is signed with the key in `packaging/apt/`. Its private half
lives only in the `APT_SIGNING_KEY` repository secret. If that secret is missing,
releases still work, but the apt repository isn't updated. To publish the
landing page or re-sign without a release, run the CI workflow manually from
the Actions tab.
