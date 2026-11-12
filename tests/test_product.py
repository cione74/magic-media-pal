import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper'))
import product


class ProductDecisionTests(unittest.TestCase):
    def setUp(self):
        self.verified = {'ok': True, 'encoding_source': {'hdr': False, 'bit_depth': 8}}
        self.runtime = {'available': True, 'encoders': ['x264', 'x265']}

    def test_only_supported_formats_are_offered(self):
        result = product.compression_choices(self.verified, {'available': True, 'encoders': ['x265']})
        self.assertTrue(result['available'])
        self.assertEqual(result['default_codec'], 'h265')
        self.assertFalse(result['formats'][0]['available'])
        self.assertTrue(result['formats'][0]['reason'])

    def test_missing_runtime_explains_setup_and_retains_original(self):
        result = product.compression_choices(self.verified, {})
        self.assertFalse(result['available'])
        self.assertIn('setup', result['reason'])
        self.assertIn('original', result['reason'])

    def test_unknown_color_leaves_compression_available(self):
        result = product.compression_choices({'ok': True, 'encoding_source': {'bit_depth': 8}}, self.runtime)
        self.assertTrue(result['available'])
        self.assertEqual(result['readiness'], 'needs_probe')

    def test_hdr_or_deep_video_needs_preservation_path(self):
        for source in ( {'hdr': True, 'bit_depth': 8}, {'hdr': False, 'bit_depth': 10}, {'blocked': True}):
            result = product.compression_choices({'ok': True, 'encoding_source': source}, self.runtime)
            self.assertFalse(result['available'])
            self.assertIn('original', result['reason'])

    def test_advertised_gpu_does_not_enable_cpu_options(self):
        result = product.compression_choices(self.verified, {'available': True, 'encoders': ['nvenc_h264']})
        self.assertFalse(result['available'])

    def test_unverified_file_is_not_offered(self):
        result = product.compression_choices({'ok': False}, self.runtime)
        self.assertFalse(result['available'])
        self.assertIn('file check', result['reason'])
