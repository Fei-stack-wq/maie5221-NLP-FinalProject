"""Serve a no-network browser regression fixture using the real Studio UI.

Run: python tests/web/serve_stability_fixture.py
Open http://127.0.0.1:8767, wait about 10 seconds, and inspect PASS/FAIL.
The fixture asserts stable DOM identity, expanded disclosures, focus, text
selection, scroll and animation state over changing polling responses.
It replaces fetch before app boot; no model, task, tool or user data is used.
"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / 'miniagent' / 'web'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            data = (WEB / 'index.html').read_text().replace(
                '<script src="/app.js" defer></script>',
                '<script src="/studio_live_dom.js" defer></script><script src="/app.js" defer></script>',
            ).encode()
            content_type = 'text/html'
        elif self.path in {f'/{asset.name}' for asset in WEB.iterdir() if asset.suffix in {'.js', '.css'}}:
            data = (WEB / self.path[1:]).read_bytes()
            content_type = 'text/css' if self.path.endswith('css') else 'text/javascript'
        elif self.path == '/studio_live_dom.js':
            data = Path(__file__).with_name('studio_live_dom.js').read_bytes()
            content_type = 'text/javascript'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    with HTTPServer(("127.0.0.1", parser.parse_args().port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}/", flush=True)
        server.serve_forever()
