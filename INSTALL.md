# Install Magic Media PAL with official MakeMKV on Debian 13

This is the proposed source alpha procedure for Debian 13 amd64. The Magic Media PAL image is built locally without the third-party MakeMKV donor. MakeMKV remains a separate vendor installation and is mounted read-only. Review the [MakeMKV Linux download instructions](https://forum.makemkv.com/forum/viewtopic.php?f=3&t=224) and its EULA before installing the proprietary part. The VM verified the build and no-device setup. The vendor prompt and in-app UHD extraction passed on the physical ripper with an existing support cache. The full fresh-cache VM walkthrough remains unverified.

## Build Magic Media PAL

From the source checkout on a trusted Debian host:

```sh
sudo apt-get update
sudo apt-get install -y docker.io python3 make less
sudo docker build -f tools/runtime-candidate/Dockerfile.external-makemkv \
  -t dexter-ripper:external-makemkv-security .
sudo sh tools/test-image.sh dexter-ripper:external-makemkv-security
sudo python3 tools/test-runtime-candidate.py --image dexter-ripper:external-makemkv-security
```

The build compiles patched FFmpeg and checks the installed zlib version. Base images are pinned. Debian package indexes can change, so record the image ID and /usr/share/dexter-build/packages.tsv when reporting an issue.

## Install MakeMKV separately

Download the official 2.0.0 binary and OSS archives directly from MakeMKV. Keep them outside the Magic Media PAL checkout and verify the [vendor-published checksums](https://www.makemkv.com/download/makemkv-sha-2.0.0.txt). The archive hashes checked during validation were:

```text
f1265e74875a186efdfbbbec7459a64e969033515e53cbc4d805f0a374f0a124  makemkv-bin-2.0.0.tar.gz
435316b2d219eb48c880526557addd076b5f5e6de5171424c7651f9cac95b161  makemkv-oss-2.0.0.tar.gz
```

Use Bash for the pipeline failure check. In the example, the downloaded files are in ~/Downloads. Change that path if needed.

```bash
set -euo pipefail
dexter_source="$PWD"
work="$(mktemp -d "$HOME/dexter-makemkv.XXXXXX")"
install -m 644 "$HOME/Downloads/makemkv-bin-2.0.0.tar.gz" "$work/"
install -m 644 "$HOME/Downloads/makemkv-oss-2.0.0.tar.gz" "$work/makemkv-oss.tar.gz"
(
  cd "$work"
  printf '%s\n' \
    'f1265e74875a186efdfbbbec7459a64e969033515e53cbc4d805f0a374f0a124  makemkv-bin-2.0.0.tar.gz' \
    '435316b2d219eb48c880526557addd076b5f5e6de5171424c7651f9cac95b161  makemkv-oss.tar.gz' \
    | sha256sum -c -
  tar -xzf makemkv-bin-2.0.0.tar.gz
)
vendor="$work/makemkv-bin-2.0.0"
make -C "$vendor"
make -C "$vendor" install "DESTDIR=$work/installed" PREFIX=/opt/makemkv
runtime="$work/installed/opt/makemkv"
mkdir "$work/oss-context"
mv "$work/makemkv-oss.tar.gz" "$work/oss-context/"
sudo docker build -f "$dexter_source/tools/runtime-candidate/Dockerfile.makemkv-oss-check" \
  --target export --output type=tar,dest=- "$work/oss-context" \
  | tar --no-same-owner -xf - -C "$runtime"
install -m 644 "$vendor/src/eula_en_linux.txt" "$runtime/share/MakeMKV/eula_en_linux.txt"
printf '%s\n' \
  "ed3d4c7249fe282593ca50a72fabe146689c20f9903f6cbe78a213bdc47f08e8  $runtime/bin/makemkvcon" \
  "59c1ef173c65d4e3240d9f0a46222def97274d45ad6fa3dd22066751f12d43ee  $runtime/share/MakeMKV/eula_en_linux.txt" \
  | sha256sum -c -
```

The vendor Makefile displays the Linux EULA. Quit the pager with Q and type yes only if you accept. If you decline, installation stops. The staging prefix keeps the vendor install out of the host system directories. Keep the runtime path for later. The OSS build context contains only the verified OSS archive.

## Run Magic Media PAL locally

Use a dedicated empty workspace and the runtime path above. Identify the optical drive and its matching generic node with ls /sys/class/block/sr0/device/scsi_generic. Add only the matching device mappings. The example assumes /dev/sr0 and /dev/sg2. Omit both mappings for setup without a drive.

```sh
workspace="$HOME/dexter-local"
mkdir -p "$workspace/config" "$workspace/staging" "$workspace/library/music"
printf "Trusted LAN IPv4 address for this Debian host: "
IFS= read -r lan_ip
case "$lan_ip" in ""|0.0.0.0|*[!0-9.]*) echo "Enter a specific IPv4 address."; exit 1;; esac
sudo docker run -d --name dexter-local \
  -p "$lan_ip:5801:8080" \
  -e RIPPER_DRIVE=/dev/sr0 -e LD_LIBRARY_PATH=/opt/makemkv/lib \
  --device /dev/sr0 --device /dev/sg2 \
  --mount "type=bind,source=$runtime,target=/opt/makemkv,readonly" \
  --mount "type=bind,source=$workspace/config,target=/config" \
  --mount "type=bind,source=$workspace/staging,target=/output" \
  --mount "type=bind,source=$workspace/library,target=/media" \
  dexter-ripper:external-makemkv-security
```

Open port 5801 at the address you entered from a browser on the trusted LAN. Enter 127.0.0.1 for host-only access. Magic Media PAL has no login. Keep the port off the public internet. The first UHD scan may need vendor support data. The physical acceptance test used an existing support cache and an isolated offline network. New installs have no such cache. Network-enabled MakeMKV can contact the vendor for support data, updates and diagnostics. Keep the runtime directory and back up config, staging and library before replacing the container. Never share the runtime bundle, keys or support cache as part of the Magic Media PAL source release.
