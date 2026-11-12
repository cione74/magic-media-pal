import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper'))
import makemkv_runtime as runtime
from test_encoding_app import m


class MakeMKVRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_persistent_environment_survives_a_second_process(self):
        script = "import os,pathlib,json; p=pathlib.Path(os.environ['XDG_DATA_HOME'])/'key-marker'; old=p.exists(); p.write_text('cached'); print(json.dumps([os.environ['HOME'], os.environ['XDG_CONFIG_HOME'], old]))"
        for old in (False, True):
            rc, text = runtime.run([sys.executable, '-c', script], self.root, '/dev/test', timeout=3)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(text), [str(self.root), str(self.root / 'xdg/config'), old])

    def test_lock_refuses_overlapping_process_without_starting_it(self):
        with runtime.drive_lock(self.root, '/dev/test'), patch.object(runtime.subprocess, 'Popen') as spawn:
            with self.assertRaises(runtime.DriveBusy):
                runtime.run(['never-start'], self.root, '/dev/test')
            spawn.assert_not_called()

    def test_lines_are_reported_before_process_finishes(self):
        seen = []
        script = "import time; print('Downloading latest HK to /config/data ...',flush=True); time.sleep(.3); print('finished',flush=True)"
        rc, text = runtime.run([sys.executable, '-c', script], self.root, '/dev/test', timeout=3,
                              on_line=lambda line: seen.append((line, time.monotonic())))
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 2)
        self.assertGreater(seen[1][1] - seen[0][1], .1)
        self.assertIn('finished', text)

    def test_timeout_stops_parent_and_stubborn_helper_and_releases_lock(self):
        marker = self.root / 'helper-pid'
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
        script = f"import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c',{child!r}]); pathlib.Path({str(marker)!r}).write_text(str(p.pid)); print('started',flush=True); time.sleep(60)"
        rc, text = runtime.run([sys.executable, '-c', script], self.root, '/dev/test', timeout=.5)
        self.assertEqual(rc, 124)
        self.assertIn('process group was stopped', text)
        pid = int(marker.read_text())
        # A killed child may remain a zombie until the test container's PID 1
        # reaps it; it must no longer execute or hold a device open.
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists():
            self.assertEqual(stat.read_text().split(') ')[1].split()[0], 'Z')
        with runtime.drive_lock(self.root, '/dev/test'):
            pass

    def test_cancellation_stops_stubborn_process(self):
        rc, _ = runtime.run([sys.executable, '-c', 'import time; time.sleep(60)'],
                            self.root, '/dev/test', cancelled=lambda: True)
        self.assertEqual(rc, 130)

    def test_download_progress_and_scan_timeout_are_not_drive_diagnoses(self):
        with patch.dict(m, {'state': {'job': {'running': True, 'description': 'Disc scan', 'log': []}}}):
            m['update_makemkv_status']('MSG:1,0,0,"Downloading latest HK to /config/data ..."\n')
            self.assertIn('Updating UHD disc keys', m['state']['job']['status'])
            with patch.dict(m, {'wait_for_optical_ready': lambda drive: (True, 4),
                                'configured_makemkv_source': lambda drive: 'disc:0',
                                'run_command': lambda *args, **kwargs: (124, 'Downloading latest HK to /config/data ...'),
                                'parse_cyanrip': lambda text: self.fail('Timeout fell through to CD probing')}):
                result = m['scan_disc']()
            self.assertIn('UHD support update', result['type'])
            self.assertNotIn('Drive not ready', result['type'])

    def test_app_command_wrapper_uses_shared_runtime(self):
        with patch.object(m['makemkv_runtime'], 'run', return_value=(0, 'done')) as run:
            self.assertEqual(m['run_command']([m['MAKEMKV'], '-r', 'info', 'disc:0'], timeout=900), (0, 'done'))
            self.assertEqual(run.call_args.args[1], m['CONFIG_ROOT'])
