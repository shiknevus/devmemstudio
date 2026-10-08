# 5.2.0 验收记录（2026-10-08）

本版新增 GitHub Releases 在线升级。实现范围、操作步骤、发布方式和第一版限制见 [在线升级说明](GITHUB_UPDATES.md)。应用版本和 Windows 文件/产品版本统一为 5.2.0，固定版本资源为 5.2.0.0。

## 最终验证

执行 `venv\Scripts\python.exe tools\build.py`，最终完整构建约 66 秒。测试、源码验收和 EXE 构建并行运行，全部通过后才更新 `dist`。

| 项目 | 结果 |
| --- | --- |
| 完整回归 | 388 项，385 通过，3 项因 Windows 缺少 POSIX sh 跳过 |
| 新增升级回归 | 25 项通过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 两轮，每轮 113 项通过 |
| HTTPS 依赖 | 发布版能导入 ssl 并创建启用证书和主机名验证的 TLS context |
| 更新脚本 | 单 EXE 解包后包含 PowerShell 更新工具 |
| 缓存恢复 | 人为损坏缓存后自动修复，旧缓存自动清理 |
| ZIP 与校验文件 | ZIP 仅含 EXE，ZIP 内程序与 dist EXE 一致，SHA-256 文件匹配 |

新增用例覆盖数字版本比较、正式版筛选、仓库及 HTTPS 地址限制、GitHub digest 与校验文件回退、下载进度、中断与取消、大小和哈希错误、ZIP 路径校验、源码模式安装限制、设置保存失败、更新工具启动失败以及主窗口关闭时等待后台更新任务。

Windows 安装测试使用独立临时目录和 GCC 生成的测试 EXE，验证带中文与空格的路径、原子替换及备份、等待已有进程退出、校验错误时保留旧程序，以及新版立即以非零状态退出后恢复并重启旧版。未替换真实工作目录中运行的用户程序，也未使用真实设备或生产配置。

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,110,272 字节 | `06331b4de65190af1e0b240fd08874593e2b05b3a993ece102e0ef1541f44cf3` |
| `dist/DevmemStudio-5.2.0-win64.zip` | 20,999,051 字节 | `4de3d799ec0059c001b38ed4c8c2f7b81ed286ce87a589c5df53d21b3ca4dd2d` |

随 ZIP 生成 `dist/DevmemStudio-5.2.0-win64.zip.sha256`。更新 Release 时上传 ZIP 和此校验文件。本地构建没有上传或发布 GitHub。

记录位于 `artifacts/release-5.2.0/verification.json`，界面离线预览为同目录 `update-dialog-preview.png`；该截图的 5.3.0 是模拟数据。运行库记录见 `artifacts/singlefile-build.json`，EXE 隔离报告见 `artifacts/exe-isolated/acceptance-1/report.json` 与 `acceptance-2/report.json`，构建日志见 `build/logs/`。

## 联网边界

对实际仓库执行查询返回 HTTP 403，响应明确为当前出口 IP 的未认证 GitHub API 访问限额用尽，`X-RateLimit-Remaining: 0`。程序已正确提示重试或打开发布页；未完成实际 Release 的下载和线上版本升级验收。下载与安装流程通过内存 HTTP 数据和本地 Windows 进程进行验证。

第一版仅检查启动后 3 秒内的非零退出，不保证升级后长期业务运行正常。旧 EXE 备份保留，可手动回退。5.1.0 及更早版本尚无在线升级入口，需手动换入本版一次。
