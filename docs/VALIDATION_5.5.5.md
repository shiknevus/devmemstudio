# 5.5.5 验收记录（2026-10-19）

改动：按位宽读写寄存器、专属 COM 扫描线程池解耦、无参数例化识别与真 LRU 草稿保存。功能代码集中在：
- `devmem_studio/core.py` 与 `devmem_studio/widgets.py`：底层支持 8/16/32/64 位宽读写与参数校验，界面联动位宽选择器与 64 位掩码保护。
- `devmem_studio/window.py`：串口异步枚举扫描从全局共享线程池剥离至专属单线程池（`ports_pool`），彻底消除图表监控与高频读写时的线程饥饿；`_remember_reg` 实现真 LRU 记忆与未改动草稿保持。
- `devmem_studio/top_import.py` 与 `devmem_studio/component_parse.py`：顶层无参数模块实例化（parameterless instance）精确提取与去重；RTL 端口高阻态与通配符清洗规则优化及告警去重。
- `devmem_studio/updater.py`：升级调度轮询与退出优化，消除忙等待。
应用版本和 Windows 文件/产品版本统一为 5.5.5，固定版本资源为 5.5.5.0。

## 最终验证

执行 `tools/build.py`，完整构建 95 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 613 项，613 项全部通过（54.1 s） |
| 源码离线验收 | 通过（8.7 s） |
| 单 EXE 隔离验收 | 通过（14.7 s）；首次解压及验收，损坏缓存自动修复及验收；陈旧缓存自动清理 |
| 组件目录 | 沿用当前表（无变化不改写） |
| 快速工程检查 | `tools/check_project.py` 通过（依赖锁定、源码编译、目录完整性） |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.5 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,263,872 字节 | `5ed86987331def04d9f74b591e68a943d975a4abae55f5ee5e17b58523e16dec` |
| `dist/DevmemStudio-5.5.5-win64.zip` | 21,154,018 字节 | `9c3e8c04775436a5cf3d817780409cd5afcc38480a40f3d7f88003ee62cc1d8d` |

GitHub 正式发布地址：[v5.5.5](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.5)，发布资产为 ZIP 与 `DevmemStudio-5.5.5-win64.zip.sha256`。
