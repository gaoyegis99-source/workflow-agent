# P4.2.2 受信静态制图契约

版本 `0.4.1` 在 P4.2.1 空间资产和执行接口上增加一个封闭的制图实现；公开路由和数据库 schema 不变。

## 算子请求

`POST /api/v1/projects/{project_id}/executions` 新增目录项 `qgis-static-map@1.0.0`，实现固定为 `qgis-static-map-v1`。输入 `vector` 必须冻结精确资产版本、表示摘要和 `VECTOR_LAYER` 选择器。参数为：

```json
{
  "layer_id": "l0",
  "template_id": "v7-spatial-basic-v1",
  "output_crs": "EPSG:32650",
  "title": "缓冲区成果",
  "legend": true,
  "palette": "blue"
}
```

`layer_id` 必须与选择器一致。输出 CRS 必须是可解析、投影且以米为线性单位的 EPSG CRS。标题经 NFC 规范化后为 1–120 字符，拒绝控制字符和双向覆盖字符。调色板只接受 `blue`、`green`、`orange`。输入必须非空、几何有效并且是点、线、面或对应 Multi 类型。

客户端不能指定页面大小、DPI、字体、颜色值、文件路径、算法名、镜像、插件、脚本或服务地址。输入可以是地理 CRS；运行时只按调用者明确给出的输出 CRS重投影，不自动选择 UTM。

## 冻结模板与验证

`v7-spatial-basic-v1` 固定 A4 横向、150 DPI、1754×1240 像素、白色背景、12 mm 边距、标题、地图框、可选图例、北箭头和米制比例尺。字体固定为 `Noto Sans CJK SC`，Debian 包版本 `1:20230817+repack1-3`。模板、字体文件、符号和北箭头共同形成 `template_digest`。

图层重投影后的范围先向每个方向扩展 5%，每侧至少 10 米，再对称扩展到地图框宽高比。非有限坐标、变换失败和 CRS 有效域外数据拒绝。

Execution 的标准库 PNG 校验器独立检查签名、chunk CRC、IHDR、zlib 扫描线、固定尺寸、色深、颜色类型、非空地图框和受信调色板像素。它还核对源摘要、参数、模板摘要、manifest、method 和 PNG 摘要。额外、缺失、损坏或错配文件不能发布。

## 成果与下载

`CARTOGRAPHIC_PRODUCT` 只允许：

- `image`：`map.png`，下载类型 `image/png`；
- `manifest`：源引用、CRS、最终范围、模板/字体、页面、DPI、调色板和图片摘要；
- `method`：算子与 QGIS/GDAL/PROJ 版本、重投影和 Layout 导出方法。

Data 使用显式结果类型注册表分派 `SPATIAL_VECTOR`、`SPATIAL_SUMMARY` 和 `CARTOGRAPHIC_PRODUCT`。未知类型没有兜底发布路径。

## 三节点工作流

冻结包 `qgis-buffer-summary-map-v1` 执行：

```text
GeoPackage -> qgis-buffer -> spatial-vector-summary
                         \-> qgis-static-map
```

Summary 与 Map 在 Buffer 成功后并行。三个实际开始的节点各结算一次预算，父调度不计次。八个命名输出作为一个集合提交；任一节点失败或取消时不公开半组成果。空间任务没有自动验收模板，因此工作流成功后 Task 仍为 `OPEN`。
