"""HTTPS fault proxy for mixed-workflow acceptance tests only."""

import argparse
import hashlib
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class State:
    def __init__(self, config, upstream, ca):
        self.config, self.lock = config, threading.Lock()
        self.client = httpx.Client(base_url=upstream, verify=ca, trust_env=False, timeout=30)
        self.used = set()
        self.rejected = {}

    def once(self, name):
        with self.lock:
            enabled = bool(load(self.config).get(name))
            if enabled and name not in self.used:
                self.used.add(name)
                return True
            return False

    def mode(self):
        return load(self.config).get("terminal_mode")

    def barrier(self, name):
        if not load(self.config).get("hold_" + name):
            return
        blocked = Path(self.config).with_name(name + "-blocked")
        release = Path(self.config).with_name(name + "-release")
        blocked.write_text("blocked")
        while not release.exists():
            time.sleep(0.02)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "V7FaultProxy/1"

    @property
    def state(self):
        return self.server.state

    def body(self):
        return self.rfile.read(int(self.headers.get("content-length", "0")))

    def respond(self, status, content=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def action(self):
        parts = urlsplit(self.path).path.strip("/").split("/")
        return parts[2] if len(parts) >= 3 and parts[:2] == ["v1", "actions"] else None

    def rejected(self, action, request):
        digest = hashlib.sha256(action.encode()).hexdigest()[:32]
        implementation = request["target"]["implementation_digest"]
        return {
            "event_id": f"r{digest}:1",
            "action_id": action,
            "remote_execution_id": "r" + digest,
            "revision": 1,
            "state": "REJECTED",
            "executed": False,
            "outputs": None,
            "implementation_digest": implementation,
            "started_at": None,
            "finished_at": None,
            "cleanup_state": "NOT_REQUIRED",
        }

    def transform(self, response):
        mode = self.state.mode()
        if mode not in {"bad_digest", "missing_port"}:
            return response.content
        try:
            value = response.json()
        except ValueError:
            return response.content
        if value.get("state") != "SUCCEEDED" or not isinstance(value.get("outputs"), dict):
            return response.content
        if mode == "missing_port":
            value["outputs"].pop("method", None)
        else:
            first = next(iter(value["outputs"].values()))
            first["digest"] = "sha256:" + "0" * 64
        return json.dumps(value, separators=(",", ":")).encode()

    def forward(self):
        content = self.body()
        action = self.action()
        path = urlsplit(self.path).path
        if self.command == "POST" and action and path == f"/v1/actions/{action}/submit":
            if load(self.state.config).get("reject_submit"):
                value = self.state.rejected.setdefault(
                    action, self.rejected(action, json.loads(content))
                )
                return self.respond(202, json.dumps(value).encode())
        if self.command == "GET" and action in self.state.rejected:
            return self.respond(200, json.dumps(self.state.rejected[action]).encode())
        if self.command == "GET" and action and "/outputs/" not in path:
            if load(self.state.config).get("query_404_always") or self.state.once("query_404_once"):
                return self.respond(404, b'{"code":"RESOURCE_NOT_FOUND"}')
        if self.command == "GET" and "/outputs/" in path:
            if load(self.state.config).get("output_404_always") or self.state.once(
                "output_404_once"
            ):
                return self.respond(404, b'{"code":"RESOURCE_NOT_FOUND"}')
        if (
            self.command == "POST"
            and action
            and path == f"/v1/actions/{action}/cancel"
            and load(self.state.config).get("cancel_404_always")
        ):
            return self.respond(404, b'{"code":"RESOURCE_NOT_FOUND"}')
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in {"authorization", "content-type", "content-length", "digest"}
        }
        response = self.state.client.request(
            self.command, self.path, headers=headers, content=content
        )
        if self.command == "PUT" and action and path.endswith("/inputs/vector"):
            self.state.barrier("upload")
        if self.command == "POST" and action and path == f"/v1/actions/{action}/submit":
            if self.state.once("drop_submit_response_once"):
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return None
        body = self.transform(response) if self.command == "GET" and action else response.content
        if self.command == "GET" and "/outputs/" in path and self.state.once("drop_output_once"):
            self.send_response(response.status_code)
            self.send_header(
                "content-type", response.headers.get("content-type", "application/json")
            )
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[: max(1, len(body) // 2)])
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return None
        return self.respond(
            response.status_code,
            body,
            response.headers.get("content-type", "application/json"),
        )

    do_GET = forward
    do_POST = forward
    do_PUT = forward

    def log_message(self, format, *args):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--upstream-ca", required=True)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--ready", required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.state = State(args.config, args.upstream, args.upstream_ca)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.certificate, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    Path(args.ready).write_text(json.dumps({"port": server.server_port}))
    try:
        server.serve_forever()
    finally:
        server.state.client.close()


if __name__ == "__main__":
    main()
