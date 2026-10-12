# 5.4.1 验收记录（2026-10-12）

改动：寄存器监视「按时间段导出」交互增强——选区左右边缘可直接拖动调整范围（边缘调整手柄、左右箭头光标，可拖过对侧翻转选区），在选区外拖动重新选择，在选区内点击保持原选区，选区高亮与边缘线只绘制可见区域；新增选区边缘调整、拖动重新选择、跨越翻转与越界钳制的回归测试。打包内置寄存器表随最新 RTL 刷新：`ec_slv_pul_axis` 增加 `PARAM56-59`（slave 调试字 12-15）；已从 RTL 移除的 `ec_dv300_485_modbus_rtu`、`ec_superisys_485_modbus_rtu`、`ec_sygole_485_modbus_rtu` 类型按设计保留（老 bit 仍在使用）。应用版本和 Windows 文件/产品版本统一为 5.4.1，固定版本资源为 5.4.1.0。

## 最终验证

执行 `tools/build.py`，完整构建 149 秒。

| 项目 | 结果 |
| --- | --- |
| 完整回归（`--full`） | 执行 512 项，509 项通过，3 项依赖 POSIX sh 在 Windows 跳过 |
| 源码离线验收 | 113 项通过 |
| 单 EXE 隔离验收 | 113 项通过；首次解压及验收 7.8 s，损坏缓存自动修复及验收 7.8 s；陈旧缓存自动清理 |
| Windows 版本资源 | 文件版本、产品版本均为 5.4.1 |
| ZIP 与校验文件 | ZIP 仅含 `DevmemStudio/DevmemStudio.exe`，`.sha256` 匹配，不含本机配置或凭据 |
| 构建源码一致性 | 构建结束后与构建前已跟踪文件的 SHA-256 快照一致，工作树仅含本次发布改动（含构建期 catalog 刷新产生的目录变化） |

## 发布产物

| 文件 | 大小 | SHA-256 |
| --- | --- | --- |
| `dist/DevmemStudio.exe` | 21,229,056 字节 | `7350381998c6f3934b2c4ac376badcb6821934efe2f2a6ee0cf18480cc1764c7` |
| `dist/DevmemStudio-5.4.1-win64.zip` | 21,119,502 字节 | `c3bc26ed584a13af236c4be56f024a1dc31fc2a9ee0bc0d46a9c90b955666987` |

GitHub 正式发布地址：[v5.4.1](https://github.com/shiknevus/devmemstudio/releases/tag/v5.4.1)，发布资产为 ZIP 与 `DevmemStudio-5.4.1-win64.zip.sha256`。