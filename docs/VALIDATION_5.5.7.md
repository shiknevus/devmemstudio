# 5.5.7 验收记录（2026-10-20）

改动：多组件同时监视、监视停止/暂停立即终止板端采样通道、RTL 解析与寄存器表增强（字符串字面量注释保护、全 case 块读臂、begin/end 写块归因、条件编译告警、无偏置无参数例化容错）、串口专属线程池、升级助手可选签名固定、下载残留清理、测试 LOCALAPPDATA 隔离。功能代码改动集中在 `devmem_studio/monitor.py`、`devmem_studio/core.py`、`devmem_studio/component_parse.py`、`devmem_studio/top_import.py`、`devmem_studio/updater.py`、`devmem_studio/window.py`，工具与测试同步更新。应用版本和 Windows 文件/产品版本统一为 5.5.7，固定版本资源为 5.5.7.0。

## 最终验证

执行 `tools/build.py`，完整构建 100 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 637 项全部通过（64.7 s） |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 6.0 s，损坏缓存自动修复及验收 6.0 s；陈旧缓存自动清理 |
| 组件目录 | RTL 根未配置（${DEVMEMSTUDIO_RTL} 未设置），沿用内置目录 |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.7 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 文档链接完整性 | README 与 `docs/CHANGELOG.md` 内相对链接全部指向存在的文件 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,273,600 字节 | `53e606184d59be04c6889b4e2d2fd0df1524db3300757eb7174cb6ae68d4ad9b` |
| `dist/DevmemStudio-5.5.7-win64.zip` | 21,164,766 字节 | `b47260b883a163d3ed240e3b32875a03a94599b00a16b228f04210c62f05da71` |

GitHub 正式发布地址：[v5.5.7](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.7)，发布资产为 ZIP 与 `DevmemStudio-5.5.7-win64.zip.sha256`。
