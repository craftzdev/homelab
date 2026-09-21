"""Fixed-destination Responses relay. Only this separate Pod mounts credentials."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import time
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def upstream_request(raw, auth):
    body = json.loads(raw)
    if not isinstance(body, dict) or not isinstance(body.get('model'), str) or body.get('stream') is not True:
        raise ValueError('invalid Responses request')
    # A connectivity probe never needs remote tools or stored conversations.
    body.update(store=False, tools=[], tool_choice='none')
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream', 'User-Agent': 'ai-config-trial-broker'}
    if auth.get('OPENAI_API_KEY'):
        url = 'https://api.openai.com/v1/responses'
        token = auth['OPENAI_API_KEY']
    else:
        url = 'https://chatgpt.com/backend-api/codex/responses'
        token = auth['tokens']['access_token']
        account = auth['tokens'].get('account_id')
        if not account:
            raise ValueError('Codex account ID is required')
        headers['ChatGPT-Account-Id'] = account
        headers['OpenAI-Beta'] = 'responses=experimental'
        headers['originator'] = 'codex_cli_rs'
    headers['Authorization'] = 'Bearer ' + token
    return urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='POST')


class Broker(BaseHTTPRequestHandler):
    # One bounded upstream request at a time. No request/response/auth logging.
    protocol_version = 'HTTP/1.0'
    auth_path = Path('/auth/auth.json')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    calls = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == '/ready' else 404)
        self.end_headers()

    def do_POST(self):
        self.connection.settimeout(100)
        if self.path != '/v1/responses':
            self.send_error(404)
            return
        started = time.monotonic()
        Broker.calls[:] = [t for t in Broker.calls if t > started - 1800]
        if len(Broker.calls) >= 128:
            self.send_error(429)
            return
        sent = False
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if self.headers.get('Transfer-Encoding') or not 0 < size <= 1_000_000:
                raise ValueError('invalid body length')
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('incomplete body')
            request = upstream_request(raw, json.loads(self.auth_path.read_text()))
            Broker.calls.append(started)
            with self.opener.open(request, timeout=90) as response:
                # The Codex account endpoint can omit Content-Type. Validate
                # actual SSE framing before returning any provider bytes. A
                # JSON/HTML error with status 200 must still stay private.
                prefix = response.readline(65_537)
                if response.status != 200 or len(prefix) > 65_536 or not prefix.lstrip().startswith((b'event:', b'data:', b':')):
                    raise ValueError('invalid upstream response')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                sent = True
                self.wfile.write(prefix)
                self.wfile.flush()
                total = len(prefix)
                while time.monotonic() - started < 90:
                    chunk = response.read1(16_384)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 2_000_000:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except Exception:
            # No upstream error body or headers reach the candidate.
            if not sent:
                self.send_error(502, 'trial provider unavailable')
        self.close_connection = True


class BoundedServer(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


if __name__ == '__main__':
    server = BoundedServer(('0.0.0.0', 8080), Broker)
    server.timeout = 10
    server.serve_forever()
