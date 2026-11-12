import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from test_encoding_app import m
from test_install import installer
import storage_setup


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.staging = self.root / 'staging'
        self.library = self.root / 'library'
        self.staging.mkdir()
        self.library.mkdir()
        self.mounts = [{'source': '/dev/system', 'target': '/', 'fstype': 'ext4',
                        'size': 120 * 1024**3, 'avail': 90 * 1024**3, 'options': 'rw', 'uuid': 'system-uuid'}]
        self.disks = [{'path': '/dev/system', 'type': 'disk', 'size': 128 * 1024**3,
                       'children': [{'mountpoints': ['/']} ]},
                      {'path': '/dev/unused', 'type': 'disk', 'model': 'Extra SSD', 'size': 256 * 1024**3}]
        self.document = storage_setup.inventory(self.root, self.staging, self.library, self.mounts, self.disks)
        self.folder = self.root / 'config/install'
        storage_setup.atomic_json(self.folder / 'storage-inventory.json', self.document)
        self.ids = {c['role']: c['id'] for c in self.document['choices'] if c['current']}

    def test_unconfigured_disk_has_no_selectable_destination(self):
        self.assertEqual(self.document['unready'][0]['reason'], 'No filesystem detected')
        self.assertTrue(all(c['source'] == '/dev/system' for c in self.document['choices']))
        changed = copy.deepcopy(self.mounts)
        changed[0]['uuid'] = 'replacement-disk'
        fresh = storage_setup.inventory(self.root, self.staging, self.library, changed, self.disks)
        with self.assertRaises(ValueError):
            storage_setup.selected(fresh, self.ids['staging'], self.ids['library'])

    def test_host_discovery_keeps_partition_mounts_with_parent_disk(self):
        disk = {'path': '/dev/system', 'type': 'disk', 'size': 128 * 1024**3, 'mountpoints': []}
        partition = {'path': '/dev/system2', 'type': 'part', 'fstype': 'ext4', 'mountpoints': ['/']}
        def command(args, capture=False):
            if args[0] == 'findmnt':
                return SimpleNamespace(stdout=json.dumps({'filesystems': self.mounts}))
            # Match lsblk's real behavior when NAME is omitted from columns.
            devices = [{**disk, 'children': [partition]}, self.disks[1]] if '--tree' in args else [disk, partition, self.disks[1]]
            return SimpleNamespace(stdout=json.dumps({'blockdevices': devices}))
        with patch.object(installer, 'run', side_effect=command):
            document = installer.discover_storage(self.root)
        self.assertEqual([d['device'] for d in document['unready']], ['/dev/unused'])
        self.assertTrue(all(c['system'] for c in document['choices']))

    def test_form_rejects_cross_site_and_busy_changes_and_shows_capacity(self):
        with patch.dict(m, {'CONFIG_ROOT': self.root / 'config', 'OUTPUT': self.staging,
                            'state': {'job': {'running': False}}}):
            client = m['app'].test_client()
            response = client.get('/storage')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Extra SSD', response.data)
            self.assertIn(b'90.0 GiB', response.data)
            self.assertEqual(client.post('/storage', data=self.ids).status_code, 400)
            m['state']['job']['running'] = True
            response = client.post('/storage', data={**self.ids, 'token': m['STORAGE_FORM_TOKEN'], 'action': 'save'})
            self.assertIn(b'Finish the current job', response.data)
            self.assertFalse((self.folder / 'storage-request.json').exists())

    def test_existing_rip_blocks_change_and_pending_can_be_cancelled(self):
        alternate = self.root / 'other-staging'
        alternate.mkdir()
        document = storage_setup.inventory(self.root, alternate, self.library, self.mounts, self.disks)
        other = next(c for c in document['choices'] if c['path'] == str(alternate))
        other['current'] = False
        document['choices'].extend(c for c in self.document['choices'] if c['role'] == 'staging')
        storage_setup.atomic_json(self.folder / 'storage-inventory.json', document)
        data = {'staging': other['id'], 'library': self.ids['library'], 'token': m['STORAGE_FORM_TOKEN'], 'action': 'save'}
        with patch.dict(m, {'CONFIG_ROOT': self.root / 'config', 'OUTPUT': self.staging,
                            'state': {'job': {'running': False}}}):
            client = m['app'].test_client()
            original = self.staging / 'movie.mkv'
            original.write_bytes(b'original')
            self.assertIn(b'Temporary storage still contains files', client.post('/storage', data=data).data)
            self.assertEqual(original.read_bytes(), b'original')
            original.unlink()
            self.assertIn(b'Change waiting to be applied', client.post('/storage', data=data).data)
            self.assertTrue((self.folder / 'storage-request.json').exists())
            client.post('/storage', data={'action': 'cancel', 'token': m['STORAGE_FORM_TOKEN']})
            self.assertFalse((self.folder / 'storage-request.json').exists())

    def test_host_revalidates_request_instead_of_trusting_browser_paths(self):
        request = {'schema': 1, 'staging_id': self.ids['staging'], 'library_id': self.ids['library'],
                   'staging': '/arbitrary/path', 'library': str(self.library), 'request_id': 'one'}
        storage_setup.atomic_json(self.folder / 'storage-request.json', request)
        with patch.object(installer, 'discover_storage', return_value=self.document):
            with self.assertRaises(ValueError):
                installer.storage_request(self.root)

    def test_apply_failure_restores_wiring_and_keeps_request(self):
        alternate = self.root / 'new-staging'
        document = storage_setup.inventory(self.root, alternate, self.library, self.mounts, self.disks)
        ids = {c['role']: c['id'] for c in document['choices'] if c['current']}
        request = {'schema': 1, 'staging_id': ids['staging'], 'library_id': ids['library'],
                   'staging': str(alternate), 'library': str(self.library), 'request_id': 'one'}
        storage_setup.atomic_json(self.folder / 'storage-request.json', request)
        original = installer.compose_document(self.root, [], 1000, 1000)
        storage_setup.atomic_json(self.folder / 'compose.json', original)
        def failure(root, new_document, devices):
            storage_setup.atomic_json(self.folder / 'compose.json', new_document)
            raise ValueError('Build failed')
        read_text = Path.read_text
        def host_file(path, *args, **kwargs):
            if str(path) == '/etc/os-release':
                return 'ID=debian\nVERSION_ID="13"\n'
            return read_text(path, *args, **kwargs)
        with patch.object(Path, 'read_text', autospec=True, side_effect=host_file), patch.object(installer.platform, 'machine', return_value='x86_64'), patch.dict(installer.os.environ, {'SUDO_UID': '1000', 'SUDO_GID': '1000'}), patch.object(installer, 'discover_storage', return_value=document), patch.object(installer, 'optical_devices', return_value=[]), patch.object(installer, 'docker_command', return_value=['docker']), patch.object(installer, 'run', return_value=SimpleNamespace(stdout='')), patch.object(installer, 'apply_install', side_effect=failure):
            with self.assertRaisesRegex(ValueError, 'Build failed'):
                installer.install(self.root, apply_storage=True)
        self.assertEqual(json.loads((self.folder / 'compose.json').read_text()), original)
        self.assertTrue((self.folder / 'storage-request.json').exists())

    def test_symlinked_request_refused(self):
        (self.folder / 'storage-request.json').symlink_to(self.folder / 'storage-inventory.json')
        with self.assertRaises(ValueError):
            storage_setup.load(self.folder / 'storage-request.json')

    def test_preparation_page_requires_disk_specific_acknowledgement_and_never_executes(self):
        self.document['unready'][0].update(serial='future-disk', by_id='/dev/disk/by-id/test-disk', size_bytes=2000000000000)
        storage_setup.atomic_json(self.folder / 'storage-inventory.json', self.document)
        data = {'device': '/dev/unused', 'token': m['STORAGE_FORM_TOKEN'], 'confirmation': 'wrong'}
        with patch.dict(m, {'CONFIG_ROOT': self.root / 'config'}), patch.object(m['subprocess'], 'run') as run:
            client = m['app'].test_client()
            self.assertIn(b'exact serial number', client.post('/storage/prepare', data=data).data)
            data['confirmation'] = 'future-disk'
            page = client.post('/storage/prepare', data=data)
            self.assertIn(b'2000000000000', page.data)
            self.assertIn(b'--erase', page.data)
            self.assertIn(b'Nothing has been erased', page.data)
            run.assert_not_called()
