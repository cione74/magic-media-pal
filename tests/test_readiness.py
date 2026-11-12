import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper'))
import readiness


class ReadinessTests(unittest.TestCase):
    def test_storage_roots_must_not_overlap(self):
        for roots in (('/tmp/a', '/tmp/a', '/tmp/b'), ('/tmp/a', '/tmp/a/inside', '/tmp/b')):
            with self.assertRaises(ValueError):
                readiness.validate_roots(*roots)
        readiness.validate_roots('/tmp/config', '/tmp/workspace', '/tmp/library')

    def test_missing_components_have_actionable_explanations(self):
        with patch.object(readiness.shutil, 'which', return_value=None):
            checks = readiness.report(False, '/definitely-missing-dexter-folder')
        self.assertTrue(all(not item['available'] for item in checks))
        self.assertTrue(all(item['detail'] for item in checks))
        self.assertIn('Original-quality', checks[2]['detail'])

    def test_installed_does_not_claim_disc_or_gpu_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(readiness.shutil, 'which', return_value='/usr/bin/tool'):
                checks = readiness.report(True, directory)
        self.assertTrue(all(item['available'] for item in checks))
        self.assertIn('scan', checks[0]['detail'])
        self.assertIn('depend', checks[2]['detail'])
