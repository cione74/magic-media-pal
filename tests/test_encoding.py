import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper'))
import encoding


class EncodingTests(unittest.TestCase):
    def setUp(self):
        self.caps = {'available': True, 'encoders': ['x264', 'x265', 'nvenc_h264']}

    def plan(self, **kwargs):
        return encoding.plan('source.mkv', 'new.mkv', self.caps,
                             media={'hdr': False, 'bit_depth': 8}, **kwargs)

    def test_original_needs_no_runtime(self):
        self.assertIsNone(encoding.plan('source', 'source', {}, profile='original')['command'])

    def test_advertised_nvenc_does_not_enable_hardware(self):
        p = self.plan(profile='balanced', hardware=True)
        self.assertEqual(p['encoder'], 'x264')
        self.assertTrue(p['fallback'])
        self.assertIn('--all-subtitles', p['command'])
        self.assertIn('copy', p['command'])
        self.assertNotIn('--width', p['command'])

    def test_verified_nvenc_requires_explicit_quality(self):
        with self.assertRaises(ValueError):
            self.plan(profile='balanced', hardware=True, verified_hardware=['nvenc_h264'])
        self.assertEqual(self.plan(profile='custom', hardware=True,
                         verified_hardware=['nvenc_h264'], quality=22)['encoder'], 'nvenc_h264')

    def test_nvenc_checks_each_codec_and_rejects_failed_sample(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        samples = [SimpleNamespace(returncode=0, stderr=""),
                   SimpleNamespace(returncode=1, stderr="driver denied")]
        with patch.dict(encoding.os.environ, {"DEXTER_NVENC": "1"}), patch.object(
            encoding, "_nvenc_cache", (0, {})
        ), patch.object(encoding.subprocess, "run", side_effect=samples) as run:
            results = encoding.nvenc_probe()
            self.assertTrue(results["h264"]["available"])
            self.assertFalse(results["h265"]["available"])
            self.assertIn("h264_nvenc", run.call_args_list[0].args[0])
            self.assertIn("hevc_nvenc", run.call_args_list[1].args[0])

    def test_unknown_hdr_and_depth_are_refused(self):
        for media in (None, {}, {'hdr': True, 'bit_depth': 8}, {'hdr': False, 'bit_depth': 10}):
            with self.assertRaises(ValueError):
                encoding.plan('a', 'b', self.caps, profile='balanced', media=media)

    def test_missing_runtime(self):
        def missing(*a, **k):
            raise FileNotFoundError('missing')
        self.assertFalse(encoding.probe(run=missing)['available'])

    def test_probe_uses_exact_encoder_names(self):
        result = encoding.probe(run=lambda *a, **k: SimpleNamespace(
            returncode=0, stdout='x264 x265_10bit nvenc_h264', stderr=''))
        self.assertEqual(result['encoders'], ['nvenc_h264', 'x264'])
        self.assertFalse(result['hardware_verified'])

    def test_progress(self):
        self.assertEqual(encoding.progress('Encoding: task 1 of 1, 42.50 %'), 42.5)
        self.assertIsNone(encoding.progress('Scanning title 42'))

    def test_same_path_refused(self):
        with self.assertRaises(ValueError):
            encoding.plan('a', 'a', self.caps, profile='balanced', media={'hdr': False, 'bit_depth': 8})


if __name__ == '__main__':
    unittest.main()
