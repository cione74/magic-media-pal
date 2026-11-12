# Magic Media PAL

**A little magic for your media collection.**

![Pal the Genie mascot artwork](rootfs/opt/dexter-ripper/static/pal-the-genie.png)

P.A.L. stands for Personal Archive Librarian. This release is Magic Media PAL Home v0.2.0-alpha.1.

An experimental source-only alpha for trusted local use on Debian 13 amd64. Magic Media PAL has no built-in authentication. Keep the web interface on loopback and use a trusted SSH tunnel if needed.

- [Install and build from source](INSTALL.md)
- [Security and known issues](SECURITY.md)
- [Release notes and test scope](RELEASE_NOTES.md)
- [Third-party terms](THIRD_PARTY.md) and [acknowledgments](ACKNOWLEDGMENTS.md)

Magic Media PAL's original code is [GPLv3 only](LICENSE). MakeMKV is installed separately under its vendor terms. This archive ships no container image or MakeMKV binaries.

## Product editions

- **Home** is the Linux appliance available in this alpha. It copies optical media, prepares smaller files when supported, and can deliver media to Jellyfin.
- **Go** is a planned portable Linux edition with offline workflows and a future Import When Home option.
- **Desktop** is a planned Windows edition.

Go and Desktop are roadmap items. Pal the Genie is the product mascot.
