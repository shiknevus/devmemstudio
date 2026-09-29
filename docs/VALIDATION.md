# 寄存器调试工作台 4.1.7 验收记录

完成时间：2026-09-29（4.1.7 构建）

- 构建环境：本项目 venv，Python 3.12.2，Paramiko 4.0.0，Windows 10 x64。
- 最终产物：`dist/DevmemStudio.exe`；文件及产品版本均为 4.1.7。
- 发布包：`dist/DevmemStudio-4.1.7-win64.zip`。
- EXE：31,381,449 字节，SHA-256 `5117516815ed4974639cc75243e60a73a888b081975d56ef804614d7c8772fde`。
- 独立 EXE 验收：只复制 EXE 运行，PATH 仅保留 Windows System32，清除 Python / venv 及 Qt 外部插件路径。

## 本次功能

导入 top 按 `ec_` 实例解析全部控件。有 `flow_comp` 头的块仍用头上的名称和禁用态；
没有头、或同一个头下面还有别的 `ec_` 实例时一并收进来。详见 README「4.1.7 发布」。

## 自动化验收

与 `build_exe.bat` 相同的流水线一次通过：全量 208 项回归测试（OK，较 4.1.6 的 207 项新增 1 项）
→ 离线 smoke 75 项检查（全部通过）→ PyInstaller 打包 → EXE 隔离验证 → 发布包生成。

重点覆盖：

- 有 `flow_comp` 头的文件仍保留头上的名称、编号和禁用态。
- 头前面或块外的 `ec_*` 实例一并解析，不再只留下带头的那一个控件。
- 同一个头下面的第二个 `ec_` 实例不会被第一个挤掉。
- 主机密钥变化确认/拒绝、寄存器读写回读、批量读写、CSV 快照、导入组件。

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
- 全量回归结果：208 项，`Ran 208 tests` / `OK`

## 验证范围

本次用离线演示和本机 SSH 回环服务进行验收，未连接或读写实际开发板。实际硬件上的权限、
总线访问行为、寄存器副作用和板端日志路径仍需在现场验证。
