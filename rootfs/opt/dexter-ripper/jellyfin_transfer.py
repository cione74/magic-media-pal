"""Publish a verified movie on the configured NFS share as its writer."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile
import time

ROOT = Path("/jellyfin-import")


class ImportIssue(Exception):
    """An actionable message safe to show in the job status."""


def check_mount():
    expected = os.environ["DEXTER_JELLYFIN_EXPORT"]
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields = right.split()
        if left.split()[4] == str(ROOT) and fields[0] in ("nfs", "nfs4") and fields[1] == expected:
            return
    raise ImportIssue("Jellyfin storage is unavailable. Reconnect the share and try again.")


def publish(source, title, year, *, check_only=False, expected_bytes=None, episode=None):
    if source.suffix.lower() not in (".mp4", ".mkv") or source.is_symlink():
        raise ImportIssue("Choose a verified staged MP4 or MKV movie.")
    check_mount()
    movies = ROOT / ("tv" if episode else "movies")
    if movies.is_symlink() or not movies.is_dir():
        raise ImportIssue("The configured share has no " + ("TV" if episode else "Movies") + " folder.")
    season_directory = None
    if episode:
        from disc_intelligence import episode_filename
        series = str(episode.get("series", ""))
        if not series or series in (".", "..") or Path(series).name != series:
            raise ImportIssue("Choose a valid series name.")
        filename = episode_filename(series, int(episode["season"]), int(episode["number"]),
                                    episode.get("name", ""), source.suffix.lower())
        series_directory = movies / series
        season_directory = series_directory / f"Season {int(episode['season']):02d}"
        if series_directory.is_symlink() or season_directory.is_symlink():
            raise ImportIssue("The TV destination must use regular folders.")
        display = filename[:-len(source.suffix)]
    else:
        display = f"{title} ({year})" if year else title
    if display in (".", "..") or Path(display).name != display:
        raise ValueError("Invalid movie folder name.")
    destination = movies / display

    def check_duplicate():
        if episode:
            if series_directory.is_symlink() or season_directory.is_symlink():
                raise ImportIssue("The TV destination changed. Check its folders.")
            if season_directory.exists():
                prefix = f"{series} - S{int(episode['season']):02d}E{int(episode['number']):02d}"
                if any(item.name == filename or item.name.startswith(prefix + " - ") or item.name.startswith(prefix + ".")
                       for item in season_directory.iterdir()):
                    raise ImportIssue("This episode already exists in Jellyfin TV. Review it before importing again.")
            return
        normalized = re.sub(r" \(\d{4}\)$", "", display).casefold()
        for item in movies.iterdir():
            if re.sub(r" \(\d{4}\)$", "", item.name).casefold() == normalized:
                raise ImportIssue("A movie with this name already exists. Choose a different movie or review it in Jellyfin.")

    check_duplicate()
    try:
        with tempfile.TemporaryFile(dir=season_directory if season_directory and season_directory.is_dir() else movies) as handle:
            handle.write(b"Dexter destination check")
            handle.flush()
    except OSError as exc:
        raise ImportIssue("Dexter cannot write to the Movies share. Check its writer permissions.") from exc
    if check_only:
        if shutil.disk_usage(movies).free < (expected_bytes if expected_bytes is not None else source.stat().st_size):
            raise ImportIssue("Jellyfin storage needs more free space.")
        return
    with source.open("rb") as src:
        before = os.fstat(src.fileno())
        if shutil.disk_usage(movies).free < before.st_size:
            raise ImportIssue("Jellyfin storage is full. Free up space and try again.")
        with tempfile.TemporaryDirectory(prefix=".dexter-import-", dir=movies) as scratch:
            partial = Path(scratch) / "movie.partial"
            digest = hashlib.sha256()
            copied = 0
            next_report = 0.0
            with partial.open("xb") as dst:
                while chunk := src.read(8 * 1024**2):
                    dst.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    now = time.monotonic()
                    if now >= next_report:
                        print(f"Transfer bytes: {copied}", flush=True)
                        next_report = now + 2
                dst.flush()
                os.fchmod(dst.fileno(), 0o664)
                os.fsync(dst.fileno())
            print(f"Transfer bytes: {copied}", flush=True)
            print("Checking the copy…", flush=True)
            with partial.open("rb") as dst:
                received = hashlib.file_digest(dst, "sha256").hexdigest()
            after = source.stat()
            if (copied != before.st_size or received != digest.hexdigest()
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise ImportIssue("The copy could not be verified. Your staged movie is ready to retry.")
            check_mount()
            check_duplicate()
            if episode:
                series_directory.mkdir(mode=0o775, exist_ok=True)
                season_directory.mkdir(mode=0o775, exist_ok=True)
                check_duplicate()
                os.link(partial, season_directory / filename)
            else:
                destination.mkdir(mode=0o775)
                try:
                    os.link(partial, destination / (display + source.suffix.lower()))
                except OSError:
                    destination.rmdir()
                    raise
            print(f"Verified SHA-256 {received}", flush=True)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Verify and publish a staged video")
    parser.add_argument("source", type=Path)
    parser.add_argument("title")
    parser.add_argument("year")
    parser.add_argument("--check", type=int, nargs="?", const=-1, default=None)
    parser.add_argument("--episode", type=json.loads)
    args = parser.parse_args(argv)
    try:
        publish(args.source, args.title, args.year, check_only=args.check is not None,
                expected_bytes=None if args.check in (None, -1) else args.check, episode=args.episode)
    except ImportIssue as exc:
        print(f"Action: {exc}", flush=True)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Transfer detail: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    def cancelled(signum, frame):
        raise SystemExit(130)
    signal.signal(signal.SIGTERM, cancelled)
    raise SystemExit(main())
