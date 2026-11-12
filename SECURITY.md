# Security and Known Issues

Magic Media PAL is an experimental local app with no built-in login. Keep it bound to loopback on a trusted machine. Use a trusted SSH tunnel for remote access. Mount only the folders and drive you intend Magic Media PAL to use. Process trusted media and keep private settings, API keys and support caches out of bug reports.

The alpha has known upstream issues. Debian still lists the installed libxml2 package as affected by CVE-2026-6653 and rates the issue minor for Debian 13. Magic Media PAL's media tools load the library. Exposure through normal workflows is unverified. Other upstream media parser findings remain pending. The Debian FFmpeg build includes the HEVC count fix, and the runtime requires Debian's zlib security update. These changes do not cover every advisory.

Magic Media PAL fixes significant bugs it owns and updates or mitigates significant upstream risks when its workflows expose them. Lower-risk or unverified findings can be accepted with a recorded reason. A scanner match is a lead, and unknown exposure remains pending. Unresolved critical or high-risk exposure goes to the release owner for an explicit decision. The detailed [findings register](SECURITY_FINDINGS.json) records what is known.

The UI version of this page is deferred. See [release notes](RELEASE_NOTES.md) for the hardware and installation test scope.
