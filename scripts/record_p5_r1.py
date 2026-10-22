# ruff: noqa: E501
"""Record P5-R1 / 0.7.0 evidence only after the unified gate passes."""

import hashlib
import json
import platform
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from release_source_manifest import manifest

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.7.0"
REQUIRED_IDENTITY_TESTS = {
    "test_direct_output_is_canonicalized_and_saved_as_exact",
    "test_workflow_output_preserves_call_and_scatter_identity",
    "test_schema_v2_freezes_role_binding_and_explicit_registration",
    "test_verified_plan_rejects_missing_or_wrong_explicit_registration[None]",
    "test_verified_plan_rejects_missing_or_wrong_explicit_registration[wrong-registration]",
    "test_0008_upgrade_marks_existing_deliverables_legacy_and_is_repeatable",
    "test_new_verified_task_rejects_legacy_output_identity",
}


def sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def suite(path):
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    result = {
        "tests": len(cases),
        "failures": len(root.findall(".//failure")),
        "errors": len(root.findall(".//error")),
        "skipped": len(root.findall(".//skipped")),
        "seconds": sum(float(case.attrib.get("time", 0)) for case in cases),
    }
    return result, {case.attrib["name"] for case in cases}


def clean(result, minimum):
    return (
        result["tests"] >= minimum
        and result["failures"] == result["errors"] == result["skipped"] == 0
    )


def main():
    verification = ROOT / ".p5-runtime/verification"
    release = ROOT / ".p5-runtime/release"
    focused, names = suite(verification / "p5-focused-tests.xml")
    full, _ = suite(verification / "tests.xml")
    response_loss, _ = suite(verification / "response-loss.xml")
    if not clean(focused, 60) or not REQUIRED_IDENTITY_TESTS <= names:
        raise RuntimeError("P5-R1 focused gate is incomplete")
    if not clean(full, 631):
        raise RuntimeError("P5-R1 full regression gate is incomplete")
    if not clean(response_loss, 1):
        raise RuntimeError("HTTP response-loss gate is incomplete")
    p1 = (verification / "p1-rules.log").read_text()
    if "Ran 13 tests" not in p1 or "OK" not in p1:
        raise RuntimeError("P1 selection rules did not pass")
    checker = (verification / "p53-checker.log").read_text().strip()
    if "PASS: fixed 0.7.0 checker container" not in checker:
        raise RuntimeError("0.7.0 checker image gate did not pass")
    distribution = (verification / "distribution.log").read_text().splitlines()
    if not any("built 9 wheels and source distributions" in line for line in distribution):
        raise RuntimeError("Repository-external distribution gate did not pass")

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        value["name"]: value["version"]
        for value in lock["package"]
        if value["name"].startswith("groundwater-") and "editable" in value["source"]
    }
    if len(packages) != 9 or set(packages.values()) != {VERSION}:
        raise RuntimeError("All nine workspace packages must be 0.7.0")
    artifacts = sorted((release / "dist").glob("*.whl")) + sorted(
        (release / "dist").glob("*.tar.gz")
    )
    if len(artifacts) != 18 or any(VERSION not in value.name for value in artifacts):
        raise RuntimeError("0.7.0 release artifacts are incomplete")
    frozen = json.loads((release / "source-manifest.json").read_text())
    current = manifest()
    if frozen != current:
        raise RuntimeError("Source changed after artifacts were built")
    source_digest = current["source_digest"]
    image = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", "groundwater-v7-checker:0.7.0"], text=True
        )
    )[0]
    labels = image["Config"]["Labels"]
    if labels.get("org.opencontainers.image.version") != VERSION:
        raise RuntimeError("Checker version label differs")
    if labels.get("org.opencontainers.image.revision") != source_digest:
        raise RuntimeError("Checker source label differs")

    source_path = ROOT / "docs/verification/p5-r1-source-manifest.json"
    source_path.write_text(json.dumps(current, indent=2) + "\n")
    evidence = {
        "phase": "P5-R1-exact-output-identity",
        "version": VERSION,
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "command": "bash scripts/check_p5.sh --phase all",
        "focused_pytest": focused,
        "full_regression": full,
        "response_loss_http": response_loss,
        "p1_rules": {"tests": 13, "failures": 0, "errors": 0, "skipped": 0},
        "packages": packages,
        "release_artifacts": {
            value.name: {"bytes": value.stat().st_size, "sha256": sha256(value)}
            for value in artifacts
        },
        "source": {
            "digest": source_digest,
            "file_count": current["file_count"],
            "manifest": "docs/verification/p5-r1-source-manifest.json",
        },
        "checker_image": {
            "tag": "groundwater-v7-checker:0.7.0",
            "id": image["Id"],
            "source_digest_label": labels["org.opencontainers.image.revision"],
        },
        "schemas": {
            "platform": "0009_output_identity",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0002_lifecycle_outbox",
        },
        "python": platform.python_version(),
        "identity_cases": sorted(REQUIRED_IDENTITY_TESTS),
        "limits": [
            "P5-R2 checker capability composition is not implemented in this release.",
            "Legacy VERIFIED task specifications remain readable but cannot start new acceptance.",
        ],
    }
    output = ROOT / "docs/verification/p5-r1-release-evidence.json"
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    report = f"""# P5-R1 / 0.7.0 精确成果身份发行报告

状态：**PASS**  
完成时间：{evidence["finished_at"]}

## 完成内容

- TaskSpec 使用成果角色描述未来交付，Deliverable 将角色绑定到精确 Action、Execution、工作流节点、调用路径、scatter/数组位置、AssetRef、表示及摘要。
- Platform 重新查询执行、节点、发布回执和 Data manifest 后保存规范化引用；调用者提供的派生身份不能直接成为权威事实。
- VERIFIED 验收显式冻结当前分析登记。旧 `output_id` VERIFIED 记录保持可读，但不能发起新验收。
- CoveragePlan schema v2 冻结角色映射、精确输出和登记；旧 EXPLORATORY 报告明确包含 `LEGACY_OUTPUT_IDENTITY`。
- 新发布表示写入 `schema_id`；旧 P3/P4 资产通过固定兼容目录解析且不重写。

## 测试结果

- P5 聚焦测试：{focused["tests"]} passed，0 failed，0 skipped。
- 完整 pytest：{full["tests"]} passed，0 failed，0 skipped。
- HTTP 响应丢失：{response_loss["tests"]} passed。
- P1 规则：13 passed。
- 九个 wheel、九个 sdist、仓库外安装、真实 PostgreSQL 迁移和固定 checker 容器通过。

## 范围

本轮只完成 P5-R1。领域复算、空间检查、制图检查、结构化主张检查和多 checker 组合属于 P5-R2 / 0.7.1，未提前实现。
"""
    (ROOT / "docs/verification/p5-r1-release-report.md").write_text(report)
    print("PASS: wrote P5-R1 release evidence and report")


if __name__ == "__main__":
    main()
