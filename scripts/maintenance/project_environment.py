"""Project-local maintenance paths and process environment (standard library only).

Writable roots are validated before any directory creation. This is operational
isolation, not a sandbox against other processes owned by the same OS user.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
from pathlib import Path

PROJECT = "groundwater-v7"
SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = SOURCE_ROOT.parent


class EnvironmentRefusalError(RuntimeError):
    """A maintenance path or Docker target is outside this project."""


EnvironmentRefusal = EnvironmentRefusalError


def owned_path(value: str | Path, allowed: Path) -> Path:
    raw = Path(value).expanduser()
    path = raw if raw.is_absolute() else PROJECT_ROOT / raw
    if ".." in path.parts:
        raise EnvironmentRefusal("PATH_ESCAPE_REJECTED")
    allowed = allowed.absolute()
    if not path.is_relative_to(allowed):
        raise EnvironmentRefusal("PATH_OUTSIDE_OWNED_ROOT")
    current = PROJECT_ROOT
    if not path.is_relative_to(current):
        raise EnvironmentRefusal("PATH_OUTSIDE_PROJECT")
    for part in path.relative_to(current).parts:
        current /= part
        if current.is_symlink():
            raise EnvironmentRefusal("WRITABLE_PATH_SYMLINK_REJECTED")
    return path


def layout() -> dict:
    path = PROJECT_ROOT / "project-layout.json"
    if not path.exists():
        raise EnvironmentRefusal("PROJECT_NOT_BOOTSTRAPPED")
    if path.is_symlink():
        raise EnvironmentRefusal("LAYOUT_SYMLINK_REJECTED")
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise EnvironmentRefusal("LAYOUT_INVALID") from error
    if (
        not isinstance(result, dict)
        or result.get("project") != PROJECT
        or result.get("source") != "source"
        or result.get("schema_version") != 2
        or not isinstance(result.get("runtime"), dict)
    ):
        raise EnvironmentRefusal("LAYOUT_INVALID")
    return result


def socket_path() -> Path:
    expected = Path(f"/run/user/{os.getuid()}/{PROJECT}-docker/docker.sock")
    configured = layout()["runtime"].get("docker_socket", str(expected))
    if configured != str(expected):
        raise EnvironmentRefusal("DOCKER_SOCKET_OUTSIDE_PROJECT")
    for parent in [expected, *expected.parents[:-1]]:
        if parent.is_symlink():
            raise EnvironmentRefusal("DOCKER_SOCKET_SYMLINK_REJECTED")
    return expected


def require_socket() -> Path:
    path = socket_path()
    try:
        info = path.stat()
    except OSError as error:
        raise EnvironmentRefusal("PROJECT_DOCKER_UNAVAILABLE") from error
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise EnvironmentRefusal("PROJECT_DOCKER_UNAVAILABLE")
    return path


def paths(inherited: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if inherited is None else inherited
    contract = layout()["runtime"]
    prefix = "V6" if PROJECT.endswith("v6") else "V7"
    runtime = PROJECT_ROOT / "runtime"
    specs = {
        f"{prefix}_RUNTIME_ROOT": ("root", "runtime", runtime),
        f"{prefix}_RUNTIME_WORK_ROOT": ("work", "runtime/work", runtime / "work"),
        f"{prefix}_RUNTIME_ACTIVE_ROOT": ("active", "runtime/active", runtime / "active"),
        f"{prefix}_RUNTIME_ARCHIVE_ROOT": ("archive", "runtime/archive", runtime / "archive"),
        f"{prefix}_RUNTIME_TOOLCHAIN_ROOT": (
            "toolchain",
            "runtime/toolchain",
            runtime / "toolchain",
        ),
        f"{prefix}_DOCKER_DATA_ROOT": ("docker", "runtime/docker", runtime / "docker"),
    }
    if prefix == "V7":
        specs.update(
            {
                f"V7_P{i}_RUNTIME_ROOT": (f"p{i}", f"runtime/work/p{i}", runtime / "work")
                for i in range(1, 8)
            }
        )
        specs["V7_P8_RUNTIME_ROOT"] = ("p8", "runtime/p8", runtime / "p8")
    else:
        specs["GROUNDWATER_GENERATED_ROOT"] = ("work_v6", "runtime/work/v6", runtime / "work")
    result = {
        key: str(owned_path(env.get(key) or contract.get(field, default), allowed))
        for key, (field, default, allowed) in specs.items()
    }
    # The main root and P8 root are not configurable deployment destinations.
    if result[f"{prefix}_RUNTIME_ROOT"] != str(runtime):
        raise EnvironmentRefusal("RUNTIME_ROOT_MUST_BE_PROJECT_RUNTIME")
    if prefix == "V7" and result["V7_P8_RUNTIME_ROOT"] != str(runtime / "p8"):
        raise EnvironmentRefusal("P8_RUNTIME_ROOT_MUST_NOT_CHANGE")
    result[f"{prefix}_SOURCE_ROOT"] = str(SOURCE_ROOT)
    result[f"{prefix}_PROJECT_ROOT"] = str(PROJECT_ROOT)
    toolchain = owned_path(
        env.get(f"{prefix}_TOOLCHAIN_ENVIRONMENT", f"runtime/toolchain/{prefix.lower()}"),
        runtime / "toolchain",
    )
    result[f"{prefix}_TOOLCHAIN_ENVIRONMENT"] = str(toolchain)
    result["UV_PROJECT_ENVIRONMENT"] = str(
        owned_path(env.get("UV_PROJECT_ENVIRONMENT", str(toolchain)), runtime / "toolchain")
    )
    # A uv venv's interpreter normally links to its base interpreter. Validate
    # its owning environment, not the base executable's resolved location.
    interpreter = Path(env.get(f"{prefix}_PYTHON", str(toolchain / "bin/python")))
    owned_path(interpreter.parent, runtime / "toolchain")
    if interpreter.name not in {"python", "python3", "python3.12"}:
        raise EnvironmentRefusal("PROJECT_PYTHON_INVALID")
    result[f"{prefix}_PYTHON"] = str(interpreter)
    if prefix == "V7":
        result["V7_P1_TOOLCHAIN_ENVIRONMENT"] = str(
            owned_path(
                env.get("V7_P1_TOOLCHAIN_ENVIRONMENT", "runtime/toolchain/p1-toil"),
                runtime / "toolchain",
            )
        )
    return result


def sensitive_test_variable(name: str) -> bool:
    return name.startswith(
        ("V6_", "V7_", "PLATFORM_", "DATA_", "KNOWLEDGE_", "EXECUTOR_", "POSTGRES_", "MINIO_")
    ) or name.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_DATABASE_URL", "_DSN"))


def child_environment(
    *, testing: bool = False, inherited: dict[str, str] | None = None
) -> dict[str, str]:
    original = dict(os.environ if inherited is None else inherited)
    resolved = paths(original)
    result = {
        key: value
        for key, value in original.items()
        if key
        not in {
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "PYTHONHOME",
            "PYTHONPATH",
            "UV_PROJECT",
            "UV_WORKING_DIRECTORY",
            "UV_ACTIVE",
        }
        and not (testing and sensitive_test_variable(key))
    }
    result.update(resolved)
    result["PYTHONPATH"] = str(SOURCE_ROOT)
    result["VIRTUAL_ENV"] = result["UV_PROJECT_ENVIRONMENT"]
    result["DOCKER_HOST"] = f"unix://{socket_path()}"
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    result["UV_LINK_MODE"] = "copy"
    result["PATH"] = os.pathsep.join(
        (
            str(PROJECT_ROOT / "bin"),
            str(Path(result["UV_PROJECT_ENVIRONMENT"]) / "bin"),
            original.get("PATH", os.defpath),
        )
    )
    return result


def activate(*, testing: bool = False) -> None:
    resolved = child_environment(testing=testing)
    for key in tuple(os.environ):
        if key not in resolved:
            del os.environ[key]
    os.environ.update(resolved)


def docker_executable() -> str:
    """Absolute launcher; also safe when a Python helper is invoked directly."""
    socket_path()
    return str(PROJECT_ROOT / "bin/docker")


def docker_arguments(arguments: list[str]) -> list[str]:
    # Long-form target overrides (and ``-H``) are unambiguous, so reject them
    # wherever they appear.  The short ``-c`` spelling is different: after the
    # Docker subcommand it can be a legitimate run option or container command
    # (for example ``docker run IMAGE /bin/sh -c ...``).  Only interpret it as
    # Docker's global context option while parsing the global option prefix.
    for arg in arguments:
        if (
            arg in {"-H", "--host", "--context"}
            or arg.startswith(("--host=", "--context="))
            or arg.startswith("-H")
        ):
            raise EnvironmentRefusal("DOCKER_TARGET_OVERRIDE_REJECTED")

    global_options_with_value = {
        "--config",
        "--log-level",
        "--tlscacert",
        "--tlscert",
        "--tlskey",
        "-l",
    }
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg == "--":
            index += 1
            break
        if arg == "-c" or (arg.startswith("-c") and not arg.startswith("--")):
            raise EnvironmentRefusal("DOCKER_TARGET_OVERRIDE_REJECTED")
        if not arg.startswith("-"):
            break
        if arg in global_options_with_value:
            index += 2
        else:
            index += 1
    return ["/usr/bin/docker", *arguments]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("shell", "docker"))
    parser.add_argument("--testing", action="store_true")
    args, rest = parser.parse_known_args()
    try:
        if args.action == "docker":
            command = docker_arguments(rest)
            require_socket()
            os.execve(command[0], command, child_environment())
        if rest:
            parser.error("unexpected arguments")
        result = child_environment(testing=args.testing)
        for key in sorted(set(os.environ) - set(result)):
            print("unset " + shlex.quote(key))
        keys = set(paths()) | {
            "DOCKER_HOST",
            "PATH",
            "PYTHONDONTWRITEBYTECODE",
            "UV_LINK_MODE",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        }
        for key in sorted(keys):
            print(f"export {key}={shlex.quote(result[key])}")
    except EnvironmentRefusal as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(75) from error


if __name__ == "__main__":
    main()
