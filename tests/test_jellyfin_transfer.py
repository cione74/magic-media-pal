import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs/opt/dexter-ripper"))
import jellyfin_transfer


class JellyfinTransferTests(unittest.TestCase):
    def test_publish_retains_source_and_refuses_existing_movie(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movies = root / "movies"
            movies.mkdir()
            source = root / "staged.mkv"
            source.write_bytes(b"verified media bytes")
            with patch.object(jellyfin_transfer, "ROOT", root), patch.object(jellyfin_transfer, "check_mount"), contextlib.redirect_stdout(io.StringIO()):
                jellyfin_transfer.publish(source, "Example Movie", "2004")
                destination = movies / "Example Movie (2004)" / "Example Movie (2004).mkv"
                self.assertEqual(destination.read_bytes(), source.read_bytes())
                with self.assertRaisesRegex(jellyfin_transfer.ImportIssue, "already exists"):
                    jellyfin_transfer.publish(source, "Example Movie", "")
            self.assertEqual([p.name for p in movies.iterdir()], ["Example Movie (2004)"])

    def test_episode_targets_tv_season_and_never_overwrites_or_removes_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'tv').mkdir()
            source=root/'staged.mkv'
            source.write_bytes(b'verified episode')
            episode={'series':'Tour of Duty','season':1,'number':2,'name':'Notes from the Underground'}
            with patch.object(jellyfin_transfer,'ROOT',root), patch.object(jellyfin_transfer,'check_mount'), contextlib.redirect_stdout(io.StringIO()):
                jellyfin_transfer.publish(source,'Episode','',check_only=True,episode=episode)
                self.assertEqual(list((root/'tv').iterdir()),[])
                jellyfin_transfer.publish(source,'Episode','',episode=episode)
                destination=root/'tv'/'Tour of Duty'/'Season 01'/'Tour of Duty - S01E02 - Notes from the Underground.mkv'
                self.assertEqual(destination.read_bytes(),source.read_bytes())
                with self.assertRaisesRegex(jellyfin_transfer.ImportIssue,'already exists'):
                    jellyfin_transfer.publish(source,'Episode','',episode=episode)
            self.assertTrue(source.is_file())
            self.assertFalse((root/'movies').exists())

    def test_episode_preflight_options_parse_with_or_without_size(self):
        episode={'series':'Example','season':1,'number':1,'name':'Pilot'}
        for options,size in ((['--check','--episode',json.dumps(episode)],None),
                             (['--check','1234','--episode',json.dumps(episode)],1234),
                             (['--episode',json.dumps(episode),'--check'],None)):
            with self.subTest(options=options), patch.object(jellyfin_transfer,'publish') as publish:
                self.assertEqual(jellyfin_transfer.main(['source.mkv','Example','']+options),0)
                publish.assert_called_once_with(Path('source.mkv'),'Example','',check_only=True,
                                                expected_bytes=size,episode=episode)

    def test_mp4_keeps_suffix_and_preflight_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movies = root / "movies"
            movies.mkdir()
            source = root / "staged.mp4"
            source.write_bytes(b"verified movie")
            with patch.object(jellyfin_transfer, "ROOT", root), patch.object(jellyfin_transfer, "check_mount"), contextlib.redirect_stdout(io.StringIO()):
                jellyfin_transfer.publish(source, "Example", "", check_only=True)
                self.assertEqual(list(movies.iterdir()), [])
                jellyfin_transfer.publish(root / "planned.mp4", "Example", "", check_only=True, expected_bytes=100)
                self.assertEqual(list(movies.iterdir()), [])
                jellyfin_transfer.publish(source, "Example", "")
            self.assertEqual((movies / "Example" / "Example.mp4").read_bytes(), source.read_bytes())

    def test_checksum_mismatch_prevents_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movies = root / "movies"
            movies.mkdir()
            source = root / "staged.mkv"
            source.write_bytes(b"verified media bytes")
            with patch.object(jellyfin_transfer, "ROOT", root), patch.object(jellyfin_transfer, "check_mount"), patch.object(jellyfin_transfer.hashlib, "file_digest") as digest, contextlib.redirect_stdout(io.StringIO()):
                digest.return_value.hexdigest.return_value = "wrong"
                with self.assertRaisesRegex(jellyfin_transfer.ImportIssue, "could not be verified"):
                    jellyfin_transfer.publish(source, "Example Movie", "")
            self.assertTrue(source.is_file())
            self.assertEqual(list(movies.iterdir()), [])
