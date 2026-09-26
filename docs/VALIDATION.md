# 寄存器调试工作台 4.1.5 验收记录

完成时间：2026-09-26（4.1.5 修订构建）

- 构建环境：本项目 venv，Python 3.12.2，Paramiko 4.0.0，Windows 10 x64。
- 最终产物：`dist/DevmemStudio.exe`；文件及产品版本均为 4.1.5。
- 发布包：`dist/DevmemStudio-4.1.5-win64.zip`。
- EXE：31,368,254 字节，SHA-256 `ec9fa690c3c5e1639b48f31ce9f32711f6204fe1f66a7a7fa1d019322a9d98d8`。
- 独立 EXE 验收：只复制 EXE 运行，PATH 仅保留 Windows System32，清除 Python / venv 及 Qt 外部插件路径。

## 本次功能

会话终端重构为四来源（tail log / ssh / serial / system）独立缓冲，终端本身即 shell；
串口连接成为独立于 SSH 的并行通道，连接与断开均可主动取消；侧栏可拖拽调宽；
顶部横幅并入寄存器标题行；使用指南同步当前交互。

4.1.5 修订（09-26 全面评审）：bit 上传先传后换、rename 失败回滚；批量读取按标签切分；
拼接字段按 RTL 声明位宽（内置目录修正 35 处）；待写入单元格按 HEX；关闭窗口不再卡死；
自动读取不抢输入焦点；串口握手/断开竞态；RTL 解析注释、BOM、字面量与无标签例化修复。
详见 README「4.1.5 修订」。

## 自动化验收

`build_exe.bat` 流水线一次通过：全量 174 项回归测试（OK，含本次新增 12 项）→ 离线 smoke 75 项检查（全部通过）
→ PyInstaller 打包 → EXE 隔离验证 → 发布包生成。

重点覆盖：

- 会话终端四来源缓冲隔离、切换、默认 system 且不记忆。
- 终端内输入命令（回车执行、↑↓ 历史）、tail log / system 只读。
- 串口与 SSH 同时在线、互不禁用；两种连接的取消均可即时中止。
- serial 握手期间显示与 SSH 相同的不定进度条，结束后收回。
- SSH / serial 双徽章状态：离线 / 连接中 / 已连接 / 演示，宽度固定不跳动。
- 底栏状态提示并入 system 终端，底栏只留读写错误计数与版本。
- 查找日志随输随查、当前/总数计数、Ctrl+F 填充选中文字；导出当前终端。
- 侧栏拖拽调宽、寄存器标题行在 1180–1920 宽度下不重叠。
- 主机密钥变化确认/拒绝、寄存器读写回读、批量读写、CSV 快照、导入组件。
- bit 上传中断不动原 bit、换入失败恢复、回退失败恢复；上传进行中关闭窗口正常退出。
- 批量读取静默 devmem 不吞下一项；待写入单元格按 HEX 解析；拼接字段按声明位宽。
- 无标签 top 例化不串联、注释 bias 忽略、整块/块注释组件禁用、BOM、地址重叠警告。

## 界面检查

- [主窗口与寄存器解析](workbench.png)
- [会话终端（system 视图）](board-log.png)
- [紧凑窗口](compact.png)
- [基础组件](basic-component.png)
- [字段解析](rpt-fields-compact.png)
- [主机密钥变化提示](host-key-change.png)

截图均为明确标注的离线演示数据（DemoSession），未连接实际板卡。

## 证据位置

- EXE 隔离验收：`artifacts/exe-isolated/acceptance/report.json`（75 项检查全部通过）
- 全量回归结果：包含在 `build_exe.bat` 构建日志中
- 完整构建流水线日志：`artifacts/build.log`

## 验证范围

本次用离线演示和本机 SSH 回环服务进行验收，未连接或读写实际开发板。实际硬件上的权限、
总线访问行为、寄存器副作用和板端日志路径仍需在现场验证。
