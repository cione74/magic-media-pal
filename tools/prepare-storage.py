"""Explicit administrator-only full-disk preparation. Never invoked by the web app."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout.strip()


def descendants(node):
    yield node
    for child in node.get('children', []):
        yield from descendants(child)


def validate_disk(document, device, serial, size):
    target = str(Path(device).resolve())
    disk = next((d for d in document['blockdevices'] if d['path'] == target), None)
    if not disk or disk.get('type') != 'disk':
        raise ValueError('Select a whole physical disk using its stable by-id path.')
    if not serial or str(disk.get('serial') or '').strip() != serial or int(disk['size']) != size:
        raise ValueError('Disk identity or capacity changed. No erase is allowed.')
    for node in descendants(disk):
        if node.get('mountpoints') and any(node['mountpoints']):
            raise ValueError('This disk has a mounted filesystem or active swap. It cannot be erased here.')
        if node.get('ro'):
            raise ValueError('This disk is read-only.')
        # Device-mapper, RAID and encrypted holders must not be overwritten.
        if node is not disk and node.get('type') != 'part':
            raise ValueError('This disk has active storage mappings. Review them before erasing.')
        holders = Path('/sys/class/block') / Path(node['path']).name / 'holders'
        if holders.exists() and any(holders.iterdir()):
            raise ValueError('This disk is in use by another storage device.')
    return disk


def main():
    parser = argparse.ArgumentParser(description='Erase one explicitly identified disk and use its full usable capacity for Dexter')
    parser.add_argument('--device', required=True, help='Stable /dev/disk/by-id/... whole-disk path')
    parser.add_argument('--serial', required=True)
    parser.add_argument('--size-bytes', required=True, type=int)
    parser.add_argument('--mountpoint', required=True)
    parser.add_argument('--erase', action='store_true', help='Explicitly authorize destructive preparation; otherwise preview only')
    args = parser.parse_args()
    if not args.device.startswith('/dev/disk/by-id/') or not Path(args.device).is_symlink():
        raise ValueError('A stable whole-disk by-id link is required.')
    mountpoint = Path(args.mountpoint)
    if not re.fullmatch(r'/mnt/[A-Za-z0-9_-]+', str(mountpoint)) or mountpoint != mountpoint.resolve():
        raise ValueError('Choose a new direct folder under /mnt without symbolic links.')
    if mountpoint.exists() and (not mountpoint.is_dir() or any(mountpoint.iterdir()) or mountpoint.is_mount()):
        raise ValueError('The mount destination is already in use.')
    fstab = Path('/etc/fstab')
    for line in fstab.read_text().splitlines():
        fields = line.split()
        if fields and not fields[0].startswith('#') and len(fields) > 1 and fields[1] == str(mountpoint):
            raise ValueError('That mount destination already appears in fstab. Review the existing setup.')
    root = Path(__file__).resolve().parents[1]
    state = root / 'config/dexter-ripper-state.json'
    if state.exists() and json.loads(state.read_text())['state']['job'].get('running'):
        raise ValueError('Finish the Dexter job before preparing storage.')
    disk = validate_disk(json.loads(command(['lsblk', '--tree', '--json', '--bytes', '-o',
                         'PATH,SIZE,TYPE,SERIAL,MODEL,MOUNTPOINTS,RO'])), args.device, args.serial, args.size_bytes)
    print(f"Disk: {disk.get('model', '')} | {disk['path']} | serial {args.serial} | {args.size_bytes} bytes", flush=True)
    print(f'Plan: erase disk, create GPT and one full-capacity ext4 partition; mount at {mountpoint}.', flush=True)
    print('This is preparation for reuse, not a secure data-sanitization operation.', flush=True)
    if not args.erase:
        print('Preview only. Nothing was changed.')
        return
    if os.geteuid() != 0 or not os.environ.get('SUDO_UID') or int(os.environ['SUDO_UID']) == 0:
        raise ValueError('Run through sudo from the account that will use Dexter.')
    uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
    for binary in ('sfdisk', 'mkfs.ext4', 'blkid', 'udevadm', 'mount', 'systemctl'):
        if not shutil.which(binary):
            raise ValueError(f'Required system tool is missing: {binary}')
    # Recheck immediately before the first destructive command.
    validate_disk(json.loads(command(['lsblk', '--tree', '--json', '--bytes', '-o',
                  'PATH,SIZE,TYPE,SERIAL,MODEL,MOUNTPOINTS,RO'])), args.device, args.serial, args.size_bytes)
    command(['sfdisk', '--wipe', 'always', '--wipe-partitions', 'always', args.device],
            input='label: gpt\n, , L\n')
    command(['udevadm', 'settle', '--timeout=15'])
    partition = Path(args.device + '-part1')
    if not partition.exists():
        raise ValueError('Partition created but its stable device link is unavailable. Stop and inspect before continuing.')
    command(['mkfs.ext4', '-m', '0', '-L', 'dexter-storage', str(partition)])
    uuid = command(['blkid', '-s', 'UUID', '-o', 'value', str(partition)])
    if not re.fullmatch(r'[a-fA-F0-9-]+', uuid):
        raise ValueError('Filesystem UUID could not be verified. No mount configuration written.')
    mountpoint.mkdir(mode=0o755, exist_ok=True)
    backup = fstab.with_name('fstab.dexter-backup-' + str(time.time_ns()))
    shutil.copy2(fstab, backup)
    with fstab.open('a') as stream:
        stream.write(f'\n# Dexter storage — explicitly approved disk {args.serial}\nUUID={uuid} {mountpoint} ext4 defaults,nofail,x-systemd.automount,x-systemd.device-timeout=10s 0 2\n')
        stream.flush()
        os.fsync(stream.fileno())
    command(['systemctl', 'daemon-reload'])
    command(['mount', str(mountpoint)])
    if not mountpoint.is_mount() or command(['findmnt', '-n', '-o', 'UUID', '--target', str(mountpoint)]) != uuid:
        raise ValueError('The new filesystem was not mounted as expected. Do not select it until reviewed.')
    for path in (mountpoint / 'dexter-ripper', mountpoint / 'dexter-ripper/staging', mountpoint / 'dexter-ripper/library'):
        path.mkdir(mode=0o775, exist_ok=True)
        os.chown(path, uid, gid)
    print(f'Ready: {mountpoint}/dexter-ripper/staging and {mountpoint}/dexter-ripper/library', flush=True)
    print(f'fstab backup: {backup}. Refresh the Dexter storage list before choosing folders.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        raise SystemExit('Storage preparation stopped: ' + str(exc))
