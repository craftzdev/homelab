"""Refresh narrowly scoped installation tokens; never persist issued tokens."""
from datetime import datetime
from pathlib import Path
import json
import os
import time
import urllib.request


class GitHubAuth:
    def __init__(self, repositories, *, env=None, clock=time.time, opener=urllib.request.urlopen):
        self.env = os.environ if env is None else env
        self.repositories = sorted({repo.split('/')[1] for repo in repositories})
        self.clock, self.opener = clock, opener
        self.token, self.expires = None, 0

    def __call__(self):
        if self.env.get('GITHUB_APP_ID'):
            if self.clock() >= self.expires - 120:
                import jwt
                now = int(self.clock())
                key = Path(self.env['GITHUB_APP_PRIVATE_KEY_FILE']).read_bytes()
                assertion = jwt.encode({'iat': now - 60, 'exp': now + 540, 'iss': self.env['GITHUB_APP_ID']}, key, algorithm='RS256')
                installation = self.env['GITHUB_INSTALLATION_ID']
                if not installation.isdigit():
                    raise ValueError('invalid installation ID')
                request = urllib.request.Request('https://api.github.com/app/installations/' + installation + '/access_tokens',
                    headers={'Authorization': 'Bearer ' + assertion, 'Accept': 'application/vnd.github+json', 'User-Agent': 'ai-config-controller', 'X-GitHub-Api-Version': '2022-11-28'},
                    data=json.dumps({'repositories': self.repositories, 'permissions': {'contents': 'write', 'pull_requests': 'write', 'checks': 'read', 'actions': 'read'}}).encode())
                with self.opener(request, timeout=30) as response:
                    result = json.load(response)
                self.token = result['token']
                self.expires = datetime.fromisoformat(result['expires_at'].replace('Z', '+00:00')).timestamp()
                if self.expires <= now + 120:
                    raise ValueError('installation token expires too soon')
        elif self.env.get('GITHUB_TOKEN_FILE'):
            self.token = Path(self.env['GITHUB_TOKEN_FILE']).read_text().strip()
        else:
            self.token = self.env['GITHUB_TOKEN']
        if not self.token:
            raise ValueError('GitHub credential unavailable')
        return {'Authorization': 'Bearer ' + self.token, 'X-GitHub-Api-Version': '2022-11-28'}
