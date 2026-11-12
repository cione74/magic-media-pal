import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, MagicMock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper'))
import library_connection


class LibraryConnectionTests(unittest.TestCase):
    def test_library_paths_discovered_without_server_changes(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(json.dumps([
            {'Name': 'Movies', 'CollectionType': 'movies', 'Locations': ['/server/movies']}]).encode())
        with patch.object(library_connection.urllib.request, 'build_opener', return_value=opener):
            result = library_connection.inspect('http://media:8096', 'secret')
        self.assertTrue(result['ok'])
        self.assertEqual(result['libraries'][0]['paths'], ['/server/movies'])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.full_url, 'http://media:8096/Library/VirtualFolders')
        self.assertNotIn('secret', str(result))

    def test_failed_auth_and_invalid_response_do_not_claim_connection(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError('http://media', 401, 'secret', {}, None)
        with patch.object(library_connection.urllib.request, 'build_opener', return_value=opener):
            result = library_connection.inspect('http://media', 'secret')
        self.assertFalse(result['ok'])
        self.assertIn('permissions', result['message'])
        self.assertNotIn('secret', result['message'])
        opener.open.side_effect = None
        opener.open.return_value.__enter__.return_value = io.BytesIO(b'<html>login</html>')
        with patch.object(library_connection.urllib.request, 'build_opener', return_value=opener):
            self.assertFalse(library_connection.inspect('http://media', 'secret')['ok'])

    def test_missing_settings_and_embedded_credentials(self):
        for url, key in [('', ''), ('http://media', ''), ('file:///etc/passwd', 'key'), ('http://user:pass@media', 'key')]:
            self.assertFalse(library_connection.inspect(url, key)['ok'])
