"""Non-invasive first-run checks; installed binaries do not prove disc support."""
import os
from pathlib import Path
import shutil


def validate_roots(*roots):
    """Workspace cleanup must never overlap settings or library storage."""
    resolved = [Path(root).resolve() for root in roots]
    for index, root in enumerate(resolved):
        for other in resolved[index + 1:]:
            if root == other or root in other.parents or other in root.parents:
                raise ValueError('Configuration, working storage and media folders must be separate and must not contain one another.')


def report(drive_available, workspace, makemkv='/opt/makemkv/bin/makemkvcon'):
    checks = []
    def add(label, available, detail, tools=''):
        checks.append({'label': label, 'available': bool(available), 'detail': detail, 'tools': tools})
    video = bool(shutil.which(makemkv))
    add('Read movie and TV discs', video,
        'Disc-reading software is installed. Compatibility is checked when you scan a disc.' if video else
        'Disc-reading software is missing. Run Dexter setup again to install it.', 'MakeMKV')
    music = bool(shutil.which('cyanrip'))
    add('Save music CDs', music,
        'Music-reading software is installed.' if music else
        'Music-reading software is missing. Run Dexter setup again to install it.', 'cyanrip')
    compression = bool(shutil.which('HandBrakeCLI'))
    add('Make smaller video copies', compression,
        'Compression software is installed. Available choices depend on the video and this computer.' if compression else
        'Optional compression is unavailable. Original-quality video does not require it.', 'HandBrake')
    checks_available = all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe'))
    add('Check and preserve media files', checks_available,
        'File inspection and stream-copy tools are installed.' if checks_available else
        'File-checking software is missing. Run Dexter setup again before saving media.', 'FFmpeg / ffprobe')
    add('Optical drive', drive_available,
        'A drive is available to select below.' if drive_available else
        'Connect a drive. If it still does not appear, rerun setup so Dexter can access it.')
    workspace = Path(workspace)
    writable = workspace.is_dir() and os.access(workspace, os.W_OK | os.X_OK)
    add('Working storage', writable,
        'The working folder is writable. Required free space is checked before each operation.' if writable else
        'The working folder is unavailable or not writable. Check the installation before saving media.')
    return checks
