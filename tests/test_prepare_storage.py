import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'tools/prepare-storage.py'
spec = importlib.util.spec_from_file_location('prepare_storage', SOURCE)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class DiskPreparationTests(unittest.TestCase):
    def disk(self, **fields):
        return {'path': '/dev/dexter-test-disk', 'type': 'disk', 'serial': 'test-serial',
                'size': 256000000000, 'mountpoints': [], 'ro': False, **fields}

    def validate(self, disk, **kwargs):
        return prepare.validate_disk({'blockdevices': [disk]}, '/dev/dexter-test-disk',
                                     kwargs.get('serial', 'test-serial'), kwargs.get('size', 256000000000))

    def test_blank_disk_identity_is_required(self):
        self.assertEqual(self.validate(self.disk())['serial'], 'test-serial')
        for options in ({'serial': 'other-disk'}, {'serial': ''}, {'size': 2000000000000}):
            with self.assertRaises(ValueError):
                self.validate(self.disk(), **options)

    def test_mounted_boot_data_swap_and_storage_mappings_are_refused(self):
        for child in ({'type': 'part', 'path': '/dev/fake-part', 'mountpoints': ['/']},
                      {'type': 'part', 'path': '/dev/fake-part', 'mountpoints': ['/data']},
                      {'type': 'part', 'path': '/dev/fake-part', 'mountpoints': ['[SWAP]']},
                      {'type': 'crypt', 'path': '/dev/fake-mapping', 'mountpoints': []}):
            with self.assertRaises(ValueError):
                self.validate(self.disk(children=[child]))
        with self.assertRaises(ValueError):
            self.validate(self.disk(ro=True))

    def test_partition_cannot_be_selected_as_whole_disk(self):
        with self.assertRaises(ValueError):
            self.validate(self.disk(type='part'))
