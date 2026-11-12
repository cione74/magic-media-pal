# Acknowledgments and data sources

## Disc identification

TVmaze series/episode metadata is licensed under CC BY-SA 4.0. Credit https://www.tvmaze.com and preserve per-result links, license https://creativecommons.org/licenses/by-sa/4.0/ and modification notices when distributing adapted metadata. Magic Media PAL normalizes selected text fields. Artwork is not redistributed with the application. ShareAlike applies to adapted data, not merely to a specific cache folder.

TheDiscDb data is MIT licensed. TheDiscDb web software is Apache-2.0 and is not incorporated. Data notice follows:

MIT License

Copyright (c) 2021 Luke Foust

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

libdvdread is GPL-2.0-or-later, installed from Debian in the Debian candidate and from Alpine in the legacy image. Source and package details: https://www.videolan.org/developers/libdvdread.html and https://pkgs.alpinelinux.org/package/v3.19/community/x86_64/libdvdread . Distribution must retain applicable component notices and satisfy corresponding source obligations.

OVID's fingerprint specification is CC0. Magic Media PAL uses the standard libdvdread Disc ID alias documented there. No OVID AGPL software is incorporated. https://github.com/The-Artificer-of-Ciphers-LLC/OVID/blob/main/docs/fingerprint-spec.md

See [third-party build and distribution notes](THIRD_PARTY.md). The application provides Credits and Data Sources at `/data-sources`.


## Integrated software and service inventory

Updated November 10, 2026. Visible in Magic Media PAL at /credits (legacy /data-sources remains available).

### Disc reading and media processing

- [MakeMKV / LibMMBD / LibreDrive](https://www.makemkv.com/): Disc scanning, selected-title extraction and supported Blu-ray access. MakeMKV has its own licensing terms.
- [HandBrake](https://handbrake.fr/): CPU video compression.
- [FFmpeg and ffprobe](https://ffmpeg.org/): NVIDIA encoding, media inspection, packaging, verification and decode checks.
- [cyanrip](https://github.com/cyanreg/cyanrip): Audio CD extraction to lossless FLAC.
- [libbluray](https://www.videolan.org/developers/libbluray.html): Blu-ray structure and sample reading.
- [libdvdread](https://www.videolan.org/developers/libdvdread.html): DVD structure reading and standard disc identification.
- [x264 and x265](https://www.videolan.org/developers/): Video encoding libraries used by the bundled media tools.
- [FLAC](https://xiph.org/flac/): Lossless audio format and supporting software.

### Media library and metadata services

- [Jellyfin](https://jellyfin.org/): Optional existing movie, TV and music library integration; server is separate from Magic Media PAL.
- [MusicBrainz](https://musicbrainz.org/): Audio CD album and track metadata.
- [Deezer](https://developers.deezer.com/): Optional ISRC-based music metadata fallback.
- [TVmaze](https://www.tvmaze.com/): Series and episode suggestions; metadata under CC BY-SA 4.0.
- [TheDiscDb](https://thediscdb.com/): Exact physical-disc lookup when a matching data record exists; MIT data notice below.

### Application and container runtime

- [Python](https://www.python.org/): Application, installer and test language.
- [Flask, Werkzeug and Jinja](https://palletsprojects.com/): Web application, request handling and templates.
- [MarkupSafe, ItsDangerous, Click and Blinker](https://palletsprojects.com/): Template escaping, signed browser sessions and Flask runtime support.
- [Docker Engine and Docker Compose](https://www.docker.com/): Application packaging and development deployment.
- [jlesage/docker-makemkv](https://github.com/jlesage/docker-makemkv): Upstream MakeMKV container base.
- [jlesage/docker-handbrake](https://github.com/jlesage/docker-handbrake): Upstream donor image for the isolated HandBrake runtime.
- [Debian, Alpine Linux and Linux](https://www.debian.org/): Host/build/runtime operating-system projects; Alpine: https://alpinelinux.org/; Linux: https://www.kernel.org/.
- [GNU C Library and musl](https://www.gnu.org/software/libc/): C runtimes for the isolated media tools; musl: https://musl.libc.org/.
- [NVIDIA NVENC and Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/): Optional supported GPU encoding and container access. NVIDIA components have their own terms.
- [NFS utilities and util-linux](https://github.com/util-linux/util-linux): Existing shared-storage access, device inspection and writer-identity tools.

### Build, verification and interoperability

- [GCC, GNU Coreutils and Bash](https://www.gnu.org/software/): Build compiler and shell/file utilities.
- [pax-utils / lddtree](https://wiki.gentoo.org/wiki/Hardened/PaX_Utilities): Linked-library inventory for isolated runtimes.
- [Python unittest, Ruff, Bandit and pip-audit](https://docs.python.org/3/library/unittest.html): Development tests and release audit tools; not media-processing features.
- [Git and Gitea](https://git-scm.com/): Development history and private source hosting; Gitea: https://about.gitea.com/.
- [OVID fingerprint specification](https://github.com/The-Artificer-of-Ciphers-LLC/OVID/blob/main/docs/fingerprint-spec.md): CC0 interoperability reference for the standard libdvdread Disc ID alias. OVID software is not incorporated.

Credits do not establish redistribution clearance. Retain upstream license texts and satisfy corresponding-source obligations for the actual image. A complete transitive, version-specific package inventory remains a release task. HandBrake GPLv2 reference: https://handbrake.fr/project_information.php . MakeMKV licensing reference: https://www.makemkv.com/buy/ .

## Branding artwork

Pal the Genie artwork supplied by the project owner for Magic Media PAL. The GPLv3 license notice applies to the original application code.
