"""Test a private gVisor OCI runtime without reconfiguring the host Docker daemon.

The trusted outer runtime needs SYS_ADMIN/SYS_PTRACE and relaxed host profiles.
Guest code has no capabilities, host mounts, Docker socket, or network access.
This is a tested deployment prerequisite, not a rootless production claim.
"""

import argparse
import copy
import hashlib
import json
import os
import posixpath
import select
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.runtime_layout import P1_RUNTIME_ROOT  # noqa: E402
from scripts.maintenance.project_environment import docker_executable  # noqa: E402

PINS = json.loads((HERE / "pins.json").read_text())


def docker_command(*args: str) -> list[str]:
    """Route every host-side P1 container action through the V7 launcher."""

    return [docker_executable(), *args]


def run(args: list[str], *, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{args}: {result.returncode}\n{result.stderr[-4000:]}")
    return result


def container_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    member = copy.copy(member)
    if member.issym() and member.linkname.startswith("/"):
        member.linkname = posixpath.relpath(
            member.linkname.lstrip("/"), posixpath.dirname(member.name)
        )
    return tarfile.data_filter(member, destination)


def prepare_tools(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "gvisor.tar.bz2"
    if not archive.exists():
        with urllib.request.urlopen(PINS["gvisor"]["url"], timeout=120) as response:
            archive.write_bytes(response.read())
    assert hashlib.sha512(archive.read_bytes()).hexdigest() == PINS["gvisor"]["sha512"]
    binary = cache / "bin/runsc"
    if not binary.exists():
        with tarfile.open(archive) as tar:
            tar.extractall(cache / "bin", filter="data")
    assert PINS["gvisor"]["version"] in run([str(binary), "--version"]).stdout
    return binary


def prepare_bundle(directory: Path, runsc: Path, script: str) -> Path:
    directory.mkdir(parents=True)
    rootfs = directory / "rootfs"
    rootfs.mkdir()
    container = "v7-p1-rootfs-" + uuid.uuid4().hex[:12]
    try:
        run(
            [
                *docker_command(),
                "create",
                "--name",
                container,
                "--label",
                "v7.phase=P1",
                PINS["alpine"],
                "true",
            ]
        )
        archive = directory / "rootfs.tar"
        run(docker_command("export", "-o", str(archive), container))
        with tarfile.open(archive) as tar:
            tar.extractall(rootfs, filter=container_filter)
        archive.unlink()
    finally:
        run(docker_command("rm", "-f", container), check=False)
    (rootfs / "input.txt").write_text("immutable-input\n")
    (rootfs / "input.txt").chmod(0o666)
    (rootfs / "tmp").mkdir(exist_ok=True)
    run([str(runsc), "spec", "--bundle", str(directory), "--", "/bin/sh", "-ec", script])
    config_file = directory / "config.json"
    config = json.loads(config_file.read_text())
    config["root"]["readonly"] = True
    config["process"]["terminal"] = False
    config["process"]["user"] = {"uid": 65534, "gid": 65534}
    config["process"]["capabilities"] = {
        key: [] for key in ["bounding", "effective", "inheritable", "permitted", "ambient"]
    }
    config["process"]["noNewPrivileges"] = True
    config["mounts"].append(
        {
            "destination": "/tmp",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "nodev", "mode=1777", "size=16777216"],
        }
    )
    config_file.write_text(json.dumps(config, indent=2))
    return directory


def command(name: str, bundle: Path, tools: Path) -> list[str]:
    return [
        *docker_command(),
        "run",
        "--name",
        name,
        "--label",
        "v7.phase=P1",
        "--network",
        "none",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m",
        "--cap-add",
        "SYS_ADMIN",
        "--cap-add",
        "SYS_PTRACE",
        "--security-opt",
        "seccomp=unconfined",
        "--security-opt",
        "apparmor=unconfined",
        "--mount",
        f"type=bind,src={tools},dst=/tools,readonly",
        "--mount",
        f"type=bind,src={bundle},dst=/bundle,readonly",
        PINS["alpine"],
        "/tools/runsc",
        "--root=/tmp/runsc",
        "--ignore-cgroups",
        "--platform=systrap",
        "--network=none",
        "run",
        "--bundle=/bundle",
        "guest",
    ]


def probe_rootless_do(runsc: Path) -> dict:
    result = run(
        [str(runsc), "--rootless", "--network=none", "--platform=systrap", "do", "/bin/true"],
        check=False,
        timeout=30,
    )
    return {
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "returncode": result.returncode,
        "stderr": result.stderr,
    }


def probe_rootless_oci(root: Path, runsc: Path) -> dict:
    directory = root / "rootless"
    directory.mkdir()
    config = json.loads((root / "boundary/config.json").read_text())
    config["root"]["path"] = str(root / "boundary/rootfs")
    config["process"]["args"] = ["/bin/sh", "-c", "echo READY; sleep 60; echo LATE"]
    config["process"]["user"] = {"uid": 0, "gid": 0}
    config["linux"]["namespaces"].append({"type": "user"})
    config["linux"]["uidMappings"] = [{"containerID": 0, "hostID": os.getuid(), "size": 1}]
    config["linux"]["gidMappings"] = [{"containerID": 0, "hostID": os.getgid(), "size": 1}]
    (directory / "config.json").write_text(json.dumps(config))
    # OCI user mappings use gVisor's native rootless path, not the separate
    # --rootless shortcut intended for `runsc do`. This host has no delegated
    # cgroup tree, so this diagnostic cannot qualify resource enforcement.
    prefix = [
        str(runsc),
        "--root",
        str(root / "rootless-state"),
        "--ignore-cgroups",
        "--network=none",
        "--platform=systrap",
    ]
    name = "v7-p1-rootless-" + uuid.uuid4().hex[:8]
    process = subprocess.Popen(
        prefix + ["run", "--bundle", str(directory), name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            state = run(prefix + ["state", name], check=False, timeout=5)
            if state.returncode == 0 and json.loads(state.stdout)["status"] == "running":
                break
            assert process.poll() is None, "rootless OCI process exited before running"
            assert time.monotonic() < deadline, "rootless OCI did not enter running state"
            time.sleep(0.1)
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 10)[0], "rootless guest did not start"
        ready = process.stdout.readline()
        assert ready == "READY\n", ready
        run(prefix + ["kill", name, "KILL"], timeout=10)
        remaining, stderr = process.communicate(timeout=10)
        stdout = ready + remaining
        assert "LATE" not in stdout, stdout
        assert process.returncode != 0, process.returncode
        return {
            "status": "PASS",
            "run_returncode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "cgroups": "IGNORED: host does not delegate writable cgroups",
        }
    except (AssertionError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {"status": "FAIL", "error": str(error), "cgroups": "IGNORED"}
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
        run(prefix + ["delete", "--force", name], check=False)


def suite(root: Path, cache: Path) -> dict:
    root.mkdir(parents=True)
    runsc = prepare_tools(cache)
    checks = []
    canary = root / "host-canary"
    canary.write_text("private-host-canary")
    # The listener stays open but no accept/read is needed: a successful TCP connect
    # would make nc return successfully and fail the guest's negative assertion.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        script = f"""
test "$(uname -r)" = "4.19.0-gvisor"
test "$(id -u)" = 65534
test "$(cat /input.txt)" = immutable-input
test ! -e '{canary}'
test ! -e /var/run/docker.sock
test ! -e /tools/runsc
if echo altered > /input.txt; then exit 21; fi
if echo bad > /escape; then exit 22; fi
echo permitted > /tmp/output
test "$(cat /tmp/output)" = permitted
grep -q 'lo:' /proc/net/dev
test "$(grep -c : /proc/net/dev)" = 1
if nc -z -w 1 127.0.0.1 {port}; then exit 23; fi
if dd if=/dev/zero of=/tmp/excess bs=1048576 count=24; then exit 24; fi
test "$(stat -c %s /tmp/excess)" -le 16777216
test "$(stat -c %s /tmp/excess)" -gt 15700000
rm /tmp/excess
echo FILE_NETWORK_DISK_PASS
"""
        bundle = prepare_bundle(root / "boundary", runsc, script)
        name = "v7-p1-sandbox-" + uuid.uuid4().hex[:12]
        try:
            result = run(command(name, bundle, runsc.parent), timeout=30)
            assert "FILE_NETWORK_DISK_PASS" in result.stdout
            assert (bundle / "rootfs/input.txt").read_text() == "immutable-input\n"
            assert canary.read_text() == "private-host-canary"
            checks.append(
                {
                    "check": "guest_files_network_and_tmp_quota",
                    "status": "PASS",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
        finally:
            run(docker_command("rm", "-f", name), check=False)

    bundle = prepare_bundle(root / "cancel", runsc, "echo READY; sleep 60; echo LATE_OUTPUT")
    name = "v7-p1-sandbox-" + uuid.uuid4().hex[:12]
    process = subprocess.Popen(
        command(name, bundle, runsc.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while "READY" not in run(docker_command("logs", name), check=False).stdout:
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline
            time.sleep(0.2)
        container_id = run(docker_command("inspect", "--format", "{{.Id}}", name)).stdout.strip()
        run(
            docker_command(
                "exec", name, "/tools/runsc", "--root=/tmp/runsc", "kill", "guest", "KILL"
            )
        )
        stdout, stderr = process.communicate(timeout=15)
        assert "LATE_OUTPUT" not in stdout
        state = json.loads(
            run(docker_command("inspect", "--format", "{{json .State}}", name)).stdout
        )
        assert not state["Running"] and state["ExitCode"] != 0
        checks.append(
            {
                "check": "guest_cancel_and_container_relocate",
                "status": "PASS",
                "container_id": container_id,
                "state": state,
                "stdout": stdout,
                "stderr": stderr,
            }
        )
    finally:
        run(docker_command("rm", "-f", name), check=False)
        process.communicate(timeout=10)
    return {
        "runtime": "gVisor",
        "version": PINS["gvisor"]["version"],
        "status": "PASS",
        "deployment": "dedicated Docker wrapper; SYS_ADMIN/SYS_PTRACE; no daemon changes",
        "checks": checks,
        "rootless_host": {
            "do": probe_rootless_do(runsc),
            "oci_lifecycle": probe_rootless_oci(root, runsc),
        },
    }


def main() -> None:
    if sys.flags.optimize:
        raise RuntimeError("Probe assertions must not be disabled")
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    cache = P1_RUNTIME_ROOT / "gvisor"
    result = suite(args.directory.resolve(), cache)
    (args.directory / "report.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
