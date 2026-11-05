#!/usr/bin/env python3
"""Add .deb files to a signed apt repository and regenerate its indexes.

    python3 packaging/build_apt_repo.py SITE_DIR dist/*.deb [--key FINGERPRINT]

SITE_DIR is the GitHub Pages checkout. The repository lives in SITE_DIR/apt:

    apt/pool/main/n/nvenc-shrink/*.deb         every version ever released
    apt/dists/stable/main/binary-*/Packages     index (+ .gz)
    apt/dists/stable/{Release,InRelease,Release.gpg}
    apt/nvenc-shrink-archive-keyring.{asc,gpg}  public key

Signing uses gpg with the given key, which must already be in the keyring
(CI imports it from the APT_SIGNING_KEY secret). Without --key the indexes
are built but left unsigned, which is useful for local testing only.
"""

import argparse
import datetime as dt
import gzip
import hashlib
import shutil
import subprocess
import sys
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = "stable"
COMPONENT = "main"
# "all" packages are listed under every architecture so any apt version finds them.
ARCHES = ["amd64", "arm64", "armhf", "i386"]


def hashes(data):
    return {"MD5sum": hashlib.md5(data).hexdigest(),
            "SHA1": hashlib.sha1(data).hexdigest(),
            "SHA256": hashlib.sha256(data).hexdigest()}


def control(deb):
    return subprocess.run(["dpkg-deb", "-f", str(deb)], capture_output=True, text=True, check=True).stdout.strip()


def packages_index(repo, debs):
    entries = []
    for deb in debs:
        data = deb.read_bytes()
        h = hashes(data)
        rel = deb.relative_to(repo).as_posix()
        entries.append("\n".join([control(deb), f"Filename: {rel}", f"Size: {len(data)}",
                                  f"MD5sum: {h['MD5sum']}", f"SHA1: {h['SHA1']}", f"SHA256: {h['SHA256']}"]))
    return ("\n\n".join(entries) + "\n").encode()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site", type=Path)
    ap.add_argument("debs", nargs="*", type=Path)
    ap.add_argument("--key", help="gpg key fingerprint to sign with")
    args = ap.parse_args()

    repo = args.site / "apt"
    for deb in args.debs:
        name = subprocess.run(["dpkg-deb", "-f", str(deb), "Package"], capture_output=True, text=True,
                              check=True).stdout.strip()
        dest = repo / "pool" / COMPONENT / name[0] / name / deb.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.read_bytes() != deb.read_bytes():
            sys.exit(f"{dest.name} is already published with different contents; bump the version instead")
        shutil.copy2(deb, dest)

    pool = sorted((repo / "pool").rglob("*.deb")) if (repo / "pool").exists() else []  # apt picks the newest
    if not pool:
        sys.exit("no packages in the pool")
    index = packages_index(repo, pool)

    dist = repo / "dists" / SUITE
    if dist.exists():
        shutil.rmtree(dist)
    files = {}
    for arch in [*ARCHES, "all"]:
        d = dist / COMPONENT / f"binary-{arch}"
        d.mkdir(parents=True)
        (d / "Packages").write_bytes(index)
        gz = gzip.compress(index, mtime=0)
        (d / "Packages.gz").write_bytes(gz)
        meta = f"Archive: {SUITE}\nComponent: {COMPONENT}\nOrigin: nvenc-shrink\nLabel: nvenc-shrink\nArchitecture: {arch}\n"
        (d / "Release").write_text(meta)
        for fname, data in (("Packages", index), ("Packages.gz", gz), ("Release", meta.encode())):
            files[f"{COMPONENT}/binary-{arch}/{fname}"] = data

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    release = [
        "Origin: nvenc-shrink",
        "Label: nvenc-shrink",
        f"Suite: {SUITE}",
        f"Codename: {SUITE}",
        f"Date: {format_datetime(now, usegmt=True)}",
        f"Architectures: {' '.join(ARCHES)} all",
        f"Components: {COMPONENT}",
        "Description: nvenc-shrink: safely re-encode a media library to HEVC",
    ]
    for field in ("MD5Sum", "SHA1", "SHA256"):
        release.append(f"{field}:")
        for path, data in sorted(files.items()):
            release.append(f" {hashes(data)['MD5sum' if field == 'MD5Sum' else field]} {len(data):>16} {path}")
    (dist / "Release").write_text("\n".join(release) + "\n")

    keyring = ROOT / "packaging/apt/nvenc-shrink-archive-keyring.asc"
    shutil.copy2(keyring, repo / keyring.name)
    subprocess.run(["gpg", "--batch", "--yes", "--dearmor", "-o", str(repo / "nvenc-shrink-archive-keyring.gpg"),
                    str(keyring)], check=True)

    if args.key:
        sign = ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", "", "--local-user", args.key,
                "--digest-algo", "SHA256"]
        subprocess.run([*sign, "--clearsign", "-o", str(dist / "InRelease"), str(dist / "Release")], check=True)
        subprocess.run([*sign, "--armor", "--detach-sign", "-o", str(dist / "Release.gpg"), str(dist / "Release")],
                       check=True)
    else:
        print("warning: no --key given, repository is unsigned (local testing only)", file=sys.stderr)

    (args.site / ".nojekyll").touch()  # serve files as-is on GitHub Pages
    print(f"{len(pool)} package(s) in {repo}")


if __name__ == "__main__":
    main()
