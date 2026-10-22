# P4.2.2 受信静态制图闭环验收报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

当前版本：八个 V7 Python 发行包及相关 runtime `0.4.1`。基线为 P4.2.1 `0.4.0`。

## 完成内容

- 新增封闭的 `qgis-static-map@1.0.0`，使用固定 QGIS 镜像、固定 A4 横向模板、三套调色板和固定 Noto CJK 字体生成 1754×1240 PNG。
- 新增标准库 PNG 独立校验，检查文件结构、CRC、解压扫描线、尺寸、地图框、调色板和完整 provenance。
- Data 改为显式成果类型注册分派，`CARTOGRAPHIC_PRODUCT` 只允许 image/manifest/method，图片以 `image/png` 下载。
- 新增 `qgis-buffer-summary-map-v1`：Buffer 完成后并行运行 Native Summary 与 StaticMap，整组八个输出原子发布，三个实际节点各结算一次。
- 最小任务页支持标题、图例、调色板、输出 CRS、节点状态、带 Bearer Token 的 Blob 预览和独立下载；令牌和图片不写入持久存储。

## 测试与修复

统一入口 `bash scripts/check_p4.sh --phase cartography` 最终以 0 退出：**515 项 pytest、13 项 P1 规则，0 失败、0 错误、0 跳过**。八个 wheel 和 sdist 在仓库外锁定安装；安装后的 API、CLI、三数据库 schema、P3 科学闭环、P4.1 Toil、P4.2.1 Buffer/Summary 与 P4.2.2 三节点制图全部通过。结果和不可变镜像写入[结构化证据](p422-cartography-evidence.json)。

必测范围包括点/线/面、中文标题、三套调色板、图例开关、退化范围、显式重投影、非法 CRS/文本/几何、确定性渲染、PNG 篡改、三节点故障与取消、唯一预算和发布、真实浏览器预览，以及 P3、P4.1、P4.2.1 兼容回归。

开发中发现并修复四类问题：

- 页面 Content Security Policy 未允许 `blob:` 图片，导致后端已正确发布的 PNG 无法预览。首页仅为 `img-src` 增加 `blob:`，仍禁止外部资源；预览等待浏览器成功解码后再原子替换旧 URL。
- PyQGIS Layout 完成导出后在 QObject 解构期间可崩溃。一次性受信容器先 fsync 全部成果，再以受控方式退出，避免已完成渲染被误报失败。
- 排队工作流取消存在通知顺序竞态，可在 `REJECTED` 和 `CANCELLED` 之间波动。现在确认未执行时统一为 `REJECTED`，已启动仍为 `CANCELLED`；排队路径连续重复 3 次通过。
- 真实 Toil 编译器测试漏传私有 `--workDir`，多作业会竞争 `/run/user/.../toil` 的 deferred 文件。测试现与生产引擎一致，每次使用独立工作目录；嵌套 scatter 连续重复 5 次和完整 EnginePackage 12 项均通过。

## 范围与限制

- 只输出 PNG；没有 PDF、GeoPDF、栅格制图、在线底图、QPT/QML、插件、脚本或地图编辑器。
- 空间工作流成功只证明固定实现生成并发布了符合契约的成果，不证明地图表达或领域解释适合某个科学结论。Task 保持 `OPEN`。
- 恢复范围仍是同一主机、同一 Docker daemon 和持久工作目录，没有声明跨主机 HA。

## 阶段结论

**P4.2.2 通过。** 本轮到此结束，不自动进入 P4.3 或 P5。
