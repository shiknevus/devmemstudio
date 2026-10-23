# 5.6.0 验收记录（2026-10-24）

改动：单文件启动器并行解压（分块校验 + 多线程写出）与启动提示窗；任务栏多窗口折叠；窗口最大化/恢复黑边填充；监视 resize 帧预算渲染；「回退bit」当前版本可下载；目录解析与样式重刷优化。功能代码改动集中在 `tools/runtime_launcher/launcher.c`（并行解压、splash、`move_directory` 改名重试）、`devmem_studio/window_state.py`（任务栏折叠、居中、黑边填充）、`devmem_studio/taskbar.py`（`sync_taskbar_tabs`）、`devmem_studio/monitor.py`（resize 帧预算）、`devmem_studio/dialogs.py` 与 `devmem_studio/window.py`（当前版本可下载）等；`tools/verify_exe.py` 新增「从 `.tmp-` 暂存目录运行视为失败」断言，防止修复路径残留回归。本轮发布版本由 5.5.10 改号 5.6.0（用户指定）。应用版本和 Windows 文件/产品版本统一为 5.6.0，固定版本资源为 5.6.0.0。

## 最终验证

执行 `tools/build.py`，完整构建 82 秒（期间两次并行测试偶发超时，均经单测/串行复跑确认通过后重试成功）。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 647 项全部通过（58.4 s） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 4.7 s，损坏缓存自动修复及验收 5.1 s；陈旧缓存自动清理；修复路径无 `.tmp-*` 残留 |
| 组件目录 | RTL 根未配置（${DEVMEMSTUDIO_RTL} 未设置），沿用内置目录 |
| Windows 版本资源 | 文件版本、产品版本均为 5.6.0 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 文档链接完整性 | README 与 `docs/CHANGELOG.md` 内相对链接全部指向存在的文件 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,288,960 字节 | `9579f9f686ace46c6a2b225cbeba458238ab7620d86fa43ac2a3f72045cd6c6d` |
| `dist/DevmemStudio-5.6.0-win64.zip` | 21,175,456 字节 | `e71a1c58b3489579920c6ffcd57039a9af8cdf095327dbaf5aa3ec755a0758ca` |

GitHub 正式发布地址：[v5.6.0](https://github.com/shiknevus/devmemstudio/releases/tag/v5.6.0)，发布资产为 ZIP 与 `DevmemStudio-5.6.0-win64.zip.sha256`。