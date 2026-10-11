# 5.3.8 验收记录（2026-10-11）

改动：界面术语调整——原「导入组件」改名为「更新控件」（按钮、提示、帮助文本与说明文档统一）；重新打包发布时自动从 RTL 重新生成组件寄存器表（打包前运行 `gen_component_catalog.py --refresh`，按表内记录的 RTL 根目录或环境变量 `DEVMEMSTUDIO_RTL` 重新生成，自动收集 `<RTL>/top` 下的 top，RTL 已删除的旧类型保留，内容无变化且本机没有更新的「更新控件」结果时不改写文件，RTL 目录不存在时跳过，存在未解析文件或 top 类型无法匹配时构建失败，`--no-catalog` 可跳过）；组件寄存器表随 RTL 更新——`ec_2di_2do` 等类型的 `DEBUG_REG2` 位快照字段（`irq_posedge_cnt`/`irq_negedge_cnt`）已随 RTL 断开，行为表注释更新，新增 `ec_sg_rfid`、`ec_sp_rfid`、`ec_osm41_485_laser_distance` 类型；新增「构建刷新寄存器表」行为测试。应用版本和 Windows 文件/产品版本统一为 5.3.8，固定版本资源为 5.3.8.0。

## 最终验证

执行 `tools/build.py`，完整构建 85 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 468 项，465 项通过，3 项依赖 POSIX sh 在 Windows 跳过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 4.8 s，损坏缓存自动修复及验收 7.9 s；陈旧缓存自动清理 |
| Windows 版本资源 | 文件版本、产品版本均为 5.3.8 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 构建源码一致性 | 构建结束后与构建前已跟踪文件的 SHA-256 快照一致，工作树仅含本次发布改动 |

首轮并行全量回归曾偶发 1 条监视窗口时间轴刷新测试失败（`test_monitor`，断言追加采样后出现 20/30/40 s 标签），该测试单独运行及全量重跑均通过，判定为并行负载下的时序抖动，与本次改动无关；未改动任何功能或测试代码。本次验证使用离线模拟会话，未在实机窗口复测。

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,212,672 字节 | `3338247e21c02e69c287618f66efb519b4e11903cc174dfa41766ce06203b662` |
| `dist/DevmemStudio-5.3.8-win64.zip` | 21,100,762 字节 | `f66405cde1c5291a59808a418bde3418516b7d9310d045cde6c46ac27d93ee16` |

GitHub 正式发布地址：[v5.3.8](https://github.com/shiknevus/devmemstudio/releases/tag/v5.3.8)，发布资产为 ZIP 与 `DevmemStudio-5.3.8-win64.zip.sha256`。
