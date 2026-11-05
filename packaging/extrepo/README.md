# extrepo submission

Files for listing nvenc-shrink in Debian's [extrepo](https://salsa.debian.org/extrepo-team/extrepo-data),
so users can run `sudo extrepo enable nvenc-shrink`.

- `nvenc-shrink.yaml`: the repository definition
- `nvenc-shrink.asc`: the repository signing key (same as `../apt/nvenc-shrink-archive-keyring.asc`);
  `gpg-key-checksum` is its SHA-256

To submit: fork extrepo-data on salsa.debian.org, add both files next to the
other repository definitions (check `template.yaml` there for any newly
required fields), and open a merge request. The packages are MIT licensed and
depend only on Debian main, so `policy: main` applies.

If the signing key ever changes, update the key and checksum here and send
the change to extrepo-data as well.
