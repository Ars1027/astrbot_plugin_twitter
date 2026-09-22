"""Local-only WebUI preview: python tests/webui_preview.py [--port 8765].

Open /?review-tests=1 for the browser DOM regressions. The page displays
PASS/FAIL results for stop controls and untrusted text rendering; no AstrBot
or real subscription data is used. Reload to run again.
"""

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "pages/subscriptions"), **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            content = (ROOT / "pages/subscriptions/index.html").read_text("utf-8")
            bridge = (
                "review-tests.js"
                if "review-tests" in parse_qs(urlsplit(self.path).query)
                else "preview-bridge.js"
            )
            content = content.replace(
                '<script type="module" src="./app.js"></script>',
                f'<script src="./{bridge}"></script>'
                '<script type="module" src="./app.js"></script>',
            ).encode()
            mime = "text/html; charset=utf-8"
        elif path == "/preview-bridge.js":
            content = (ROOT / "tests/fixtures/webui_preview.js").read_bytes()
            mime = "text/javascript; charset=utf-8"
        elif path == "/review-tests.js":
            content = (ROOT / "tests/fixtures/webui_review_tests.js").read_bytes()
            mime = "text/javascript; charset=utf-8"
        else:
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    port = parser.parse_args().port
    print(f"Mock WebUI: http://127.0.0.1:{port} (Ctrl+C to stop)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), PreviewHandler).serve_forever()
