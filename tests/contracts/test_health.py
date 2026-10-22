from platform_contracts.health import touch_worker_heartbeat


def test_worker_heartbeat_is_opt_in_and_updates_mtime(monkeypatch, tmp_path):
    path = tmp_path / "worker.heartbeat"
    monkeypatch.delenv("V7_WORKER_HEARTBEAT_FILE", raising=False)
    touch_worker_heartbeat()
    assert not path.exists()

    monkeypatch.setenv("V7_WORKER_HEARTBEAT_FILE", str(path))
    touch_worker_heartbeat()
    assert path.is_file()
