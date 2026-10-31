# P4.2.1 空间资产与算子契约

版本 `0.4.0` 在原上传、资产、执行和工作流路由上增加受控空间类型，没有开放命令、镜像、插件、脚本、URL 或宿主路径参数。

## 上传与导入

`POST /api/v1/projects/{project_id}/uploads` 的 `format` 支持 `geopackage` 和 `geotiff`。文件仍限制为 20 MiB，内容上传后使用：

```json
{"kind":"SPATIAL","profile":"strict-spatial-v1"}
```

调用现有 `POST .../uploads/{upload_id}/finalize`。空间 Worker 使用固定 QGIS/GDAL 检查镜像。它按上传 ID、源摘要和镜像摘要恢复，容器没有数据库凭据、用户令牌或网络。

GeoPackage manifest 的 `kind` 为 `SPATIAL_VECTOR`，包含实际 driver、原文件表示、稳定 `layer_id`、字段、几何类型、XY/Z/M、要素数、空图层、有效性、范围以及 CRS 的 WKT2、EPSG、单位、权威轴顺序。对外范围统一声明传统 GIS `x,y` 顺序。多图层资产可以保存，但执行必须指定 `layer_id`。

GeoTIFF manifest 的 `kind` 为 `SPATIAL_RASTER`，包含 CRS、范围、宽高、仿射变换、像元大小，以及各波段的数据类型、NoData、颜色解释和校验值。本版没有栅格计算算子。

以下内容拒绝导入：缺失或不可解析 CRS、伪装 driver、外部文件或子数据集、空几何、无效几何、规范化名称冲突、越界资源和损坏文件。空图层本身可以导入。

## 空间语义修订

现有语义修订路由接受 `spatial-generic-v1`：

```json
{
  "expected_revision": 0,
  "version_id": "1",
  "semantics": {
    "profile": "spatial-generic-v1",
    "annotations": [{
      "namespace": "https://example.org/groundwater",
      "term": "monitoring_well",
      "target": {"kind": "LAYER", "layer_id": "l0"},
      "value": true
    }]
  }
}
```

目标可以是资产、图层、字段或波段。值只能是有界标量或标量数组。注解是可追加的领域解释，不能覆盖检查器提取的 CRS、范围、摘要、图层、波段和数量。

## 精确选择与算子

空间算子输入冻结资产版本、manifest 摘要、表示摘要、选择器及可选语义修订：

```json
{
  "asset": {"project_id":"p","asset_id":"a","version_id":"1","version_manifest_digest":"sha256:..."},
  "representation_id": "original",
  "representation_digest": "sha256:...",
  "selector": {"kind":"VECTOR_LAYER","layer_id":"l0"},
  "semantic_revision": 1
}
```

目录新增两个精确版本：

- `qgis-buffer@1.0.0` / `qgis-buffer-v1`：使用固定 `native:reprojectlayer` 和 `native:buffer`，输出 `vector`、`manifest`、`method`。
- `spatial-vector-summary@1.0.0` / `spatial-vector-summary-v1`：在不含 QGIS SDK 的 Native GDAL 镜像中独立读取矢量，输出 `summary`、`method`。

Buffer 参数为正的有限距离、`metre|kilometre`、明确 EPSG 输出 CRS和 1–64 的 `segments`。输入必须是投影 CRS；输出必须是以米为线性单位的投影 EPSG CRS。不同 CRS 时先明确重投影。空图层返回 `EMPTY_INPUT`，不产生可发布成果。

## 工作流和发布

冻结示例包 `qgis-buffer-summary-v1` 具有以下端口：

```text
vector + buffer_parameters
  -> qgis-buffer(vector, manifest, method)
  -> spatial-vector-summary(summary, method)
```

节点之间的 File 通过来源节点、输出端口和摘要绑定。QGIS 中间文件只保留在 Execution 恢复目录。两个节点都成功后，父 Execution 才按完整命名输出集合原子提交发布。预算按两个实际开始的节点分别结算；父调度不计次。空间成果没有 P3 科学验收模板，因此发布成功后 Task 保持 `OPEN`。

公开路由和状态码沿用 P3/P4.1。执行查询会返回节点、运行时、发布和清理事实；通信未知不会被解释为未执行、已停止或可以换身份重算。

