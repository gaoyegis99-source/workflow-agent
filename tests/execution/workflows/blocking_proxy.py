"""Test-only proxy that blocks one workflow-node file download."""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--blocked", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    args = parser.parse_args()
    client = httpx.Client(base_url=args.upstream, trust_env=False, timeout=15)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            if "/nodes/" in self.path and "/files/" in self.path and not args.blocked.exists():
                args.blocked.write_text(self.path)
                deadline = time.monotonic() + 60
                while not args.release.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
            self.forward()

        def do_POST(self):  # noqa: N802
            self.forward()

        def forward(self):
            length = int(self.headers.get("Content-Length", "0"))
            content = self.rfile.read(length) if length else None
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "content-length", "connection"}
            }
            response = client.request(self.command, self.path, headers=headers, content=content)
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in {"content-length", "connection", "transfer-encoding"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(response.content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer((args.host, 0), Handler)
    args.ready.write_text(json.dumps({"port": server.server_address[1]}))
    try:
        server.serve_forever()
    finally:
        client.close()


if __name__ == "__main__":
    main()
