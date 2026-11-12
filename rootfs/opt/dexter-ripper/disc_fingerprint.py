"""Read the interoperable libdvdread Disc ID, never entire video streams."""
import ctypes
import ctypes.util
import json
import sys


def dvd_id(device):
    library = ctypes.util.find_library("dvdread") or "libdvdread.so.8"
    dvd = ctypes.CDLL(library)
    dvd.DVDOpen.argtypes = [ctypes.c_char_p]
    dvd.DVDOpen.restype = ctypes.c_void_p
    dvd.DVDDiscID.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dvd.DVDDiscID.restype = ctypes.c_int
    dvd.DVDClose.argtypes = [ctypes.c_void_p]
    handle = dvd.DVDOpen(str(device).encode())
    if not handle:
        raise ValueError("DVD structure could not be opened for identification")
    try:
        result = (ctypes.c_ubyte * 16)()
        if dvd.DVDDiscID(handle, result):
            raise ValueError("DVD Disc ID is unavailable")
        return {"type": "libdvdread-discid", "value": bytes(result).hex(),
                "alias": "dvdread1-" + bytes(result).hex(), "source": "libdvdread DVDDiscID"}
    finally:
        dvd.DVDClose(handle)


if __name__ == "__main__":
    try:
        print(json.dumps(dvd_id(sys.argv[1])))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
