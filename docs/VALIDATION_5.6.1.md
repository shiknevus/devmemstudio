# 5.6.1 验收记录（2026-10-24）

改动：修复从 5.5.x 在线升级到 5.6.0 后提示失败并回滚；启动提示窗显示阶段与进度、失败在提示窗内重试/取消、主窗口显示前先关闭；启动器被强制结束时不再遗留挂起进程。功能代码改动集中在 `tools/runtime_launcher/launcher.c`（提示窗由隐藏窗口持有、失败界面、进度与 60 帧/秒动画、局部重绘、与程序的 ready/gone 握手、`PROC_THREAD_ATTRIBUTE_JOB_LIST` 让程序出生即在作业内）、`devmem_studio/taskbar.py` 与 `devmem_debug.py`（主窗口显示前请启动器关闭提示窗）；`tools/build_singlefile.py` 拆出 `pack()` 供测试打包假运行库；新增 `tests/test_launcher.py`。应用版本和 Windows 文件/产品版本统一为 5.6.1，固定版本资源为 5.6.1.0。

## 问题根因

5.6.0 首次运行解压时立即显示启动提示窗（无 owner 的可见工具窗口）。升级助手 `apply_update.ps1` 以 `Process.MainWindowHandle` 非零判定「新版本显示了错误框」，而 .NET Framework 只排除有 owner 或不可见的窗口，于是 0.9 s 内回滚并提示升级失败。升级时运行的是旧版自带的助手脚本，因此修复必须在新版 EXE 一侧：启动中的提示窗挂在隐藏 owner 下，失败界面去掉 owner 并设标题，旧助手照常据此回滚。

## 最终验证

执行 `tools/build.py`，完整构建 97 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 656 项全部通过（65.7 s） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 5.0 s，损坏缓存自动修复及验收 4.8 s；陈旧缓存自动清理；修复路径无 `.tmp-*` 残留 |
| 启动器专项（`tests/test_launcher.py`，私有桌面） | 8 项通过：启动中提示窗有 owner 且无标题；应用窗口出现即关闭；失败界面无 owner、带标题、含「重试/取消」且无额外错误框；重试在同一窗口重新启动；Esc 取消（退出码 3）；显示窗口前非 0 退出视为失败；正常 0 退出不报错；启动器被杀时程序随之结束（对 5.6.0 源码首轮即失败）；应用 ready 信号后提示窗先消失 |
| 旧版升级助手端到端 | 用 5.5.9 的 `apply_update.ps1` 安装本版 EXE（空运行库缓存）：3.8 s 成功；令启动失败时 0.8 s 判错回滚，报 `showed an error: DevmemStudio 5.6.1` |
| 提示窗与主窗口先后 | 首次运行 3 次：提示窗消失后 48–59 ms 主窗口才可见，2 ms 轮询中从未同时可见（5.6.0 两次均同时可见）；常规启动 0.76 s 出主窗口、不显示提示窗 |
| 进度条平滑度 | 逐帧记录：帧间隔中位 16.7 ms（5.6.0 为 47 ms）；百分比逐帧连续、单帧至多约 3%；满 100% 停留 200 ms 再切到往返动画；往返段单帧至多 7 px 且边缘亚像素过渡 |
| 提示窗 CPU | 往返动画期间启动器 1.9% 单核（局部重绘前 8.8%） |
| Windows 版本资源 | 文件版本、产品版本均为 5.6.1 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 文档链接完整性 | README 与 `docs/CHANGELOG.md` 内相对链接全部指向存在的文件 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,294,080 字节 | `65206e4fba077356db7fe75bede6185a08abfe5b80219e99d918f906bf5b9f3c` |
| `dist/DevmemStudio-5.6.1-win64.zip` | 21,178,160 字节 | `b6e7c25185c6fc6563680c124db3432588f26ed926fb6524c907213a8377c2ca` |

GitHub 正式发布地址：[v5.6.1](https://github.com/shiknevus/devmemstudio/releases/tag/v5.6.1)，发布资产为 ZIP 与 `DevmemStudio-5.6.1-win64.zip.sha256`。
