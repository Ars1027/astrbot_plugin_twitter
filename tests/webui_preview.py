"""Local-only WebUI preview: python tests/webui_preview.py (no AstrBot required)."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "pages/subscriptions"), **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            content = (ROOT / "pages/subscriptions/index.html").read_text("utf-8")
            content = content.replace(
                '<script type="module" src="./app.js"></script>',
                '<script src="./preview-bridge.js"></script>'
                '<script type="module" src="./app.js"></script>',
            ).encode()
            mime = "text/html; charset=utf-8"
        elif path == "/preview-bridge.js":
            content = (ROOT / "tests/fixtures/webui_preview.js").read_bytes()
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
    print("Mock WebUI: http://127.0.0.1:8765 (Ctrl+C to stop)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8765), PreviewHandler).serve_forever()
