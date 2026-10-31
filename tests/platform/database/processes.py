"""Owned subprocesses and bounded condition waits for P2 fault tests."""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .peer_client import PeerClient

HERE = Path(__file__).parent


def _project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "project-layout.json").is_file():
            return candidate
    return Path(__file__).resolve().parents[3]


def _json_file(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _image_id(value: object, fallback: str) -> str:
    return (
        value
        if isinstance(value, str) and value.startswith("sha256:") and len(value) == 71
        else fallback
    )


def _platform_environment(directory: Path, execution_token: str) -> dict[str, str]:
    """Create explicit, process-safe test composition inputs once per harness."""

    from platform_core.application.catalog import build_catalog
    from platform_core.application.checker_catalog import build_catalog as build_checker_catalog

    work = _project_root() / "runtime/work"
    p3 = _json_file(work / "p3/images.json")
    spatial = _json_file(work / "p4/spatial/images.json").get("images", {})
    spatial = spatial if isinstance(spatial, dict) else {}
    checker_lock = _json_file(work / "p5/checker-catalog.json")
    checker_images = checker_lock.get("images", {})
    checker_images = checker_images if isinstance(checker_images, dict) else {}
    fallback = {
        name: "sha256:" + digit * 64
        for name, digit in {
            "operator": "1",
            "checker": "2",
            "gdal_checker": "3",
            "qgis": "4",
            "spatial_native": "5",
            "tabular": "6",
            "cwl": "7",
        }.items()
    }
    tabular_path = work / "p7/tabular/image-id.txt"
    cwl_path = work / "p4/cwl-image.txt"
    images = {
        "operator": _image_id(p3.get("operator"), fallback["operator"]),
        "checker": _image_id(p3.get("checker"), fallback["checker"]),
        "gdal_checker": _image_id(checker_images.get("GDAL_CHECKER"), fallback["gdal_checker"]),
        "qgis": _image_id(spatial.get("qgis"), fallback["qgis"]),
        "spatial_native": _image_id(spatial.get("native"), fallback["spatial_native"]),
        "tabular": _image_id(
            tabular_path.read_text(encoding="utf-8").strip() if tabular_path.is_file() else None,
            fallback["tabular"],
        ),
        "cwl": _image_id(
            cwl_path.read_text(encoding="utf-8").strip() if cwl_path.is_file() else None,
            fallback["cwl"],
        ),
    }
    operator_catalog = build_catalog(
        {
            "OPERATOR": images["operator"],
            "QGIS": images["qgis"],
            "SPATIAL_NATIVE": images["spatial_native"],
            "TABULAR": images["tabular"],
        }
    )
    checker_roles = {
        "CHECKER": images["checker"],
        "GDAL_CHECKER": images["gdal_checker"],
    }
    operator_path = directory / "operator-catalog.json"
    checker_path = directory / "checker-catalog.json"
    operator_path.write_text(json.dumps(operator_catalog), encoding="utf-8")
    checker_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "images": checker_roles,
                "catalog": build_checker_catalog(checker_roles),
            }
        ),
        encoding="utf-8",
    )
    environment = {
        "V7_OPERATOR_CATALOG": str(operator_path),
        "V7_CHECKER_CATALOG": str(checker_path),
        "V7_OPERATOR_IMAGE": images["operator"],
        "V7_CHECKER_IMAGE": images["checker"],
        "V7_GDAL_CHECKER_IMAGE": images["gdal_checker"],
        "V7_SPATIAL_QGIS_IMAGE_ID": images["qgis"],
        "V7_SPATIAL_NATIVE_IMAGE_ID": images["spatial_native"],
        "V7_TABULAR_OPERATOR_IMAGE": images["tabular"],
        "V7_CWL_IMAGE": images["cwl"],
        "V7_EXECUTION_PLATFORM_TOKEN": execution_token,
    }
    studio = work / "capability-studio"
    if (studio / "index.html").is_file() and (studio / "assets").is_dir():
        environment["V7_STUDIO_ROOT"] = str(studio)
    return environment


def eventually(condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Condition did not become true before deadline")


class Processes:
    def __init__(self, directory, database_url, *, peer_directory=None):
        self.directory, self.database_url = directory, database_url
        self.peer_directory = peer_directory or directory
        self.children = []
        self.logs = []
        self.token = secrets.token_urlsafe(32)
        self.platform_environment = _platform_environment(self.directory, self.token)

    def start(self, args, extra=None):
        log = (self.directory / f"process-{len(self.children)}.log").open("wb")
        self.logs.append(log)
        process = subprocess.Popen(
            [str(part) for part in args],
            env=os.environ
            | self.platform_environment
            | {"V7_DATABASE_URL": self.database_url, "P2_PEER_TOKEN": self.token}
            | (extra or {}),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.children.append(process)
        return process

    def peer(self):
        marker = self.directory / f"peer-{len(self.children)}.json"
        process = self.start(
            [
                sys.executable,
                HERE / "peer.py",
                "--database",
                self.peer_directory / "peer.sqlite",
                "--ready-file",
                marker,
            ]
        )
        eventually(marker.exists, timeout=30)
        port = json.loads(marker.read_text())["port"]
        self.client = PeerClient(f"http://127.0.0.1:{port}", self.token)
        return process, self.client

    def worker(self, *, once=False):
        return self.start(
            [sys.executable, HERE / "driver.py", "worker", *(["--once"] if once else [])],
            {"P2_PEER_URL": self.client.base},
        )

    def api(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = self.start([sys.executable, "-m", "platform_api", "--port", str(port)])
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5)

        def ready():
            try:
                return client.get("/api/v1/ready").status_code == 200
            except httpx.TransportError:
                return False

        eventually(ready)
        return process, client

    @staticmethod
    def kill(process):
        process.kill()
        process.wait(timeout=8)

    def close(self):
        for process in reversed(self.children):
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    self.kill(process)
        for log in self.logs:
            log.close()
