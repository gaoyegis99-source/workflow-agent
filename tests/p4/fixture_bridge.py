"""Trusted compiler fixture, never installed in a production image or catalog."""

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--path")
parser.add_argument("--request")
args = parser.parse_args()
value = json.loads(Path(args.request).read_text())
result = {
    "value": value["value"] + value.get("other", 0),
    "identity": json.dumps([json.loads(args.path), json.loads(value["v7_context"])]),
}
Path("cwl.output.json").write_text(json.dumps(result))
