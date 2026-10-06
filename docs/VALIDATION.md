# 寄存器调试工作台 4.2.0 验收记录

完成时间：2026-10-07（4.2.0 构建）

## 构建与发布产物

- 构建环境：本项目 venv，Python 3.12.2，Paramiko 4.0.0，PyInstaller 6.22.2，PySide6 6.11.2；单文件启动器由 MinGW-w64 `x86_64-w64-mingw32-gcc` 14.3 编译；Windows 10 x64。
- 最终产物：`dist/DevmemStudio.exe`（单文件），**19,621,376 字节**（4.1.8 为 31,383,044 字节，-37.5%）。
- 应用版本、Windows 文件/产品版本均为 **4.2.0**；固定版本资源为 `4.2.0.0`，PE 架构为 x64；启动器仅导入 Windows 自带的 kernel32、user32、Cabinet、bcrypt 与 msvcrt。
- EXE SHA-256：`3814a1cbb8c3e5c32994cc53cc65dcb72c9c2e6b1ee4e40ce7ff27523165329a`。
- 内嵌运行库：110 个文件、74.4 MB，LZMS 压缩后 19,482,634 字节；缓存目录 `%LOCALAPPDATA%\DevmemStudio\runtime\4.2.0-23ee15ccffc6`。
- 发布包：`dist/DevmemStudio-4.2.0-win64.zip`，19,510,383 字节。
- ZIP SHA-256：`814692ff54137f8740242c5f06d3d3572fba55a20e03afacef13e484a6ab94cd`。
- 发布包只包含 `DevmemStudio/DevmemStudio.exe`，不含本机配置、凭据、日志或测试数据；ZIP CRC 校验通过，包内 EXE 的 SHA-256 与构建产物一致。

## 本次变更

详见 README「4.2.0 发布」：

- 连接卡片：SSH / serial 每通道一行（状态圆点、目标摘要、连接按钮），参数按需展开、一次一个；Enter 连接；连接状态集中到卡片，标题行去掉重复徽章与会话摘要；修复类型徽标空间足够仍被省略。
- 单文件封装：原生启动器 + Windows LZMS 压缩运行库，每版本首次解压到用户缓存，之后直接复用；SHA-256 校验、原子发布、损坏自愈、旧版本缓存自动清理、并发启动互斥；任务栏固定指向外层 EXE。
- 启动与体积：SSH 库延迟到窗口出现后后台加载；剔除 `invoke`、OpenSSL 版 hashlib 与 `libcrypto-3.dll`、随包 UCRT 副本、Qt 翻译与多余平台插件；PYZ 以存储方式交给 LZMS 统一压缩。
- 集成打包 bit 工具与单行响应式工具栏（4.2.0 前期工作）。

内置组件目录未重生成或修改，保留 **57 个类型 / 2612 个寄存器**。

## 自动化验收

`build_exe.bat` 全流水线一次通过：固定依赖 → 图标生成 → 全量回归 → 源码离线 smoke → PyInstaller 运行库 → 单文件封装 → 单 EXE 隔离验收 → 发布 ZIP。

| 验收项 | 结果 |
| --- | --- |
| 全量回归（含本机 SSH 回环、虚拟串口、GUI、连接卡片、启动导入约束） | **294 项通过**，365.7 秒 |
| 按发布包剔除条件（无 `invoke`、无 `_hashlib`）重跑 SSH 回环全套 | **24 项通过** |
| 源码离线 smoke | **102 项检查全部通过** |
| 单 EXE 隔离验收 第 1 轮（空缓存，首次解压） | **102 项检查全部通过**，`passed: true` / `frozen: true` |
| 单 EXE 隔离验收 第 2 轮（人为损坏缓存 + 预置 2 小时前的旧版本缓存） | **102 项通过**；损坏文件已自动恢复，旧缓存已清理，无临时目录残留 |
| 两个实例同时首次启动（空缓存） | 均通过 smoke，仅生成一份运行库缓存 |
| Windows 文件与产品版本 | 均为 **4.2.0** |
| ZIP 完整性与内容 | CRC、包内 EXE 哈希、仅包含 EXE 均通过 |
| 源码与差异检查 | `compileall`、`git diff --check` 通过 |

单 EXE 隔离验收仅复制 EXE 启动，PATH 仅保留 Windows System32，清除 Python / venv 和外部 Qt 插件路径，并使用空的独立 `LOCALAPPDATA`；打包的 Paramiko 版本与项目 venv 一致。

## 启动速度

见 [启动速度验证](startup-performance.md)：日常启动中位数 3.09 秒 → **0.75 秒**（-76%），同版本仅首次启动需要解压。

## 界面检查

以下截图来自本次单 EXE 隔离验收（离线演示数据）：

- [基础组件（常规窗口）](basic-component.png)
- [紧凑窗口](compact.png)
- [会话终端](board-log.png)
- [字段解析](rpt-fields-compact.png)
- [主机密钥变化提示](host-key-change.png)
- [离线初始界面](offline.png)
- [批量写入预览](batch-preview.png)
- [打包 bit 弹窗](bitpack-dialog.png)

README 中的 [工作台](workbench.png) 与 [连接卡片三种状态](connection-card.png) 由同一源码在 1540×960 窗口离线渲染。

## 证据位置

- 流水线日志：`artifacts/claude-ui/build-exe-final.log`。
- 源码验收：`artifacts/build-smoke/report.json`。
- EXE 隔离验收：`artifacts/exe-isolated/acceptance-1/report.json`、`acceptance-2/report.json`。
- 单文件封装记录：`artifacts/singlefile-build.json`。
- 启动测速原始数据：`artifacts/startup-compare-*/report.json`、`artifacts/startup-singlefile-4.2.0/report.json`。

## 验证范围

本次使用离线模拟、确定性 SFTP/串口替身和本机 SSH 回环服务，**未连接或读写实际开发板**。
实际硬件上的文件权限、串口时序、总线访问行为、寄存器副作用、FPGA 加载和板端日志路径仍需现场验证。
RTL 解析不是完整的 Verilog 预处理/表达式求值器，复杂输入仍需人工核对地址。
