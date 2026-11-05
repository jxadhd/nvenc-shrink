# Contributing

Thanks for helping out. Bug reports from real libraries are the most useful
thing you can send: odd containers, unusual streams, GPUs that behave
differently.

## Reporting a bug

Open an issue and include:

- `shrink-library --version`
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
2. Commit, then `git tag vX.Y.Z && git push --tags`.
3. CI runs the tests, builds the `.deb`, wheel and sdist, and publishes the GitHub release.
