# 5.2.3 验收记录（2026-10-08）

改动：更新窗口取消/关闭立即生效；升级备份改名为 `DevmemStudio-<旧版本>-backup.exe`。应用版本和 Windows 文件/产品版本统一为 5.2.3，固定版本资源为 5.2.3.0。

## 最终验证

执行 `build_exe.bat`（`tools/build.py`），完整构建 99 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 393 项，390 通过，3 项因 Windows 缺少 POSIX sh 跳过 |
| 升级回归 | 30 项通过（新增：请求卡住时取消/关闭立即返回、迟到的下载结果被丢弃并删除、取消后可立即重新检查、备份名保持 `.exe` 且不覆盖已有备份） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 两轮，每轮 113 项通过 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配 |

## 透明加密对照

本机企业透明加密对 PowerShell 生成的 `.bak`、`.zip` 自动加密（Git Bash 读到加密头 `18 1b 03 1a …`），改名为 `.exe` 后无法运行；PowerShell `File.Replace` 生成 `.exe` 名的备份、Python 写入或改名均为明文。端到端：以 5.2.1 为旧程序、5.2.2 包为新程序走真实升级工具，备份 `DevmemStudio-<版本>-backup.exe` 磁盘为明文，哈希与 5.2.1 原版一致，新版从原路径启动。

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,115,392 字节 | `4ea02a5ea05e35b8a2437f7af287eb981937b8b40171ba3b8e5760a362e76996` |
| `dist/DevmemStudio-5.2.3-win64.zip` | 21,004,972 字节 | `8eb96b7c133bae1aca05ccd3395796b737ae78d6239c4b2c12e1c5b2554a144a` |

GitHub Release `v5.2.3` 上传 ZIP 与 `DevmemStudio-5.2.3-win64.zip.sha256`。
