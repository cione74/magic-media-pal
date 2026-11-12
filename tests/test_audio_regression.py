"""Exercise current October functions without importing startup side effects."""
import ast
from pathlib import Path
import re
import unittest
import uuid

source = Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper/app.py'
tree = ast.parse(source.read_text())
names = {'parse_cyanrip', 'selected_audio_tracks'}
scope = {'re': re, 'uuid': uuid}
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                             and n.name in names], type_ignores=[]), str(source), 'exec'), scope)
RAW = 'Disc tracks: 2\nDiscID: test-id\nTrack 2 info:\n Duration: 00:04:24.240\nTrack 4 info:\n Duration: 00:03:00.000\n'


class AudioRegressionTests(unittest.TestCase):
    def test_physical_numbering_and_subset(self):
        disc = scope['parse_cyanrip'](RAW)
        self.assertEqual(disc['selected_tracks'], [2, 4])
        self.assertEqual(scope['selected_audio_tracks'](disc, ['4']), [4])

    def test_invalid_empty_and_stale_track_numbers(self):
        disc = scope['parse_cyanrip'](RAW)
        for submitted in ([], ['3'], ['bad']):
            with self.assertRaises(ValueError):
                scope['selected_audio_tracks'](disc, submitted)

    def test_truncated_and_duplicate_scan(self):
        self.assertIsNone(scope['parse_cyanrip'](RAW.replace('tracks: 2', 'tracks: 3')))
        self.assertIsNone(scope['parse_cyanrip'](RAW.replace('Track 4', 'Track 2')))
