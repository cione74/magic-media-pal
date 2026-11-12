"""Refuse a stale image before running tests against mounted candidate sources."""
from pathlib import Path
import hashlib
import subprocess
import sys


def verify(source_root=Path('/workspace/rootfs'), image_root=Path('/')):
    failures = []
    checked = 0
    for source in sorted(source_root.rglob('*')):
        if not source.is_file() or '__pycache__' in source.parts or source.suffix == '.pyc':
            continue
        relative = source.relative_to(source_root)
        installed = image_root / relative
        if (not installed.is_file() or installed.is_symlink() or
                hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(installed.read_bytes()).digest()):
            failures.append(str(relative))
        checked += 1
    if not checked or failures:
        raise ValueError('Image/source mismatch: ' + ', '.join(failures or ['no source files checked']))
    return checked


if __name__ == '__main__':
    try:
        print(f'Packaged source verified: {verify()} files', flush=True)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(subprocess.call([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']))
