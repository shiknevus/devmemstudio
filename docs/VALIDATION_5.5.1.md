# 5.5.1 验收记录（2026-10-15）

改动：寄存器监视新增**采样间隔设置**——监视窗口标题行加入采样间隔输入框（1–60000 ms，默认 5 ms），运行与暂停期间锁定、下次开始监视时生效，选择的间隔持久化到配置（`normalize_monitor_settings` 校验并回退默认值）；监视按钮、启动按钮与状态提示随设定间隔动态更新。功能代码未做其他改动。应用版本和 Windows 文件/产品版本统一为 5.5.1，固定版本资源为 5.5.1.0。

## 最终验证

执行 `tools/build.py`，完整构建 102 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 577 项，574 项通过，3 项依赖 POSIX sh 在 Windows 跳过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 5.9 s，损坏缓存自动修复及验收 6.3 s；陈旧缓存自动清理 |
| Windows 版本资源 | 文件版本、产品版本均为 5.5.1 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 构建源码一致性 | 构建结束后与构建前已跟踪文件的 SHA-256 快照一致，工作树仅含本次发布改动 |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,252,608 字节 | `4057758c33e43f13a2ae2f2b671cbc11955d6a3f6ebcb0ac5a0aa946a0900fa0` |
| `dist/DevmemStudio-5.5.1-win64.zip` | 21,142,771 字节 | `507f72589fdeb95de6df7129f7aff3bdfef4b26925d26bd1ce4c2f0b7beaa234` |

GitHub 正式发布地址：[v5.5.1](https://github.com/shiknevus/devmemstudio/releases/tag/v5.5.1)，发布资产为 ZIP 与 `DevmemStudio-5.5.1-win64.zip.sha256`。