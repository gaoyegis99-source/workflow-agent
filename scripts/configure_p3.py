"""Generate a new, local P3 installation configuration. Never overwrite existing credentials."""

import json
import os
import secrets
import shlex
from pathlib import Path

root = Path(__file__).resolve().parents[1]
runtime = root / ".p3-runtime/local"
runtime.mkdir(parents=True, exist_ok=True)
images = json.loads((root / ".p3-runtime/images.json").read_text())
config = {
    name: secrets.token_hex(32)
    for name in [
        "V7_P3_PG_PASSWORD",
        "V7_PLATFORM_DB_PASSWORD",
        "V7_DATA_DB_PASSWORD",
        "V7_EXECUTION_DB_PASSWORD",
        "V7_PLATFORM_DATA_TOKEN",
        "V7_PLATFORM_EXECUTION_TOKEN",
        "V7_EXECUTION_DATA_TOKEN",
        "V7_EXECUTION_PLATFORM_TOKEN",
    ]
}
for service in ["PLATFORM", "DATA", "EXECUTION"]:
    key = "V7_DATABASE_URL" if service == "PLATFORM" else f"V7_{service}_DATABASE_URL"
    config[key] = (
        f"postgresql+psycopg://v7_{service.lower()}:{config[f'V7_{service}_DB_PASSWORD']}"
        f"@127.0.0.1:55437/v7_{service.lower()}"
    )
for service, port in [("PLATFORM", 18070), ("DATA", 18071), ("EXECUTION", 18072)]:
    config[f"V7_{service}_URL"] = f"http://127.0.0.1:{port}"
    config[f"V7_{service}_PORT"] = str(port)
config.update(
    V7_DATA_STORAGE=str(runtime / "assets"),
    V7_EXECUTION_WORKDIR=str(runtime / "work"),
    V7_OPERATOR_IMAGE=images["operator"],
    V7_CHECKER_IMAGE=images["checker"],
)
path = runtime / "p3.env"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write("".join(key + "=" + shlex.quote(value) + "\n" for key, value in config.items()))
print("Created .p3-runtime/local/p3.env (0600); no service was started and no migration was run.")
