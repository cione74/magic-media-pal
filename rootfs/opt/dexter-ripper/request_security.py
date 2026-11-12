"""Protect local browser actions without imposing user accounts or passwords."""
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import urlsplit
from flask import request, session


def session_key(config_root):
    path = Path(config_root) / 'browser-session-key'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Atomic no-overwrite creation; concurrent startup must read the same key.
        temporary = path.with_name('.browser-key-' + secrets.token_hex(12))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(secrets.token_hex(32)); handle.flush(); os.fsync(handle.fileno())
            try: os.link(temporary, path)
            except FileExistsError: pass
        finally: temporary.unlink(missing_ok=True)
    if path.is_symlink(): raise ValueError('Browser session key must be a private regular file')
    with path.open() as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size != 64:
            raise ValueError('Browser session key must be a private regular file')
        key = handle.read(65)
    if not re.fullmatch(r'[0-9a-f]{64}', key): raise ValueError('Invalid browser session key')
    return key


def install(app, config_root):
    app.secret_key = session_key(config_root)
    app.config.update(SESSION_COOKIE_NAME='dexter_browser', SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict', MAX_CONTENT_LENGTH=2 * 1024 * 1024)

    @app.before_request
    def protect_actions():
        if request.method in ('GET', 'HEAD', 'OPTIONS'): return None
        origin = request.headers.get('Origin')
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return 'Open Dexter directly, then retry this action.', 403
        if origin:
            try: parsed = urlsplit(origin)
            except ValueError: return 'Invalid request origin.', 403
            if (parsed.scheme != request.scheme or parsed.netloc.lower() != request.host.lower()
                    or parsed.username or parsed.password or parsed.path not in ('', '/')):
                return 'Open Dexter directly, then retry this action.', 403
        supplied = request.form.get('_csrf') or request.headers.get('X-CSRF-Token', '')
        expected = session.get('csrf', '')
        if (not supplied or not isinstance(supplied, str) or not supplied.isascii()
                or not hmac.compare_digest(supplied, expected)):
            return 'This page has expired. Reload Dexter, then retry this action.', 403
        return None

    @app.after_request
    def protect_pages(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        if response.mimetype == 'text/html' and not response.direct_passthrough:
            response.headers['Cache-Control'] = 'no-store'
            if 'csrf' not in session: session['csrf'] = secrets.token_urlsafe(32)
            token = session['csrf']
            def form(match):
                tag = match.group(0)
                if re.search(r'\bmethod\s*=\s*[\"\']?post\b', tag, re.I):
                    return tag + '<input type="hidden" name="_csrf" value="' + token + '">'
                return tag
            response.set_data(re.sub(r'<form\b[^>]*>', form, response.get_data(as_text=True), flags=re.I))
        return response
