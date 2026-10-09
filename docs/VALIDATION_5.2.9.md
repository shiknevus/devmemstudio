# 5.2.9 验收记录（2026-10-09）

改动：开始监视后点击图表放置 A/B 标记不再中断后续采样刷新；子窗口最大化后，点击主窗口或任务栏可以把主窗口提到前面。应用版本和 Windows 文件/产品版本统一为 5.2.9，固定版本资源为 5.2.9.0。

## 最终验证

执行 `tools/build.py`，完整构建 149 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 425 项，422 项通过，3 项依赖 POSIX sh 在 Windows 跳过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 两轮均 113 项通过；首次解压及验收 9.1 s，损坏缓存自动修复及验收 8.5 s；陈旧缓存自动清理 |
| Windows 版本资源 | 文件版本、产品版本均为 5.2.9 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 构建源码一致性 | 构建结束后与构建前 120 个已跟踪文件的 SHA-256 快照一致 |

本次验证使用离线模拟会话，未在实机窗口上复测最大化子窗口的前后台切换。

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,130,240 字节 | `781ff36c8d083f6860778c65fbcdfde4fad61797dc6e3213fdabb06e6657d5c5` |
| `dist/DevmemStudio-5.2.9-win64.zip` | 21,019,137 字节 | `10735de0da27a6ca9df63132464dc51172c55b483248000ebbaec1a0113870f1` |

GitHub 正式发布地址：[v5.2.9](https://github.com/shiknevus/devmemstudio/releases/tag/v5.2.9)，发布资产为 ZIP 与 `DevmemStudio-5.2.9-win64.zip.sha256`。
