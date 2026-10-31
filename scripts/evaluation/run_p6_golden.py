"""Execute the exact tests frozen by the P6 golden evaluation manifest."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", required=True)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    suite = json.loads((root / "evaluations/p6/golden_tasks.json").read_text())
    nodeids = list(dict.fromkeys(task["nodeid"] for task in suite["tasks"]))
    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be between 1 and 10")
    base = Path(args.junit)
    failures = []
    for repeat in range(1, args.repeats + 1):
        destination = (
            base if args.repeats == 1 else base.with_name(f"{base.stem}-r{repeat}{base.suffix}")
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                *nodeids,
                "--tb=short",
                f"--junitxml={destination}",
            ],
            cwd=root,
            check=False,
        )
        if result.returncode:
            failures.append(repeat)
    if failures:
        raise SystemExit(f"Golden evaluation failed in repetitions: {failures}")


if __name__ == "__main__":
    main()
