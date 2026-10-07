# 寄存器调试工作台 4.2.2 验收记录

完成日期：2026-10-07。

## 本次变更

- 打包弹窗「仅复制为 sunny_fpga.bit，不压缩」勾选状态写入 `bitpack_settings.nopack`，软件重启后恢复；勾选时项目号保留但禁用，取消勾选后恢复可编辑。旧配置或非布尔值按未勾选处理。
- 应用版本、Windows 文件与产品版本同步更新为 4.2.2，固定版本资源为 4.2.2.0。
- README 更新当前版本、本次变更说明及发布包名称；单元测试与离线验收新增仅复制记忆检查。

## 验证结果

| 验收项 | 结果 |
| --- | --- |
| 全量回归（含本机 SSH 回环、虚拟串口与 GUI） | 296 项通过，408.514 秒 |
| 源码离线验收 | 104 项检查通过（新增仅复制勾选记忆、恢复、取消勾选 3 项） |
| 单 EXE 隔离验收：空缓存首次启动 | 104 项检查通过，10.0 秒 |
| 单 EXE 隔离验收：损坏缓存与旧缓存清理 | 104 项检查通过，9.8 秒；损坏缓存已修复，旧缓存已清理 |
| 发布版版本资源 | 文件/产品版本均为 4.2.2 |
| ZIP 完整性与内容 | CRC 通过，仅含 EXE，包内 EXE 与构建产物逐字节一致 |
| 源码与差异检查 | `compileall` 与 `git diff --check` 通过 |

隔离验收仅复制 EXE，使用独立的 `LOCALAPPDATA`，PATH 仅保留 Windows System32，并清除 Python、venv 与外部 Qt 插件路径。

首次 `build_exe.bat` 在隔离验收第 1 轮中，新解压的运行库改名就位失败（疑似安全软件扫描占用），启动器按设计回退到暂存目录运行并通过验收；第 2 轮因该暂存目录未满 10 分钟清理期而残留，判定失败。EXE 未改动，重跑 `tools\verify_exe.py` 与 `tools\make_release.py` 后两轮均通过。

## 发布产物

- 单文件 EXE：`dist/DevmemStudio.exe`，19,623,424 字节。
- EXE SHA-256：`c08cab5519aecdd6663b536b843a4bce00bfe5c24c02a0853305109974fd1d31`。
- 发布包：`dist/DevmemStudio-4.2.2-win64.zip`，19,514,432 字节。
- ZIP SHA-256：`278812f71620fc6d60c51e90e967423990b0606d73cb937c7b2de5a8deacfe17`。
- ZIP 内唯一文件：`DevmemStudio/DevmemStudio.exe`。
- 内嵌运行库：110 个文件，74,416,065 字节；LZMS 压缩后 19,484,596 字节。
- 缓存标识：`4.2.2-9d2f9142a171`。

## 证据位置

- 全量回归与源码验收：`artifacts/build-smoke/report.json`。
- EXE 隔离验收：`artifacts/release-4.2.2-exe/acceptance-1/report.json` 与 `acceptance-2/report.json`。
