"""Bounded compression checks against a selected Blu-ray playlist."""
from pathlib import Path
import re
import subprocess
import tempfile

import encoding
import makemkv_runtime


READER = Path("/opt/bluray-runtime")
MAKEMKV = Path("/opt/makemkv")


def playlist_number(source):
    match = re.fullmatch(r"([0-9]{5})\.mpls", source or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


def check(playlist, drive, config):
    library = MAKEMKV / "lib/libmmbd.so.0"
    sampler = READER / "bluray-sample"
    if not sampler.is_file() or not library.is_file():
        return "Couldn’t check compression. The disc sample reader is unavailable.", ""

    with tempfile.TemporaryDirectory(prefix="dexter-disc-sample-") as scratch:
        folder = Path(scratch)
        for name in ("libaacs.so.0", "libbdplus.so.0"):
            (folder / name).symlink_to(library)
        sample = folder / "sample.m2ts"
        library_path = ":".join(map(str, (folder, READER / "lib", MAKEMKV / "lib")))
        command = [
            "env", "MMBD_TRACE=1", f"MAKEMKVCON={MAKEMKV / 'bin/makemkvcon'}",
            f"LD_LIBRARY_PATH={library_path}",
            str(READER / "lib/ld-linux-x86-64.so.2"),
            "--library-path", library_path, str(sampler), drive, str(playlist), str(sample),
        ]
        rc, log = makemkv_runtime.run(command, config, drive, timeout=30)
        if rc or not sample.is_file():
            return "Couldn’t read a sample for this title. You can still request compression. Dexter will inspect the extracted title.", log
        try:
            metadata = encoding.inspect(sample)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return "Couldn’t inspect this sample. Dexter can check the verified rip later.", str(exc)
        videos = [stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "video"]
        if len(videos) != 1:
            return "Couldn’t identify a single video stream in the sample. Dexter can check the verified rip later.", log
        try:
            properties = encoding.source_properties(metadata)
        except ValueError as exc:
            return "Dexter needs the extracted title to check its streams.", str(exc)
        if properties["protected"]:
            return "The sample may need special handling to preserve picture information. Dexter will inspect the extracted title.", log
        if properties["conversion"]:
            return "This title may need picture preparation. Dexter will check the extracted title before encoding.", log
        if properties["hdr"] is None:
            return "Color settings are missing in the sample. You can request compression now and Dexter will check the extracted title.", log
        return "The sample looks ready for a test encode. Dexter will check the extracted title before encoding.", log
