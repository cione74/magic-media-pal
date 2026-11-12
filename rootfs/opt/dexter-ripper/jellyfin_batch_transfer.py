"""Verified, no-overwrite batch publication on the configured Jellyfin share."""
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import signal
import sys
import tempfile
import time
import jellyfin_transfer


def targets(manifest):
    kind=manifest.get("kind")
    if kind not in ("tv","movies"):
        raise ValueError("Choose the Movies or TV destination")
    library=jellyfin_transfer.ROOT/kind
    if library.is_symlink() or not library.is_dir():
        raise ValueError("The Jellyfin share is missing the selected library folder")
    files=manifest.get("files")
    if not isinstance(files,list) or not 1<=len(files)<=1000:
        raise ValueError("Choose a verified batch of files")
    seen=set();episodes=set();result=[]
    for item in files:
        source=Path(item["source"])
        relative=PurePosixPath(item["relative"])
        if relative.is_absolute() or not relative.parts or any(p in (".","..") for p in relative.parts):
            raise ValueError("Invalid library destination")
        if source.is_symlink() or not source.is_file() or source.suffix.lower() not in (".mp4",".mkv"):
            raise ValueError("A batch source is unavailable. Local files are kept")
        if relative.suffix.lower()!=source.suffix.lower() or len(relative.parts)>4:
            raise ValueError("Invalid batch filename")
        key=str(relative).casefold()
        if key in seen:
            raise ValueError("Two selected files have the same destination")
        seen.add(key)
        target=library.joinpath(*relative.parts)
        for parent in (target,*target.parents):
            if parent==library.parent:break
            if parent.is_symlink():raise ValueError("Library destinations must use regular folders")
        if kind == "tv":
            match = re.match(r"^(.+ - S[0-9]+E[0-9]+)(?: - |\.)", target.name, re.IGNORECASE)
            if not match:
                raise ValueError("TV files need a confirmed season and episode filename")
            prefix = match.group(1).casefold()
            episode_key = (str(target.parent).casefold(), prefix)
            if episode_key in episodes:
                raise ValueError("Two selected files have the same episode number")
            episodes.add(episode_key)
            if target.parent.is_dir():
                for existing in target.parent.iterdir():
                    name = existing.name.casefold()
                    if name.startswith(prefix + " - ") or name.startswith(prefix + "."):
                        raise ValueError("This episode already exists. Review it before importing again")
        if target.exists():raise ValueError("A destination file already exists. Review it before importing again")
        if item.get("expected_bytes") is not None and source.stat().st_size!=int(item["expected_bytes"]):
            raise ValueError("A source differs from the verified processing record")
        result.append((source,target))
    return library,result


def publish(manifest, *, check_only=False):
    jellyfin_transfer.check_mount()
    library,plans=targets(manifest)
    total=sum(source.stat().st_size for source,_ in plans)
    if shutil.disk_usage(library).free < total+64*1024**2:
        raise ValueError("Jellyfin storage needs more free space")
    checked=set()
    for _,target in plans:
        parent=target.parent
        while not parent.exists():parent=parent.parent
        if parent not in checked:
            with tempfile.TemporaryFile(dir=parent) as handle:
                handle.write(b"Dexter destination check");handle.flush()
            checked.add(parent)
    if check_only:return
    copied=0;last_report=0;linked=[];created=[]
    with tempfile.TemporaryDirectory(prefix=".dexter-batch-",dir=library) as scratch:
        prepared=[]
        try:
            for index,(source,target) in enumerate(plans,1):
                partial=Path(scratch)/(str(index)+".partial")
                digest=hashlib.sha256()
                with source.open("rb") as src,partial.open("xb") as dst:
                    before=os.fstat(src.fileno())
                    while chunk:=src.read(8*1024**2):
                        dst.write(chunk);digest.update(chunk);copied+=len(chunk)
                        if time.monotonic()-last_report>=1:
                            print(f"Transfer bytes: {copied}",flush=True);last_report=time.monotonic()
                    dst.flush();os.fchmod(dst.fileno(),0o664);os.fsync(dst.fileno())
                print(f"Checking file: {index} of {len(plans)}",flush=True)
                with partial.open("rb") as handle:received=hashlib.file_digest(handle,"sha256").hexdigest()
                after=source.stat()
                if (received!=digest.hexdigest() or partial.stat().st_size!=before.st_size or
                        any(getattr(before,k)!=getattr(after,k) for k in ("st_dev","st_ino","st_size","st_mtime_ns"))):
                    raise ValueError("A copied file did not verify. Local files are kept")
                prepared.append((partial,target))
            jellyfin_transfer.check_mount()
            targets(manifest)
            for partial,target in prepared:
                missing=[];parent=target.parent
                while parent!=library and not parent.exists():missing.append(parent);parent=parent.parent
                for directory in reversed(missing):
                    directory.mkdir(mode=0o775);created.append(directory)
                # Recheck symlinks/duplicates immediately before no-replace links.
                for parent in target.parents:
                    if parent==library.parent:break
                    if parent.is_symlink():raise ValueError("The library destination changed")
                os.link(partial,target);linked.append((partial,target))
            print(f"Transfer bytes: {copied}",flush=True)
            print(f"Batch published and verified: {len(prepared)}",flush=True)
        except BaseException:
            for partial,target in reversed(linked):
                try:
                    if target.samefile(partial):target.unlink()
                except OSError:pass
            for directory in reversed(created):
                try:directory.rmdir()
                except OSError:pass
            raise


def main():
    try:
        raw=sys.stdin.read(4*1024**2+1)
        if len(raw)>4*1024**2:raise ValueError("The batch plan is too large")
        publish(json.loads(raw),check_only="--check" in sys.argv[1:])
        return 0
    except (OSError,ValueError,KeyError,TypeError,jellyfin_transfer.ImportIssue) as exc:
        print("Action: "+str(exc),flush=True);return 1


if __name__=="__main__":
    def cancel(signum,frame):raise SystemExit(130)
    signal.signal(signal.SIGTERM,cancel)
    raise SystemExit(main())
