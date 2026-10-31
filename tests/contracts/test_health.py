from platform_contracts.health import touch_worker_heartbeat


def test_worker_heartbeat_uses_only_the_explicit_composed_path(tmp_path):
    path = tmp_path / "worker.heartbeat"
    touch_worker_heartbeat(None)
    assert not path.exists()

    touch_worker_heartbeat(path)
    assert path.is_file()
