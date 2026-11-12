from pathlib import Path
import os
import subprocess
import tempfile
import unittest

WRAPPER = Path(__file__).resolve().parents[1] / 'rootfs/usr/local/bin/HandBrakeCLI'


class HandBrakeWrapperTests(unittest.TestCase):
    def test_missing_runtime_reports_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['sh', str(WRAPPER), '--version'], capture_output=True, text=True,
                                    env=dict(os.environ, DEXTER_HANDBRAKE_RUNTIME=directory))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('incomplete', result.stderr)

    def test_private_loader_and_arguments_are_used_without_modifying_source(self):
        with tempfile.TemporaryDirectory(prefix='dexter runtime ') as directory:
            root = Path(directory)
            (root / 'lib').mkdir()
            (root / 'usr/bin').mkdir(parents=True)
            binary = root / 'usr/bin/HandBrakeCLI'
            binary.write_text('not executed by test')
            binary.chmod(0o700)
            loader = root / 'lib/ld-musl-test.so.1'
            loader.write_text('#!/bin/sh\nprintf "<%s>\\n" "$@"\n')
            loader.chmod(0o700)
            result = subprocess.run(['sh', str(WRAPPER), '-i', 'movie with spaces.mkv', '--version'],
                                    capture_output=True, text=True, check=True,
                                    env=dict(os.environ, DEXTER_HANDBRAKE_RUNTIME=directory))
            self.assertEqual(result.stdout.splitlines(), [
                '<--library-path>', f'<{root}/lib:{root}/usr/lib>', f'<{binary}>',
                '<-i>', '<movie with spaces.mkv>', '<--version>'])
            self.assertEqual(binary.read_text(), 'not executed by test')

    def test_glibc_loader_preserves_arguments_and_ambiguous_runtime_is_refused(self):
        with tempfile.TemporaryDirectory(prefix='dexter glibc ') as directory:
            root = Path(directory)
            (root / 'lib64').mkdir()
            (root / 'usr/bin').mkdir(parents=True)
            binary = root / 'usr/bin/HandBrakeCLI'
            binary.write_text('not executed by test')
            binary.chmod(0o700)
            loader = root / 'lib64/ld-linux-x86-64.so.2'
            loader.write_text('#!/bin/sh\nprintf "<%s>\\n" "$@"\n')
            loader.chmod(0o700)
            env = dict(os.environ, DEXTER_HANDBRAKE_RUNTIME=directory)
            result = subprocess.run(['sh', str(WRAPPER), '-i', 'a movie.mkv'],
                                    capture_output=True, text=True, env=env, check=True)
            self.assertEqual(result.stdout.splitlines()[-3:], [f'<{binary}>', '<-i>', '<a movie.mkv>'])
            (root / 'lib').mkdir()
            (root / 'lib/ld-musl-test.so.1').write_text('ambiguous')
            result = subprocess.run(['sh', str(WRAPPER), '--version'],
                                    capture_output=True, text=True, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('more than one loader', result.stderr)
