#!/usr/bin/env python3
"""Build dist/nvenc-shrink_<version>_all.deb from this repo.

    python3 packaging/build_deb.py

Needs only Python and dpkg-deb (Debian/Ubuntu). CI runs this for every tagged release.
"""

import datetime as dt
import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/nvenc_shrink/__init__.py").read_text()).group(1)
NAME = "nvenc-shrink"
REPO_URL = "https://jxadhd.github.io/nvenc-shrink/apt"
KEYRING = f"/usr/share/keyrings/{NAME}-archive-keyring.gpg"
SOURCES = f"/etc/apt/sources.list.d/{NAME}.sources"

CONTROL = f"""Package: {NAME}
Version: {VERSION}
Section: video
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), ffmpeg
Maintainer: Josh Cross <me@joshcross.co.nz>
Homepage: https://github.com/jxadhd/nvenc-shrink
Description: safely re-encode a media library to HEVC to save disk space
 Re-encodes H.264/MPEG-4/VC-1 video in a Jellyfin, Plex or Emby library to
 HEVC with NVIDIA NVENC, or CPU x265 as a fallback. Each file is predicted
 from short samples, encoded beside the original, verified, and only then
 swapped in under the same name. It throttles disk IO, skips HDR, hardlinked
 and already-efficient files, and keeps state so it can run nightly.
"""

# Maintainer scripts: run by dpkg during install/remove, never by the user.
POSTINST = f"""#!/bin/sh
set -e
mkdir -p /var/lib/{NAME}
if [ -d /run/systemd/system ]; then systemctl daemon-reload || true; fi

# On first install only, add the project's apt repository so `apt upgrade` delivers new
# versions, unless something already points at it (e.g. `extrepo enable {NAME}`).
# If you delete the file afterwards, upgrades won't put it back.
if [ "$1" = configure ] && [ ! -e /var/lib/{NAME}/.apt-source-done ]; then
    if [ ! -e {SOURCES} ] && ! grep -rqs "{REPO_URL}" /etc/apt/sources.list /etc/apt/sources.list.d/; then
        cat > {SOURCES} <<EOF
# Added by the {NAME} package so updates arrive with apt upgrade. Safe to delete.
Types: deb
URIs: {REPO_URL}
Suites: stable
Components: main
Signed-By: {KEYRING}
EOF
    fi
    touch /var/lib/{NAME}/.apt-source-done
fi
"""
PRERM = f"""#!/bin/sh
set -e
if [ -d /run/systemd/system ] && [ "$1" = remove ]; then
    systemctl disable --now {NAME}.timer >/dev/null 2>&1 || true
    systemctl stop {NAME}.service >/dev/null 2>&1 || true
fi
"""
POSTRM = f"""#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then systemctl daemon-reload || true; fi
rm -rf /usr/lib/{NAME}/nvenc_shrink/__pycache__
if [ "$1" = purge ]; then rm -rf /var/lib/{NAME} {SOURCES}; fi
"""

LAUNCHER = f"""#!/usr/bin/python3
import sys
sys.dont_write_bytecode = True  # keep /usr/lib clean so dpkg can remove it fully
sys.path.insert(0, "/usr/lib/{NAME}")
from nvenc_shrink.cli import main
main()
"""


def man_page():
    """A real man page built from --help, so `man nvenc-shrink` matches the code."""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "NVENC_SHRINK_CONF": "/nonexistent", "COLUMNS": "80"}
    help_text = subprocess.run([sys.executable, "-c",
                                "import sys; from nvenc_shrink.cli import build_parser;"
                                "build_parser().print_help()"],
                               capture_output=True, text=True, env=env, check=True).stdout
    esc = lambda t: t.replace("\\", "\\e").replace("-", "\\-").replace("\n.", "\n\\&.").replace("\n'", "\n\\&'")
    date = dt.date.today().strftime("%B %Y")
    return f""".TH NVENC\\-SHRINK 1 "{date}" "{NAME} {VERSION}" "User Commands"
.SH NAME
nvenc\\-shrink \\- safely re\\-encode a media library to HEVC to save disk space
.SH SYNOPSIS
.B nvenc\\-shrink
[\\fIoptions\\fR] [\\fIfolder\\fR ...]
.SH DESCRIPTION
.nf
{esc(help_text)}
.fi
.SH FILES
.TP
.I /etc/nvenc\\-shrink.conf
Default arguments. Command\\-line options override them. \\fB\\-\\-apply\\fR is not allowed here.
.TP
.I ~/.config/nvenc\\-shrink.conf
Per\\-user defaults when not running as root.
.TP
.I /var/lib/nvenc\\-shrink/state.db
Progress and results (as root).
.SH SEE ALSO
.BR systemctl (1),
.BR ffmpeg (1)
.PP
https://github.com/jxadhd/nvenc\\-shrink
"""


def gz(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(path, "wb", mtime=0) as f:
        f.write(data.encode())
    path.chmod(0o644)


def put(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def main():
    if not shutil.which("dpkg-deb"):
        sys.exit("dpkg-deb not found: build on Debian/Ubuntu, or let CI do it")
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        pkg = Path(d) / "pkg"
        # Code lives in a private dir so it never clashes with a pipx/pip install.
        shutil.copytree(ROOT / "src/nvenc_shrink", pkg / f"usr/lib/{NAME}/nvenc_shrink",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        put(pkg / "usr/bin/nvenc-shrink", LAUNCHER, 0o755)
        for unit in (f"{NAME}.service", f"{NAME}.timer"):
            put(pkg / "usr/lib/systemd/system" / unit, (ROOT / "systemd" / unit).read_text())
        put(pkg / f"etc/{NAME}.conf", (ROOT / f"etc/{NAME}.conf").read_text())
        keyring = pkg / KEYRING.lstrip("/")
        keyring.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["gpg", "--batch", "--yes", "--dearmor", "-o", str(keyring),
                        str(ROOT / f"packaging/apt/{NAME}-archive-keyring.asc")], check=True)
        keyring.chmod(0o644)

        man = pkg / "usr/share/man/man1"
        gz(man / "nvenc-shrink.1.gz", man_page())

        doc = pkg / f"usr/share/doc/{NAME}"
        put(doc / "README.md", (ROOT / "README.md").read_text())
        put(doc / "copyright", (ROOT / "LICENSE").read_text())
        gz(doc / "changelog.gz", (ROOT / "CHANGELOG.md").read_text())

        deb = pkg / "DEBIAN"
        put(deb / "control", CONTROL)
        put(deb / "conffiles", f"/etc/{NAME}.conf\n")
        put(deb / "postinst", POSTINST, 0o755)
        put(deb / "prerm", PRERM, 0o755)
        put(deb / "postrm", POSTRM, 0o755)
        for p in pkg.rglob("*"):
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o755)

        target = out / f"{NAME}_{VERSION}_all.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(pkg), str(target)],
                       check=True, stdout=subprocess.DEVNULL)
    print(target)


if __name__ == "__main__":
    main()
