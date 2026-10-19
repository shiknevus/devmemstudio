# 5.5.6 验收记录（2026-10-19）

改动：寄存器监视与实时读取上限扩展到 64 路，实时读取不再重复打印截断提示。功能代码集中在：
- `devmem_studio/core.py`：`MONITOR_MAX_REGISTERS` 由 32 提升至 64（与板端常驻采样程序 regmon 的 `MAX_REGS 64` 一致），监视脚本、配置归一化与内存预算按路数自动折算（64 路总内存与 32 路持平）。
- `devmem_studio/live_read.py` 与 `devmem_studio/window.py`：实时读取按 `MONITOR_MAX_REGISTERS` 截断并去除重复日志，单次常驻监视明确覆盖 64 路。
- `devmem_studio/monitor.py`：监视勾选器展示 64 路上限；位宽超过 32 位的寄存器不可监视并给出 tooltip 说明。
- 文档与文案：README、帮助文本、CHANGELOG 同步"最多 64 个"。
应用版本和 Windows 文件/产品版本统一为 5.5.6，固定版本资源为 5.5.6.0。

## 最终验证

执行 `tools/build.py`，完整构建 136 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 615 项全部通过（91.8 s） |
| 源码离线验收 | 通过（17.3 s） |
| 单 EXE 隔离验收 | 通过（22.9 s）；首次解压及验收，损坏缓存自动修复及验收；陈旧缓存自动清理 |
| 组件目录 | 沿用当前表（无变化不改写） |
| 快速工程检查 | `tools/check_project.py` 通过（依赖锁定、源码编译、目录完整性） |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.6 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,268,992 字节 | `e4ab17ffe19b22e4d00a255578ae662f5dab03f22f92b9e0dc020e9bbb5ebce9` |
| `dist/DevmemStudio-5.5.6-win64.zip` | 21,158,859 字节 | `da5c32f0904c2c2c3cf3f7c158d0567bf8c6bc3120a13161e00f0dad77add8b9` |

GitHub 正式发布地址：[v5.5.6](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.6)，发布资产为 ZIP 与 `DevmemStudio-5.5.6-win64.zip.sha256`。