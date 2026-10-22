# ruff: noqa: E501
"""Record P5-R2 / 0.7.1 evidence only after the unified gate passes."""

import hashlib
import json
import platform
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from platform_core.application.checker_catalog import load_catalog
from release_source_manifest import manifest

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.7.1"
REQUIRED = {
    "test_runtime_then_reproducer_use_one_envelope_and_complete_once",
    "test_pause_blocks_only_the_next_checker_admission",
    "test_cancel_releases_unstarted_checker_allowance",
    "test_stale_plan_cancels_queued_run_and_releases_unstarted_allowance",
    "test_error_retry_reuses_pass_and_runs_only_failed_capability",
    "test_decimal_reproduction_passes_and_wrong_slope_fails",
    "test_map_checker_rejects_blank_map_and_false_source",
    "test_unknown_result_has_uncovered_indeterminate_without_generic_pass",
    "test_0009_to_0010_preserves_existing_task_and_is_repeatable",
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


def clean(value, minimum):
    return (
        value["tests"] >= minimum and value["failures"] == value["errors"] == value["skipped"] == 0
    )


def inspect_image(tag, source_digest):
    value = json.loads(subprocess.check_output(["docker", "image", "inspect", tag], text=True))[0]
    labels = value["Config"]["Labels"]
    if labels.get("org.opencontainers.image.version") != VERSION:
        raise RuntimeError(f"{tag} version label differs")
    if labels.get("org.opencontainers.image.revision") != source_digest:
        raise RuntimeError(f"{tag} source label differs")
    return {"tag": tag, "id": value["Id"], "source_digest_label": source_digest}


def main():
    verification = ROOT / ".p5-runtime/verification"
    release = ROOT / ".p5-runtime/release"
    focused, names = suite(verification / "p5-focused-tests.xml")
    full, _ = suite(verification / "tests.xml")
    response_loss, _ = suite(verification / "response-loss.xml")
    if not clean(focused, 70) or not REQUIRED <= names:
        raise RuntimeError("P5-R2 focused gate is incomplete")
    if not clean(full, 640):
        raise RuntimeError("P5-R2 full regression gate is incomplete")
    if not clean(response_loss, 1):
        raise RuntimeError("HTTP response-loss gate is incomplete")
    p1 = (verification / "p1-rules.log").read_text()
    if "Ran 13 tests" not in p1 or "OK" not in p1:
        raise RuntimeError("P1 selection rules did not pass")
    if "PASS: fixed 0.7.1 checker container" not in (verification / "p53-checker.log").read_text():
        raise RuntimeError("Compatibility checker gate did not pass")
    if (
        "PASS: locked 0.7.1 generic and GDAL checker"
        not in (verification / "p5-r2-checkers.log").read_text()
    ):
        raise RuntimeError("R2 capability container gate did not pass")
    distribution = (verification / "distribution.log").read_text()
    if "built 9 wheels and source distributions" not in distribution:
        raise RuntimeError("Repository-external distribution gate did not pass")

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        value["name"]: value["version"]
        for value in lock["package"]
        if value["name"].startswith("groundwater-") and "editable" in value["source"]
    }
    if len(packages) != 9 or set(packages.values()) != {VERSION}:
        raise RuntimeError("All nine workspace packages must be 0.7.1")
    artifacts = sorted((release / "dist").glob("*.whl")) + sorted(
        (release / "dist").glob("*.tar.gz")
    )
    if len(artifacts) != 18 or any(VERSION not in value.name for value in artifacts):
        raise RuntimeError("0.7.1 release artifacts are incomplete")
    frozen = json.loads((release / "source-manifest.json").read_text())
    current = manifest()
    if frozen != current:
        raise RuntimeError("Source changed after release artifacts were built")
    source_digest = current["source_digest"]
    generic = inspect_image("groundwater-v7-checker:0.7.1", source_digest)
    gdal = inspect_image("groundwater-v7-gdal-checker:0.7.1", source_digest)
    catalog_path = ROOT / ".p5-runtime/checker-catalog.json"
    catalog = load_catalog(catalog_path)
    catalog_lock = json.loads(catalog_path.read_text())
    if catalog_lock["images"] != {"CHECKER": generic["id"], "GDAL_CHECKER": gdal["id"]}:
        raise RuntimeError("Capability catalogue image locks differ")

    source_path = ROOT / "docs/verification/p5-r2-source-manifest.json"
    source_path.write_text(json.dumps(current, indent=2) + "\n")
    evidence = {
        "phase": "P5-R2-trusted-checker-composition",
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
            "manifest": "docs/verification/p5-r2-source-manifest.json",
        },
        "checker_catalog": {
            "digest": catalog["catalog_digest"],
            "capabilities": [value["capability_id"] for value in catalog["capabilities"]],
            "file_sha256": sha256(catalog_path),
        },
        "checker_images": {"generic": generic, "gdal": gdal},
        "schemas": {
            "platform": "0010_checker_runs",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0002_lifecycle_outbox",
        },
        "python": platform.python_version(),
        "required_cases": sorted(REQUIRED),
        "limits": [
            "CLAIM_SUPPORT is limited to registered P3 descriptive scalar assertions.",
            "Associational, predictive and causal claims remain INDETERMINATE.",
            "Unknown result domains remain UNCOVERED and cannot obtain a generic PASS.",
        ],
    }
    (ROOT / "docs/verification/p5-r2-release-evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    )
    report = f"""# P5-R2 / 0.7.1 可信检查器组合发行报告

状态：**PASS**  
完成时间：{evidence["finished_at"]}

## 完成内容

- 部署生成的只读 capability catalogue 同时冻结能力、契约和两个不可变 checker 镜像；Platform 与 Execution 启动时校验同一目录。
- CoveragePlan schema v3 按精确成果身份匹配固定能力。无匹配能力形成 `UNCOVERED / INDETERMINATE`，不能使用通用检查器兜底。
- 运行完整性先执行，领域复算通过后才运行结构化主张支持；每个实际启动的 checker 独立结算，未启动额度在取消、依赖失败或许可拒绝时释放。
- ERROR 重试只重跑失败能力，并仅在计划、来源和能力摘要完全一致时复用 PASS。
- P3 Decimal 复算、GDAL 空间有效性、PNG/模板/源事实检查和狭窄描述性数值主张已进入受信目录。

## 测试结果

- P5 聚焦测试：{focused["tests"]} passed，0 failed，0 skipped。
- 完整 pytest：{full["tests"]} passed，0 failed，0 skipped。
- HTTP 响应丢失：{response_loss["tests"]} passed。
- P1 规则：13 passed。
- 真实 PostgreSQL `0009→0010`、九个发行包、仓库外安装、通用 checker 和 GDAL checker 容器通过。

## 范围

本轮完成 P5-R2，不进入 P6。未知领域结果保持未覆盖；关联、预测和因果主张保持无法判定。
"""
    (ROOT / "docs/verification/p5-r2-release-report.md").write_text(report)
    print("PASS: wrote P5-R2 release evidence and report")


if __name__ == "__main__":
    main()
