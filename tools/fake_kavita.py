"""A tiny stand-in Kavita OPDS server for real-HTTP smoke/E2E testing.

Serves the test fixtures plus fake EPUB/PDF/cover bytes so RetroShelf can be
exercised end-to-end over real sockets (not TestClient). Run:
    .venv/bin/python tools/fake_kavita.py 5599
"""
import sys
import pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIX = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
ROOT = (FIX / "opds_root.xml").read_text(encoding="utf-8")
ACQ = (FIX / "opds_acquisition.xml").read_text(encoding="utf-8")

# A syntactically-real minimal EPUB starts with a ZIP local-file header "PK\x03\x04".
FAKE_EPUB = b"PK\x03\x04" + b"FAKE-EPUB-CONTENT" * 8
FAKE_PDF = b"%PDF-1.4\n" + b"FAKE-PDF-CONTENT" * 8 + b"\n%%EOF"
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"FAKEIMG" * 4


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/opds/KEY":
            return self._send(200, ROOT, "application/atom+xml;profile=opds-catalog;kind=navigation")
        if "recently-added" in path or "/libraries" in path or "/search" in path:
            return self._send(200, ACQ, "application/atom+xml;profile=opds-catalog;kind=acquisition")
        if path.endswith(".epub"):
            return self._send(200, FAKE_EPUB, "application/epub+zip")
        if path.endswith(".pdf"):
            return self._send(200, FAKE_PDF, "application/pdf")
        if "/api/image" in path:
            return self._send(200, FAKE_PNG, "image/png")
        return self._send(404, "not found", "text/plain")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5599
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
