# 寄存器调试工作台 2.0.8 验收记录

完成时间：2026-09-09T13:32:51+08:00

- 构建环境：本项目 venv，Python 3.12.2，Paramiko 4.0.0，Windows 11 x64。
- 最终产物：dist/DevmemStudio.exe，52,349,331 字节。
- 格式：Windows x64 GUI 单文件 EXE，无控制台窗口；文件及产品版本均为 2.0.8。
- 发布包：dist/DevmemStudio-2.0.8-win64.zip；ZIP 完整性和文件清单检查通过。
- SHA-256：`1f4e3a9d200ce8e462e4c6ff740c83e25c49cf2f65feb7f83f22f5fd1cb790df`

## 本次功能

打印日志窗口默认开启「自动换行」和「显示行号」，两个开关可分别切换。长文本与连续十六进制数据随窗口宽度折行显示，复制和保存仍保留原始内容。

左侧行号与 Ctrl+G 使用相同的缓存原始行编号。一条原始日志折成多行时，只在首个显示行绘制行号；续行不占用新编号。行号宽度随位数和字体调整，与日志视口同步滚动及缩放。当前定位行的行号使用金色强调。

切换换行或行号显示保留当前查找结果与自动跟随状态，日志继续接收。最多保留最近 5000 个原始行；淘汰旧行或清空后按当前缓存重新编号。

既有高亮、快捷查找与跳转、独立日志通道、SSH 兼容与主机密钥恢复功能保留。基础组件与 irq1 / irq2、a / b / c rpt 字段解析继续通过回归验收。

## 自动化验收

52 项回归测试全部通过。本次重点覆盖：

- 中文、表情与连续无空格十六进制长日志自动折行，原始行数不变；关闭换行后恢复单行及横向滚动。
- 实际复制与保存的文本等于接收原文，不包含行号或显示换行新增的换行符。
- 切换两项显示开关后，当前匹配位置、匹配计数和跟随状态保留；Ctrl+G / Enter 仍跳至原始日志行。
- 查找期间继续接收新日志，匹配数量更新；回到最新后切换换行仍跟随日志末尾，显示操作不会启动或停止日志通道。
- 行数由 99 增至 100 时行号栏扩宽；字体调整、窗口缩放后行号栏与视口对齐；缓存淘汰后缩窄并按剩余日志重新编号。
- 最终 EXE 验证默认开关、连续长数据折行、查找保留、行号开关、原始行跳转及紧凑窗口控件完整可见。

既有 SSH 兼容、主机密钥变化恢复、独立日志窗口、高亮查找、日志与寄存器并行操作测试继续通过。

| 场景 | 结果 | 检查项 | 实际设备像素比 | 内置 Paramiko |
|---|---|---:|---:|---|
| build-smoke | 通过 | 70 | 1.0 | 4.0.0 |
| exe-isolated | 通过 | 70 | 1.0 | 4.0.0 |
| exe-native | 通过 | 70 | 1.25 | 4.0.0 |
| exe-dpi150 | 通过 | 70 | 1.875 | 4.0.0 |

独立 EXE 验收只复制 EXE，PATH 仅保留 Windows System32，清除 Python / venv 及 Qt 外部插件路径。三个 EXE 验收副本、ZIP 内 EXE 的 SHA-256 均与最终发布 EXE 一致。高 DPI 场景在本机系统缩放基础上设置 QT_SCALE_FACTOR=1.5。

本机 registers.json 的 SHA-256 在修改前后保持一致，发布 ZIP 和 EXE 数据文件中不包含设备配置或 known_hosts。发布包使用说明已包含自动换行和行号操作说明。

## 界面检查

- [日志自动换行、行号及高亮查找](board-log.png)
- [紧凑日志窗口](board-log-compact.png)
- [主窗口与寄存器解析](workbench.png)
- [基础组件](basic-component.png)

已目视检查源码、原生 EXE 及高 DPI EXE 的日志截图：行号与对应原始日志首行对齐，长日志在视口内完整折行，续行不重复编号，底部开关、查找与跳转控件均可见。截图和导出验收文件均为明确标注的离线演示数据。

## 证据位置

- 完整构建流水线日志：artifacts/build-final.log
- 日志回归测试：tests/test_log_view.py；运行结果包含在完整构建日志中
- 发布包与配置校验：artifacts/log-layout-release-audit.json
- 开发版本验收：artifacts/build-smoke/report.json
- 单 EXE 隔离验收：artifacts/exe-isolated/acceptance/report.json
- Windows 原生验收：artifacts/exe-native/acceptance/report.json
- 高 DPI 验收：artifacts/exe-dpi150/acceptance/report.json
- 高 DPI 日志截图：artifacts/exe-dpi150/acceptance/board-log-compact.png

## 验证范围

本次用离线演示和本机 SSH 回环服务进行验收，未连接或读写实际板卡。行号表示当前保留日志缓存中的行，并非板端文件从文件开头计算的绝对行号；tail -f 默认输出末尾内容，现有日志缓存机制保持不变。
