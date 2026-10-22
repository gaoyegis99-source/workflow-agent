import json
import os
import secrets
import shutil
import ssl
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from tests.p2.conftest import database, owner, postgres  # noqa: F401
from tests.p2.processes import eventually
from tests.p3.conftest import service_databases  # noqa: F401
from tests.p3.harness import free_port


@pytest.fixture(scope="session")
def remote_peer():
    configured = os.environ.get("V7_P43_REMOTE_URL")
    if configured:
        token_file = Path(os.environ["V7_P43_REMOTE_TOKEN_FILE"])
        token = token_file.read_text().strip()
        certificate = Path(os.environ["V7_P43_REMOTE_CA_FILE"])
        command = json.loads(os.environ.get("V7_P43_REMOTE_RESTART_COMMAND_JSON", "null"))
        controller = None
        if command is not None:
            if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
                raise ValueError("V7_P43_REMOTE_RESTART_COMMAND_JSON must be a JSON argument list")

            def restart():
                subprocess.run(command, check=True, timeout=30)
                with httpx.Client(
                    trust_env=False,
                    verify=ssl.create_default_context(cafile=str(certificate)),
                ) as probe:

                    def ready():
                        try:
                            return (
                                probe.get(
                                    configured.rstrip("/") + "/v1/actions/probe",
                                    headers={"Authorization": "Bearer " + token},
                                    timeout=1,
                                ).status_code
                                == 404
                            )
                        except httpx.HTTPError:
                            return False

                    eventually(ready, timeout=20)

            controller = {"restart": restart}
        yield (
            configured.rstrip("/"),
            token,
            "configured-real-host",
            certificate,
            os.environ["V7_P43_REMOTE_IMPLEMENTATION_DIGEST"],
            controller,
        )
        return
    port = free_port()
    name = "v7-p43-peer-" + uuid.uuid4().hex[:12]
    token = secrets.token_urlsafe(36)
    root = Path(".p4-runtime/remote/test-state") / name
    root.mkdir(parents=True)
    job_image = Path(".p4-runtime/remote/job-image.txt")
    images = json.loads(Path(".p4-runtime/spatial/images.json").read_text())["images"]
    image = job_image.read_text().strip() if job_image.exists() else images["native"]
    certificate, key = root / "peer.crt", root / "peer.key"
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
    env = os.environ | {
        "V7_REMOTE_TOKEN": token,
        "V7_REMOTE_ROOT": str(root.resolve()),
        "V7_REMOTE_HOST": "127.0.0.1",
        "PORT": str(port),
        "V7_REMOTE_JOB_IMAGE": image,
        "V7_REMOTE_IMPLEMENTATION_DIGEST": image,
        "V7_REMOTE_INSTANCE_ID": name,
        "V7_REMOTE_TLS_CERT": str(certificate.resolve()),
        "V7_REMOTE_TLS_KEY": str(key.resolve()),
    }
    processes = [
        subprocess.Popen(
            ["python", "deploy/p43/remote_peer.py"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    ]
    url = f"https://127.0.0.1:{port}"

    def wait_ready():
        with httpx.Client(
            trust_env=False, verify=ssl.create_default_context(cafile=str(certificate))
        ) as probe:

            def ready():
                try:
                    return (
                        probe.get(
                            url + "/v1/actions/probe",
                            headers={"Authorization": "Bearer " + token},
                            timeout=1,
                        ).status_code
                        == 404
                    )
                except httpx.HTTPError:
                    return False

            eventually(ready, timeout=15)

    def restart():
        processes[0].terminate()
        processes[0].wait(timeout=10)
        processes[0] = subprocess.Popen(
            ["python", "deploy/p43/remote_peer.py"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        wait_ready()

    wait_ready()
    try:
        yield url, token, "local-host-service", certificate, image, {"restart": restart}
    finally:
        processes[0].terminate()
        processes[0].wait(timeout=10)
        containers = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=v7.remote_peer={name}"],
            capture_output=True,
            text=True,
        ).stdout.split()
        if containers:
            subprocess.run(["docker", "rm", "-f", *containers], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def remote_cluster(tmp_path, database, service_databases, remote_peer):  # noqa: F811
    from tests.p3.harness import Cluster

    cluster = Cluster(tmp_path, database, service_databases)
    images = json.loads(Path(".p4-runtime/spatial/images.json").read_text())["images"]
    cluster.env["V7_SPATIAL_QGIS_IMAGE_ID"] = images["qgis"]
    cluster.env["V7_SPATIAL_NATIVE_IMAGE_ID"] = images["native"]
    cluster.env["V7_DATA_SPATIAL_WORK_ROOT"] = str(tmp_path / "spatial-import")
    cluster.env["V7_CWL_IMAGE"] = Path(".p4-runtime/cwl-image.txt").read_text().strip()
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.env["V7_EXECUTION_URL"]
    credential_dir = tmp_path / "remote-credentials"
    credential_dir.mkdir()
    credential = credential_dir / "p43-peer-token"
    credential.write_text(remote_peer[1])
    credential.chmod(0o600)
    cluster.env["V7_REMOTE_CREDENTIAL_DIR"] = str(credential_dir)
    ca_dir = tmp_path / "remote-ca"
    ca_dir.mkdir()
    shutil.copyfile(remote_peer[3], ca_dir / "p43-test-ca")
    cluster.env["V7_REMOTE_CA_DIR"] = str(ca_dir)
    try:
        cluster.start_services()
        cluster.launch("data-spatial-worker")
        yield cluster, remote_peer
    finally:
        cluster.close()
