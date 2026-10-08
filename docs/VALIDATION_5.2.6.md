# 5.2.6 验收记录（2026-10-08）

改动：寄存器监视的勾选按组件类型记忆（按偏移匹配，实例自己的勾选优先，只打开不写入记忆）；README 图片与表格居中。应用版本和 Windows 文件/产品版本统一为 5.2.6，固定版本资源为 5.2.6.0。

## 最终验证

执行 `tools/build.py`，完整构建 98 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 395 项通过 |
| 监视记忆回归 | 新增同类型实例沿用勾选、实例优先、重启后保留；配置清洗覆盖 `types` |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过，首次解压 7.0 s，缓存损坏自动修复 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,115,392 字节 | `06a9f993e8b7bd2f4285e7bf3150b305d10164255ee4a40d7c358d17d922a956` |
| `dist/DevmemStudio-5.2.6-win64.zip` | 21,004,801 字节 | `5f0dbcdfcc0715c81f5d1a385d43685bd95673badd6802a36ed18b5cae974493` |

GitHub Release `v5.2.6` 上传 ZIP 与 `DevmemStudio-5.2.6-win64.zip.sha256`。
