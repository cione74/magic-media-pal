"""Local access stays password-free; cross-site actions fail closed."""
from pathlib import Path
import re
import sys
import tempfile
import unittest
from flask import Flask
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rootfs/opt/dexter-ripper'))
import request_security

class RequestSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = Flask(__name__)
        request_security.install(self.app, self.root)
        self.calls = []
        @self.app.route('/', methods=['GET','POST'])
        def home():
            if request_security.request.method == 'POST': self.calls.append('changed')
            return '<form method="post" action="/"><button>Change</button></form><form method="get"></form>'
        self.client = self.app.test_client()

    def token(self, client=None):
        response = (client or self.client).get('/')
        self.assertEqual(response.status_code,200)
        self.assertNotIn(b'password',response.data)
        self.assertEqual(response.data.count(b'name="_csrf"'),1)
        return re.search(rb'name="_csrf" value="([^"]+)"',response.data).group(1).decode()

    def test_local_actions_need_token_but_no_signin(self):
        self.assertEqual(self.client.post('/').status_code,403)
        token = self.token()
        self.assertEqual(self.client.post('/',data={'_csrf':token}).status_code,200)
        self.assertEqual(self.calls,['changed'])

    def test_cross_site_and_invalid_tokens_refused(self):
        token = self.token()
        for headers,value in [({'Origin':'https://evil.invalid'},token),
                              ({'Sec-Fetch-Site':'cross-site'},token),
                              ({'Origin':'http://[invalid'},token), ({},'wrong'), ({},'invalid-\u00e9')]:
            self.assertEqual(self.client.post('/',data={'_csrf':value},headers=headers).status_code,403)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.client.post('/',data={'_csrf':token},headers={'Origin':'http://localhost'}).status_code,200)

    def test_token_cannot_be_reused_by_another_browser(self):
        token = self.token()
        self.assertEqual(self.app.test_client().post('/',data={'_csrf':token}).status_code,403)
        self.assertEqual(self.calls,[])

    def test_persistent_private_key_and_page_headers(self):
        key = request_security.session_key(self.root)
        self.assertEqual(key, request_security.session_key(self.root))
        self.assertEqual((self.root/'browser-session-key').stat().st_mode & 0o777,0o600)
        self.assertFalse(list(self.root.glob('.browser-key-*')))
        response = self.client.get('/')
        self.assertEqual(response.headers['X-Frame-Options'],'DENY')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertIn('HttpOnly', response.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', response.headers['Set-Cookie'])

    def test_public_or_symlink_key_refuses_startup(self):
        path = self.root/'browser-session-key';path.chmod(0o644)
        with self.assertRaises(ValueError): request_security.session_key(self.root)
        path.chmod(0o600);other=self.root/'real-key';path.rename(other);path.symlink_to(other)
        with self.assertRaises(ValueError): request_security.session_key(self.root)

    def test_oversized_form_is_rejected_before_any_action(self):
        token = self.token()
        payload = ('_csrf=' + token + '&field=').encode() + b'x' * (2 * 1024 * 1024)
        response = self.client.post('/', data=payload, content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.calls, [])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
