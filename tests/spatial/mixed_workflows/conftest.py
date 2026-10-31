import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.platform.database.conftest import database, owner, postgres  # noqa: F401
from tests.platform.database.processes import eventually
from tests.platform.services.conftest import service_databases  # noqa: F401
from tests.spatial.remote.conftest import remote_cluster, remote_peer  # noqa: F401


@pytest.fixture
def fault_remote_cluster(remote_cluster):  # noqa: F811
    cluster, peer = remote_cluster
    directory = cluster.directory / "p432-fault-proxy"
    directory.mkdir()
    certificate, key = directory / "proxy.crt", directory / "proxy.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "2",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        ],
        check=True,
        capture_output=True,
    )
    config, ready = directory / "config.json", directory / "ready.json"
    config.write_text("{}")
    cluster.start(
        [
            sys.executable,
            str(Path(__file__).with_name("fault_proxy.py")),
            "--upstream",
            peer[0],
            "--upstream-ca",
            str(peer[3]),
            "--certificate",
            str(certificate),
            "--key",
            str(key),
            "--config",
            str(config),
            "--ready",
            str(ready),
        ]
    )
    eventually(ready.exists)
    port = json.loads(ready.read_text())["port"]
    shutil.copyfile(certificate, Path(cluster.env["V7_REMOTE_CA_DIR"]) / "p43-test-ca")

    def configure(**faults):
        for marker in directory.glob("*-blocked"):
            marker.unlink()
        for marker in directory.glob("*-release"):
            marker.unlink()
        temporary = config.with_suffix(".tmp")
        temporary.write_text(json.dumps(faults))
        temporary.replace(config)

    configure.directory = directory

    proxied = (f"https://127.0.0.1:{port}", peer[1], "fault-proxy", certificate, peer[4])
    yield cluster, proxied, configure, peer
