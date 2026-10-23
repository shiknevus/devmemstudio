# 5.5.10 验收记录（2026-10-24）

改动：单文件启动器并行解压（分块校验 + 多线程写出）与启动提示窗；任务栏多窗口折叠；窗口最大化/恢复黑边填充；监视 resize 帧预算渲染；「回退bit」当前版本可下载；目录解析与样式重刷优化。功能代码改动集中在 `tools/runtime_launcher/launcher.c`（并行解压、splash、`move_directory` 改名重试）、`devmem_studio/window_state.py`（任务栏折叠、居中、黑边填充）、`devmem_studio/taskbar.py`（`sync_taskbar_tabs`）、`devmem_studio/monitor.py`（resize 帧预算）、`devmem_studio/dialogs.py` 与 `devmem_studio/window.py`（当前版本可下载）等；`tools/verify_exe.py` 新增「从 `.tmp-` 暂存目录运行视为失败」断言，防止修复路径残留回归。应用版本和 Windows 文件/产品版本统一为 5.5.10，固定版本资源为 5.5.10.0。

## 最终验证

执行 `tools/build.py`，完整构建 78 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 647 项全部通过（55.3 s） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 4.5 s，损坏缓存自动修复及验收 4.8 s；陈旧缓存自动清理；修复路径无 `.tmp-*` 残留 |
| 组件目录 | RTL 根未配置（${DEVMEMSTUDIO_RTL} 未设置），沿用内置目录 |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.10 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 文档链接完整性 | README 与 `docs/CHANGELOG.md` 内相对链接全部指向存在的文件 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,292,032 字节 | `06cb4d472a446c46b8d5191517baa89489692b3ceab146081519b9c76e5b9b17` |
| `dist/DevmemStudio-5.5.10-win64.zip` | 21,177,610 字节 | `ca5a0ee26390bb21ed2a70ef901fa32cd7b6a44c70e8e93cd34d443de73a6e87` |

GitHub 正式发布地址：[v5.5.10](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.10)，发布资产为 ZIP 与 `DevmemStudio-5.5.10-win64.zip.sha256`。