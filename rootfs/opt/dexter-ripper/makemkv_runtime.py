"""Persistent MakeMKV environment and bounded, drive-exclusive processes."""
import codecs
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


class DriveBusy(RuntimeError):
    pass


def environment(config):
    config = Path(config).resolve()
    result = os.environ.copy()
    result.update(HOME=str(config), XDG_CONFIG_HOME=str(config / 'xdg/config'),
                  XDG_DATA_HOME=str(config / 'xdg/data'))
    for path in (config / 'data', config / 'xdg/config', config / 'xdg/data'):
        path.mkdir(parents=True, exist_ok=True)
    return result


@contextmanager
def drive_lock(config, device):
    # Persist outside the container's /tmp so cooperating diagnostic commands
    # and the app contend for the same lock. Never unlink a live lock inode.
    config = Path(config).resolve()
    config.mkdir(parents=True, exist_ok=True)
    path = config / ('makemkv-' + Path(device).name + '.lock')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DriveBusy('The optical drive is already in use by MakeMKV. Wait for that operation to finish.')
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def stop_group(proc, grace=2):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        proc.poll()
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
    # SIGKILL delivery is asynchronous for helpers. Keep the drive lock until
    # non-zombie group members have exited, rather than releasing it as soon
    # as the primary process has been reaped.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        active = False
        for entry in Path('/proc').glob('[0-9]*/stat'):
            try:
                fields = entry.read_text().rsplit(') ', 1)[1].split()
                if int(fields[2]) == proc.pid and fields[0] != 'Z':
                    active = True
                    break
            except (OSError, ValueError, IndexError):
                continue
        if not active:
            break
        time.sleep(0.05)


def run(args, config, device, *, timeout=900, cwd=None, on_line=None, on_start=None, cancelled=None):
    with drive_lock(config, device):
        env = environment(config)
        # Separate file descriptions avoid both inherited-pipe EOF hangs and
        # reader seeks changing the child's write offset.
        with tempfile.TemporaryDirectory(prefix='dexter-makemkv-') as directory:
            path = Path(directory) / 'output.log'
            with path.open('wb') as writer, path.open('rb') as reader:
                proc = subprocess.Popen(args, stdout=writer, stderr=subprocess.STDOUT,
                                        env=env, cwd=cwd, start_new_session=True)
                decoder = codecs.getincrementaldecoder('utf-8')('replace')
                chunks = []
                pending = ''
                def drain(final=False):
                    nonlocal pending
                    text = decoder.decode(reader.read(), final=final)
                    chunks.append(text)
                    pending += text
                    while '\n' in pending:
                        line, pending = pending.split('\n', 1)
                        if on_line:
                            on_line(line + '\n')
                    if final and pending and on_line:
                        on_line(pending)
                started = time.monotonic()
                timed_out = False
                was_cancelled = False
                try:
                    if on_start:
                        on_start(proc)
                    while True:
                        drain()
                        if proc.poll() is not None:
                            break
                        if cancelled and cancelled():
                            was_cancelled = True
                            break
                        if timeout is not None and time.monotonic() - started >= timeout:
                            timed_out = True
                            break
                        time.sleep(0.1)
                finally:
                    # Also clean up any helpers that outlive a normal parent.
                    stop_group(proc)
                drain(final=True)
                text = ''.join(chunks)
                if was_cancelled:
                    return 130, text + '\nMakeMKV cancelled; its process group was stopped.'
                return (124, text + '\nMakeMKV timed out; its process group was stopped.') if timed_out else (proc.returncode, text)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Read-only MakeMKV disc information with Dexter locking and persistence')
    parser.add_argument('--device', default=os.environ.get('RIPPER_DRIVE', '/dev/sr0'))
    parser.add_argument('--source', default='disc:0')
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    if not args.source.startswith('disc:') or not args.source[5:].isdigit() or not 1 <= args.timeout <= 3600:
        parser.error('Choose disc:N and a timeout between 1 and 3600 seconds.')
    try:
        code, _ = run([os.environ.get('MAKEMKV_CLI', '/opt/makemkv/bin/makemkvcon'), '-r', '--cache=128', 'info', args.source],
                      os.environ.get('RIPPER_CONFIG', '/config'), args.device, timeout=args.timeout,
                      on_line=lambda line: print(line, end='', flush=True))
    except DriveBusy as exc:
        print(str(exc))
        code = 75
    raise SystemExit(code)
