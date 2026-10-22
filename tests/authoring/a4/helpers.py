from copy import deepcopy

from platform_core.application.catalog import build_catalog


def catalog_and_materials(operator_id="table-profile"):
    catalog = build_catalog()
    operator = next(item for item in catalog["operators"] if item["operator_id"] == operator_id)
    semantic = {
        key: deepcopy(operator[key])
        for key in (
            "operator_id",
            "version_id",
            "ownership",
            "trust_level",
            "inputs",
            "parameters",
            "outputs",
            "effects",
            "supports_workflow",
            "legacy",
            "operator_digest",
        )
    }
    materials = {
        "title": "受控表格剖析算子",
        "purpose": "登记已经审核并随平台发布的表格剖析实现。",
        "limitations": "只处理目录声明的平面 Parquet 类型。",
        "operator": semantic,
        "implementation": deepcopy(operator["implementations"][0]),
        "source": {
            "package_name": "groundwater-tabular-operator-pack",
            "package_version": "0.14.3",
            "package_source": "controlled-release/tabular-operator-pack",
            "source_digest": "sha256:" + "a" * 64,
            "tests_digest": "sha256:" + "b" * 64,
            "test_summary": "离线固定夹具及真实容器回归通过。",
            "checker_coverage": ["tabular-profile-reproduction-v1"],
        },
    }
    release = {
        "release_digest": "sha256:" + "c" * 64,
        "operator_catalog": {"digest": catalog["catalog_digest"]},
        "source_tree": {"digest": materials["source"]["source_digest"]},
    }
    return catalog, materials, release
