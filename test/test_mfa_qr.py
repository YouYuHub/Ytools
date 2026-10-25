"""QR -> standard URI -> independent TOTP -> HTTP confirmation and login."""
import base64
import hashlib
import hmac
import io
import json
import shutil
import struct
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from security.app import install_security
from security.settings import SecuritySettings

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'disposable fixture password 2026'


def code_from_uri(uri, timestamp):
    query = parse_qs(urlsplit(uri).query)
    assert query.get('algorithm', ['SHA1']) == ['SHA1']
    assert query.get('digits', ['6']) == ['6'] and query.get('period', ['30']) == ['30']
    secret = query['secret'][0]
    digest = hmac.new(base64.b32decode(secret + '=' * (-len(secret) % 8)), struct.pack('>Q', int(timestamp) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff
    return str(number % 1_000_000).zfill(6)


@pytest.mark.parametrize('name', ['admin', 'user+qr.' + 'a' * 56])
def test_scan_payload_confirms_and_signs_in_with_standard_authenticators(tmp_path, monkeypatch, name):
    decoder = pytest.importorskip('zxingcpp', reason='optional independent QR decoder used for release verification')
    node = shutil.which('node')
    if node is None:
        pytest.skip('independent QR decode test needs Node.js to run the browser encoder')
    app = FastAPI(); store = install_security(app, SecuritySettings(tmp_path / 'identity'))
    user = store.create_user(name, PASSWORD, 'admin', bootstrap=True)
    assert user['username'] == name and user['name'] == name[:32]
    client = TestClient(app, base_url=store.settings.origin)
    headers = {'Origin': store.settings.origin}
    login = client.post('/auth/login', json={'username': name, 'password': PASSWORD}, headers=headers).json()
    headers['X-CSRF-Token'] = login['csrf']
    response = client.post('/auth/mfa/start', json={'password': PASSWORD}, headers=headers)
    assert response.status_code == 200
    binding = response.json()
    assert time.time() < binding['expires_at'] <= time.time() + 300
    script = "const q=require(process.argv[1])(0,'M');q.addData(process.argv[2],'Byte');q.make();process.stdout.write(q.createDataURL(6,24));"
    encoded = subprocess.check_output([node, '-e', script, str(ROOT / 'H5/js/vendor/qrcode-generator/qrcode.js'), binding['uri']], text=True)
    image = Image.open(io.BytesIO(base64.b64decode(encoded.split(',', 1)[1]))).convert('RGB')
    decoded = decoder.read_barcode(image)
    assert decoded and decoded.text == binding['uri']
    parts = urlsplit(decoded.text)
    assert parts.scheme == 'otpauth' and parts.netloc == 'totp'
    assert unquote(parts.path) == '/Ytools:' + name
    assert parse_qs(parts.query)['issuer'] == ['Ytools']
    now = time.time(); code = code_from_uri(decoded.text, now)
    confirmed = client.post('/auth/mfa/confirm', json={'code': code}, headers=headers)
    assert confirmed.status_code == 200
    data = confirmed.json(); assert data['mfa_verified'] and len(data['recovery_codes']) == 10
    headers['X-CSRF-Token'] = data['csrf']
    assert client.post('/auth/logout', headers=headers).status_code == 200
    login = client.post('/auth/login', json={'username': name, 'password': PASSWORD}, headers={'Origin': store.settings.origin}).json()
    assert not login['mfa_verified']
    headers['X-CSRF-Token'] = login['csrf']
    assert client.post('/auth/mfa/verify', json={'code': code}, headers=headers).status_code == 403
    from memory import identity_store
    monkeypatch.setattr(identity_store.time, 'time', lambda: now + 60)
    verified = client.post('/auth/mfa/verify', json={'code': code_from_uri(decoded.text, now + 60)}, headers=headers)
    assert verified.status_code == 200 and verified.json()['mfa_verified']


def test_start_binding_expires_with_pending_login(tmp_path, monkeypatch):
    app = FastAPI(); store = install_security(app, SecuritySettings(tmp_path / 'identity'))
    user = store.create_user('admin', PASSWORD, 'admin', bootstrap=True)
    client = TestClient(app, base_url=store.settings.origin)
    response = client.post('/auth/login', json={'username': 'admin', 'password': PASSWORD}, headers={'Origin': store.settings.origin})
    deadline = time.time() + 10
    with store.database(True) as db:
        db.execute('UPDATE login_sessions SET expires=? WHERE user_id=?', (deadline, user['id']))
    binding = client.post('/auth/mfa/start', json={'password': PASSWORD}, headers={'Origin': store.settings.origin, 'X-CSRF-Token': response.json()['csrf']})
    assert binding.status_code == 200 and binding.json()['expires_at'] == deadline
