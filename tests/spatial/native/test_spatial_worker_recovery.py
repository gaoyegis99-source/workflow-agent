import json

from platform_data.spatial_worker import SpatialWorker


class Repository:
    def __init__(self, claim):
        self.claim_record = claim
        self.generation = 0
        self.results = []

    def claim(self, owner, lease_seconds, formats):
        self.generation += 1
        return self.claim_record | {"owner": owner, "generation": self.generation}

    def renew(self, claim, lease_seconds):
        return True

    def update_import_runtime(self, claim, **changes):
        runtime = dict(self.claim_record.get("runtime") or {}) | changes
        self.claim_record["runtime"] = runtime
        claim["runtime"] = runtime
        return True

    def import_result(self, claim, result=None, error=None):
        self.results.append((result, error))
        return True


class Blobs:
    def __init__(self, source):
        self.source = source

    def path(self, digest):
        return self.source


class Inspector:
    image_id = "sha256:" + "a" * 64

    def __init__(self, output):
        self.output = output
        self.create_calls = 0
        self.created = False
        self.started = False
        self.removed = []

    def inspect(self, upload_id):
        if not self.created:
            return None
        return {
            "Id": "container-id",
            "State": {
                "StartedAt": "2026-10-31T00:00:00Z" if self.started else "0001-01-01T00:00:00Z",
                "Running": False,
                "ExitCode": 0,
            },
        }

    def create(self, claim, source):
        self.create_calls += 1
        self.created = True
        raise OSError("lost create response")

    def check(self, claim, info):
        return None

    def start(self, container_id):
        self.started = True

    def paths(self, claim):
        return self.output.parent, self.output

    def stop(self, container_id):
        raise AssertionError("completed recovery must not stop the container")

    def remove(self, container_id):
        self.removed.append(container_id)


def test_spatial_worker_reconciles_a_create_after_the_cli_loses_its_response(tmp_path):
    source = tmp_path / "source.gpkg"
    source.write_bytes(b"fixture")
    output = tmp_path / "output"
    output.mkdir()
    descriptor = {
        "kind": "SPATIAL_VECTOR",
        "driver": "GPKG",
        "spatial": {"layers": []},
        "source": {"digest": "sha256:" + "1" * 64, "size": 7},
    }
    (output / "inspection.json").write_text(json.dumps(descriptor), encoding="utf-8")
    claim = {
        "upload_id": "upload-id",
        "project_id": "project-id",
        "payload": {
            "filename": "source.gpkg",
            "format": "geopackage",
            "digest": descriptor["source"]["digest"],
            "size": descriptor["source"]["size"],
        },
        "runtime": {},
    }
    repository = Repository(claim)
    inspector = Inspector(output)
    worker = SpatialWorker.__new__(SpatialWorker)
    worker.repo = repository
    worker.blobs = Blobs(source)
    worker.inspector = inspector
    worker.owner = "worker-id"
    worker.lease_seconds = 30
    worker.heartbeat_seconds = 10
    worker.heartbeat_file = None

    assert worker.tick() is True
    assert claim["runtime"]["phase"] == "CREATE_INTENT"
    assert repository.results == []
    assert inspector.removed == []

    assert worker.tick() is True
    assert inspector.create_calls == 1
    assert inspector.started is True
    assert repository.results[0][0]["asset_id"] == "upload-id"
    assert repository.results[0][1] is None
    assert inspector.removed == ["container-id"]
