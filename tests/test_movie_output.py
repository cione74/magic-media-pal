"""Track-selection and playback-copy regression tests."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs/opt/dexter-ripper"))
import encoding
import movie_output


def policy(**changes):
    return {"container": "mp4", "language": "eng", "surround": True, "subtitles": False,
            "subtitle_language": "eng", "audio_track": "", "subtitle_track": "", **changes}


class TrackSelectionTests(unittest.TestCase):
    def setUp(self):
        self.audio = {"index": 4, "codec_type": "audio", "codec_name": "ac3",
                      "channels": 6, "channel_layout": "5.1(side)", "tags": {"language": "eng"}}
        self.document = {"streams": [self.audio]}

    def test_selects_by_metadata_not_grease_stream_number(self):
        self.assertEqual(movie_output.resolve(self.document, policy()), [self.audio])
        self.document["streams"].insert(0, {**self.audio, "index": 1, "codec_name": "truehd"})
        self.assertEqual(movie_output.resolve(self.document, policy()), [self.audio])

    def test_missing_language_and_stereo_still_require_user_choice(self):
        for streams, settings in (
            ([self.audio], policy(language="deu")),
            ([{**self.audio, "channels": 2}], policy()),
        ):
            with self.subTest(streams=streams), self.assertRaises(ValueError):
                movie_output.resolve({"streams": streams}, settings)

    def test_automatic_audio_chooses_default_then_stable_disc_order(self):
        other = {**self.audio, "index": 9}
        document = {"streams": [self.audio, other]}
        self.assertEqual(movie_output.resolve(document, policy()), [self.audio])
        other["disposition"] = {"default": 1}
        self.assertEqual(movie_output.resolve(document, policy()), [other])
        other["tags"] = {"language": "eng", "title": "Director commentary"}
        self.assertEqual(movie_output.resolve(document, policy()), [self.audio])
        other["tags"] = {"language": "deu"}
        self.assertEqual(movie_output.resolve(document, policy()), [self.audio])

    def test_explicit_track_and_subtitle_choices_are_respected(self):
        alternative = {**self.audio, "index": 9}
        subtitle = {"index": 12, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "eng"}, "disposition": {"forced": 1}}
        document = {"streams": [self.audio, alternative, subtitle]}
        selected = movie_output.resolve(document, policy(
            container="mkv", audio_track=movie_output.track_id(alternative),
            subtitles=True, subtitle_track=movie_output.track_id(subtitle)))
        self.assertEqual(selected, [alternative, subtitle])
        with self.assertRaisesRegex(ValueError, "MKV"):
            movie_output.resolve(document, policy(subtitles=True, audio_track=movie_output.track_id(alternative), subtitle_track=movie_output.track_id(subtitle)))
        self.assertEqual(movie_output.resolve(document, policy(audio_track=movie_output.track_id(alternative))), [alternative])

    def test_scan_selection_is_rebound_by_inventory_and_survives_new_indexes(self):
        raw = '\n'.join([
            'SINFO:0,2,1,0,"Audio"', 'SINFO:0,2,3,0,"eng"',
            'SINFO:0,2,5,0,"A_AC3"', 'SINFO:0,2,14,0,"6"',
        ])
        choice = movie_output.scan_tracks(raw)[0][0]
        selected_policy = policy(scan_audio=choice["selector"])
        before = {"streams": [self.audio]}
        self.assertEqual(movie_output.resolve(before, selected_policy), [self.audio])
        after = {"streams": [{**self.audio, "index": 14}]}
        rebound = movie_output.rebase_policy(before, after, selected_policy)
        self.assertEqual(movie_output.resolve(after, rebound), after["streams"])
        with self.assertRaises(ValueError):
            movie_output.resolve({"streams": [self.audio, {**self.audio, "index": 9}]}, selected_policy)



@unittest.skipUnless(all(shutil.which(name) for name in ("ffmpeg", "ffprobe")), "Requires FFmpeg")
class PlaybackOutputTests(unittest.TestCase):
    def test_mp4_keeps_selected_surround_chapters_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.mkv", root / "output.mp4"
            subtitles = root / "captions.srt"
            subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n")
            chapters = root / "chapters.txt"
            chapters.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Opening\n")
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=24:duration=1",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1", "-i", str(subtitles),
                "-i", str(chapters), "-map", "0:v", "-map", "1:a", "-map", "1:a",
                "-map", "2:s", "-map_chapters", "3", "-t", "1",
                "-c:v", "libx264", "-c:a", "ac3", "-b:a", "640k", "-c:s", "srt",
                "-metadata:s:a:0", "language=deu", "-metadata:s:a:1", "language=eng", str(source),
            ], check=True, capture_output=True, timeout=30)
            original = source.read_bytes()
            reports = []
            movie_output.prepare(source, target, policy(), update=lambda **fields: reports.append(fields))
            self.assertEqual(source.read_bytes(), original)
            document = encoding.inspect(target)
            media = [s for s in document["streams"] if s["codec_type"] != "data"]
            self.assertEqual([s["codec_type"] for s in media], ["video", "audio"])
            self.assertEqual(media[1]["channels"], 6)
            self.assertEqual(media[1]["tags"]["language"], "eng")
            self.assertEqual(len(document["chapters"]), 1)
            self.assertTrue(any("report" in item for item in reports))
            with self.assertRaises(ValueError):
                movie_output.prepare(source, target, policy())
            with self.assertRaises(InterruptedError):
                movie_output.prepare(source, root / "cancelled.mp4", policy(), cancelled=lambda: True)
            self.assertFalse((root / "cancelled.mp4").exists())
