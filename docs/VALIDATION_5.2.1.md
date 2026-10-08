# 5.2.1 验收记录（2026-10-08）

5.2.0 未在 GitHub 发布，本版是在线升级的首个公开版本。改动：状态栏“检查更新”与版本号同行同字体、更新窗口重排、API 限额时改走发布页跳转、网络重试、PowerShell 5.1 退出码修正。说明见 [在线升级说明](GITHUB_UPDATES.md)。应用版本和 Windows 文件/产品版本统一为 5.2.1，固定版本资源为 5.2.1.0。

## 最终验证

执行 `build_exe.bat`（`tools/build.py`），完整构建 80 秒。测试、源码验收和 EXE 构建并行运行，全部通过后才更新 `dist`。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 391 项，388 通过，3 项因 Windows 缺少 POSIX sh 跳过 |
| 升级回归 | 28 项通过（新增 API 限额回退、无大小下载、连接重试） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 两轮，每轮 113 项通过 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，与 dist EXE 一致，`.sha256` 匹配 |
| 状态栏 | Windows 原生渲染 100% / 125% / 150% 缩放下，计数、版本、检查更新同字号同基线 |

## 安装端到端（不经打包）

以 5.1.0 EXE 为旧程序、5.2.0 包为新程序，放在含中文与空格的目录，调用 `prepare_install` + `start_install` 后让调用进程退出：约 4 秒完成替换，`.bak` 哈希与旧程序一致，新版从原路径启动并拉起运行库子进程。

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,113,856 字节 | `20eeb9f66c95fb06ad9e4b8eb27da7b2ca3395c0c10ae53c7f7330ab8d888991` |
| `dist/DevmemStudio-5.2.1-win64.zip` | 21,004,367 字节 | `efee247a822dc97171004dde2e79b39a2ea7f3b12f74e7a5354e1a8955938771` |

GitHub Release `v5.2.1` 上传 ZIP 与 `DevmemStudio-5.2.1-win64.zip.sha256`。EXE 隔离报告见 `artifacts/exe-isolated/acceptance-1/report.json` 与 `acceptance-2/report.json`，构建日志见 `build/logs/`。

## 线上发布验证

- Release：https://github.com/shiknevus/devmemstudio/releases/tag/v5.2.1 ，指向提交 `098e4c8`；资产 ZIP 与 `.sha256` 均为 uploaded，GitHub 返回的 ZIP digest 与上表一致。
- 验证时本机出口 IP 的匿名 API 额度为 0/60（返回 403）。新版回退路径通过 `releases/latest` 跳转解析到 5.2.1，下载 ZIP（约 70 秒）并用 `.sha256` 校验，解出的 EXE SHA-256 与 `dist/DevmemStudio.exe` 一致。
- 5.2.0 本地构建只走 API，没有回退路径；额度耗尽时 5.2.0 检查更新会提示访问限额，额度恢复后才能看到 5.2.1。