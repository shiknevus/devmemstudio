# 5.5.4 验收记录（2026-10-16）

改动：凭据 DPAPI 加密与 SSH 首次信任核验，升级助手 .NET 哈希与便携 RTL 根路径，任务栏现代 Shell 兼容与界面交互优化。功能代码集中在：
- `devmem_studio/core.py` 与 `devmem_studio/dialogs.py`：`registers.json` 中的 SSH/串口密码改用 Windows DPAPI（`dpapi:v1:`）加密存储，自动迁移旧版明文；SSH 引入未知主机拒绝策略（`_RejectUnknownHostKeyPolicy`）与首次信任指纹确认对话框（`HostKeyDialog`）；板端常驻采样程序防提权检查与 SSH 读取超时恢复。
- `assets/updater/apply_update.ps1`：PowerShell 升级助手哈希计算改用原生 .NET `SHA256`（`Get-DevmemHash`），兼容受限语言与精简环境。
- `tools/gen_component_catalog.py` 与 `tools/release_github.py`：内置组件目录 RTL 路径便携化；GitHub 发布脚本支持 URL 编码与预暂存防丢失。
- `devmem_studio/window.py` 与 `devmem_studio/taskbar.py`：响应式工具栏状态缓存，串口空闲扫描周期降频至 20 秒，适配 Windows 现代 Shell 对 `ITaskbarList::HrInit` 返回 `E_NOTIMPL` 的行为。
- `devmem_studio/smoke.py`：适配组件异步导入等待。
- 新增 `tools/verify_bitpack_gui.py`、`tools/verify_bitpack_gui_worker.py` 与 `tools/benchmark_monitor_scroll.py`。
应用版本和 Windows 文件/产品版本统一为 5.5.4，固定版本资源为 5.5.4.0。

## 最终验证

执行 `tools/build.py`，完整构建 100 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 596 项，596 项全部通过（57.2 s） |
| 源码离线验收 | 119 项通过（13.7 s） |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 6.6 s，损坏缓存自动修复及验收 6.3 s；陈旧缓存自动清理 |
| 组件目录 | 沿用当前表（62 类型，无变化不改写）；支持便携 RTL 根路径 |
| 快速工程检查 | `tools/check_project.py` 通过（依赖锁定、源码编译、目录完整性） |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.4 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,264,384 字节 | `4a1f7eb169b700e4319ecd56b700a2df3203107175a52a72dfcfe355ef76ff89` |
| `dist/DevmemStudio-5.5.4-win64.zip` | 21,152,671 字节 | `43739bd6b4418bc8dc9302d80261173365e4289a13c3f8eb098ec2bc0a06a96a` |

GitHub 正式发布地址：[v5.5.4](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.4)，发布资产为 ZIP 与 `DevmemStudio-5.5.4-win64.zip.sha256`。
