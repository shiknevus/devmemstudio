# 5.6.2 验收记录（2026-10-24）

改动：启动提示窗每次启动都显示，进度条按阶段拼成一条 0→100%——启动器阶段（检查运行环境、首次解压、写入、启动 Python 运行时）与程序阶段（加载 Qt 界面库、加载模块、初始化样式、创建主窗口、载入 top）的比例按本机前几次启动实测自动调整（解压冷启动单独统计），满格后提示窗先关闭、主窗口随即出现；启动失败仍在提示窗内重试/取消。「更新控件」的组件覆盖定义改存 EXE 旁的 `component_overrides`（与 `registers.json` 同目录），随 EXE 一起复制即可带到其他电脑，旧版 `%LOCALAPPDATA%\DevmemStudio\component_overrides\` 的定义仍会读取、两处都有的取导入时间较新的一份。功能代码改动集中在 `tools/runtime_launcher/launcher.c`（`Progress` 共享内存块、阶段时序学习与 `%LOCALAPPDATA%\DevmemStudio\splash\<哈希>.times` 持久化、每启动显示、`begin_launch` 供重试重置）、新增 `devmem_studio/launcher_splash.py`（应用侧阶段上报与 ready/gone 交接）、`devmem_debug.py`/`devmem_studio/window.py`（上报各启动阶段）、`devmem_studio/core.py`/`top_import.py`/`window.py`/`smoke.py`/`dialogs.py`（组件覆盖定义便携化）；`tests/test_launcher.py` 新增阶段学习与每次启动显示测试。应用版本和 Windows 文件/产品版本统一为 5.6.2，固定版本资源为 5.6.2.0。

## 最终验证

执行 `tools/build.py`，完整构建 96 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 661 项全部通过（64.3 s） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 4.7 s，损坏缓存自动修复及验收 4.9 s；陈旧缓存自动清理；修复路径无 `.tmp-*` 残留 |
| 启动器专项（`tests/test_launcher.py`） | 通过：每次启动都显示提示窗（首次与二次均验证）；阶段时序按上次启动学习（冷/暖启动分别统计，各阶段耗时写入 `.times` 并在下次生效） |
| Windows 版本资源 | 文件版本、产品版本均为 5.6.2 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 文档链接完整性 | README 与 `docs/CHANGELOG.md` 内相对链接全部指向存在的文件；README 历史发布段已移除（历史变化见 GitHub 发布页） |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,365,760 字节 | `24915b34361ccc720d44ff7c1adb58316b2da351feee85ee720a12e07e80ae90` |
| `dist/DevmemStudio-5.6.2-win64.zip` | 21,182,448 字节 | `17ac044cec6a11e9c7413fb4b512c9fe1f061bb8ab45bdcd30e31fe92016ad80` |

GitHub 正式发布地址：[v5.6.2](https://github.com/shiknevus/devmemstudio/releases/tag/v5.6.2)，发布资产为 ZIP 与 `DevmemStudio-5.6.2-win64.zip.sha256`。