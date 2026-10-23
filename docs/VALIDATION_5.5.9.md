# 5.5.9 验收记录（2026-10-23）

改动：「回退bit」对话框新增「下载到本地」——在备份列表选择某个版本后可保存到本机，板端文件保持不变。功能代码改动集中在 `devmem_studio/dialogs.py`（`BitRollbackDialog` 增加 `download_requested` 信号与「下载到本地」按钮、`_refresh_actions` 统一按钮可用性）与 `devmem_studio/window.py`（`_start_bit_download` 下载任务、`_bit_download_name` 文件名转换、下载进度约 10 Hz 节流、记住下载目录 `bit_local_dir`），帮助文本与 README 使用流程同步更新。应用版本和 Windows 文件/产品版本统一为 5.5.9，固定版本资源为 5.5.9.0。

## 最终验证

执行 `tools/build.py`，完整构建 107 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 643 项全部通过（60.2 s） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 7.0 s，损坏缓存自动修复及验收 6.0 s；陈旧缓存自动清理 |
| 组件目录 | RTL 根未配置（${DEVMEMSTUDIO_RTL} 未设置），沿用内置目录 |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.9 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 文档链接完整性 | README 与 `docs/CHANGELOG.md` 内相对链接全部指向存在的文件 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,279,232 字节 | `ed90937f8c59dbc1ef1845280f20845d892728b48c47fd28bb8786f48157e8f2` |
| `dist/DevmemStudio-5.5.9-win64.zip` | 21,169,198 字节 | `2b92e316fa0a9a24a24b6d53cec8abcd135675eaa79de0243e55be6d782d83bc` |

GitHub 正式发布地址：[v5.5.9](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.9)，发布资产为 ZIP 与 `DevmemStudio-5.5.9-win64.zip.sha256`。