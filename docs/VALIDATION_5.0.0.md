# 寄存器调试工作台 5.0.0 验收记录

完成日期：2026-10-07。

## 本次变更

- 串口改为实时字符终端：收发线程持续读取，板卡自发打印实时显示；按键逐个直发板卡；新增 80 列 vt102 行模型（`console_screen.py`）跟随 readline 的换行与跨行退格。
- 新增「拦截U-Boot」：倒计时出现时自动按住 `&` 直到 U-Boot 提示符，随后发送 Ctrl+C 清除多余字符。
- 连接串口不再发送任何字符；自动登录改为监听 `login:` 提示，只在自动发送用户名后回填密码。
- 串口拔出立即检测（读口异常即断开）；`WM_DEVICECHANGE` 触发立即重扫端口；端口被占用时明确提示。
- 「跟随」勾选即跳到最新输出。
- 「上传bit」「导入 top」支持选择文件夹，多个候选时列表选择；「导入 top」按钮改为文件 / 文件夹菜单。
- 「上传bit」「回退bit」板端目录改为下拉框：默认 `/run/media/sda`，自动列出板端 `/run/media` 挂载目录，记住上次使用的目录。
- 应用版本、Windows 文件与产品版本同步更新为 5.0.0，固定版本资源为 5.0.0.0。

## 实板验证（ZCU102，PetaLinux 2018.3，SSH 192.168.2.83，串口 COM35）

| 验证项 | 结果 |
| --- | --- |
| 板卡自发输出 | 板端向 `/dev/ttyPS0` 写入的消息未发命令即出现在 serial 终端 |
| 长命令行编辑 | 提示符后输入 100 字符（跨 80 列换行）、55 次退格跨回上一行、回车：终端显示的命令行与板端执行结果一致 |
| 历史 / 补全 / 中断 / 行内插入 | ↑ 调出上一条长命令、Tab 补全 `/run/media/`、Ctrl+C、← 后插入字符，显示均与板端一致 |
| U-Boot 单次按键 | 倒计时出现后 0 ms 或 0.6 s 单按一次 `&`，U-Boot 不停（该板需连续按键） |
| 拦截U-Boot（GUI 全流程） | 勾选后经 SSH 重启：连续 3 个 `&`（间隔 40 ms）停在 `ZynqMP>`；终端执行 `version` 有输出；输入 `boot` 后 25.3 秒回到 Linux 提示符，期间 426 行内核日志实时显示 |
| 板端目录 | 自动列出 `/run/media/sda`；上传到所选临时目录两次，第二次生成时间戳备份；目录记忆排在历史首位；回退窗口按该目录列出当前版本与备份；测试目录已清除，`/run/media/sda/sunny_fpga.bit` 未改动 |
| 设备插拔通知 | 真实 Windows 平台向窗口发送 `WM_DEVICECHANGE`，两次通知合并为一次立即重扫 |

串口线物理拔出未在本次实板验证中操作；读口异常断开与端口消失两条路径均有回归测试覆盖。

## 验证结果

| 验收项 | 结果 |
| --- | --- |
| 全量回归（含本机 SSH 回环、串口假口、GUI） | 332 项通过，398.8 秒 |
| 源码离线验收 | 111 项检查通过（新增串口终端、跟随、板端目录、文件夹查找 7 项） |
| 单 EXE 隔离验收：空缓存首次启动 | 111 项检查通过，9.8 秒 |
| 单 EXE 隔离验收：损坏缓存与旧缓存清理 | 111 项检查通过，9.8 秒；损坏缓存已修复，旧缓存已清理 |
| 发布版版本资源 | 文件/产品版本均为 5.0.0，固定版本 5.0.0.0 |
| ZIP 完整性与内容 | CRC 通过，仅含 EXE，包内 EXE 与构建产物逐字节一致 |
| 源码与差异检查 | `compileall` 与 `git diff --check` 通过 |

隔离验收仅复制 EXE，使用独立的 `LOCALAPPDATA`，PATH 仅保留 Windows System32，并清除 Python、venv 与外部 Qt 插件路径。

## 发布产物

- 单文件 EXE：`dist/DevmemStudio.exe`，19,643,392 字节。
- EXE SHA-256：`e8ebfec9ae669d5063583bad588b413aea66d8335fc9326342df54ffefcad519`。
- 发布包：`dist/DevmemStudio-5.0.0-win64.zip`，19,532,604 字节。
- ZIP SHA-256：`ff9eddbec0ef163f8cb37b4d9c7590ca0a243f31d4702290600ae162a733c2df`。
- ZIP 内唯一文件：`DevmemStudio/DevmemStudio.exe`。
- 内嵌运行库：110 个文件；LZMS 压缩后 19,504,626 字节。
- 缓存标识：`5.0.0-30a4ff369717`。

## 证据位置

- 全量回归与源码验收：`artifacts/build-smoke/report.json`。
- EXE 隔离验收：`artifacts/exe-isolated/acceptance-1/report.json` 与 `acceptance-2/report.json`。
- 实板脚本与抓包：`artifacts/realboard/`（`serial_live.py`、`uboot_e2e.py`、`uboot_capture.txt`、`bitdir_e2e.py`）。
