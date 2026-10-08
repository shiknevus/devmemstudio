# 5.2.5 验收记录（2026-10-08）

改动：更新窗口标题栏加入最小化/最大化按钮；最小化后点“检查更新”或自动检查发现新版时恢复窗口。应用版本和 Windows 文件/产品版本统一为 5.2.5，固定版本资源为 5.2.5.0（5.2.4 未使用）。

## 最终验证

执行 `build_exe.bat`（`tools/build.py`），完整构建 94 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 394 项，391 通过，3 项因 Windows 缺少 POSIX sh 跳过 |
| 升级回归 | 31 项通过（新增最小化后恢复） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 两轮，每轮 113 项通过 |
| 原生窗口 | Windows 平台下更新窗口带 `WS_MINIMIZEBOX` / `WS_MAXIMIZEBOX`，最小化后 `present()` 可恢复 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,109,248 字节 | `5897ffefd49e09b9a7418b8023a87085d543e5bbb04a95575ef5efbd38365f70` |
| `dist/DevmemStudio-5.2.5-win64.zip` | 20,999,277 字节 | `8b54d3a29ad916f1408269b4547095e601c5fc5d19b4174e9ef82b049ef37419` |

GitHub Release `v5.2.5` 上传 ZIP 与 `DevmemStudio-5.2.5-win64.zip.sha256`。
