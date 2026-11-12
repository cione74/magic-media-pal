"""Publish a prepared album as the configured NFS writer, retaining sources."""
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import shutil
import platform
import signal
import sys
import tempfile
import jellyfin_transfer


def music_root():
    jellyfin_transfer.check_mount()
    root = jellyfin_transfer.ROOT / "music"
    if root.is_symlink() or not root.is_dir():
        raise ValueError("The Jellyfin share has no Music folder. Check its library mapping.")
    return root


def name(value):
    if not value or value in (".", "..") or Path(value).name != value:
        raise ValueError("Choose a valid artist and album name.")
    return value


def check(artist, album, expected=0):
    root = music_root()
    artist_dir = root / name(artist)
    destination = artist_dir / name(album)
    if artist_dir.is_symlink() or (artist_dir.exists() and not artist_dir.is_dir()):
        raise ValueError("The artist destination is not a regular folder.")
    if destination.exists() or destination.is_symlink():
        raise ValueError("This album already exists in Jellyfin Music. Review it before importing again.")
    with tempfile.TemporaryFile(dir=artist_dir if artist_dir.exists() else root) as handle:
        handle.write(b"Dexter music destination check")
        handle.flush()
    if shutil.disk_usage(root).free < expected + 64 * 1024**2:
        raise ValueError("Jellyfin Music needs more free space.")
    return artist_dir, destination


def rename_new(source, destination):
    # Linux RENAME_NOREPLACE publishes the entire album atomically and cannot
    # replace an album created by another writer after the destination check.
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    types = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if function is None and platform.machine() == "x86_64":
        # The supported Linux x86-64 runtime may omit the libc wrapper.
        # __NR_renameat2 is 316 in Linux asm/unistd_64.h.
        function = libc.syscall
        arguments = (316,) + arguments
        types = [ctypes.c_long] + types
    if function is None:
        raise ValueError("This runtime cannot safely publish an album without replacing existing media.")
    function.argtypes = types
    function.restype = ctypes.c_long
    if function(*arguments):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def publish_verified_album(partial, destination):
    try:
        rename_new(partial, destination)
        return
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise
    # Some NFS servers reject RENAME_NOREPLACE. Reserve the destination with
    # exclusive mkdir, then link only complete, previously verified files.
    # Each link is atomic and refuses replacement. A scanner may see a subset
    # of the album briefly, but never a partially copied track.
    destination.mkdir(mode=0o775)
    linked = []
    try:
        for source in sorted(partial.iterdir()):
            target = destination / source.name
            os.link(source, target)
            linked.append((source, target))
    except BaseException:
        for source, target in linked:
            try:
                if target.samefile(source):
                    target.unlink()
            except OSError:
                pass
        try:
            destination.rmdir()
        except OSError:
            pass
        raise
    print("Published verified tracks using the NFS-compatible method", flush=True)


def publish(source, artist, album):
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Prepared music is unavailable. Your original tracks remain in staging.")
    files = sorted(source.iterdir())
    if not files or any(p.is_symlink() or not p.is_file() or p.suffix != ".flac" for p in files):
        raise ValueError("Choose an album containing only verified FLAC tracks.")
    artist_dir, destination = check(artist, album, sum(p.stat().st_size for p in files))
    artist_dir.mkdir(mode=0o775, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dexter-album-", dir=artist_dir) as scratch:
        partial = Path(scratch) / "album"
        partial.mkdir(mode=0o775)
        for index, path in enumerate(files, 1):
            target = partial / path.name
            digest = hashlib.sha256()
            with path.open("rb") as src, target.open("xb") as dst:
                before = os.fstat(src.fileno())
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    digest.update(chunk)
                dst.flush()
                os.fchmod(dst.fileno(), 0o664)
                os.fsync(dst.fileno())
            with target.open("rb") as handle:
                received = hashlib.file_digest(handle, "sha256").hexdigest()
            after = path.stat()
            if received != digest.hexdigest() or target.stat().st_size != before.st_size or any(
                    getattr(before, key) != getattr(after, key)
                    for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
                raise ValueError("A copied track did not verify. Your staged tracks are ready to retry.")
            print(f"Track verified: {index}", flush=True)
        check(artist, album)
        publish_verified_album(partial, destination)
    print("Album published and verified", flush=True)
    return destination


if __name__ == "__main__":
    def cancel(signum, frame):
        raise SystemExit(130)
    signal.signal(signal.SIGTERM, cancel)
    try:
        if "--check" in sys.argv:
            check(sys.argv[2], sys.argv[3], int(sys.argv[5]))
        else:
            publish(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
    except (OSError, ValueError, jellyfin_transfer.ImportIssue) as exc:
        print("Music destination: " + str(exc), flush=True)
        raise SystemExit(1)
