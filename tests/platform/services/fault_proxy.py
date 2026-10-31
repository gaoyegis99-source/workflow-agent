"""Test-only forwarding proxy. Drop one selected reply after upstream commits it."""

import argparse
import http.client
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--match", required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--dropped", type=Path, required=True)
    args = parser.parse_args()
    upstream = urlsplit(args.upstream)
    lock = threading.Lock()
    state = {"dropped": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def forward(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            connection = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=8)
            try:
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in ["host", "connection", "transfer-encoding"]
                }
                connection.request(self.command, self.path, body=data, headers=headers)
                response = connection.getresponse()
                content = response.read()
                with lock:
                    drop = (
                        not state["dropped"]
                        and self.command == "POST"
                        and args.match in self.path
                        and response.status < 300
                    )
                    if drop:
                        state["dropped"] = True
                if drop:
                    args.dropped.write_text(
                        json.dumps({"status": response.status, "path": self.path})
                    )
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                else:
                    self.send_response(response.status)
                    self.send_header(
                        "Content-Type", response.getheader("Content-Type", "application/json")
                    )
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
            finally:
                connection.close()

        do_POST = do_GET = do_PUT = forward

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    args.ready.write_text(json.dumps({"port": server.server_port}))
    server.serve_forever()


if __name__ == "__main__":
    main()
