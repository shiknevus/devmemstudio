# 5.3.1 验收记录（2026-10-10）

改动：寄存器监视的读取失败显示、32 位整数采样缓存、淘汰极值范围复用、后台导出、ZIP 压缩等级 9→6 与 A/B 标记交互优化（详见 [寄存器监视检查与优化](VALIDATION_MONITOR_REVIEW.md)）。应用版本和 Windows 文件/产品版本统一为 5.3.1，固定版本资源为 5.3.1.0。

## 最终验证

执行 `tools/build.py`，完整构建 100 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 443 项，440 项通过，3 项依赖 POSIX sh 在 Windows 跳过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 6.6 s，损坏缓存自动修复及验收 6.4 s；陈旧缓存自动清理 |
| Windows 版本资源 | 文件版本、产品版本均为 5.3.1 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 构建源码一致性 | 构建结束后与构建前已跟踪文件的 SHA-256 快照一致，工作树仅含本次发布改动 |

板端回归按 [寄存器监视检查与优化](VALIDATION_MONITOR_REVIEW.md) 使用 `artifacts/emu-venv` 对真实 aarch64 二进制仿真：15 项通过，覆盖采样节拍、网络背压、部分写入、断线退出、总线异常、看门狗和长采样间隔。本次验证未连接实际板卡。

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,129,216 字节 | `cfcc9bfdb9d0d69385a6488003c057ad37e55ab784046660dccb34f91565b44d` |
| `dist/DevmemStudio-5.3.1-win64.zip` | 21,020,135 字节 | `1397ac4b6ded7974859d99652ddedc5005c2c254067bb1a73c74cadf7ee387cb` |

GitHub 正式发布地址：[v5.3.1](https://github.com/shiknevus/devmemstudio/releases/tag/v5.3.1)，发布资产为 ZIP 与 `DevmemStudio-5.3.1-win64.zip.sha256`。