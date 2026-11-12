"""Generated-media CPU smoke test; no optical device or library writes."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper'))
import encoding


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.before = {'streams': [
            {'codec_type': 'video', 'codec_name': 'mpeg2video', 'width': 64, 'height': 64,
             'pix_fmt': 'yuv420p', 'color_transfer': 'bt709'},
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 2},
            {'codec_type': 'subtitle', 'codec_name': 'subrip', 'tags': {'language': 'eng'}}],
            'format': {'duration': '10'},
            'chapters': [{'start_time': '0', 'end_time': '10', 'tags': {'title': 'Chapter 1'}}]}
        self.after = copy.deepcopy(self.before)
        self.after['streams'][0]['codec_name'] = 'h264'

    def test_stream_loss_refused(self):
        self.after['streams'].pop()
        with self.assertRaises(ValueError):
            encoding.verify_preservation(self.before, self.after, 'h264')

    def test_chapters_color_and_duration_changes_refused(self):
        for change in ('chapters', 'duration', 'color'):
            after = copy.deepcopy(self.after)
            if change == 'chapters':
                after['chapters'] = []
            elif change == 'duration':
                after['format']['duration'] = '2'
            else:
                after['streams'][0]['color_transfer'] = 'smpte2084'
            with self.assertRaises(ValueError):
                encoding.verify_preservation(self.before, after, 'h264')

    def test_matching_late_subtitles_can_extend_container_duration(self):
        before, after = copy.deepcopy(self.before), copy.deepcopy(self.after)
        for document in (before, after):
            for index, stream in enumerate(document['streams']):
                stream['index'] = index
        before['format']['duration'] = '10.0'
        after['format']['duration'] = '14.0'
        original = {0: (0.0, 10.0), 1: (0.0, 10.0), 2: (1.0, 14.0)}
        encoding.verify_preservation(before, after, 'h264', timing=(original, original))
        for track, bounds, kind in (
            (0, (0.0, 8.0), 'Video'),
            (1, (0.5, 10.5), 'Audio'),
            (2, (1.0, 10.0), 'Subtitle'),
        ):
            changed = {**original, track: bounds}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, kind + ' track 1 timing'):
                encoding.verify_preservation(before, after, 'h264', timing=(original, changed))

    def test_packet_timing_reports_activity_and_drains_final_output(self):
        updates = []
        with tempfile.TemporaryDirectory() as folder:
            probe = Path(folder) / "probe"
            probe.write_text("#!/usr/bin/env python3\nimport sys\n"
                             "for n in range(12000):\n"
                             " print(f'stream_index=0|pts_time={n / 1000}|duration_time=0.001')\n"
                             "sys.stdout.write('stream_index=1|pts_time=12|duration_time=1')\n")
            probe.chmod(0o755)
            bounds = encoding.packet_bounds(Path(folder) / "movie.mkv", str(probe),
                                            duration=13, update=lambda **fields: updates.append(fields),
                                            label="Checking original timing")
        self.assertEqual(bounds[1], (12, 13))
        self.assertAlmostEqual(bounds[0][1], 12)
        self.assertTrue(any(u.get("verification_label") == "Checking original timing" for u in updates))
        self.assertTrue(any(u.get("percent") == 100 and u.get("heartbeat_at") for u in updates))
        self.assertEqual(updates[-1], {"pid": None})

    def test_retained_properties(self):
        encoding.verify_preservation(self.before, self.after, 'h264')
        properties = encoding.source_properties(self.before)
        self.assertEqual(properties['readiness'], 'ready')
        self.assertEqual(properties['bit_depth'], 8)
        self.assertEqual(properties['stream_layout'], ['video', 'audio', 'subtitle'])

    def test_untagged_vc1_color_is_unknown_and_allows_encoding(self):
        self.before["streams"][0].update(codec_name="vc1", color_transfer="unknown")
        properties = encoding.source_properties(self.before)
        self.assertIsNone(properties["hdr"])
        self.assertEqual(properties["readiness"], "needs_probe")
        capabilities = {"available": True, "encoders": ["x264"]}
        self.assertTrue(encoding.plan("source.mkv", "copy.mkv", capabilities, profile="balanced", media=properties)["command"])

    def test_attachments_refused(self):
        self.before['streams'].append({'codec_type': 'attachment'})
        with self.assertRaises(ValueError):
            encoding.source_properties(self.before)

    def test_chroma_and_interlaced_sources_require_an_explicit_conversion(self):
        self.before['streams'][0].update(pix_fmt='yuv422p', field_order='tt')
        properties = encoding.source_properties(self.before)
        self.assertEqual(properties['bit_depth'], 8)
        self.assertTrue(properties['interlaced'])
        self.assertEqual(properties['readiness'], 'needs_fallback')
        with self.assertRaises(ValueError):
            encoding.verify_preservation(self.before, self.after, 'h264')
        encoding.verify_preservation(self.before, self.after, 'h264', converted=True)

    def test_pixel_format_and_range_changes_refuse_publication(self):
        for field, value in (('pix_fmt', 'yuv422p'), ('color_range', 'pc'), ('field_order', 'tt')):
            changed = copy.deepcopy(self.after)
            changed['streams'][0][field] = value
            with self.assertRaises(ValueError):
                encoding.verify_preservation(self.before, changed, 'h264')

    def test_untagged_video_accepts_explicit_limited_range(self):
        original = self.before["streams"][0]
        result = self.after["streams"][0]
        original.update(codec_name="vc1", color_range="unknown", color_primaries="bt709", color_space="bt709")
        result.update(color_range="tv", color_primaries="bt709", color_space="bt709")
        encoding.verify_preservation(self.before, self.after, "h264")
        original["color_range"] = "pc"
        with self.assertRaisesRegex(ValueError, "color_range"):
            encoding.verify_preservation(self.before, self.after, "h264")
        original.update(codec_name="mpeg2video", color_range="unknown")
        encoding.verify_preservation(self.before, self.after, "h264")

    @unittest.skipUnless(os.name == 'posix', 'POSIX process-group cancellation')
    def test_live_process_cancellation_retains_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source.mkv', root / 'target.mkv'
            source.write_bytes(b'original')
            script = root / 'fake-handbrake'
            script.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(60)\n')
            script.chmod(0o700)
            started = []
            def update(**fields):
                if fields.get('pid'):
                    started.append(fields['pid'])
            with patch.object(encoding, 'inspect', return_value=self.before):
                with self.assertRaises(InterruptedError):
                    encoding.encode_file(source, target, {'available': True, 'encoders': ['x264']},
                                         profile='balanced', executable=str(script), update=update,
                                         cancelled=lambda: bool(started))
            self.assertTrue(started)
            with self.assertRaises(ProcessLookupError):
                os.kill(started[0], 0)
            self.assertEqual(source.read_bytes(), b'original')
            self.assertFalse(target.exists())
            self.assertFalse(list(root.glob('.dexter-encode-*')))

    def test_low_space_refused_before_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mkv'
            source.write_bytes(b'original')
            with patch.object(encoding, 'inspect', return_value=self.before), patch.object(encoding.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), patch.object(encoding.subprocess, 'Popen') as spawn:
                with self.assertRaises(ValueError):
                    encoding.encode_file(source, source.parent / 'new.mkv', {}, profile='balanced')
                spawn.assert_not_called()


@unittest.skipUnless(all(shutil.which(name) for name in ('ffmpeg', 'ffprobe', 'HandBrakeCLI')),
                     'Requires HandBrakeCLI and FFmpeg')
class GeneratedMediaTests(unittest.TestCase):
    def test_cpu_encode_retains_original_audio_subtitles_chapters(self):
        with tempfile.TemporaryDirectory(prefix='dexter-test-') as directory:
            root = Path(directory)
            source, target = root / 'original.mkv', root / 'encoded.mkv'
            subtitles = root / 'captions.srt'
            subtitles.write_text('1\n00:00:00,000 --> 00:00:01,000\nTest subtitle\n')
            metadata = root / 'chapters.txt'
            metadata.write_text(';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Sample\n')
            command = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=24:duration=1',
                       '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                       '-i', str(subtitles), '-i', str(metadata), '-map', '0:v', '-map', '1:a', '-map', '2:s',
                       '-map_metadata', '3', '-map_chapters', '3', '-c:v', 'libx264', '-color_trc', 'bt709',
                       '-colorspace', 'bt709', '-color_primaries', 'bt709',
                       '-x264-params', 'colorprim=bt709:transfer=bt709:colormatrix=bt709', '-c:a', 'ac3', '-c:s', 'srt',
                       '-metadata:s:s:0', 'language=eng', str(source)]
            subprocess.run(command, check=True, capture_output=True, timeout=30)
            original_bytes = source.read_bytes()
            updates = []
            result = encoding.encode_file(source, target, encoding.probe(), profile='balanced',
                                          update=lambda **fields: updates.append(fields))
            self.assertEqual(result, target)
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertTrue(target.is_file())
            hevc_target = root / 'hevc.mkv'
            encoding.encode_file(source, hevc_target, encoding.probe(), profile='compact', codec='h265')
            self.assertTrue(hevc_target.is_file())
            self.assertTrue(any(fields.get('stage') == 'verifying' for fields in updates))
            self.assertFalse(list(root.glob('.dexter-encode-*')))
            with self.assertRaises(ValueError):
                encoding.encode_file(source, target, encoding.probe(), profile='balanced')
            with self.assertRaises(InterruptedError):
                encoding.encode_file(source, root / 'cancelled.mkv', encoding.probe(),
                                     profile='balanced', cancelled=lambda: True)
            self.assertFalse((root / 'cancelled.mkv').exists())


@unittest.skipUnless(all(shutil.which(name) for name in ('ffmpeg', 'ffprobe')),
                     'Requires FFmpeg and ffprobe')
class ProgressiveAnalysisTests(unittest.TestCase):
    def test_auto_prepares_untagged_interlaced_video_after_nvenc_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'original.mkv', root / 'encoded.mkv'
            subprocess.run([
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc2=size=96x64:rate=48:duration=1', '-vf',
                'tinterlace=interleave_top,format=yuv422p', '-flags', '+ilme+ildct',
                '-top', '1', '-c:v', 'mpeg2video', '-threads', '2', str(source),
            ], check=True, capture_output=True, timeout=30)
            original = source.read_bytes()
            before = encoding.inspect(source)
            self.assertTrue(encoding.source_properties(before)['interlaced'])
            self.assertIsNone(encoding.source_properties(before)['hdr'])

            unavailable = root / 'fake-nvenc'
            unavailable.write_text('#!/bin/sh\necho "Cannot load libcuda.so.1" >&2\nexit 1\n')
            unavailable.chmod(0o700)
            capabilities = {'nvenc': {'h264': {'available': True}}}
            updates = []
            with self.assertRaises(encoding.EncodePathsExhausted):
                encoding.encode_file(source, target, capabilities, profile='balanced',
                                     engine='gpu', nvenc=str(unavailable))
            self.assertFalse(target.exists())
            encoding.encode_file(source, target, capabilities, profile='balanced',
                                 engine='auto', nvenc=str(unavailable),
                                 update=lambda **fields: updates.append(fields))
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(any('Cannot load libcuda' in item.get('log', '') for item in updates))
            encoding.verify_preservation(before, encoding.inspect(target), 'h264', converted=True)
