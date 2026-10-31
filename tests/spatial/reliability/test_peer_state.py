import importlib.util
import json
import stat
import sys


def load_peer(tmp_path, monkeypatch):
    monkeypatch.setenv("V7_REMOTE_ROOT", str(tmp_path))
    monkeypatch.setenv("V7_REMOTE_TOKEN", "x" * 32)
    monkeypatch.setenv("V7_REMOTE_INSTANCE_ID", "unit")
    name = "v7_remote_peer_unit_" + tmp_path.name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, "deploy/spatial/remote/remote_peer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def add_action(peer, action="action"):
    request = {
        "schema_version": 2,
        "action_id": action,
        "parameters": {"layer_id": "l0"},
        "limits": {"timeout_seconds": 30},
    }
    with peer.connect() as db:
        db.execute(
            "INSERT INTO actions (action_id,request_digest,remote_execution_id,revision,state,"
            "executed,request,cancel_requested,schema_version,container_name,"
            "implementation_digest,cleanup_state) VALUES (?,?,?,1,'QUEUED',0,?,0,2,?,?,"
            "'NOT_REQUIRED')",
            (
                action,
                "sha256:" + "1" * 64,
                "remote",
                json.dumps(request),
                peer.container_name(action),
                "sha256:" + "2" * 64,
            ),
        )


def test_cancelled_queue_cannot_be_claimed_or_started(tmp_path, monkeypatch):
    peer = load_peer(tmp_path, monkeypatch)
    add_action(peer)
    with peer.connect() as db:
        db.execute(
            "UPDATE actions SET cancel_requested=1,state='CANCELLED' WHERE action_id='action'"
        )
    assert peer.claim("action") is False
    assert peer.advance("action") is False


def test_cancel_after_claim_is_seen_before_docker_start(tmp_path, monkeypatch):
    peer = load_peer(tmp_path, monkeypatch)
    add_action(peer)
    assert peer.claim("action") is True
    created = []

    def create(action, row):
        created.append(action)
        with peer.connect() as db:
            db.execute(
                "UPDATE actions SET cancel_requested=1,cleanup_state='PENDING' WHERE action_id=?",
                (action,),
            )
        return {"Id": "container", "State": {"StartedAt": "0001-01-01", "Running": False}}

    monkeypatch.setattr(peer, "inspect", lambda name: None)
    monkeypatch.setattr(peer, "create_job", create)
    monkeypatch.setattr(
        peer,
        "docker",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("docker start called")),
    )
    assert peer.advance("action") is True
    assert peer.advance("action") is False
    row = peer.get("action")
    assert created == ["action"]
    assert row["state"] == "CANCELLED" and not row["executed"]


def test_inspect_outage_is_not_treated_as_missing(monkeypatch, tmp_path):
    peer = load_peer(tmp_path, monkeypatch)

    class Result:
        returncode = 1
        stderr = "Cannot connect to the Docker daemon"
        stdout = ""

    monkeypatch.setattr(peer, "docker", lambda *args, **kwargs: Result())
    try:
        peer.inspect("container")
    except OSError:
        pass
    else:
        raise AssertionError("Docker outage was treated as a missing container")


def test_worker_retries_transient_docker_outage(monkeypatch, tmp_path):
    peer = load_peer(tmp_path, monkeypatch)
    calls = []

    def advance(action):
        calls.append(action)
        if len(calls) == 1:
            raise OSError("Docker inspect is unavailable")
        return False

    monkeypatch.setattr(peer, "advance", advance)
    monkeypatch.setattr(peer.time, "sleep", lambda seconds: None)
    peer.THREADS.add("action")
    peer.worker("action")
    assert calls == ["action", "action"]
    assert "action" not in peer.THREADS


def test_job_output_mount_is_writable_only_until_sealed(monkeypatch, tmp_path):
    peer = load_peer(tmp_path, monkeypatch)
    add_action(peer)
    assert peer.claim("action") is True

    class Result:
        stderr = ""

    info = {
        "Id": "container",
        "Config": {
            "Labels": {
                "v7.action_id": "action",
                "v7.request_digest": "sha256:" + "1" * 64,
                "v7.implementation_digest": "sha256:" + "2" * 64,
            }
        },
    }
    monkeypatch.setattr(peer, "docker", lambda *args, **kwargs: Result())
    monkeypatch.setattr(peer, "inspect", lambda name: info)

    peer.create_job("action", peer.get("action"))
    action_root = tmp_path / "action"
    output = action_root / "output"
    assert stat.S_IMODE(action_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o777
    assert peer.seal_output("action") is True
    assert stat.S_IMODE(output.stat().st_mode) == 0o700


def test_file_digest_streams_regular_file(monkeypatch, tmp_path):
    peer = load_peer(tmp_path, monkeypatch)
    path = tmp_path / "large.bin"
    path.write_bytes(b"abcdef" * 1024 * 1024)
    assert peer.file_digest(path) == peer.digest(path.read_bytes())
