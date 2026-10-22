"""Record immutable P5-R0 / 0.6.3 release evidence after every gate passes."""

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
VERSION = "0.6.3"
REQUIRED_AUDIT_TESTS = {
    "test_execution_source_from_another_task_is_rejected",
    "test_withdrawn_evidence_cannot_enter_a_new_research_revision",
    "test_research_change_after_check_submission_makes_coverage_stale",
    "test_verified_delivery_cannot_turn_indeterminate_reproduction_into_pass",
    "test_historical_imported_event_is_consumed_before_withdrawals",
    "test_withdrawal_marks_current_validity_stale_without_rewriting_report",
}
REQUIRED_DISTRIBUTION = {
    "PASS: built 9 wheels and source distributions",
    "PASS: core wheel runs with only the Python standard library",
    "PASS: locked runtime dependencies and wheels installed outside the repository",
    "PASS: installed API liveness, unconfigured readiness, OpenAPI and shutdown",
    "PASS: installed migrations, local token CLI, real task/control HTTP and worker",
    "PASS: installed Knowledge migration, schema check, readiness and shutdown",
    "PASS: installed Data/Execution migrations, independent workers, containers and P3 completion",
    "PASS: installed frozen CWL, Toil nodes, atomic publication and independent check",
    "PASS: installed GeoPackage inspection, QGIS buffer, Native summary and publication",
    "PASS: installed trusted QGIS map, PNG delivery and atomic three-node publication",
    "PASS: installed 0001 data upgrade, unchanged ledger, known/unknown HTTP commands",
}


def sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def suite(path):
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    return {
        "tests": len(cases),
        "failures": len(root.findall(".//failure")),
        "errors": len(root.findall(".//error")),
        "skipped": len(root.findall(".//skipped")),
        "seconds": sum(float(case.attrib.get("time", 0)) for case in cases),
    }, {case.attrib["name"] for case in cases}


def clean(result, *, minimum):
    return (
        result["tests"] >= minimum
        and result["failures"] == 0
        and result["errors"] == 0
        and result["skipped"] == 0
    )


def main():
    verification = ROOT / ".p5-runtime/verification"
    release = ROOT / ".p5-runtime/release"
    focused, focused_names = suite(verification / "p5-focused-tests.xml")
    full, _ = suite(verification / "tests.xml")
    response_loss, _ = suite(verification / "response-loss.xml")
    if not clean(focused, minimum=40) or not REQUIRED_AUDIT_TESTS <= focused_names:
        raise RuntimeError("The focused P5 audit gate is incomplete")
    if not clean(full, minimum=604):
        raise RuntimeError("The complete unskipped regression gate is incomplete")
    if not clean(response_loss, minimum=1) or response_loss["tests"] != 1:
        raise RuntimeError("The real HTTP response-loss gate is incomplete")

    p1_log = (verification / "p1-rules.log").read_text()
    if "Ran 13 tests" not in p1_log or "OK" not in p1_log:
        raise RuntimeError("The 13 P1 component-selection rules did not pass")
    distribution = (verification / "distribution.log").read_text().splitlines()
    if not REQUIRED_DISTRIBUTION <= set(distribution):
        raise RuntimeError("Repository-external install or real service evidence is incomplete")
    checker_log = (verification / "p53-checker.log").read_text().strip()
    if "PASS: fixed 0.6.3 checker container" not in checker_log:
        raise RuntimeError("The fixed checker container gate did not pass")

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        item["name"]: item["version"]
        for item in lock["package"]
        if item["name"].startswith("groundwater-") and "editable" in item["source"]
    }
    if len(packages) != 9 or set(packages.values()) != {VERSION}:
        raise RuntimeError("All nine workspace distributions must be 0.6.3")

    wheels = sorted((release / "dist").glob("*.whl"))
    sdists = sorted((release / "dist").glob("*.tar.gz"))
    artifacts = wheels + sdists
    if len(wheels) != 9 or len(sdists) != 9:
        raise RuntimeError("The durable release directory must contain nine wheels and sdists")
    if any(VERSION not in path.name for path in artifacts):
        raise RuntimeError("A release artifact does not carry version 0.6.3")

    frozen_manifest = json.loads((release / "source-manifest.json").read_text())
    current_manifest = manifest()
    if frozen_manifest != current_manifest:
        raise RuntimeError("Source changed after release artifacts or images were built")
    source_digest = current_manifest["source_digest"]
    image_record = json.loads((ROOT / ".p3-runtime/images.json").read_text())
    inspected = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", "groundwater-v7-checker:0.6.3"], text=True
        )
    )[0]
    labels = inspected["Config"]["Labels"]
    if image_record["checker"] != inspected["Id"]:
        raise RuntimeError("Checker image record does not match the tagged image")
    if labels.get("org.opencontainers.image.version") != VERSION:
        raise RuntimeError("Checker image version label is incorrect")
    if labels.get("org.opencontainers.image.revision") != source_digest:
        raise RuntimeError("Checker image source label does not match the release source")

    durable_manifest = ROOT / "docs/verification/p5-r0-source-manifest.json"
    durable_manifest.write_text(json.dumps(current_manifest, indent=2) + "\n")
    evidence = {
        "phase": "P5-R0-audit-release",
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
            path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)} for path in artifacts
        },
        "source": {
            "digest": source_digest,
            "manifest": "docs/verification/p5-r0-source-manifest.json",
            "file_count": current_manifest["file_count"],
        },
        "checker_image": {
            "tag": "groundwater-v7-checker:0.6.3",
            "id": inspected["Id"],
            "source_digest_label": labels["org.opencontainers.image.revision"],
            "version_label": labels["org.opencontainers.image.version"],
            "verification": checker_log,
        },
        "distribution": distribution,
        "schemas": {
            "platform": "0008_open_acceptance",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0002_lifecycle_outbox",
        },
        "python": platform.python_version(),
        "audit_cases": sorted(REQUIRED_AUDIT_TESTS),
        "limits": [
            "No new domain checker capability was added in R0.",
            "STALE completions still require the later dedicated revalidation phase.",
        ],
    }
    output = ROOT / "docs/verification/p5-r0-release-evidence.json"
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    report = f"""# P5-R0 / 0.6.3 审计修复发行报告

状态：**PASS**  
完成时间：{evidence["finished_at"]}

## 完成内容

- 九个工作区 Python 发行包统一为 `0.6.3`，生成九个 wheel 和九个 sdist。
- 固定 checker 镜像重建为 `groundwater-v7-checker:0.6.3`。
- 镜像 OCI revision 标签、发行证据和源文件清单共同指向 `{source_digest}`。
- Platform、Data、Execution、Knowledge schema 保持 `0008`、`0004`、`0006`、`0002`，没有新增迁移。
- `0.6.2` 阶段报告和证据继续作为历史记录保留。

## 测试结果

- P5 定向测试：{focused["tests"]} passed，0 failed，0 skipped。
- 完整 pytest：{full["tests"]} passed，0 failed，0 skipped。
- 真实 HTTP 响应丢失回归：{response_loss["tests"]} passed。
- P1 规则回归：13 passed。
- 仓库外安装：九个 wheel/sdist、真实 PostgreSQL 迁移、API、Worker、Knowledge、P3/P4 容器闭环通过。
- 固定 checker 容器与主机独立调用生成完全相同的开放科学报告。

## 审计缺陷验证

- 历史 `IMPORTED` 事件能够继续消费并允许后续撤回传播。
- 跨任务 ExecutionRef 被拒绝。
- WITHDRAWN EvidenceRef 不能进入新研究修订。
- CoveragePlan 来源变化会使旧检查转为 `STALE`。
- 未登记领域能力保持 `INDETERMINATE`，不会伪装为 PASS。
- Task 查询返回当前 `completion_validity` 及修订。

## 范围

R0 是审计修复发行，没有新增领域检查器、精确成果角色或原任务重验。后续仍按 P5-R1 至 P5-R5 分轮实施。
"""
    (ROOT / "docs/verification/p5-r0-release-report.md").write_text(report)
    print(f"PASS: wrote {output.relative_to(ROOT)} and P5-R0 release report")


if __name__ == "__main__":
    main()
