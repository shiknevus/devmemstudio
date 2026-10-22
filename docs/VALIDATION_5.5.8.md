# 5.5.8 验收记录（2026-10-22）

改动：PARAM 寄存器显示板端 DI/DO 接线——RTL 解析收割组件端口接线（纯别名连线追溯），组件目录记录参数寄存器的组件端口，top 导入记录组件实例的板端接线，主窗口在参数寄存器显示名后追加接线（如 `PARAM68 · i_axis_zero/origin · ~di_mst_msg[0]`），接线纳入上下文缓存区分。功能代码改动集中在 `devmem_studio/component_parse.py`（`wired_ports`/`plain_assigns`/`module_ports` 等）、`devmem_studio/top_import.py`（`_attach_wiring`）、`devmem_studio/window.py`（显示名与上下文），`component_catalog.json` 随 RTL 重新生成（新增 `ports` 字段），测试同步更新。应用版本和 Windows 文件/产品版本统一为 5.5.8，固定版本资源为 5.5.8.0。

## 最终验证

执行 `tools/build.py`，完整构建 117 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 640 项全部通过（69.4 s） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 7.8 s，损坏缓存自动修复及验收 7.4 s；陈旧缓存自动清理 |
| 组件目录 | RTL 根未配置（${DEVMEMSTUDIO_RTL} 未设置），沿用内置目录（已含 `ports` 字段） |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.8 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 文档链接完整性 | README 与 `docs/CHANGELOG.md` 内相对链接全部指向存在的文件 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,276,672 字节 | `4ff8a019d6b3b7543b6db4a1bf7614c4b642cc861e899873fa9dc22f938b215b` |
| `dist/DevmemStudio-5.5.8-win64.zip` | 21,166,511 字节 | `9c6e835541190323c8c39390e2cfd7a23b0b0ac4c1c6eeb834810ecf4e2cbd13` |

GitHub 正式发布地址：[v5.5.8](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.8)，发布资产为 ZIP 与 `DevmemStudio-5.5.8-win64.zip.sha256`。
