"""Batch transfer uses isolated folders and never writes a real library."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'rootfs/opt/dexter-ripper'))
import jellyfin_batch_transfer as transfer


class BatchTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'tv').mkdir();(self.root/'movies').mkdir()
        self.source=self.root/'sources';self.source.mkdir()
        self.manifest={'kind':'tv','files':[]}
        for i in (1,2):
            source=self.source/f'{i}.mp4';source.write_bytes(bytes([i])*1000)
            self.manifest['files'].append({'source':str(source),'relative':f'Series/Season 01/Series - S01E{i:02d}.mp4','expected_bytes':1000})
        self.addCleanup(patch.stopall)
        patch.object(transfer.jellyfin_transfer,'ROOT',self.root).start()
        patch.object(transfer.jellyfin_transfer,'check_mount').start()
        patch('sys.stdout',new=io.StringIO()).start()

    def test_publish_retains_all_sources_and_refuses_duplicates(self):
        transfer.publish(self.manifest,check_only=True)
        self.assertEqual(list((self.root/'tv').iterdir()),[])
        transfer.publish(self.manifest)
        for item in self.manifest['files']:
            self.assertEqual((self.root/'tv'/item['relative']).read_bytes(),Path(item['source']).read_bytes())
        with self.assertRaisesRegex(ValueError,'already exists'):transfer.publish(self.manifest)
        self.assertFalse(list((self.root/'tv').glob('.dexter-batch-*')))

    def test_bad_checksum_prevents_entire_publication_and_keeps_sources(self):
        with patch.object(transfer.hashlib,'file_digest') as digest:
            digest.return_value.hexdigest.return_value='wrong'
            with self.assertRaisesRegex(ValueError,'did not verify'):transfer.publish(self.manifest)
        self.assertEqual(list((self.root/'tv').iterdir()),[])
        self.assertEqual(len(list(self.source.iterdir())),2)

    def test_second_publication_failure_rolls_back_only_own_files(self):
        import os
        parent=self.root/'tv/Series/Season 01';parent.mkdir(parents=True)
        keep=parent/'existing.txt';keep.write_text('keep')
        original_link=os.link;calls=[]
        def fail_second(source,target):
            calls.append(str(target))
            if len(calls)==2:raise PermissionError('second link denied')
            return original_link(source,target)
        with patch.object(transfer.os,'link',side_effect=fail_second):
            with self.assertRaises(PermissionError):transfer.publish(self.manifest)
        self.assertEqual(list(parent.iterdir()),[keep])
        self.assertEqual(keep.read_text(),'keep')
        self.assertEqual(len(list(self.source.iterdir())),2)

    def test_missing_mount_wrong_size_and_escape_are_refused(self):
        with patch.object(transfer.jellyfin_transfer,'check_mount',side_effect=ValueError('Reconnect share')):
            with self.assertRaisesRegex(ValueError,'Reconnect'):transfer.publish(self.manifest)
        self.manifest['files'][0]['expected_bytes']=99
        with self.assertRaisesRegex(ValueError,'processing record'):transfer.publish(self.manifest)
        self.manifest['files'][0]['expected_bytes']=1000
        self.manifest['files'][0]['relative']='../escape.mp4'
        with self.assertRaisesRegex(ValueError,'destination'):transfer.publish(self.manifest)

    def test_same_episode_number_is_refused_despite_different_names(self):
        self.manifest['files'][1]['relative'] = 'Series/Season 01/Series - S01E01 - Alternate.mp4'
        with self.assertRaisesRegex(ValueError, 'same episode number'):
            transfer.publish(self.manifest)
        self.manifest['files'][1]['relative'] = 'Series/Season 01/Series - S01E02.mp4'
        parent = self.root / 'tv/Series/Season 01'; parent.mkdir(parents=True)
        existing = parent / 'Series - S01E01 - Older name.mkv'
        existing.write_bytes(b'existing')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            transfer.publish(self.manifest)
        self.assertEqual(existing.read_bytes(), b'existing')

    def test_movie_main_and_extras_use_the_same_verified_path(self):
        self.manifest['kind']='movies'
        self.manifest['files'][0]['relative']='Movie/Movie.mp4'
        self.manifest['files'][1]['relative']='Movie/extras/Extra.mp4'
        transfer.publish(self.manifest)
        self.assertTrue((self.root/'movies/Movie/Movie.mp4').is_file())
        self.assertTrue((self.root/'movies/Movie/extras/Extra.mp4').is_file())

    def test_symlink_library_target_is_refused(self):
        (self.root/'tv/Series').symlink_to(self.source)
        with self.assertRaisesRegex(ValueError,'regular folders'):transfer.publish(self.manifest)
