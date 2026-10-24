# 5.6.3 验收记录（2026-10-24）

改动：
1. **GitHub 发布草稿机制与平滑替换**：`release_github.py` 发布新版本时先创建 Draft，待所有文件上传并完成 SHA-256 校验后再正式发布（`draft: False`），防止客户端在线检查到未传完资产的半成品版本；更新已有文件时采用临时文件名上传、校验、删除旧文件并重命名；新增配套测试 `tests/test_release_github.py`。
2. **在线升级启动确认握手**：升级助手 `apply_update.ps1` 与主程序通过 `DEVMEMSTUDIO_UPDATE_READY` 标志文件建立就绪握手（主窗口绘制完成即调用 `report_started` 回写），取代单纯依赖子进程计时的旧方式，升级成功判定更可靠；自动检查更新在资产未就绪时静默忽略。
3. **原生启动器 System32 动态加载安全加固与落盘保障**：`launcher.c` 针对 `cabinet.dll` 与 `bcrypt.dll` 采用 `LOAD_LIBRARY_SEARCH_SYSTEM32` 动态载入，防止同目录同名 DLL 劫持；解压写出文件增加 `FlushFileBuffers` 确保目录重命名原子生效前数据已真实刷盘。
4. **RTL 解析与组件目录更强健**：支持 `case[zx]` 形式及多层嵌套 case 的精准匹对；非零位起始切片（如 `[31:16]`）准确记录 `field_low`/`field_width`，总线访问位宽保持 32 位；`locate_components_param` 区分本工程路径与相邻工程兄弟目录，跨工程引用时预填基地址并保持可编辑；组件覆盖定义采用临时文件加原子替换（`write_json_atomic`）保存。
5. **工作台界面与多显示器支持**：表格与检查器字段长度精确显示切片范围（如 `16 位（bit 31:16）`）；跨屏幕移动与多显示器适配规则优化（基于全局 QRegion 联合几何计算，拖拽窗口时不争抢位置）；模态弹窗关闭后立即调用 `deleteLater` 释放任务栏记录。
6. **Bit 打包与监视器细节**：打包 ZIP 采用 `ZipInfo.from_file` 保留源文件原始修改时间戳，按需启用 zip64；监视器暂停/恢复跨采样器（regmon 与 shell）时重对齐时间轴；串口连续大文本粘贴增加 256 字节分块平滑发送。
7. **全局与多线程异常崩溃日志**：新增 `threading.excepthook` 记录后台子线程崩溃信息到 `thread-crash.log`，主线程异常弹窗排队防叠框。

## 最终验证

执行 `tools/build.py`，完整构建 101 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 694 项全部通过（72.0 s） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 4.9 s，损坏缓存自动修复及验收 4.7 s；陈旧缓存自动清理；修复路径无 `.tmp-*` 残留 |
| Windows 版本资源 | 文件版本、产品版本均为 5.6.3 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 文档链接完整性 | README 与 `docs/CHANGELOG.md` 内相对链接全部指向存在的文件；README 保持无历史发布段 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,372,416 字节 | `21859292e86632bf69b0b1f039aaea843cfef05f5958768243e3fac771d96ab0` |
| `dist/DevmemStudio-5.6.3-win64.zip` | 21,188,913 字节 | `90b188820dd066449a81442e371cc9f0035ba40caa2b7094b37da18d7447343d` |

GitHub 正式发布地址：[v5.6.3](https://github.com/shiknevus/devmemstudio/releases/tag/v5.6.3)，发布资产为 ZIP 与 `DevmemStudio-5.6.3-win64.zip.sha256`。
