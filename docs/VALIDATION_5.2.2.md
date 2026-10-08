# 5.2.2 验收记录（2026-10-08）

功能与 5.2.1 相同，仅版本号变化，用于验证从 5.2.1 在线升级的完整流程。应用版本和 Windows 文件/产品版本统一为 5.2.2，固定版本资源为 5.2.2.0。

## 最终验证

执行 `build_exe.bat`（`tools/build.py`），完整构建 83 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 391 项，388 通过，3 项因 Windows 缺少 POSIX sh 跳过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 两轮，每轮 113 项通过 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,114,368 字节 | `7c2ac1fd9297c73c3eec141dbf22e9fe970e0ce5b50dade98802260415415590` |
| `dist/DevmemStudio-5.2.2-win64.zip` | 21,004,462 字节 | `6c7746e357f3b1ba66274b24576845e995f93c15d81f6de06d3c06c5acbdaecd` |

GitHub Release `v5.2.2` 上传 ZIP 与 `DevmemStudio-5.2.2-win64.zip.sha256`。
