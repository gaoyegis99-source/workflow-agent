"""Independent durable fault-injection peer. Test-only; never packaged with V7.

SQLite belongs exclusively to this simulated external service. Platform tests
always use PostgreSQL. A cancellation tombstone seals even not-yet-seen keys.
"""

import argparse
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


class Peer:
    def __init__(self, path: str):
        self.path = path
        # Keep WAL open across request connections. Closing the last connection
        # after every request forces a checkpoint and defeats WAL batching.
        self.keeper = sqlite3.connect(path, timeout=5)
        self.keeper.execute("PRAGMA journal_mode=WAL")
        with self.connect() as connection:
            # Keep durable commits while avoiding a rollback-journal fsync for
            # each test-service operation on a busy host filesystem.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY, request_digest TEXT, state TEXT NOT NULL,
                    revision INTEGER NOT NULL, execution_id TEXT, executed INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS faults (name TEXT PRIMARY KEY, value REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (method TEXT, action_id TEXT);
                COMMIT;
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA synchronous=FULL")
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def take(self, name: str) -> float:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM faults WHERE name=?", (name,)).fetchone()
            connection.execute("DELETE FROM faults WHERE name=?", (name,))
            return row[0] if row else 0

    def call(self, method: str, action_id: str):
        with self.connect() as connection:
            connection.execute("INSERT INTO calls VALUES (?, ?)", (method, action_id))

    @staticmethod
    def observation(row):
        return {
            "event_id": row["action_id"] + ":" + str(row["revision"]),
            "action_id": row["action_id"],
            "revision": row["revision"],
            "state": row["state"],
            "execution_id": row["execution_id"],
            "executed": bool(row["executed"]),
        }

    def query(self, action_id: str):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
            return self.observation(row) if row else None

    def submit(self, action_id: str, request: dict, *, rejected=False):
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if row and row["request_digest"] not in {None, digest}:
                raise ValueError("Conflicting external idempotency payload")
            if row is None:
                connection.execute(
                    "INSERT INTO actions VALUES (?, ?, ?, 1, ?, ?)",
                    (
                        action_id,
                        digest,
                        "REJECTED" if rejected else "RUNNING",
                        None if rejected else "mock-" + uuid4().hex,
                        int(not rejected),
                    ),
                )
            return self.observation(
                connection.execute(
                    "SELECT * FROM actions WHERE action_id=?", (action_id,)
                ).fetchone()
            )

    def cancel(self, action_id: str):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO actions VALUES (?, NULL, 'SEALED', 1, NULL, 0)", (action_id,)
                )
            elif row["state"] == "RUNNING":
                connection.execute(
                    "UPDATE actions SET state='CANCELLED', revision=revision+1 WHERE action_id=?",
                    (action_id,),
                )
            return self.observation(
                connection.execute(
                    "SELECT * FROM actions WHERE action_id=?", (action_id,)
                ).fetchone()
            )

    def configure(self, values: dict):
        with self.connect() as connection:
            for name, value in values.items():
                connection.execute("INSERT OR REPLACE INTO faults VALUES (?, ?)", (name, value))

    def complete(self, action_id: str):
        with self.connect() as connection:
            connection.execute(
                "UPDATE actions SET state='SUCCEEDED', revision=revision+1 "
                "WHERE action_id=? AND state='RUNNING'",
                (action_id,),
            )
        return self.query(action_id)

    def stats(self):
        with self.connect() as connection:
            return {
                "executions": connection.execute(
                    "SELECT count(*) FROM actions WHERE executed=1"
                ).fetchone()[0],
                "actions": [dict(row) for row in connection.execute("SELECT * FROM actions")],
                "calls": [dict(row) for row in connection.execute("SELECT * FROM calls")],
            }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()
    peer = Peer(args.database)
    token = os.environ["P2_PEER_TOKEN"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def drop(self):
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def handle_request(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.reply(401, {})
                return
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size)) if size else {}
            if self.path == "/test/configure" and self.command == "POST":
                peer.configure(body)
                self.reply(200, {})
                return
            if self.path == "/test/stats":
                self.reply(200, peer.stats())
                return
            if self.path.startswith("/test/complete/"):
                self.reply(200, peer.complete(self.path.rsplit("/", 1)[1]))
                return
            if not self.path.startswith("/actions/"):
                self.reply(404, {})
                return
            action_id = self.path.rsplit("/", 1)[1]
            peer.call(self.command, action_id)
            error = peer.take("http_error")
            if error:
                self.reply(int(error), {"error": "Test transport failure"})
                return
            if self.command == "PUT":
                delay = peer.take("submit_delay")
                if delay:
                    time.sleep(delay)
                result = peer.submit(action_id, body, rejected=bool(peer.take("reject_submit")))
                if peer.take("drop_submit_response"):
                    self.drop()
                    return
            elif self.command == "DELETE":
                if peer.take("drop_cancel_response"):
                    self.drop()
                    return
                result = peer.cancel(action_id)
            elif self.command == "GET":
                if peer.take("query_absent"):
                    result = None
                else:
                    result = peer.query(action_id)
                if peer.take("drop_query_response"):
                    self.drop()
                    return
            else:
                self.reply(405, {})
                return
            if result and peer.take("invalid_execution_identity"):
                result["execution_id"] = "invalid/execution"
            self.reply(200 if result else 404, result or {})

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def do_PUT(self):
            self.handle_request()

        def do_DELETE(self):
            self.handle_request()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    args.ready_file.write_text(json.dumps({"port": server.server_port}))
    server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
