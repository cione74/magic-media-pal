import importlib.util
import json
from pathlib import Path
import tempfile
import contextlib
import io
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / 'tools/install.py'
spec = importlib.util.spec_from_file_location('dexter_install', SOURCE)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_storage_choices_survive_updates_and_overlap_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / 'large-disk/temporary'
            library = root / 'shared-disk/library'
            staging.mkdir(parents=True)
            library.mkdir(parents=True)
            document = installer.compose_document(root, [], 1000, 1000, staging=str(staging), library=str(library))
            self.assertFalse(document['services']['dexter-ripper']['volumes'][1]['bind']['create_host_path'])
            (root / 'config/install').mkdir(parents=True)
            (root / 'config/install/compose.json').write_text(json.dumps(document))
            self.assertEqual(installer.storage_paths(root), (str(staging), str(library)))
            with self.assertRaises(ValueError):
                installer.storage_paths(root, staging=str(library))
            (staging / 'movie.mkv').write_bytes(b'original')
            replacement = root / 'other'
            replacement.mkdir()
            with self.assertRaises(ValueError):
                installer.storage_paths(root, staging=str(replacement))
            self.assertEqual((staging / 'movie.mkv').read_bytes(), b'original')

    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_compose_has_local_defaults_and_no_production_paths(self):
        document = installer.compose_document('/tmp/dexter-test', ['/dev/sr0', '/dev/sg2'], 1000, 1000)
        service = document['services']['dexter-ripper']
        self.assertEqual(service['ports'], ['127.0.0.1:5801:8080'])
        self.assertEqual(service['environment']['AUTO_DISC_RIPPER'], '0')
        self.assertEqual(service['devices'], ['/dev/sr0:/dev/sr0', '/dev/sg2:/dev/sg2'])
        self.assertNotIn('privileged', service)
        self.assertTrue(all(volume.startswith('/tmp/dexter-test/') for volume in service['volumes']))

    def test_no_drive_does_not_prevent_setup(self):
        service = installer.compose_document('/tmp/test', [], 1000, 1000)['services']['dexter-ripper']
        self.assertNotIn('devices', service)

    def test_lan_binding_and_subsequent_updates_retain_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            address = installer.bind_address(root, '192.0.2.10')
            document = installer.compose_document(root, [], 1000, 1000, address)
            self.assertEqual(document['services']['dexter-ripper']['ports'], ['192.0.2.10:5801:8080'])
            (root / 'config/install').mkdir(parents=True)
            (root / 'config/install/compose.json').write_text(json.dumps(document))
            self.assertEqual(installer.bind_address(root), '192.0.2.10')

    def test_invalid_bind_address_refused(self):
        for address in ('0.0.0.0', 'not-an-address', '224.0.0.1', '::1'):
            with self.assertRaises(ValueError):
                installer.bind_address('/unused', address)

    def test_dry_run_does_not_write_or_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(installer.Path, 'read_text', return_value='ID=debian\nVERSION_ID="13"\n'), patch.dict(installer.os.environ, {'SUDO_UID': '1000', 'SUDO_GID': '1000'}), patch.object(installer, 'optical_devices', return_value=[]), patch.object(installer, 'run') as run:
                installer.install(root, dry_run=True)
                run.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_missing_docker_is_installed_and_existing_settings_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            settings = root / 'config/existing.json'
            settings.write_text('keep me')
            with patch.object(installer.Path, 'read_text', return_value='ID=debian\nVERSION_ID="13"\n'), patch.dict(installer.os.environ, {'SUDO_UID': '1000', 'SUDO_GID': '1000'}), patch.object(installer, 'optical_devices', return_value=[]), patch.object(installer.shutil, 'which', return_value=None), patch.object(installer, 'run', return_value=SimpleNamespace(stdout='')) as run:
                installer.install(root)
            self.assertEqual(settings.read_text(), 'keep me')
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any('docker.io' in command and 'docker-compose' in command for command in commands))
            self.assertTrue(any('build' in command for command in commands))
            self.assertTrue(any('up' in command and '-d' in command for command in commands))
            self.assertTrue(any('command -v HandBrakeCLI' in str(command) for command in commands))

    def test_existing_runtime_skips_package_install(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(installer.Path, 'read_text', return_value='ID=debian\nVERSION_ID="13"\n'), patch.dict(installer.os.environ, {'SUDO_UID': '1000', 'SUDO_GID': '1000'}), patch.object(installer, 'optical_devices', return_value=[]), patch.object(installer.shutil, 'which', return_value='/usr/bin/docker'), patch.object(installer, 'run', return_value=SimpleNamespace(stdout='')) as run:
                installer.install(Path(directory))
            self.assertFalse(any('apt-get' in call.args[0] for call in run.call_args_list))

    def test_unsupported_os_is_refused_before_execution(self):
        with patch.object(installer.Path, 'read_text', return_value='ID=other\n'), patch.object(installer, 'run') as run:
            with self.assertRaises(ValueError):
                installer.install()
            run.assert_not_called()

    def test_unsupported_architecture_is_refused_before_execution(self):
        with patch.object(installer.Path, 'read_text', return_value='ID=debian\nVERSION_ID="13"\n'), patch.object(installer.platform, 'machine', return_value='aarch64'), patch.object(installer, 'run') as run:
            with self.assertRaises(ValueError):
                installer.install()
            run.assert_not_called()

    def test_active_and_corrupt_job_state_refuse_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            state = root / 'config/dexter-ripper-state.json'
            for payload in ('{"state":{"job":{"running":true}}}', 'not json'):
                state.write_text(payload)
                with self.assertRaises(ValueError):
                    installer.assert_idle(root)

    def test_running_instance_refuses_replacement(self):
        with tempfile.NamedTemporaryFile() as compose:
            with patch.object(installer, 'run', return_value=SimpleNamespace(stdout='container-id\n')):
                with self.assertRaises(ValueError):
                    installer.assert_stopped(['docker', 'compose'], Path(compose.name))

    def test_two_installers_cannot_share_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with installer.setup_lock(root):
                with self.assertRaises(ValueError):
                    with installer.setup_lock(root):
                        self.fail('Second installer acquired the lock')
