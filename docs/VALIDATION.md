# 寄存器调试工作台 4.1.8 验收记录

完成时间：2026-10-06（4.1.8 构建）

## 构建与发布产物

- 构建环境：本项目 venv，Python 3.12.2，Paramiko 4.0.0，PyInstaller 6.22.2，Windows x64。
- 依赖：按 `requirements-lock.txt` 安装；全部固定版本匹配，`pip check` 通过。
- 最终产物：`dist/DevmemStudio.exe`，31,383,044 字节。
- 应用版本、Windows 文件/产品版本均为 **4.1.8**；固定版本资源为 `4.1.8.0`，PE 架构为 x64。
- EXE SHA-256：`1bd7626411a82cfe2e825156087ddece96e5fe5091c7e326de3a74b7836ef7fc`。
- 发布包：`dist/DevmemStudio-4.1.8-win64.zip`，31,093,080 字节。
- ZIP SHA-256：`c3ed76504560f6c82bf33e9547cb0d9f03bdfc9091d7c89c381dda1c8d7708b0`。
- 发布包只包含 `DevmemStudio/DevmemStudio.exe`，不含本机配置、凭据、日志或测试数据；ZIP CRC 校验通过，包内 EXE 的 SHA-256 与构建产物一致。

## 本次修复

详见 README「4.1.8 发布」与 [缺陷排查记录](BUG_AUDIT_2026-10-06.md)：

- RTL 基地址忽略注释和相似宏名，偏移不再将表达式截断成常量；非法常量跳过并警告。
- 下载采用同目录临时文件和完成后替换，传输/替换失败不会破坏原文件。
- 串口处理密码提示、延迟登录提示和登录阶段断线，不再误报就绪。
- 异常地址配置恢复；密码记忆标志严格校验，异常真值不保存密码。
- 损坏组件表、预设、位域和行为元数据安全过滤，坏覆盖不遮蔽正常内置表。
- 离线静默读写始终执行参数校验，非法写入不污染模拟内存。

内置组件目录未重生成或修改，保留 **57 个类型 / 2612 个寄存器**。

## 自动化验收

与 `build_exe.bat` 相同的流水线逐步通过：固定依赖 → 图标生成 → 全量回归 → 源码离线 smoke → PyInstaller → 单 EXE 隔离验收 → 发布 ZIP。
本次使用独立的 4.1.8 PyInstaller 工作目录，未改动已有版本的 ZIP。

| 验收项 | 结果 |
| --- | --- |
| 全量回归（含本机 SSH 回环、虚拟串口、GUI） | **231 项通过**，382.692 秒；较 4.1.7 新增 23 项 |
| 源码离线 smoke | **75 项检查全部通过** |
| 单 EXE 隔离 smoke | **75 项检查全部通过**，`passed: true` / `frozen: true` |
| Windows 文件与产品版本 | 均为 **4.1.8** |
| ZIP 完整性与内容 | CRC、包内 EXE 哈希、仅包含 EXE 均通过 |
| 源码与差异检查 | `compileall`、`git diff --check` 通过 |

单 EXE 隔离验收仅复制 EXE 启动，PATH 仅保留 Windows System32，清除 Python / venv 和外部 Qt 插件路径；打包的 Paramiko 版本与项目 venv 一致。

## 界面检查

以下截图来自本次 4.1.8 单 EXE 隔离验收，均为明确标注的离线演示数据：

- [主窗口与寄存器解析](workbench.png)
- [会话终端（system 视图）](board-log.png)
- [紧凑窗口](compact.png)
- [基础组件](basic-component.png)
- [字段解析](rpt-fields-compact.png)
- [主机密钥变化提示](host-key-change.png)
- [离线初始界面](offline.png)
- [批量写入预览](batch-preview.png)

## 证据位置

- 流水线日志：`artifacts/release-4.1.8/build.log`。
- 源码验收：`artifacts/release-4.1.8/source-smoke/report.json`。
- EXE 隔离验收：`artifacts/release-4.1.8/exe-isolated/acceptance/report.json`。
- 版本、依赖、哈希与发布包校验：`artifacts/release-4.1.8/release-check.json`。
- 原始缺陷复现与修复验证：`docs/BUG_AUDIT_2026-10-06.md` 所列证据。

## 验证范围

本次使用离线模拟、确定性 SFTP/串口替身和本机 SSH 回环服务，**未连接或读写实际开发板**。
实际硬件上的文件权限、串口时序、总线访问行为、寄存器副作用、FPGA 加载和板端日志路径仍需现场验证。
RTL 解析不是完整的 Verilog 预处理/表达式求值器，复杂输入仍需人工核对地址。
