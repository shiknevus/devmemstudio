# 5.5.3 验收记录（2026-10-16）

改动：监视内存占用与回退采样限速，以及工程检查/清理工具与 CI。功能代码改动集中在 `devmem_studio/monitor.py`（按寄存器数量限制历史点数、有符号缓冲按需创建）与 `devmem_studio/core.py`（`devmem` 回退循环最小间隔 5 ms，常驻采样仍为 1 ms）；工具侧新增 `tools/check_project.py`、`tools/cleanup_workspace.py` 与 `.github/workflows/checks.yml`，`tools/gen_component_catalog.py` 增加 `--strict-catalog`（由 `tools/build.py` 转发）。文档方面把 5.5.3 变化写入 README，历史变更记录保持 5.5.2 及更早版本。应用版本和 Windows 文件/产品版本统一为 5.5.3，固定版本资源为 5.5.3.0。

## 最终验证

执行 `tools/build.py`，完整构建 113 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 588 项，585 项通过，3 项依赖 POSIX sh 在 Windows 跳过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 7.2 s，损坏缓存自动修复及验收 6.8 s；陈旧缓存自动清理 |
| 组件目录 | 沿用当前表（62 类型，无变化不改写）；`--strict-catalog` 门槛可按需启用 |
| 快速工程检查 | `tools/check_project.py` 通过（依赖锁定、源码编译、目录完整性） |
| 清理工具 | `tools/cleanup_workspace.py` 默认预览 73 项 / 680.25 MiB，保留最新两个发布版本，未删除任何文件 |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.3 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,258,240 字节 | `f5d8ed98261d237cf959a7346e69309b1597e207305dcc1bcd1219f2961bc538` |
| `dist/DevmemStudio-5.5.3-win64.zip` | 21,148,324 字节 | `218c8e0dd639220c9a9f3dd2fb482593b468bb86526ce1594264e14604690092` |

GitHub 正式发布地址：[v5.5.3](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.3)，发布资产为 ZIP 与 `DevmemStudio-5.5.3-win64.zip.sha256`。
