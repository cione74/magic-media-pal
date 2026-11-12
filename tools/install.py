"""Debian development installer. Does not touch disks, credentials or GPU drivers."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import fcntl
import platform
import ipaddress
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'rootfs/opt/dexter-ripper'))
import storage_setup


def optical_devices(sysfs=Path('/sys/class/block'), dev=Path('/dev')):
    result = []
    for entry in sorted(sysfs.glob('sr*')):
        candidates = [dev / entry.name]
        candidates += [dev / p.name for p in sorted((entry / 'device/scsi_generic').glob('sg*'))]
        for path in candidates:
            expected = stat.S_ISBLK if path.name.startswith('sr') else stat.S_ISCHR
            if path.exists() and expected(path.stat().st_mode):
                result.append(str(path))
    return result


def bind_address(root, requested=None):
    value = requested
    existing = Path(root) / 'config/install/compose.json'
    if value is None and existing.exists():
        try:
            port = json.loads(existing.read_text())['services']['dexter-ripper']['ports'][0]
            value, host_port, container_port = port.split(':')
            if (host_port, container_port) != ('5801', '8080'):
                raise ValueError('Unexpected port mapping')
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise ValueError('Existing network settings cannot be read. Specify --bind-address explicitly after reviewing them.')
    address = ipaddress.IPv4Address(value or '127.0.0.1')
    if address.is_unspecified or address.is_multicast:
        raise ValueError('Choose a specific IPv4 address belonging to this computer.')
    return str(address)


def storage_paths(root, staging=None, library=None):
    root = Path(root).resolve()
    choices = {'/output': str(root / 'staging'), '/media': str(root / 'library')}
    existing = root / 'config/install/compose.json'
    if existing.exists():
        try:
            volumes = json.loads(existing.read_text())['services']['dexter-ripper']['volumes']
            for volume in volumes:
                if isinstance(volume, dict) and volume.get('type') == 'bind':
                    source, target = volume['source'], volume['target']
                else:
                    source, target, mode = volume.rsplit(':', 2)
                if target in choices:
                    choices[target] = source
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError('Existing storage choices cannot be read. Review the installation before updating.')
    for target, requested in (('/output', staging), ('/media', library)):
        if requested is not None:
            path = Path(requested)
            if not path.is_absolute() or not path.is_dir():
                raise ValueError('Choose an existing absolute storage folder on a mounted disk.')
            old = Path(choices[target])
            if target == '/output' and path != old and old.exists() and any(p.is_file() or p.is_symlink() for p in old.rglob('*')):
                raise ValueError('Temporary storage still contains files. Finish or recover them before choosing another folder.')
            choices[target] = str(path)
    for target, default in (('/output', root / 'staging'), ('/media', root / 'library')):
        path = Path(choices[target])
        if path != default and (not path.is_dir() or not os.access(path, os.W_OK | os.X_OK)):
            raise ValueError('A selected storage folder is unavailable or not writable. Check its disk mount and permissions.')
    paths = [root / 'config', Path(choices['/output']), Path(choices['/media'])]
    for path in paths:
        if path != path.resolve():
            raise ValueError('Storage paths must be absolute and cannot contain symbolic links.')
    for index, path in enumerate(paths):
        for other in paths[index + 1:]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError('Configuration, temporary storage and library folders must be separate and must not contain each other.')
    return choices['/output'], choices['/media']


def compose_document(root, devices, uid, gid, address='127.0.0.1', staging=None, library=None, jellyfin_nfs=None, nvidia=None):
    root = Path(root).resolve()
    staging, library = storage_paths(root, staging, library)
    drive = next((path for path in devices if Path(path).name.startswith('sr')), '/dev/sr0')
    service = {
        'build': {'context': str(root)}, 'image': 'dexter-ripper:development',
        'restart': 'unless-stopped', 'ports': [f'{address}:5801:8080'],
        'environment': {'MAKEMKV_KEY': 'BETA', 'MAKEMKV_GUI': '0', 'AUTO_DISC_RIPPER': '0',
                        'RIPPER_DRIVE': drive, 'USER_ID': str(uid), 'GROUP_ID': str(gid)},
        'volumes': [str(root / name) + ':' + target + ':rw'
                    for name, target in (('config', '/config'), ('staging', '/output'), ('library', '/media'))],
    }
    if devices:
        service['devices'] = [path + ':' + path for path in devices]
    service['volumes'] = [str(root / 'config') + ':/config:rw', staging + ':/output:rw', library + ':/media:rw']
    # Missing removable/data storage must fail startup, rather than Docker
    # silently creating replacement folders on the system disk.
    for index, (source, target, default) in enumerate(((staging, '/output', root / 'staging'),
                                                      (library, '/media', root / 'library')), start=1):
        if Path(source) != default:
            service['volumes'][index] = {'type': 'bind', 'source': source, 'target': target,
                                        'bind': {'create_host_path': False}}
    service['environment'].update(RIPPER_STAGING_HOST=staging, RIPPER_LIBRARY_HOST=library)
    service['environment'].update(HOME='/config', XDG_CONFIG_HOME='/config/xdg/config', XDG_DATA_HOME='/config/xdg/data')
    existing = root / 'config/install/compose.json'
    previous = {}
    if existing.exists():
        previous = json.loads(existing.read_text())['services']['dexter-ripper'].get('environment', {})
    if nvidia is True or (nvidia is None and previous.get('DEXTER_NVENC') == '1'):
        service['environment'].update(DEXTER_NVENC='1', NVIDIA_DRIVER_CAPABILITIES='compute,utility,video')
        if shutil.which('nvidia-smi'):
            try:
                gpu = subprocess.run(
                    ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                    capture_output=True, text=True, timeout=5, check=False)
                if gpu.returncode == 0 and gpu.stdout.strip():
                    service['environment']['DEXTER_GPU_MODEL'] = gpu.stdout.strip().splitlines()[0]
            except (OSError, subprocess.TimeoutExpired):
                pass
        # Docker may pass the device nodes without mounting the driver libraries
        # in the Alpine image. Expose only the host's matching NVIDIA libraries.
        for name in ('libcuda.so.1', 'libnvidia-encode.so.1', 'libnvcuvid.so.1',
                     'libnvidia-ptxjitcompiler.so.1'):
            library_path = Path('/usr/lib/x86_64-linux-gnu') / name
            if library_path.is_file():
                service['volumes'].append({
                    'type': 'bind', 'source': str(library_path.resolve()),
                    'target': '/opt/nvenc-runtime/driver/' + name, 'read_only': True,
                    'bind': {'create_host_path': False},
                })
        service['deploy'] = {'resources': {'reservations': {'devices': [
            {'driver': 'nvidia', 'count': 1, 'capabilities': ['gpu']}
        ]}}}
    if jellyfin_nfs or previous.get('DEXTER_JELLYFIN_EXPORT'):
        mount = Path(jellyfin_nfs or previous['DEXTER_JELLYFIN_HOST']).resolve()
        fields = run(['findmnt', '-rn', '-o', 'SOURCE,FSTYPE', '--mountpoint', str(mount)], capture=True).stdout.split()
        if len(fields) != 2 or fields[1] not in ('nfs', 'nfs4'):
            raise ValueError('Choose the mountpoint of an active Jellyfin NFS share.')
        if not jellyfin_nfs and fields[0] != previous['DEXTER_JELLYFIN_EXPORT']:
            raise ValueError('The configured Jellyfin NFS export changed.')
        movies = mount / 'movies'
        if movies.is_symlink() or not movies.is_dir():
            raise ValueError('The Jellyfin share must contain the verified Movies directory.')
        owner = movies.stat()
        if owner.st_uid in (0, 65534) or owner.st_gid in (0, 65534):
            raise ValueError('The Movies share must have a dedicated writer identity.')
        service['environment'].update(DEXTER_JELLYFIN_HOST=str(mount), DEXTER_JELLYFIN_EXPORT=fields[0],
                                      DEXTER_JELLYFIN_UID=str(owner.st_uid), DEXTER_JELLYFIN_GID=str(owner.st_gid))
        service['volumes'].append({'type': 'bind', 'source': str(mount), 'target': '/jellyfin-import',
                                   'bind': {'create_host_path': False}})
    return {'name': 'dexter-development', 'services': {'dexter-ripper': service}}


def run(command, *, capture=False):
    return subprocess.run(command, check=True, text=True, capture_output=capture)


def discover_storage(root):
    mounts = json.loads(run(['findmnt', '--json', '--real', '--bytes', '--list', '-o',
                            'SOURCE,TARGET,FSTYPE,SIZE,AVAIL,OPTIONS,UUID'], capture=True).stdout)['filesystems']
    # Without NAME in the output, lsblk emits a flat JSON list unless tree
    # output is explicitly requested. Partition mounts belong to their disk.
    disks = json.loads(run(['lsblk', '--tree', '--json', '--bytes', '-o',
                           'PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL'], capture=True).stdout)['blockdevices']
    staging, library = storage_paths(root)
    return storage_setup.inventory(root, staging, library, mounts, disks)


def refresh_storage(root=ROOT):
    root = Path(root).resolve()
    with setup_lock(root):
        storage_setup.atomic_json(root / 'config/install/storage-inventory.json', discover_storage(root))
    print('Storage list refreshed. Open Settings → Choose or change storage. No disks or running services were changed.')


def storage_request(root):
    pending = storage_setup.load(root / 'config/install/storage-request.json')
    if pending is None:
        raise ValueError('No storage change is waiting. Choose folders in Settings first.')
    # Rediscover on the host. Browser-supplied paths are never authoritative.
    paths = storage_setup.selected(discover_storage(root), pending['staging_id'], pending['library_id'])
    if paths != {role: pending[role] for role in ('staging', 'library')}:
        raise ValueError('The storage request no longer matches this computer. Choose storage again.')
    return paths


def docker_command():
    try:
        run(['docker', 'info'], capture=True)
        return ['docker']
    except subprocess.CalledProcessError:
        run(['sudo', 'docker', 'info'], capture=True)
        return ['sudo', 'docker']


def assert_idle(root):
    for name in ('config', 'staging', 'library', 'config/install', 'config/install/compose.json', 'config/install/setup.lock'):
        if (root / name).is_symlink():
            raise ValueError('Setup refuses a linked ' + name + ' directory. Review its destination first.')
    state = root / 'config/dexter-ripper-state.json'
    if state.exists():
        try:
            document = json.loads(state.read_text())
            job = document['state']['job']
            if not isinstance(job, dict):
                raise ValueError('Invalid job record')
        except (ValueError, KeyError, TypeError):
            raise ValueError('Saved job state cannot be checked. Review recovery before installing an update.')
        if job.get('running'):
            raise ValueError('A saved job is active. Finish or recover it before running setup.')


@contextmanager
def setup_lock(root):
    destination = root / 'config/install'
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / 'setup.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another setup is already running for this installation.')
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def assert_stopped(compose, compose_file):
    if compose_file.exists():
        result = run(compose + ['ps', '--status', 'running', '--quiet'], capture=True)
        if result.stdout.strip():
            raise ValueError('The development app is running. Finish its jobs and stop it before updating.')


def install(root=ROOT, *, dry_run=False, address=None, staging=None, library=None, apply_storage=False, jellyfin_nfs=None, nvidia=None):
    os_info = Path('/etc/os-release').read_text()
    if 'ID=debian\n' not in os_info or 'VERSION_ID="13"' not in os_info:
        raise ValueError('This development installer currently supports Debian 13 only.')
    if platform.machine().lower() not in ('x86_64', 'amd64'):
        raise ValueError('This pinned development image currently supports x86-64 computers only.')
    uid = int(os.environ.get('SUDO_UID', os.getuid()))
    gid = int(os.environ.get('SUDO_GID', os.getgid()))
    if uid == 0:
        raise ValueError('Run install.sh as your regular desktop user; it requests administrator access only when needed.')
    root = Path(root)
    if apply_storage:
        if staging is not None or library is not None or jellyfin_nfs is not None:
            raise ValueError('Apply the browser storage choice separately from command-line folder overrides.')
        paths = storage_request(root)
        if dry_run:
            print('Preview only: existing files stay in place; no changes made.')
            print(json.dumps(paths, indent=2))
            return
        assert_idle(root)
        with setup_lock(root):
            assert_idle(root)
            compose_file = root / 'config/install/compose.json'
            assert_stopped(docker_command() + ['compose', '-f', str(compose_file)], compose_file)
            paths = storage_request(root)
            old_staging, _ = storage_paths(root)
            if paths['staging'] != old_staging and Path(old_staging).exists() and any(p.is_file() or p.is_symlink() for p in Path(old_staging).rglob('*')):
                raise ValueError('Temporary storage still contains files. Finish or recover them before changing storage.')
            for path in paths.values():
                if Path(path) != Path(path).resolve():
                    raise ValueError('A selected folder contains a symbolic link. Choose storage again.')
                Path(path).mkdir(parents=True, exist_ok=True)
            devices = optical_devices()
            document = compose_document(root, devices, uid, gid, bind_address(root, address), paths['staging'], paths['library'], nvidia=nvidia)
            previous = json.loads(compose_file.read_text()) if compose_file.exists() else None
            try:
                apply_install(root, document, devices)
            except Exception:
                stopped = False
                try:
                    assert_stopped(docker_command() + ['compose', '-f', str(compose_file)], compose_file)
                    stopped = True
                except (OSError, ValueError, subprocess.CalledProcessError):
                    print('Startup status needs review. Storage wiring was retained to match the possibly running app; the request remains pending.')
                if previous is not None and stopped:
                    storage_setup.atomic_json(compose_file, previous)
                    try:
                        storage_setup.atomic_json(root / 'config/install/storage-inventory.json', discover_storage(root))
                    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
                        pass
                raise
            (root / 'config/install/storage-request.json').unlink()
        return
    devices = optical_devices()
    document = compose_document(root, devices, uid, gid, bind_address(root, address), staging, library, jellyfin_nfs, nvidia)
    if dry_run:
        print('Preview only: no packages, services or files will be changed.')
        print('Install Docker and Compose if missing; build the media-tool image; check its tools; start a local development app.')
        print(json.dumps(document, indent=2))
        return
    root = Path(root)
    assert_idle(root)
    with setup_lock(root):
        assert_idle(root)
        apply_install(root, document, devices)


def apply_install(root, document, devices):
    if not shutil.which('docker'):
        print('Installing the container runtime. Your system may request administrator approval.')
        run(['sudo', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'update'])
        run(['sudo', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y',
             '-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold',
             'docker.io', 'docker-compose'])
    docker = ['docker']
    try:
        run(docker + ['info'], capture=True)
    except subprocess.CalledProcessError:
        docker = ['sudo', 'docker']
        # Do not modify user groups or existing daemon settings.
        run(docker + ['info'], capture=True)
    try:
        run(docker + ['compose', 'version'], capture=True)
    except subprocess.CalledProcessError:
        run(['sudo', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', 'docker-compose'])
        run(docker + ['compose', 'version'], capture=True)
    for name in ('config', 'staging', 'library'):
        (root / name).mkdir(exist_ok=True)
    destination = root / 'config/install'
    destination.mkdir(exist_ok=True)
    compose_file = destination / 'compose.json'
    compose = docker + ['compose', '-f', str(compose_file)]
    assert_stopped(compose, compose_file)
    # Existing config/media files are never replaced. Only installer-owned wiring
    # is regenerated to reflect the current checkout and detected optical devices.
    with tempfile.NamedTemporaryFile(mode='w', dir=destination, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(document, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, compose_file)
    try:
        storage_setup.atomic_json(destination / 'storage-inventory.json', discover_storage(root))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        print('Storage list could not be refreshed. Run install.sh --refresh-storage before choosing another disk.')
    run(compose + ['config', '--quiet'])
    run(compose + ['build'])
    # Inspect inside the built image, not the host where tools may already exist.
    run(compose + ['run', '--rm', '--no-deps', '--entrypoint', 'sh', 'dexter-ripper', '-c',
                   'command -v HandBrakeCLI && command -v cyanrip && command -v ffprobe && '
                   'test -x /opt/makemkv/bin/makemkvcon && python3 -c "import flask"'])
    # A running app is never automatically replaced by this development installer.
    assert_idle(root)
    assert_stopped(compose, compose_file)
    run(compose + ['up', '-d'])
    deadline = time.monotonic() + 90
    while True:
        try:
            run(compose + ['exec', '-T', 'dexter-ripper', 'python3', '-c',
                           'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/", timeout=3).read(1)'], capture=True)
            break
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise ValueError('The app did not become ready within 90 seconds. Inspect its container logs before retrying.')
            time.sleep(2)
    address = document['services']['dexter-ripper']['ports'][0].split(':')[0]
    print(f'Development app started at http://{address}:5801 — complete setup in your browser.')
    print('No optical drive detected; connect one and rerun setup.' if not devices else 'Optical drive access configured; ripping still requires your selection.')
    env = document['services']['dexter-ripper']['environment']
    print('Temporary storage: ' + env['RIPPER_STAGING_HOST'])
    print('Finished media: ' + env['RIPPER_LIBRARY_HOST'])
    print('Change these choices with --staging-folder and --library-folder while the app is stopped. Existing media is not moved.')


def main():
    parser = argparse.ArgumentParser(description='Set up the Dexter development app on Debian 13')
    parser.add_argument('--dry-run', action='store_true', help='Preview setup without changes')
    parser.add_argument('--bind-address', help='Host IPv4 address for browser access; existing choice is retained on updates')
    parser.add_argument('--staging-folder', help='Existing folder on a mounted disk for temporary rips; retained on updates')
    parser.add_argument('--library-folder', help='Existing folder for finished media; retained on updates')
    parser.add_argument('--refresh-storage', action='store_true', help='Refresh the browser disk list without interrupting the app')
    parser.add_argument('--apply-storage', action='store_true', help='Apply the pending browser storage choice while the app is stopped')
    parser.add_argument('--jellyfin-nfs', help='Existing Jellyfin NFS mount for verified movie imports')
    gpu = parser.add_mutually_exclusive_group()
    gpu.add_argument('--nvidia', dest='nvidia', action='store_true', default=None, help='Enable NVIDIA GPU access')
    gpu.add_argument('--no-nvidia', dest='nvidia', action='store_false', help='Disable NVIDIA GPU access')
    args = parser.parse_args()
    try:
        if args.refresh_storage:
            if args.dry_run or args.apply_storage or args.staging_folder or args.library_folder or args.bind_address or args.jellyfin_nfs or args.nvidia is not None:
                raise ValueError('Refresh storage separately from installation options.')
            refresh_storage()
        else:
            install(dry_run=args.dry_run, address=args.bind_address, staging=args.staging_folder, library=args.library_folder, apply_storage=args.apply_storage, jellyfin_nfs=args.jellyfin_nfs, nvidia=args.nvidia)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('Setup stopped: ' + str(exc), file=sys.stderr)
        print('Existing settings and media were retained. Resolve the reported issue and run setup again.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
