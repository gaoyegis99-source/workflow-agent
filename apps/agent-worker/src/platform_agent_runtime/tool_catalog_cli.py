"""Generate or verify a deployment-frozen P6 tool catalogue."""

import argparse
import json

from platform_core.application.tool_catalog import load_tool_catalog, write_tool_catalog

from platform_agent_runtime.composition import AgentToolCatalogComposition


def main():
    composition = AgentToolCatalogComposition.from_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("--output", default=composition.tool_catalog_path)
    parser.add_argument(
        "--stage",
        choices=("readonly", "actions", "science", "p7-actions", "p7-science"),
        default="p7-science",
    )
    args = parser.parse_args()
    if not args.output:
        raise SystemExit("--output or V7_AGENT_TOOL_CATALOG is required")
    catalog = (
        write_tool_catalog(args.output, args.stage)
        if args.command == "generate"
        else load_tool_catalog(args.output)
    )
    print(json.dumps({"catalog_digest": catalog["catalog_digest"], "status": "ok"}))
