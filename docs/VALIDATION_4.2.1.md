# 寄存器调试工作台 4.2.1 验收记录

完成日期：2026-10-07。

## 本次变更

- 修复 SSH 与 serial 连接设置中两个「记住密码」复选框底部被裁切的问题。移除按标题初始高度固定复选框高度的限制，由 Qt 按控件样式与实际尺寸布局。
- 应用版本、Windows 文件与产品版本同步更新为 4.2.1，固定版本资源为 4.2.1.0。
- README 更新当前版本、本次修复说明及发布包名称。

## 验证结果

| 验收项 | 结果 |
| --- | --- |
| 全量回归（含本机 SSH 回环、虚拟串口与 GUI） | 296 项通过，343.014 秒 |
| 源码离线验收 | 102 项检查通过 |
| 单 EXE 隔离验收：空缓存首次启动 | 102 项检查通过，9.9 秒 |
| 单 EXE 隔离验收：损坏缓存与旧缓存清理 | 102 项检查通过，9.8 秒；损坏缓存已修复，旧缓存已清理 |
| 复选框几何检查 | 100%、125%、150%、200% 缩放下两个指示器完整显示，用户名与密码输入框保持对齐 |
| 发布版代码与版本资源 | 打包代码包含复选框修复；单文件启动器和内嵌程序均为 x64，文件/产品版本均为 4.2.1.0 |
| ZIP 完整性与内容 | CRC 通过，仅含 EXE，包内 EXE 与构建产物逐字节一致 |
| 源码与差异检查 | `compileall` 与 `git diff --check` 通过 |

隔离验收仅复制 EXE，使用独立的 `LOCALAPPDATA`，PATH 仅保留 Windows System32，并清除 Python、venv 与外部 Qt 插件路径。

## 发布产物

- 单文件 EXE：`dist/DevmemStudio.exe`，19,625,472 字节。
- EXE SHA-256：`cccff1044fb41411165995fa5860e724f6a4f1ea784df201eb183d7922e1f40d`。
- 发布包：`dist/DevmemStudio-4.2.1-win64.zip`，19,515,505 字节。
- ZIP SHA-256：`c8cc3d2b82e7dca55d2729af0be199763b14f0ddb47e762e3ea3bbca10b989b6`。
- ZIP 内唯一文件：`DevmemStudio/DevmemStudio.exe`。
- 内嵌运行库：110 个文件，74,414,721 字节；LZMS 压缩后 19,486,492 字节。
- 缓存标识：`4.2.1-ab4ef5a75ace`。

## 证据位置

- 全量回归：`artifacts/release-4.2.1/unit-tests.log`。
- 源码验收：`artifacts/release-4.2.1/source-smoke/report.json`。
- EXE 隔离验收：`artifacts/release-4.2.1-exe/acceptance-1/report.json` 与 `acceptance-2/report.json`。
- 版本与打包记录：`artifacts/release-4.2.1/version-verification.json`、`singlefile-build.json`、`validation.json`。
- 复选框几何记录：`artifacts/remember-checkbox-verified/scale-*.json`。
