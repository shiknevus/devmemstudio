# 5.5.0 验收记录（2026-10-15）

本版在现有功能代码基础上完成版本发布整理：新增实时读取入口，监视固定为 1 ms 采样并支持暂停/恢复，曲线使用分段与极值缓存并对 Ctrl + 滚轮缩放做平滑处理；README 和离线验收截图同步更新。构建只使用现有组件寄存器目录，未刷新或改写功能性生成物。应用版本和 Windows 文件/产品版本统一为 5.5.0，固定版本资源为 5.5.0.0。

## 最终验证

执行 `tools/build.py --no-catalog`，完整构建约 94 秒。所有并行阶段均通过，构建结束后才写入 `dist/` 发布产物。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 577 项，574 项通过，3 项依赖 POSIX sh 在 Windows 跳过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 6.3 s，损坏缓存自动修复及验收 6.2 s；陈旧缓存自动清理 |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.0 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 运行库 | 116 个文件、3 个 Windows LZMS 压缩块；运行库标识 `5.5.0-2c07318a72de` |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | ---: | --- |
| `dist/DevmemStudio.exe` | 21,256,192 字节 | `02f4bfbc23855bf35399ba752f1584c6ceac854c72c782563813b59140aeb048` |
| `dist/DevmemStudio-5.5.0-win64.zip` | 21,147,317 字节 | `f13d97432e30209e0f25fba5c5a46eb840c277aa4be9132014952297f227a5bd` |

构建日志位于 `build/logs/`，源码离线验收报告位于 `artifacts/build-smoke/report.json`，单文件封装信息位于 `artifacts/singlefile-build.json`。GitHub 正式发布地址：[v5.5.0](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.0)，发布资产为 ZIP 与 `DevmemStudio-5.5.0-win64.zip.sha256`。
