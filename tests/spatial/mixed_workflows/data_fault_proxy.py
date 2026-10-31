"""Test-only Data proxy that rejects a publication commit before forwarding it."""

import argparse
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--ready", type=Path, required=True)
    args = parser.parse_args()
    upstream = urlsplit(args.upstream)

    class Handler(BaseHTTPRequestHandler):
        def forward(self):
            content = self.rfile.read(int(self.headers.get("content-length", "0")))
            if self.command == "POST" and self.path.endswith("/commit"):
                body = b'{"code":"INVALID_BINDING","message":"injected commit rejection"}'
                self.send_response(422)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            connection = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=8)
            try:
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in {"host", "connection", "transfer-encoding"}
                }
                connection.request(self.command, self.path, body=content, headers=headers)
                response = connection.getresponse()
                body = response.read()
                self.send_response(response.status)
                self.send_header(
                    "content-type", response.getheader("content-type", "application/json")
                )
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                connection.close()

        do_GET = do_POST = do_PUT = forward

        def log_message(self, *_):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    args.ready.write_text(json.dumps({"port": server.server_port}))
    server.serve_forever()


if __name__ == "__main__":
    main()
