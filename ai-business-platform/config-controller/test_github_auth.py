from datetime import datetime, timezone
import io
import json
import time
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from github_auth import GitHubAuth


def test_installation_token_is_repo_scoped_cached_and_refreshed(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / 'app.pem'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    now, requests = [time.time()], []
    def opener(request, timeout):
        requests.append(request)
        claims = jwt.decode(request.headers['Authorization'].removeprefix('Bearer '), key.public_key(), algorithms=['RS256'], options={'verify_iat': False, 'verify_exp': False})
        assert claims['iss'] == '123' and claims['exp'] - claims['iat'] <= 600
        assert json.loads(request.data) == {'repositories': ['agent', 'worker'], 'permissions': {'contents': 'write', 'pull_requests': 'write', 'checks': 'read', 'actions': 'read'}}
        return io.BytesIO(json.dumps({'token': 'installation-' + str(len(requests)), 'expires_at': datetime.fromtimestamp(now[0] + 3600, timezone.utc).isoformat()}).encode())
    auth = GitHubAuth(['owner/worker', 'owner/agent'], env={'GITHUB_APP_ID': '123', 'GITHUB_INSTALLATION_ID': '456', 'GITHUB_APP_PRIVATE_KEY_FILE': str(path)}, clock=lambda: now[0], opener=opener)
    assert auth()['Authorization'] == 'Bearer installation-1'
    assert auth()['Authorization'] == 'Bearer installation-1' and len(requests) == 1
    now[0] += 3500
    assert auth()['Authorization'] == 'Bearer installation-2' and len(requests) == 2
