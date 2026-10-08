# 寄存器调试工作台 5.1.0 验收记录

完成日期：2026-10-08。

## 本次变更

- 寄存器监视列表与图表联动高亮：运行中可切换观察对象，点击曲线左侧名称定位列表；采样勾选与 A/B 标记互不影响，清空采样数据后保留高亮。
- 运行库拆为 3 个独立的 LZMS 压缩块，构建时并行压缩，首次释放或缓存修复时并行解压；继续校验完整运行库 SHA-256 并通过临时目录原子发布。
- 新增 `tools/build.py`：全量测试、源码验收、EXE 构建与隔离验收并行运行，全部通过后才更新 `dist`；固定依赖和图标已符合要求时跳过生成，运行库内容未变时复用压缩结果。
- 新增 `tools/run_tests.py`：按测试用时分块，多进程运行；正式构建包含 `--full` 检查。界面测试去重、模拟读取取消人为延迟、SSH 测试使用独立临时目录，串口夹具及进度条等待条件修正。
- 参数文件查找减少重复目录遍历；演示会话对象回收时清理临时模拟板端目录。
- README 增加本版本说明、监视操作步骤、升级方法、构建日志索引、测试命令、配置位置与常见问题。
- 应用版本、Windows 文件和产品版本统一为 5.1.0，固定版本资源为 5.1.0.0。

## 验证结果

环境：Windows 10 Enterprise x64（10.0.19045）、Python 3.12.2、PySide6 6.11.2、PyInstaller 6.22.2、Paramiko 4.0.0，使用项目 venv 和 `requirements-lock.txt` 固定依赖。

| 验收项 | 结果 |
| --- | --- |
| 全量回归（`tools/run_tests.py --full`） | 执行 363 项：360 项通过、3 项需 POSIX sh 在 Windows 上跳过；测试执行 28.5 秒，测试阶段 34.9 秒 |
| 源码离线验收 | 111 项检查通过，阶段耗时 11.3 秒 |
| 单 EXE 隔离验收：空缓存首次启动 | 111 项检查通过，5.5 秒 |
| 单 EXE 隔离验收：损坏缓存与旧缓存清理 | 111 项检查通过，5.4 秒；损坏缓存已修复，旧缓存已清理 |
| 三块运行库释放 | 隔离目录中独立 EXE 首次启动及缓存修复均通过，运行库缓存标识为 `5.1.0-ce9ba7e0e6cf` |
| 版本资源 | 文件/产品版本均为 5.1.0，固定版本 5.1.0.0 |
| ZIP 完整性与内容 | CRC 通过，仅含 `DevmemStudio/DevmemStudio.exe`；包内 EXE 与构建产物逐字节一致 |
| 构建输入一致性 | 构建开始前记录源码及构建输入 SHA-256，构建结束后比对无变更 |
| 打包源码与文档检查 | PYZ 中 19 个项目模块的编译内容与源码一致；42 个 Python 文件语法检查、README 本地文件链接和 Git 差异检查通过 |
| 完整构建 | `build_exe.bat` 成功，约 75 秒；各项检查通过后生成发布包 |

完整构建期间依赖版本与图标均已符合要求，因此跳过对应生成步骤。PyInstaller 阶段 46.9 秒，单文件封装阶段 14.5 秒，EXE 隔离验收阶段 13.0 秒，发布包生成 0.6 秒。各阶段并行运行，总耗时不等于各阶段耗时之和。

## 发布产物

- 单文件 EXE：`dist/DevmemStudio.exe`，19,836,928 字节。
- EXE SHA-256：`33781c86caa2c143b1ecf07b195c9bbd08886556621ebf60b6b281338fd84650`。
- 发布包：`dist/DevmemStudio-5.1.0-win64.zip`，19,726,817 字节。
- ZIP SHA-256：`3cbc71b7ae855c9b7698454a9131dc00def58b10f5d3607d427d8521ed643dad`。
- ZIP 内唯一文件：`DevmemStudio/DevmemStudio.exe`；不包含本机配置、密码或日志。
- 内嵌运行库：111 个文件，74,610,160 字节；3 个 LZMS 压缩块，共 19,697,654 字节。
- 本次压缩缓存未复用，重新生成了 5.1.0 运行库载荷。
- 缓存位置：`%LOCALAPPDATA%\DevmemStudio\runtime\5.1.0-ce9ba7e0e6cf\`。

## 证据位置

- 完整构建输出：`artifacts/release-5.1.0-build.log`。
- 各阶段日志：`build/logs/tests.log`、`smoke.log`、`pyinstaller.log`、`singlefile.log`、`verify_exe.log`、`release.log`。
- 源码离线验收：`artifacts/build-smoke/report.json`。
- EXE 隔离验收：`artifacts/exe-isolated/acceptance-1/report.json` 与 `acceptance-2/report.json`。
- 单文件构建信息：`artifacts/singlefile-build.json`。
- 构建输入快照：`artifacts/release-5.1.0-source-snapshot.json`。
- 发布包及源码一致性检查：`artifacts/release-5.1.0-integrity.json`。

## 验证边界

EXE 隔离验收仅复制单文件 EXE，使用独立的 `LOCALAPPDATA`，PATH 仅保留 Windows System32，并清除 Python、venv 及外部 Qt 插件路径。验收使用 offscreen 渲染与缩放系数 1；这两轮启动验收耗时包含完整离线检查，不能作为用户主窗口启动时间或纯解压时间。

本次未连接或改写实际开发板，未新增实板采样周期、CPU 占用或物理串口拔插测量。此前的实板验证与常驻采样程序仿真范围见 [5.0.0 验收记录](VALIDATION_5.0.0.md)。3 项 POSIX sh 测试在当前 Windows 环境跳过，未将其计为通过。
