"""Album publication checks use isolated folders, never the real share."""
from pathlib import Path
import tempfile
import errno
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper'))
import music_transfer


class MusicTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'music').mkdir()
        self.source = self.root / 'prepared'
        self.source.mkdir()
        (self.source / '01 - Song.flac').write_bytes(b'verified prepared track' * 1000)
        self.addCleanup(patch.stopall)
        patch.object(music_transfer.jellyfin_transfer, 'ROOT', self.root).start()
        patch.object(music_transfer.jellyfin_transfer, 'check_mount').start()

    def test_publish_verifies_and_retains_source(self):
        original = (self.source / '01 - Song.flac').read_bytes()
        target = music_transfer.publish(self.source, 'Artist', 'Album')
        self.assertEqual((target / '01 - Song.flac').read_bytes(), original)
        self.assertEqual((self.source / '01 - Song.flac').read_bytes(), original)
        self.assertFalse(list((self.root / 'music' / 'Artist').glob('.dexter-album-*')))

    def test_duplicate_and_concurrent_empty_album_are_never_replaced(self):
        artist = self.root / 'music' / 'Artist'
        artist.mkdir()
        target = artist / 'Album'
        target.mkdir()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            music_transfer.publish(self.source, 'Artist', 'Album')
        scratch = artist / 'scratch'
        scratch.mkdir()
        (scratch / 'track').write_bytes(b'new')
        with self.assertRaises(FileExistsError):
            music_transfer.rename_new(scratch, target)
        self.assertTrue(scratch.is_dir())
        self.assertFalse(list(target.iterdir()))

    def test_linux_syscall_fallback_publishes_without_libc_wrapper(self):
        import platform
        if platform.machine() != 'x86_64':
            self.skipTest('Fallback is limited to the supported Linux x86-64 image')
        libc = music_transfer.ctypes.CDLL(None, use_errno=True)
        class SyscallOnly:
            syscall = libc.syscall
        with patch.object(music_transfer.ctypes, 'CDLL', return_value=SyscallOnly()):
            target = music_transfer.publish(self.source, 'Artist', 'Album')
        self.assertTrue((target / '01 - Song.flac').is_file())

    def test_nfs_rejected_rename_publishes_only_verified_complete_tracks(self):
        original = (self.source / '01 - Song.flac').read_bytes()
        with patch.object(music_transfer, 'rename_new', side_effect=OSError(errno.EINVAL, 'NFS rename flags unsupported')):
            target = music_transfer.publish(self.source, 'Artist', 'Album')
        self.assertEqual((target / '01 - Song.flac').read_bytes(), original)
        self.assertEqual((self.source / '01 - Song.flac').read_bytes(), original)

    def test_nfs_fallback_does_not_replace_concurrently_created_album(self):
        target = self.root / 'music' / 'Artist' / 'Album'
        def collision(source, destination):
            destination.mkdir()
            (destination / 'existing.flac').write_bytes(b'existing')
            raise OSError(errno.EINVAL, 'NFS rename flags unsupported')
        with patch.object(music_transfer, 'rename_new', side_effect=collision):
            with self.assertRaises(FileExistsError):
                music_transfer.publish(self.source, 'Artist', 'Album')
        self.assertEqual((target / 'existing.flac').read_bytes(), b'existing')
        self.assertFalse((target / '01 - Song.flac').exists())

    def test_nfs_fallback_link_failure_removes_only_its_own_publication(self):
        with patch.object(music_transfer, 'rename_new', side_effect=OSError(errno.EINVAL, 'unsupported')), \
                patch.object(music_transfer.os, 'link', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                music_transfer.publish(self.source, 'Artist', 'Album')
        self.assertFalse((self.root / 'music' / 'Artist' / 'Album').exists())
        self.assertTrue((self.source / '01 - Song.flac').is_file())

    def test_corrupt_copy_is_not_published(self):
        class WrongDigest:
            def hexdigest(self):
                return 'wrong'
        with patch.object(music_transfer.hashlib, 'file_digest', return_value=WrongDigest()):
            with self.assertRaisesRegex(ValueError, 'did not verify'):
                music_transfer.publish(self.source, 'Artist', 'Album')
        self.assertFalse((self.root / 'music' / 'Artist' / 'Album').exists())
        self.assertTrue((self.source / '01 - Song.flac').is_file())
        self.assertFalse(list((self.root / 'music' / 'Artist').glob('.dexter-album-*')))

    def test_missing_mount_and_symlink_destination_are_refused(self):
        with patch.object(music_transfer.jellyfin_transfer, 'check_mount', side_effect=ValueError('Reconnect')):
            with self.assertRaisesRegex(ValueError, 'Reconnect'):
                music_transfer.check('Artist', 'Album')
        (self.root / 'music' / 'Artist').symlink_to(self.source)
        with self.assertRaises(ValueError):
            music_transfer.check('Artist', 'Album')

    def test_names_cannot_escape_music_root(self):
        for artist, album in (('../escape', 'Album'), ('Artist', '..'), ('', 'Album')):
            with self.subTest(artist=artist, album=album), self.assertRaises(ValueError):
                music_transfer.check(artist, album)

    def test_non_flac_or_symlink_source_refused(self):
        (self.source / 'other').write_bytes(b'not a track')
        with self.assertRaises(ValueError):
            music_transfer.publish(self.source, 'Artist', 'Album')
