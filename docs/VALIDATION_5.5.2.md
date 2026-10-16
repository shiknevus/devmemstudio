# 5.5.2 验收记录（2026-10-16）

改动：修复停止监视后图表曲线与寄存器错位——空闲状态下图表按左侧勾选重新排布曲线时，原先用**当前**寄存器列表的序号去取采样列，停止监视后改动勾选或切换组件（列表随之重建）会取到别的寄存器的数据。现在 `MonitorBuffer` 记录本次运行固定的寄存器顺序，空闲图表与「清空数据」都按该顺序取列，并新增回归用例 `test_idle_chart_uses_buffer_addresses_after_run_list_is_rebuilt`。功能代码仅此一处改动（`devmem_studio/monitor.py`）。文档方面把 README 的历史变更记录拆分为 `docs/CHANGELOG.md`，并修正 README 中过期的监视间隔与命令超时说明。应用版本和 Windows 文件/产品版本统一为 5.5.2，固定版本资源为 5.5.2.0。

## 最终验证

执行 `tools/build.py`，完整构建 107 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 578 项，575 项通过，3 项依赖 POSIX sh 在 Windows 跳过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 6.5 s，损坏缓存自动修复及验收 6.6 s；陈旧缓存自动清理 |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.2 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 文档链接完整性 | README 与 `docs/CHANGELOG.md` 内 29 处相对链接全部指向存在的文件 |
| 构建源码一致性 | 构建产生的改动仅限本次发布内容（监视缓冲区、版本资源、README/CHANGELOG），无其他已跟踪文件被改写 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,258,240 字节 | `b1247cea95cae9c34dd51e68632fd5e97cdc2468655e6bf2a3733dd2b9525c78` |
| `dist/DevmemStudio-5.5.2-win64.zip` | 21,147,635 字节 | `4502138f6a914095cae24c5e925a4d8861cc581a75d6267644b86c9d13def9a7` |

GitHub 正式发布地址：[v5.5.2](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.2)，发布资产为 ZIP 与 `DevmemStudio-5.5.2-win64.zip.sha256`。
