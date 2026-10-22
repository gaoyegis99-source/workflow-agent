"""Persistent v2 conformance peer backed by one fixed Docker container per Action."""

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import ssl
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("V7_REMOTE_ROOT", "/state")).resolve()
UTC = timezone.utc  # noqa: UP017 -- deployment peer supports Python 3.8.
DB = ROOT / "peer.sqlite3"
TOKEN_FILE = os.environ.get("V7_REMOTE_TOKEN_FILE")


def secret(path):
    value = Path(path)
    if not value.is_file() or value.is_symlink() or stat.S_IMODE(value.stat().st_mode) & 0o077:
        raise RuntimeError("Remote token must be a private regular file")
    return value.read_text().strip()


TOKEN = secret(TOKEN_FILE) if TOKEN_FILE else os.environ.get("V7_REMOTE_TOKEN", "")
IMAGE = os.environ.get("V7_REMOTE_JOB_IMAGE", "groundwater-v7-spatial-native:0.6.2")
IMPLEMENTATION_DIGEST = os.environ.get("V7_REMOTE_IMPLEMENTATION_DIGEST", "")
INSTANCE_ID = os.environ.get("V7_REMOTE_INSTANCE_ID", "default")
TIMEOUT_SECONDS = int(os.environ.get("V7_REMOTE_JOB_TIMEOUT", "120"))
MAX_INPUT = 20 * 1024 * 1024
MAX_OUTPUT = 100 * 1024 * 1024
TERMINAL = {"SUCCEEDED", "FAILED", "REJECTED", "CANCELLED"}
ACTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
LOCK = threading.RLock()
THREADS = set()
ROOT.mkdir(parents=True, exist_ok=True)


def digest(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def file_digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def container_name(action):
    instance = re.sub(r"[^a-zA-Z0-9_.-]", "", INSTANCE_ID)[:20] or "default"
    return "v7-remote-" + instance + "-" + hashlib.sha256(action.encode()).hexdigest()[:24]


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("""CREATE TABLE IF NOT EXISTS actions (
      action_id TEXT PRIMARY KEY, request_digest TEXT, remote_execution_id TEXT,
      revision INTEGER NOT NULL, state TEXT NOT NULL, executed INTEGER NOT NULL,
      request TEXT, outputs TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
      schema_version INTEGER NOT NULL DEFAULT 1, container_name TEXT,
      container_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
      implementation_digest TEXT, started_at TEXT, finished_at TEXT,
      cleanup_state TEXT NOT NULL DEFAULT 'NOT_REQUIRED', error TEXT)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(actions)")}
    additions = {
        "schema_version": "INTEGER NOT NULL DEFAULT 1",
        "container_name": "TEXT",
        "container_id": "TEXT",
        "generation": "INTEGER NOT NULL DEFAULT 0",
        "implementation_digest": "TEXT",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "cleanup_state": "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'",
        "error": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE actions ADD COLUMN {name} {declaration}")
    db.commit()
    return db


def get(action):
    with connect() as db:
        return db.execute("SELECT * FROM actions WHERE action_id=?", (action,)).fetchone()


def observation(row):
    value = {
        "event_id": f"{row['remote_execution_id']}:{row['revision']}",
        "action_id": row["action_id"],
        "remote_execution_id": row["remote_execution_id"],
        "revision": row["revision"],
        "state": row["state"],
        "executed": bool(row["executed"]),
        "outputs": json.loads(row["outputs"]) if row["outputs"] else None,
    }
    if row["schema_version"] >= 2:
        value.update(
            {
                "implementation_digest": row["implementation_digest"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "cleanup_state": row["cleanup_state"],
            }
        )
    return value


def docker(*args, check=True, timeout=40):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True, timeout=timeout
    )


def inspect(name):
    result = docker("inspect", name, check=False)
    if not result.returncode:
        return json.loads(result.stdout)[0]
    if "No such object" in result.stderr or "No such container" in result.stderr:
        return None
    raise OSError("Docker inspect is unavailable")


def set_state(action, state, *, executed=None, outputs=None, error=None, cleanup=None):
    with LOCK, connect() as db:
        row = db.execute("SELECT * FROM actions WHERE action_id=?", (action,)).fetchone()
        if row is None or row["state"] in TERMINAL:
            return row
        values = {
            "state": state,
            "executed": int(bool(row["executed"]) or bool(executed)),
            "outputs": json.dumps(outputs, sort_keys=True)
            if outputs is not None
            else row["outputs"],
            "error": error,
            "cleanup_state": cleanup or row["cleanup_state"],
            "finished_at": now() if state in TERMINAL else row["finished_at"],
        }
        parameters = dict(values)
        parameters["action_id"] = action
        db.execute(
            "UPDATE actions SET revision=revision+1,state=:state,executed=:executed,"
            "outputs=:outputs,error=:error,cleanup_state=:cleanup_state,finished_at=:finished_at "
            "WHERE action_id=:action_id",
            parameters,
        )
        return db.execute("SELECT * FROM actions WHERE action_id=?", (action,)).fetchone()


def claim(action):
    with LOCK, connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM actions WHERE action_id=?", (action,)).fetchone()
        if row is None or row["state"] != "QUEUED" or row["cancel_requested"]:
            return False
        return (
            db.execute(
                "UPDATE actions SET state='STARTING',revision=revision+1,generation=generation+1 "
                "WHERE action_id=? AND state='QUEUED' AND cancel_requested=0",
                (action,),
            ).rowcount
            == 1
        )


def cancelled(action):
    row = get(action)
    return row is None or bool(row["cancel_requested"]) or row["state"] in TERMINAL


def create_job(action, row):
    root = ROOT / action
    input_dir, output_dir = root / "input", root / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    request = json.loads(row["request"])
    local_request = {
        "target": {
            "kind": "OPERATOR",
            "operator_id": "spatial-vector-summary",
            "version_id": "1.0.0",
            "execution_binding": {
                "kind": "LOCAL",
                "implementation_id": "spatial-vector-summary-v1",
            },
        },
        "parameters": request["parameters"],
    }
    temporary = input_dir / "request.json.tmp"
    temporary.write_bytes(canonical(local_request))
    temporary.replace(input_dir / "request.json")
    uid, gid = os.getuid(), os.getgid()
    name = row["container_name"]
    result = docker(
        "create",
        "--name",
        name,
        "--label",
        f"v7.action_id={action}",
        "--label",
        f"v7.request_digest={row['request_digest']}",
        "--label",
        f"v7.implementation_digest={row['implementation_digest']}",
        "--label",
        f"v7.remote_peer={INSTANCE_ID}",
        "--network",
        "none",
        "--read-only",
        "--cpus",
        "1",
        "--memory",
        "1g",
        "--pids-limit",
        "128",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--user",
        f"{uid}:{gid}",
        "-v",
        f"{input_dir.resolve()}:/input:ro",
        "-v",
        f"{output_dir.resolve()}:/output:rw",
        IMAGE,
        "platform-spatial",
        "execute",
        "--input",
        "/input",
        "--output",
        "/output",
        check=False,
    )
    info = inspect(name)
    if info is None:
        raise OSError("Docker create result is unknown: " + result.stderr[:200])
    labels = info["Config"].get("Labels") or {}
    if (
        labels.get("v7.action_id") != action
        or labels.get("v7.request_digest") != row["request_digest"]
        or labels.get("v7.implementation_digest") != row["implementation_digest"]
    ):
        raise RuntimeError("Stable container identity conflict")
    with LOCK, connect() as db:
        db.execute(
            "UPDATE actions SET container_id=? WHERE action_id=? AND state='STARTING'",
            (info["Id"], action),
        )
    return info


def stop_job(info):
    docker("stop", "--time", "10", info["Id"], check=False, timeout=15)
    latest = inspect(info["Id"])
    if latest and latest["State"]["Running"]:
        docker("kill", latest["Id"], check=False, timeout=10)
        latest = inspect(info["Id"])
    return latest


def collect(action):
    output = ROOT / action / "output"
    expected = {"summary": "summary.json", "method": "method.json"}
    names = (
        {path.name for path in output.iterdir() if path.name != "started.marker"}
        if output.is_dir()
        else set()
    )
    if names != set(expected.values()):
        raise ValueError("Remote output set differs")
    values, total = {}, 0
    for port, filename in expected.items():
        path = output / filename
        if not path.is_file() or path.is_symlink():
            raise ValueError("Remote output is not a regular file")
        size = path.stat().st_size
        total += size
        if total > MAX_OUTPUT:
            raise ValueError("Remote output exceeds limit")
        values[port] = {
            "path": f"/v1/actions/{action}/outputs/{port}",
            "digest": file_digest(path),
            "size": size,
            "media_type": "application/json",
        }
    return values


def advance(action):
    row = get(action)
    if row is None or row["state"] in TERMINAL:
        return False
    if row["state"] == "QUEUED":
        if row["cancel_requested"]:
            set_state(action, "CANCELLED", executed=False, cleanup="CONFIRMED")
            return False
        if not claim(action):
            return True
        row = get(action)
    name = row["container_name"] or container_name(action)
    info = inspect(name)
    if row["cancel_requested"]:
        if info is not None:
            info = stop_job(info)
            if info is not None and info["State"]["Running"]:
                with LOCK, connect() as db:
                    db.execute(
                        "UPDATE actions SET cleanup_state='UNKNOWN' WHERE action_id=?", (action,)
                    )
                return True
        executed = bool(row["executed"]) or bool(
            info and not info["State"]["StartedAt"].startswith("0001-")
        )
        set_state(action, "CANCELLED", executed=executed, cleanup="CONFIRMED")
        return False
    if info is None:
        if row["state"] != "STARTING":
            set_state(action, "FAILED", executed=bool(row["executed"]), error="CONTAINER_MISSING")
            return False
        info = create_job(action, row)
        if cancelled(action):
            return True
    state = info["State"]
    started = not state["StartedAt"].startswith("0001-")
    if not started:
        if cancelled(action):
            return True
        docker("start", info["Id"], check=False)
        info = inspect(info["Id"])
        if info is None:
            raise OSError("Docker start result is unknown")
        state = info["State"]
        started = not state["StartedAt"].startswith("0001-")
    if started and not row["executed"]:
        with LOCK, connect() as db:
            db.execute(
                "UPDATE actions SET state='RUNNING',executed=1,revision=revision+1,"
                "started_at=COALESCE(started_at,?) WHERE action_id=? AND state NOT IN "
                "('SUCCEEDED','FAILED','REJECTED','CANCELLED')",
                (now(), action),
            )
        row = get(action)
    if state["Running"]:
        started_at = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
        requested_timeout = (
            json.loads(row["request"]).get("limits", {}).get("timeout_seconds", TIMEOUT_SECONDS)
        )
        timeout = min(TIMEOUT_SECONDS, requested_timeout)
        if (datetime.now(UTC) - started_at).total_seconds() > timeout:
            with LOCK, connect() as db:
                db.execute(
                    "UPDATE actions SET cancel_requested=1,cleanup_state='PENDING',"
                    "error='TIME_LIMIT',revision=revision+1 WHERE action_id=?",
                    (action,),
                )
        return True
    if row["state"] != "COLLECTING":
        with LOCK, connect() as db:
            db.execute(
                "UPDATE actions SET state='COLLECTING',revision=revision+1 WHERE action_id=?",
                (action,),
            )
        row = get(action)
    if row["cancel_requested"]:
        terminal = "FAILED" if row["error"] == "TIME_LIMIT" else "CANCELLED"
        set_state(action, terminal, executed=True, error=row["error"], cleanup="CONFIRMED")
    elif state["ExitCode"]:
        set_state(action, "FAILED", executed=True, error="CONTAINER_EXIT")
    else:
        try:
            set_state(action, "SUCCEEDED", executed=True, outputs=collect(action))
        except (OSError, ValueError, json.JSONDecodeError):
            set_state(action, "FAILED", executed=True, error="INVALID_OUTPUT")
    return False


def worker(action):
    try:
        while True:
            try:
                if not advance(action):
                    return
                time.sleep(0.2)
            except (OSError, sqlite3.OperationalError, subprocess.TimeoutExpired) as exc:
                print(
                    json.dumps(
                        {
                            "remote_peer": "transient",
                            "action": action,
                            "error": str(exc)[:300],
                        }
                    ),
                    flush=True,
                )
                time.sleep(1)
    except Exception as exc:
        print(
            json.dumps({"remote_peer": "recovery", "action": action, "error": str(exc)[:300]}),
            flush=True,
        )
    finally:
        with LOCK:
            THREADS.discard(action)


def schedule(action):
    with LOCK:
        if action in THREADS:
            return
        THREADS.add(action)
    threading.Thread(target=worker, args=(action,), daemon=True).start()


def recover():
    with connect() as db:
        active = [
            row[0]
            for row in db.execute(
                "SELECT action_id FROM actions WHERE state NOT IN "
                "('SUCCEEDED','FAILED','REJECTED','CANCELLED')"
            )
        ]
    for action in active:
        schedule(action)


class Handler(BaseHTTPRequestHandler):
    server_version = "V7Remote/2"

    def send_json(self, status, value):
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        supplied = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(supplied, "Bearer " + TOKEN):
            self.send_json(401, {"code": "AUTHENTICATION_REQUIRED"})
            return False
        return True

    def parts(self):
        parts = [part for part in self.path.split("?", 1)[0].split("/") if part]
        return [] if len(parts) >= 3 and not ACTION.fullmatch(parts[2]) else parts

    def content_length(self, maximum):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        return length if 0 <= length <= maximum else None

    def do_PUT(self):
        if not self.authorized():
            return
        parts = self.parts()
        if len(parts) != 5 or parts[:2] != ["v1", "actions"] or parts[3:] != ["inputs", "vector"]:
            return self.send_json(404, {"code": "RESOURCE_NOT_FOUND"})
        length = self.content_length(MAX_INPUT)
        if length is None:
            return self.send_json(413, {"code": "INPUT_LIMIT"})
        action = parts[2]
        target = ROOT / action / "input" / "vector.gpkg"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".upload")
        hasher, remaining = hashlib.sha256(), length
        with temporary.open("wb") as stream:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    temporary.unlink(missing_ok=True)
                    return self.send_json(400, {"code": "INCOMPLETE_UPLOAD"})
                stream.write(chunk)
                hasher.update(chunk)
                remaining -= len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        actual, claimed = "sha256:" + hasher.hexdigest(), self.headers.get("Digest")
        if actual != claimed:
            temporary.unlink(missing_ok=True)
            return self.send_json(422, {"code": "INVALID_BINDING"})
        if target.exists() and file_digest(target) != actual:
            temporary.unlink(missing_ok=True)
            return self.send_json(409, {"code": "IDEMPOTENCY_CONFLICT"})
        temporary.unlink(missing_ok=True) if target.exists() else temporary.replace(target)
        return self.send_json(200, {"digest": actual, "size": length})

    def do_POST(self):
        if not self.authorized():
            return
        parts = self.parts()
        if len(parts) != 4 or parts[:2] != ["v1", "actions"]:
            return self.send_json(404, {"code": "RESOURCE_NOT_FOUND"})
        action, operation = parts[2], parts[3]
        if operation == "submit":
            length = self.content_length(64 * 1024)
            if length is None:
                return self.send_json(413, {"code": "REQUEST_LIMIT"})
            try:
                body = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self.send_json(400, {"code": "MALFORMED_REQUEST"})
            schema, source, target = (
                body.get("schema_version"),
                ROOT / action / "input" / "vector.gpkg",
                body.get("target", {}),
            )
            valid_target = (
                target.get("operator_id") == "spatial-vector-summary"
                and target.get("version_id") == "1.0.0"
            )
            if (
                schema not in {1, 2}
                or body.get("action_id") != action
                or not valid_target
                or not isinstance(body.get("request_digest"), str)
                or not source.is_file()
                or file_digest(source) != body.get("inputs", {}).get("vector", {}).get("digest")
            ):
                return self.send_json(422, {"code": "INVALID_BINDING"})
            implementation = (
                target.get("implementation_digest")
                if schema == 2
                else target.get("contract_digest")
            )
            if schema == 2 and implementation != IMPLEMENTATION_DIGEST:
                return self.send_json(422, {"code": "IMPLEMENTATION_MISMATCH"})
            identity = "r" + hashlib.sha256(action.encode()).hexdigest()[:32]
            with LOCK, connect() as db:
                row = db.execute("SELECT * FROM actions WHERE action_id=?", (action,)).fetchone()
                if row and row["request_digest"] != body["request_digest"]:
                    return self.send_json(409, {"code": "IDEMPOTENCY_CONFLICT"})
                if row is None:
                    db.execute(
                        "INSERT INTO actions "
                        "(action_id,request_digest,remote_execution_id,revision,state,executed,"
                        "request,outputs,cancel_requested,schema_version,container_name,"
                        "implementation_digest,cleanup_state) "
                        "VALUES (?,?,?,1,'QUEUED',0,?,NULL,0,?,?,?,'NOT_REQUIRED')",
                        (
                            action,
                            body["request_digest"],
                            identity,
                            json.dumps(body, sort_keys=True),
                            schema,
                            container_name(action),
                            implementation,
                        ),
                    )
                    db.commit()
                    row = db.execute(
                        "SELECT * FROM actions WHERE action_id=?", (action,)
                    ).fetchone()
            schedule(action)
            return self.send_json(202, observation(row))
        if operation == "cancel":
            with LOCK, connect() as db:
                row = db.execute("SELECT * FROM actions WHERE action_id=?", (action,)).fetchone()
                if row is None:
                    return self.send_json(404, {"code": "RESOURCE_NOT_FOUND"})
                if row["state"] not in TERMINAL:
                    if row["state"] == "QUEUED":
                        db.execute(
                            "UPDATE actions SET state='CANCELLED',revision=revision+1,"
                            "cancel_requested=1,"
                            "cleanup_state='CONFIRMED',finished_at=? WHERE action_id=?",
                            (now(), action),
                        )
                    else:
                        db.execute(
                            "UPDATE actions SET cancel_requested=1,cleanup_state='PENDING',"
                            "revision=revision+1 WHERE action_id=?",
                            (action,),
                        )
                    row = db.execute(
                        "SELECT * FROM actions WHERE action_id=?", (action,)
                    ).fetchone()
            schedule(action)
            return self.send_json(200, observation(row))
        return self.send_json(404, {"code": "RESOURCE_NOT_FOUND"})

    def do_GET(self):
        if not self.authorized():
            return
        parts = self.parts()
        if len(parts) == 3 and parts[:2] == ["v1", "actions"]:
            row = get(parts[2])
            if row and row["state"] not in TERMINAL:
                schedule(parts[2])
            return (
                self.send_json(200, observation(row))
                if row
                else self.send_json(404, {"code": "RESOURCE_NOT_FOUND"})
            )
        if len(parts) == 5 and parts[:2] == ["v1", "actions"] and parts[3] == "outputs":
            filename = {"summary": "summary.json", "method": "method.json"}.get(parts[4])
            row, path = get(parts[2]), ROOT / parts[2] / "output" / (filename or "missing")
            if not row or row["state"] != "SUCCEEDED" or not filename or not path.is_file():
                return self.send_json(404, {"code": "RESOURCE_NOT_FOUND"})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
            return
        return self.send_json(404, {"code": "RESOURCE_NOT_FOUND"})

    def log_message(self, fmt, *args):
        print(json.dumps({"remote_peer": fmt % args}))


def serve():
    recover()
    server = ThreadingHTTPServer(
        (os.environ.get("V7_REMOTE_HOST", "0.0.0.0"), int(os.environ.get("PORT", "19080"))), Handler
    )
    certificate, key = os.environ.get("V7_REMOTE_TLS_CERT"), os.environ.get("V7_REMOTE_TLS_KEY")
    if certificate and key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
