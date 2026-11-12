# Third-party terms

Magic Media PAL's original code is GPLv3 only. Third-party components and data retain their terms. See [Acknowledgments](ACKNOWLEDGMENTS.md).

The source build retrieves Debian packages and FFmpeg source through APT. It applies the included upstream HEVC backport and records package versions, the patch and build metadata in the locally built image. The build recipe pins its base image. Debian package indexes can change, so build output is not guaranteed to be byte-identical. Locally installed Debian copyright files are under /usr/share/doc. Distribution of a built image requires applicable notices and corresponding-source arrangements. [FFmpeg licensing guidance](https://ffmpeg.org/legal.html).

Recipients obtain official MakeMKV 2.0.0 separately and review the Linux EULA at the vendor Makefile prompt. Magic Media PAL mounts the recipient's installed runtime read-only and uses an unmodified executable. The source release contains no MakeMKV program files, keys or support data and does not authorize redistribution of a prebuilt MakeMKV image. The vendor terms distinguish installing and using the software from redistributing it. [Official MakeMKV Linux downloads](https://forum.makemkv.com/forum/viewtopic.php?f=3&t=224).

Online metadata providers can receive disc or track lookup information. MakeMKV can contact its vendor for updates, support data or diagnostics when networking is available. Provider and data attribution is in ACKNOWLEDGMENTS.md.
