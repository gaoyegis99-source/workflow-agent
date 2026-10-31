"""Test-only proxy: receive a complete API response, then drop the client socket."""

import argparse
import http.client
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--receipt-file", type=Path, required=True)
    args = parser.parse_args()
    upstream = urlsplit(args.upstream)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            connection = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=5)
            try:
                connection.request(
                    "POST",
                    self.path,
                    body,
                    {
                        key: self.headers[key]
                        for key in ["Authorization", "Idempotency-Key", "Content-Type"]
                    },
                )
                response = connection.getresponse()
                receipt = json.loads(response.read())
                args.receipt_file.write_text(
                    json.dumps({"status": response.status, "receipt": receipt})
                )
            finally:
                connection.close()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    args.ready_file.write_text(json.dumps({"port": server.server_port}))
    server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
