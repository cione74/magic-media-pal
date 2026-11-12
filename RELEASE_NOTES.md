# Magic Media PAL v0.2.0-alpha.1

Magic Media PAL Home is the available Linux edition. Go and Desktop are planned. Experimental source-only alpha for trusted local use on Debian 13 amd64. No built-in authentication. See [Security and Known Issues](SECURITY.md) and [installation instructions](INSTALL.md).

The Debian build excludes MakeMKV. Users install the official runtime separately, after reviewing its license. The updated build requires Debian's zlib security fix and includes the FFmpeg HEVC backport.

The clean Debian source build passed 192 packaged tests and no-device setup, restart, update and rollback checks. The later image without the donor and the zlib-updated image each passed 192 tests. The zlib-updated image also passed no-device startup with official MakeMKV mounted. The complete recipient guide with a fresh MakeMKV cache was not replayed. The disposable VM walkthrough was staged, but the interactive vendor license step was not run there. The physical test used an existing support cache.

On the physical ripper, the official vendor installer prompt and in-app UHD scan, short extraction, and verified MP4 preparation passed before the zlib update. The original MKV was retained. The first attempt lost the USB optical drive, and the retry passed. Earlier Debian candidate evidence covers in-app CD ripping and local import, separate P1000 encoding, verified NFS transfer, and Jellyfin video and music playback. Full-length workflows, wider hardware coverage, app-driven shared NFS import on this image, and live subtitle playback remain limited or unverified. Multi-drive development is outside this alpha.

Earlier tests and physical results were recorded under the internal Dexter Ripper name. The public branding pass changes visible labels and documentation. It preserves internal paths, image tags and configuration identifiers.

Post-branding checks on the disposable Debian VM passed for Home, Settings, Credits and History, the first-run mascot image, setup without hardware, and restart persistence. Physical-media evidence remains from the earlier images.
