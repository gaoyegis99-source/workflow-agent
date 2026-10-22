"""Only generated CommandLineTools invoke this service bridge; no scientific code here."""

import argparse
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TERMINAL = {"SUCCEEDED", "FAILED", "REJECTED", "CANCELLED", "SEALED"}
RETRYABLE_HTTP = {404, 409, 429, 500, 502, 503, 504}


def await_terminal(call, identity, request, bridge_instance, sleep=time.sleep):
    """Submit once after a known response, then follow the durable revision only."""
    delay = 1
    node = None
    while node is None:
        try:
            node = json.loads(call("POST", "/nodes/" + identity, request))
            delay = 1
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP:
                raise RuntimeError("Workflow node request rejected") from None
            delay = min(30, delay * 2)
        except (OSError, TimeoutError):
            delay = min(30, delay * 2)
        if node is None:
            sleep(delay)

    while node["state"] not in TERMINAL:
        try:
            query = urllib.parse.urlencode(
                {
                    "after_revision": node["revision"],
                    "wait_seconds": 5,
                    "bridge_instance_id": bridge_instance,
                }
            )
            node = json.loads(call("GET", "/nodes/" + identity + "?" + query))
            delay = 1
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP:
                raise RuntimeError("Workflow node query rejected") from None
            delay = min(30, delay * 2)
        except (OSError, TimeoutError):
            delay = min(30, delay * 2)
        if delay > 1:
            sleep(delay)
    return node


def engine_job_id(proc_root=Path("/proc"), pid=None):
    """Best-effort Toil diagnostic; platform identity never depends on it."""
    pid = os.getppid() if pid is None else pid
    seen = set()
    for _ in range(16):
        if type(pid) is not int or pid <= 1 or pid in seen:
            break
        seen.add(pid)
        try:
            args = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
            for index, arg in enumerate(args):
                if arg.rsplit(b"/", 1)[-1] != b"_toil_worker":
                    continue
                values = args[index + 2 : index + 4]
                if len(values) != 2 or values[0] != b"file:/run/work/jobstore":
                    return None
                job = values[1].decode()
                return job if job else None
            fields = (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            pid = int(fields[1])
        except (IndexError, OSError, UnicodeError, ValueError):
            return None
    return None


def node_identity(execution_id, call_path, scatter_indices):
    value = json.dumps([execution_id, call_path, scatter_indices], separators=(",", ":"))
    return "node_" + hashlib.sha256(value.encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    config = json.loads(Path("/run/config.json").read_text())
    bridge_started = time.monotonic_ns()
    bridge_instance = secrets.token_hex(16)
    values = json.loads(Path(args.request).read_text())
    context = json.loads(values.pop("v7_context"))
    # The flat compiler uses this trusted value only for the CWL `when`
    # decision.  It is not a scientific operator input and must not alter the
    # platform node request or its frozen input digest.
    values.pop("v7_run_condition", None)

    def freeze(value):
        if isinstance(value, dict) and value.get("class") == "File":
            content = Path(value["path"]).read_bytes()
            return {"class": "File", "digest": "sha256:" + hashlib.sha256(content).hexdigest()}
        if isinstance(value, dict):
            return {key: freeze(item) for key, item in value.items()}
        if isinstance(value, list):
            return [freeze(item) for item in value]
        return value

    binding = {"kind": "toil", "id": engine_job_id(), "epoch": config["epoch"]}
    request = {
        "call_path": json.loads(args.path),
        "scatter_indices": context,
        "inputs": freeze(values),
        "engine_binding": binding,
        "epoch": config["epoch"],
        "trace": {
            "bridge_instance_id": bridge_instance,
            "started_monotonic_ns": bridge_started,
        },
    }
    identity = node_identity(config["execution_id"], request["call_path"], context)
    base = config["execution_url"] + "/internal/workflows/" + config["execution_id"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + config["token"],
                "Content-Type": "application/json",
                **(
                    {"X-V7-Release-Digest": config["release_digest"]}
                    if config.get("release_digest")
                    else {}
                ),
            },
        )
        with opener.open(req, timeout=8) as response:
            return response.read()

    node = await_terminal(call, identity, request, bridge_instance)
    if node["state"] != "SUCCEEDED":
        raise RuntimeError("Computation did not succeed")
    outputs = {}
    for item in node["files"]:
        content = call("GET", "/nodes/" + identity + "/files/" + item["representation_id"])
        if "sha256:" + hashlib.sha256(content).hexdigest() != item["digest"]:
            raise RuntimeError("Node output digest mismatch")
        path = Path(identity + "--" + item["representation_id"])
        path.write_bytes(content)
        outputs[item["representation_id"]] = {
            "class": "File",
            "path": str(path.resolve()),
        }
    Path("cwl.output.json").write_text(json.dumps(outputs))
    try:
        call(
            "POST",
            "/nodes/" + identity + "/trace",
            {
                "event_type": "BRIDGE_RETURNED",
                "bridge_instance_id": bridge_instance,
                "source_monotonic_ns": time.monotonic_ns(),
                "revision": node["revision"],
            },
        )
    except (OSError, TimeoutError, urllib.error.HTTPError):
        # Diagnostics cannot change an already verified computation result.
        pass


if __name__ == "__main__":
    main()
