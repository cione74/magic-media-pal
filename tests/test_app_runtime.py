"""Start the real app with isolated storage; never scan, rip, or import media."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

APP = Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper/app.py'


class AppRuntimeTests(unittest.TestCase):
    def test_first_run_http_with_isolated_folders(self):
        with tempfile.TemporaryDirectory(prefix='dexter-http-test-') as directory:
            root = Path(directory)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = dict(os.environ, RIPPER_CONFIG=str(root / 'config'),
                       RIPPER_OUTPUT=str(root / 'staging'), RIPPER_MEDIA=str(root / 'library'),
                       RIPPER_BIND='127.0.0.1', RIPPER_PORT=str(port), JELLYFIN_URL='')
            with (root / 'server.log').open('w+b') as log:
                server = subprocess.Popen([sys.executable, str(APP)], env=env, stdout=log,
                                          stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=1) as response:
                                html = response.read().decode()
                                self.assertTrue(response.url.endswith('/settings'))
                            break
                        except (urllib.error.URLError, TimeoutError):
                            if server.poll() is not None or time.monotonic() >= deadline:
                                log.seek(0)
                                self.fail('App startup failed: ' + log.read().decode())
                            time.sleep(0.1)
                    # The actual app, not only an isolated Flask fixture, guards disc actions.
                    try:
                        urllib.request.urlopen(f'http://127.0.0.1:{port}/scan', data=b'', timeout=2)
                        self.fail('A tokenless scan was accepted')
                    except urllib.error.HTTPError as exc:
                        self.assertEqual(exc.code, 403)
                    self.assertIn('name="_csrf"', html)
                    # The actual packaged app enforces its body cap before disc actions.
                    try:
                        oversized = urllib.request.Request(f'http://127.0.0.1:{port}/scan',
                            data=b'field=' + b'x' * (2 * 1024 * 1024),
                            headers={'Content-Type':'application/x-www-form-urlencoded'})
                        urllib.request.urlopen(oversized, timeout=3)
                        self.fail('Oversized form was accepted')
                    except urllib.error.HTTPError as exc:
                        self.assertEqual(exc.code, 413)

                    self.assertIn('What this computer can do', html)
                    self.assertIn('Connect Jellyfin (optional)', html)
                    self.assertTrue((root / 'staging/video').is_dir())
                    self.assertTrue((root / 'config/dexter-ripper-state.json').is_file())
                    self.assertFalse((root / 'library').exists())
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()

    def test_overlapping_folders_refused_before_app_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, RIPPER_CONFIG=str(root / 'same'), RIPPER_OUTPUT=str(root / 'same'),
                       RIPPER_MEDIA=str(root / 'library'))
            result = subprocess.run([sys.executable, str(APP)], env=env, capture_output=True,
                                    text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('must be separate', result.stderr)
            self.assertFalse((root / 'same').exists())
