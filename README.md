# 寄存器调试工作台 5.5.6

Auth：szzhang / cgliu / bxli

面向 FPGA / ARM 的 EMCC60 主板的寄存器调试工作台。以控件为中心：导入 RTL 顶层文件后直接选择组件调试，寄存器表精确到组件类型。

当前发布包：**`DevmemStudio-5.5.6-win64.zip`**，内含单文件 `DevmemStudio/DevmemStudio.exe`。应用版本和 Windows 文件/产品版本均为 **5.5.6**。5.2.0 起加入 GitHub 在线升级，使用和发布步骤见 [在线升级说明](docs/GITHUB_UPDATES.md)，构建结果及联网验证边界见 [5.5.6 验收记录](docs/VALIDATION_5.5.6.md)。

<div align="center">

| 需要做什么 | 阅读位置 |
| --- | --- |
| 下载后的运行与升级 | [直接运行 EXE](#直接运行-exe) |
| 连接板卡、导入 top、调试寄存器 | [使用流程](#使用流程) |
| 查看本次变化 | [5.5.6 发布](#556-发布2026-10-19) |
| 查找更早版本的变化 | [变更记录](docs/CHANGELOG.md) |
| 找配置、缓存或日志 | [配置与日志](#配置与日志) |
| 开发、测试与生成发布包 | [使用当前 venv 开发](#使用当前-venv-开发)、[重新打包](#重新打包) |
| 排查启动、监视或构建问题 | [常见问题](#常见问题) |

</div>

## 5.5.6 发布（2026-10-19）

- **寄存器监视与实时读取扩展至 64 路**：监视窗口与实时读取的寄存器上限从 32 提升到 64（与板端常驻采样程序 regmon 的 64 寄存器上限一致），内存预算按路数自动折算保持不变；实时读取不再重复打印截断提示。
- 5.5.5 的变更见下方说明。

## 5.5.5 发布（2026-10-19）

- **按位宽读写寄存器**：支持按 8-bit、16-bit、32-bit、64-bit 访问位宽读写寄存器（`devmem` 宽度参数 `8`/`16`/`32`/`64`），写入设置与批量写入面板联动位宽选择器，支持 64 位大数掩码截断与溢出保护。
- **串口扫描专属线程池解耦**：COM 串口异步扫描探测从全局共享线程池剥离至专属单工作线程池（`ports_pool`），彻底消除图表监控密集采样与高频读写时的线程饥饿与界面卡顿。
- **顶层模块解析与实例化增强**：无参数例化模块（parameterless instance）支持精确提取端口映射与基地址，避免同名/重名覆盖；RTL 端口与信号高阻态/通配符清洗规则优化并抑制重复告警。
- **寄存器写入草稿真 LRU 记忆**：优化寄存器输入状态保留策略，草稿列表采用严格访问顺序 LRU 淘汰机制，并智能保留未提交改动的草稿。
- **升级助手与后台调度优化**：优化升级调度轮询频率，消除退出与轮询时的忙等待，减少后台 CPU 占用。

更早版本的变化见 [变更记录](docs/CHANGELOG.md)。

## 当前流程：选组件 · 切视图

- 界面不再有「模块目录」和「基地址 + 组件地址」栏：**左侧「导入 top」→ 组件目录（按类型分组、可搜索）→ 点击组件**即锁定其 `REG_SPACE_BIAS` 地址并加载该类型的精确寄存器表。标题行显示设备名、类型徽标、全地址；基地址自动取自 `components_param.vh`（找不到时左侧出现可编辑的基地址框）。
- 寄存器区新增八个**视图页签**，按 RTL 实测语义切分该组件的寄存器：
  - **基础**：`RST_EN`（写 1 运行 / 0 复位保持）、`EC_ID/SC_ID/BHV_PRIORITY`（仲裁序 0:abc…5:cba）、`UNIT_*/M_*/M_WK_MOD`（=3 手动模式拒绝 A 通道行为）、`M_SAF_ST/LINK_M_SAF_ST`
  - **A通道 / B通道 / C通道**：三个通道各自独立一页。每页含该通道的 `X_EN`（门控，A 复位默认开、C 默认关）、`X_BHV_ID`（行为触发，**预设直接来自 RTL 的行为表注释**，如 home/jog/move[jog[safe]）、`X_TX_OT`（超时，单位秒）、`EC_CHx_ST`（通道忙）、`X_TX_ID`（10/20/30/40 事务）、`X_ALM_NUM`（100 预检超时…106 强制停止…110 手动模式）；A 通道另含 `A_TASK_ID/A_TASK_BHV_ID`，C 通道另含 `C_GAP_CRL`（心跳周期 ms，0 关）
  - **中断**：`IRQ_REG1`（ec_id[31:18]/sc_id[17:8]/bhv_id[7:0]）、`IRQ_REG2`（tx_id/alm_num，锁存不自动清）**及 A/B/C `X_TX_RSULT_RPT` 上报应答**（PS 写 `{beh,tx,0x51/0x52,alm}` 应答中断）
  - **参数**：该类型实现的 PARAM 子集，**每个参数都带标注**——有接线的显示实际信号名（`PARAM1 · rcfg_spd_max`、`PARAM64 · i_axis_limf`，来自 RTL 端口连接、assign、拼接表达式与端口注释收割）；顶层例化中被注释的标 `未接线`（读回值无意义）；只接空线、组件内未使用的标 `预留`。悬停另显示 RTL 注释与状态说明
  - **调试**：`DEBUG_REG1/2/3` = A/B/C 行为状态机历史，每字节一个状态（**四拍字段解码**：前第3拍/前第2拍/前第1拍/当前；状态 0 空闲 1 预检 2 发IRQ10 … 12/13 结束）；例外自动标注（dv300_do REG3=输出脚快照、2di_2do REG2=IRQ 边沿计数）
  - **全部**：完整精确表
- 读取全部 / 轮询 / 批量写入只作用于当前视图；视图只是过滤，切换不丢已读值和写入草稿。参数信号名参与搜索与 CSV 快照。权限过滤改为下拉（全部/只读/可读写）；未实现寄存器不出现在表里（`BHV_EN` 等读取返回 0x7FFFFFFF 的已剔除）。

### top 组件解析与精确寄存器表

- 导入 top 按 `ec_` 实例解析全部控件（设备名/类型/地址/禁用态）。有 `flow_comp` 头时用头上的名称和编号；没有头、或同一个头下面还有别的 `ec_` 实例，也会收进来。重启自动重载；类型无精确表时回退通用表并标 `≈`。
- 寄存器表由 `tools/gen_component_catalog.py` 从 `reg_addr_pl.vh` + 各 `ps_rw_pl_reg_*.sv` 生成（含 PARAM 注释与行为表收割），打包于 `devmem_studio/data/component_catalog.json`；`build_exe.bat` 打包前自动从表内记录的 RTL 根目录重新生成（见[重新打包](#重新打包)）。

### 运行时更新控件（RTL 改动后免重新打包）

某个组件**内部**修改后，在发布版 EXE 里点「更新控件」，选择该组件的文件夹（如 `...\pl_exe_io\ec_dv300_do\`，需含 `ps_rw_pl_reg_*.sv`，可选同级 `ec_*.sv`；寄存器地址表从上级 `include_files/reg_addr_pl.vh` 向上查找）：

- 解析出的寄存器表/信号名/行为表/DEBUG 语义**立即覆盖**内置定义并作用于该类型组件（正在查看的组件自动刷新）
- 定义持久化到 `%LOCALAPPDATA%\DevmemStudio\component_overrides\<类型>.json`，重启自动加载；删除该文件即恢复内置定义
- 本地定义与内置表按新旧取舍：在线升级后，若新版内置表的生成时间晚于该文件，以内置表为准；之后再导入的定义仍优先
- 类型名取目录里 `ec_*.sv` 的 `module` 声明，并同时更新内置表中同一解码目录的别名（如 `ec_1di1do\` 同时更新 `ec_1di_1do` 与 `ec_1di1do`）
- 只对本机生效；要让没有 RTL 源码的电脑也更新，需重新打包发布（打包时自动重新生成内置表）
- 与生成器同源（`devmem_studio/component_parse.py`），两端解析结果一致

## 直接运行 EXE

解压 **`DevmemStudio-5.5.6-win64.zip`**，双击 **`DevmemStudio/DevmemStudio.exe`**（本地构建路径为 `dist/DevmemStudio.exe`）。这是 Windows x64 单文件程序，已包含 Python、Qt、SSH 加密库、HTTPS 库及所需运行库，无需安装 Python 或配置 venv，也不需要任何附带文件夹。每个版本首次启动会把运行库解压到 `%LOCALAPPDATA%\DevmemStudio\runtime\`，之后直接复用。

可复制这个 EXE 到其他目录或电脑使用。程序首次保存设置时会在 EXE 旁生成 `registers.json`。如果要沿用本机设备设置，可自行将项目中的 `registers.json` 放到 EXE 旁边；该文件可能包含保存的密码。发布包使用空白设备配置，不含本机设备凭据。

从 5.1.0 或更早版本升级：先关闭正在运行的程序，保留原 `registers.json`，再将新 EXE 放到原目录（5.2.1 起可直接在线升级）。用户目录中的主机指纹、组件覆盖定义及日志仍沿用原位置。文件属性的「详细信息」中，文件版本和产品版本应均为 **5.5.6**。之后可通过底部“检查更新”在线升级。

本次产物已在当前 Windows 10 x64 系统上验收：EXE 被单独复制到独立目录，PATH 仅保留 Windows 系统目录，清除了 Python / venv 环境变量，并使用空的 `LOCALAPPDATA`；依次验证首次解压运行、缓存损坏后自动修复、旧版本缓存自动清理。

## 使用流程

1. 左栏「设备连接」卡片：点 SSH 行展开，填写主板地址、端口、用户名和密码，点该行右侧「连接」（或在输入框中按 Enter）。设备端需要 SSH、可用的 shell 和 `devmem` 命令。需要串口控制台时，点 serial 行展开，选本机端口与波特率后连接，终端自动切到 `serial` 实时显示板卡输出（连接时不向板卡发送任何字符，不会误打断 U-Boot 倒计时）。填写了串口用户名/密码时，出现 `login:` 提示会自动登录；留空则在终端里手动输入。SSH 与 serial 两条连接相互独立、可同时在线，各行的状态圆点分别显示状态，串口线拔出会立即断开并提示；参数保存过之后，下次打开直接点该行「连接」即可。Esc 可取消正在进行的连接。
2. 连接前点击左侧「导入 top」，选择工程文件夹，软件在该文件夹及子目录中找出包含 `ec_` 控件的 top（如 `emcc_mix_top.sv`），先在列表中列出路径、控件数量和修改时间，只有一个结果也列出；选择后再确认导入。组件目录按类型列出全部组件（可按设备名 / 类型 / 地址搜索，禁用组件灰色），点击组件即加载该类型的精确寄存器表并自动读取。基地址自动取自 `components_param.vh`。
3. 用「基础 / A通道 / B通道 / C通道 / 中断 / 参数 / 调试 / 全部」页签切换视图。选中寄存器后右侧显示完整地址、HEX / DEC、位状态及字段解析，并沿用上次的「写入设置 / 解析与位状态」（只读寄存器仍显示解析页）。在「写入设置」编辑数值或选预设，点击「写入并回读」。「批量写入」先预览再执行。点筛选栏「监视」打开寄存器监视窗口，设置采样间隔（1–60000 ms，默认 5 ms）并勾选最多 64 个寄存器后点「开始监视」，在板端连续采样并绘制曲线，可放置 A/B 标记测量时间差、导出 ZIP / CSV（需已连接 SSH）。aarch64 常驻采样支持 1 ms；回退到 `devmem` 循环时会自动把低于 5 ms 的请求调整为 5 ms。
4. 本地发布包：点「打包bit」，选择或拖入 `.bit`，填写项目号并确认输出目录，点「开始打包」；无需连接设备，源文件保留。「上传bit」选择文件、选择文件夹（自动查找其下 `.bit`，多个时列表选择）或拖入 `*.bit`（严格校验后缀，非 bit 文件提示「选中文件不是bit文件」），在「板端目录」下拉选择上传目录（默认 `/run/media/sda`，自动列出板端 `/run/media` 下的挂载目录，也可直接输入；记住上次使用的目录），上传前确认文件路径、日期与目标位置，确认后先传为 `sunny_fpga.bit.uploading`，传完再把旧文件备份为 `sunny_fpga.bit_时间戳` 并换入为 `sunny_fpga.bit`（传输失败时板端原 bit 不受影响）；「回退bit」列出所选板端目录下的时间戳备份，选择后当前 bit 先备份、所选版本恢复为 `sunny_fpga.bit`；「下载log」把板端 `sunny.log` 保存到本地。
5. 会话终端：来源选 `ssh` 时在终端底部 `❯` 提示行输入 shell 命令，`←`/`→` 在行内移动光标，Tab 补全命令或板端路径，`↑`/`↓` 翻历史，回车执行。来源选 `serial` 时终端就是串口控制台：板卡自发的打印实时显示，按键逐个直发板卡（Tab 补全、历史由板端 shell 处理），无选中文字时 Ctrl+C 发送中断、Ctrl+V 粘贴发送；进 U-Boot 时在倒计时期间按住 Shift+7，或先勾选工具栏「U-Boot」再重启/上电。来源为 `tail log` 时查看板端 `sunny.log` 实时流（SSH 连上后自动后台打印），`system` 查看软件自身运行日志。点击「查找日志」随输随查，Ctrl+F 填充选中文字。点击「导出」保存当前终端内容。
6. RTL 组件内部修改后点「更新控件」，选择该组件文件夹（或上级目录批量导入），先列出组件目录、类型和寄存器定义文件，只有一个结果也列出。默认全选，可点击行取消不需要的组件；点击「选择」后仅解析所选组件，再确认导入。重新解析的寄存器定义立即生效并持久化到本机；取消列表不会解析或导入组件。

「导入 top」与「更新控件」分别记住上次选择的文件夹，重启后仍保留。选择文件夹后即更新记忆，不依赖扫描或导入成功；取消文件夹选择不会覆盖记忆。记住的目录已不存在时，回退到已导入 top 所在目录；该目录也不存在时使用系统默认位置。

板卡重启后若提示主机密钥变化，核对弹窗中的设备地址和新指纹，再点击「信任新密钥并重连」。取消会停止连接并保留原主机密钥记录。

## 界面与功能

- 石墨蓝侧栏、浅灰工作区、统一按钮和状态色、等宽地址与数据字体、矢量图标。
- 连接卡片：SSH / serial 每通道一行（状态圆点、连接目标、连接按钮），参数按需展开、一次一个。
- 可调整寄存器区、终端区与检查器尺寸；支持 Windows DPI 缩放，较小窗口中的检查器可滚动。宽度或高度低于 1400×850 时自动收紧侧栏、检查器和标题行，窗口限制在当前屏幕工作区内。
- 搜索名称、分组、偏移或完整地址；按全部、只读、可读写、动作筛选。
- 待写入默认 HEX。切换 HEX / DEC 只改变解释方式，已填文本保持不变；预设按当前进制填入。访问位宽、写入值范围与地址对齐仍会校验。
- 32 位状态视图以及 `irq1`、`irq2`、`a/b/c rpt` 的字段名、位区间和指定进制解析。
- 自动轮询；前一轮完成后再等待指定间隔，不累积后台请求。
- 寄存器监视：最多 64 个寄存器，采样间隔可设 1–60000 ms（默认 5 ms，运行与暂停期间锁定，随配置保存）的板端连续采样曲线，列表与曲线联动高亮、A/B 标记测时差、Ctrl+滚轮缩放、有符号显示、ZIP / CSV 导出；aarch64 板端使用常驻采样程序，减少重复启动 `devmem` 的开销；回退到 `devmem` 循环时低于 5 ms 的请求会自动限速。
- 网络操作在工作线程执行；批量操作可停止，遇错立即终止后续项。
- SSH 命令终端即 shell：会话终端选择 ssh 来源后在提示行输入命令，行内移动光标编辑，Tab 补全命令与板端路径，`↑`/`↓` 浏览历史、回车执行；tail log / system 视图只读。
- 串口终端：serial 来源是字符模式控制台，板卡输出（启动日志、内核消息、U-Boot）实时显示，按键直发；勾选「U-Boot」自动按住 & 停在 U-Boot 提示符。
- 一键查看板端 `tail -f /run/media/sda/sunny.log`：SSH 连上自动后台打印，切到 tail log 来源即可查看，与寄存器操作互不抢占输出。
- 查找日志随输随查、匹配计数、上一处/下一处定位；自动换行与跟随；导出当前终端缓冲区。
- 寄存器 CSV 快照、会话日志和板端日志导出；日志分级着色、复制和自动跟随。
- 本机日志自动轮转；配置原子写入、旧配置兼容、损坏配置备份。

### 地址和位宽

```text
完整地址 = 基地址 + 组件地址 + 寄存器偏移
0xB0102208 = 0xB0100000 + 0x2200 + 0x008

读：devmem 0xb0102208
写：devmem 0xb0102208 32 0x1
```

地址无论是否带 `0x` 都按十六进制解析；普通数值由 HEX / DEC 选择决定。组件偏移取自导入 top 中的 `REG_SPACE_BIAS`；基地址来自 `components_param.vh`，无法解析时在左侧手动确认并输入基地址。

寄存器的字段长度与总线访问位宽分开：原定义中的 1 位、20 位字段默认采用 32 位访问；8 / 16 / 32 / 64 位访问参数原样保留。读取命令沿用旧版默认访问方式。目标板 `devmem` 的实际访问能力需要与所选参数一致。

写入后显示的是设备回读值。动作、自清零或有副作用的寄存器，回读值可能与写入值不同。停止操作在当前已发送的命令结束后生效。普通命令默认 8 秒超时；超时或取消时向板端发送 Ctrl+C 中断该命令并保留会话，只有 Ctrl+C 后设备仍无响应才关闭连接。

### 快捷键

<div align="center">

| 快捷键 | 操作 |
|---|---|
| F5 | 读取全部寄存器 |
| Ctrl+F | 聚焦查找日志输入框（选中终端文字时自动填充） |
| Enter | 查找下一处匹配 |
| Ctrl+L | 聚焦会话终端 |
| Ctrl+Shift+S | 导出寄存器快照 |
| Enter（连接参数输入框内） | 连接该通道（SSH 或 serial） |
| Esc | 停止后续批量操作 / 取消 SSH 或串口连接 |
| ↑ / ↓ | 会话终端提示行浏览历史并执行 |
| ← / → | 在提示行内移动光标 |
| Tab | 补全提示行光标处的命令或路径 |
| Ctrl+Backspace | 删除提示行光标前的一个词 |
| PageUp / PageDown | 滚动终端历史 |

</div>

## 使用当前 venv 开发

双击 `run.bat`，或在当前目录执行：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe devmem_debug.py
```

`run.bat` 固定使用本项目 `venv\Scripts\pythonw.exe`，不会选择全局 Python。

## 重新打包

双击 **`build_exe.bat`**，或在项目根目录运行：

```powershell
.\venv\Scripts\python.exe tools\build.py
```

构建脚本先核对 `requirements-lock.txt` 的固定依赖版本和图标更新时间，再刷新组件寄存器表：按 `component_catalog.json` 记录的 `source.rtl_root`（或环境变量 `DEVMEMSTUDIO_RTL`）重新生成。控件类型名取各组件目录里 `ec_*.sv` 的 `module` 声明（如 `ec_1di1do\` 目录对应 `ec_1di_1do`），不依赖 top；`<RTL>/top` 存在时其下含 `ec_` 控件的文件只用于补充别名并检查每个控件都有定义。RTL 已删除的旧类型保留；内容无变化且本机没有更新的「更新控件」结果时不改写文件。RTL 目录不存在时跳过并沿用现有表，存在未解析文件或 top 类型无法匹配时构建失败；需要把未收割的参数/行为注释也作为发布门槛时加 `--strict-catalog`；`--no-catalog` 跳过此步。之后并行执行三个任务：

- 全量回归：`tools/run_tests.py --full`，包含正式发布所需的慢速检查。
- 源码离线验收：保存检查报告与截图到 `artifacts/build-smoke/`。
- EXE 构建及隔离验收：PyInstaller 生成 `build/dist/_runtime/`，封装为候选 `build/DevmemStudio.exe`，再单独复制候选 EXE 到隔离目录，验证首次解压、损坏缓存修复和旧缓存清理。

三个任务全部通过后才复制候选 EXE 到 `dist/DevmemStudio.exe`，并生成 **`dist/DevmemStudio-5.5.6-win64.zip`** 和同名 **`.zip.sha256`** 校验文件。测试、源码验收或 EXE 验收失败时保留原 `dist` 产物，显示失败阶段日志的末尾；完整日志见下表。发布 ZIP 仅含 `DevmemStudio/DevmemStudio.exe`。

<div align="center">

| 阶段 | 日志 |
| --- | --- |
| 安装固定依赖（需要时） | `build/logs/pip.log` |
| 生成图标（需要时） | `build/logs/icon.log` |
| 刷新组件寄存器表 | `build/logs/catalog.log` |
| 全量测试 | `build/logs/tests.log` |
| 源码离线验收 | `build/logs/smoke.log` |
| PyInstaller 运行库打包 | `build/logs/pyinstaller.log` |
| 单文件封装与压缩信息 | `build/logs/singlefile.log` |
| EXE 隔离验收 | `build/logs/verify_exe.log` |
| 发布包生成 | `build/logs/release.log` |

</div>

只构建和验收候选 EXE、暂不更新 `dist`：

```powershell
.\venv\Scripts\python.exe tools\build.py --no-dist
```

清理历史发布包、过期验收产物和根目录旧构建日志时，先预览清理计划；确认后再加 `--apply`。默认保留最新两个发布版本、`artifacts/build-smoke` 与当前 EXE 隔离验收目录，不触碰 `venv`、配置和源码：

```powershell
.\venv\Scripts\python.exe tools\cleanup_workspace.py --root .
.\venv\Scripts\python.exe tools\cleanup_workspace.py --root . --apply
```

运行库原始内容未变时，封装工具复用 `build/singlefile-launcher/payload.bin` 和 `payload.json` 中记录的压缩结果。构建报告 `artifacts/singlefile-build.json` 包含版本、缓存标识、文件数量、压缩块数、是否复用压缩结果、EXE 大小及 SHA-256。不同机器和不同测试分块的耗时会有变化，以本次日志为准。

构建单文件启动器需要 MinGW-w64 的 **x86_64** 编译器（`x86_64-w64-mingw32-gcc` / `x86_64-w64-mingw32-windres` 在 PATH 中；本机 `D:\MinGW64\mingw-w64-gcc-14.3-stable-r43\bin` 已具备）。启动器源码在 `tools/runtime_launcher/launcher.c`，只依赖 Windows 自带的 Cabinet（LZMS 解压）、bcrypt（SHA-256）、kernel32 与 user32。

也可手动检查各阶段；正式发布推荐使用上面的完整构建命令，以保证失败时不会继续发布：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\venv\Scripts\python.exe tools\make_icon.py
.\venv\Scripts\python.exe tools\run_tests.py --full
.\venv\Scripts\python.exe devmem_debug.py --offscreen --smoke-test artifacts\source-smoke
.\venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath build\dist DevmemStudio.spec
.\venv\Scripts\python.exe tools\build_singlefile.py --output build\DevmemStudio.exe
.\venv\Scripts\python.exe tools\verify_exe.py --executable build\DevmemStudio.exe
New-Item -ItemType Directory -Force -Path dist | Out-Null
Copy-Item -LiteralPath build\DevmemStudio.exe -Destination dist\DevmemStudio.exe
.\venv\Scripts\python.exe tools\make_release.py
```

日常测试可以只跑本次修改涉及的模块，或运行完整的并行回归：

```powershell
# 日常回归
.\venv\Scripts\python.exe tools\run_tests.py
# 指定模块和测试类，使用 4 个并行子进程
.\venv\Scripts\python.exe tools\run_tests.py test_ui test_monitor.DialogTests -j 4
# 发布前全量回归
.\venv\Scripts\python.exe tools\run_tests.py --full
```

默认跳过会重新执行 SSH 回环套件的依赖裁剪检查；`--full` 或设置 `DEVMEMSTUDIO_FULL_TESTS=1` 时包含该检查，正式构建使用 `--full`。分块依据上次各测试用时（`build/test-times.json`），每个分块在独立子进程内运行。仍可用 `python -m unittest discover -s tests` 串行运行；需要覆盖完整发布范围时先设置 `DEVMEMSTUDIO_FULL_TESTS=1`。

复测启动速度：`.\venv\Scripts\python.exe tools\benchmark_startup.py --exe dist\DevmemStudio.exe --output artifacts\startup-singlefile --runs 5`（独立桌面、隔离的配置与缓存目录，不影响本机设置）。

内置引擎和说明位于 `assets/bitpack/`；C 源码已集成到 `tools/bitpack/`，保留原工程并增加阶段输出与命名事件取消接口。修改引擎后运行 `powershell -ExecutionPolicy Bypass -File tools/build_bitpack.ps1`（需要 MinGW-w64 gcc / windres），脚本更新内置 EXE；随后按上面步骤重新构建工作台。工作台构建直接使用已附带的引擎 EXE，不依赖外部 `D:\bitfile`。不要直接用未带集成接口的旧引擎替换内置 EXE。

构建配置仅打包程序代码、图标、内置打包工具和依赖，**不打包当前 `registers.json`、原版备份或测试中的设备配置**。完整依赖版本见 `requirements-lock.txt`。

打包实现参考 [PyInstaller 的独立运行机制](https://pyinstaller.org/en/stable/operating-mode.html)；界面基于 [Qt for Python](https://doc.qt.io/qtforpython-6/)。第三方组件说明见 `THIRD_PARTY_NOTICES.md`。

## 配置与日志

<div align="center">

| 内容 | 位置 |
|---|---|
| 本机连接设置、模块地址、写入值记忆、监视间隔与勾选 | 程序旁 `registers.json` |
| 程序目录不可写时的配置 | `%LOCALAPPDATA%\DevmemStudio\registers.json` |
| 单文件运行库缓存（每版本一份，旧版自动清理） | `%LOCALAPPDATA%\DevmemStudio\runtime\<版本-哈希>\` |
| SSH 已知主机指纹 | `%LOCALAPPDATA%\DevmemStudio\known_hosts` |
| 主机密钥更新前的备份 | `%LOCALAPPDATA%\DevmemStudio\known_hosts.backup-时间-编号` |
| 运行时导入的组件覆盖定义 | `%LOCALAPPDATA%\DevmemStudio\component_overrides\<类型>.json` |
| 内置 bit 打包引擎缓存 | `%LOCALAPPDATA%\DevmemStudio\tools\bitpack\<SHA256>\pack_bit.exe` |
| 会话日志、异常日志 | `%LOCALAPPDATA%\DevmemStudio\logs\` |
| 原始程序备份 | `backups/devmem_debug_legacy.py` |
| 寄存器定义及分组 | `devmem_studio/catalog.py` |

</div>

勾选「记住密码」会将密码保存于本机配置；取消后下一次保存清空密码字段。会话日志不会自动记录登录密码；手动发送的 shell 命令会作为调试记录保存。

## 验证范围

当前 **5.5.6** 构建及验收证据见 [5.5.6 验收记录](docs/VALIDATION_5.5.6.md)。此前的测试与实板验证见 [5.5.5 验收记录](docs/VALIDATION_5.5.5.md)、[5.5.4 验收记录](docs/VALIDATION_5.5.4.md)、[5.5.3 验收记录](docs/VALIDATION_5.5.3.md)、[5.5.2 验收记录](docs/VALIDATION_5.5.2.md)、[5.5.1 验收记录](docs/VALIDATION_5.5.1.md) 与 [5.5.0 验收记录](docs/VALIDATION_5.5.0.md)。

- 核心与交互回归：原始寄存器定义逐项比对、字段解析、地址与数值校验、配置恢复、异常操作及关闭行为。
- 本机真实 SSH 回环测试：Paramiko 加密连接、读写回读、命令失败退出码、超时失效、断线检测、独立日志通道和分片 UTF-8 输出。
- 主机密钥变化恢复：拒绝未确认的密钥、确认后重连、更新前备份、保留其他主机、默认端口和非默认端口、哈希主机名、记录变动保护及再次变化时重新核对。
- 日志查找：分片高亮、普通文本与中文/表情定位、大小写、键盘跳转、循环查找、实时增量、旧行淘汰、清空和主窗口并行读写。
- 寄存器监视：采样脚本与常驻程序交接规则、时间戳与失败点解析、缓冲淘汰、缩放/标记/布局交互、列表与曲线联动高亮及清空后的高亮保留；`tools/regmon/emulate_test.py` 用 unicorn 在 PC 上执行真实 aarch64 二进制并模拟系统调用。常驻采样程序尚未在实板上测量 CPU 占用。
- EXE 离线验收：模块切换、搜索筛选、预设与进制、自动轮询、取消、日志、导出、小窗口、DPI 和退出；单文件分块解压、首次启动、缓存损坏自愈与旧版本缓存清理。并发首启等其他启动检查的具体覆盖范围见对应验收记录。

未连接或改写实际开发板。实际硬件上的权限、总线访问行为、寄存器副作用和板端日志路径仍需在现场验证。

## 常见问题

<div align="center">

| 现象 | 检查与处理 |
| --- | --- |
| 新电脑没有 Python 或 Qt | 直接使用发布包中的 EXE，所需运行库已内嵌；`venv` 仅用于源码开发和构建。 |
| 升级后想保留设备设置 | 关闭程序，保留原 EXE 旁的 `registers.json`，只替换 EXE；不可写目录的设置在 `%LOCALAPPDATA%\DevmemStudio\registers.json`。 |
| 首次启动较慢，或更换 EXE 后出现新的缓存目录 | 新版本或不同内容会释放新的运行库；后续启动复用缓存。首次释放文件也可能受本机安全软件扫描影响。 |
| 缓存文件缺失或损坏 | 重新启动 EXE，启动器会检查并重新释放运行库；无需另行安装 Python。若仍失败，检查用户缓存目录是否可写及磁盘空间。 |
| 构建提示 `dist/DevmemStudio.exe` 正在使用 | 先关闭正在运行的程序，再重新执行构建；候选 EXE 位于 `build/DevmemStudio.exe`，详细提示见构建输出。 |
| 不希望试验构建覆盖已有发布文件 | 使用 `tools/build.py --no-dist`，它只构建并验收候选 EXE。 |
| 构建失败但不清楚阶段 | 查看输出中的阶段名和 `build/logs/<阶段>.log`，测试失败记录在 `tests.log`，EXE 验收失败记录在 `verify_exe.log`。 |
| 缺少 MinGW-w64 | 将 x86_64 的 gcc 和 windres 加入 PATH；它们用于编译单文件启动器，发布版用户无需安装。 |
| 切换监视高亮后，列表勾选没有变化 | 监视运行中点击只切换观察对象；先停止监视，再更改采样勾选并重新开始。 |
| 点击图表名称后列表没有滚动到对应项 | 条目可能被当前搜索隐藏；清空或调整搜索条件，再点击名称定位。 |
| 寄存器不能勾选监视 | 检查是否只写、读清零或读取有副作用，这些寄存器不能持续采样。 |
| 状态栏显示使用 `devmem` 循环 | 常驻采样程序可能无法上传或执行，例如 `/tmp` 挂载为 `noexec`；查看状态提示。程序会使用回退方式，实际采样周期以状态栏为准。 |
| 曲线数据暂停 | 查看 SSH 连接和底部状态。短时网络停顿后可继续接收；寄存器访问故障或 `/dev/mem` 打不开时会停止并提示原因。 |
| RTL 已修改但寄存器定义仍是旧版 | 使用「更新控件」重新解析组件文件夹，本机无需重新构建 EXE；其他电脑需重新打包发布。要恢复内置定义，删除对应的组件覆盖 JSON 并重启。 |

</div>

监视「导出数据」保存采样数据；主工作台的「导出快照」保存当前寄存器表，两者都可选择 ZIP 无损压缩或普通 CSV。报告问题时，可提供版本号、操作步骤、相关日志及错误提示；配置文件可能包含已保存的密码，检查后再分享。

## 代码结构

```text
devmem_debug.py             启动入口
devmem_studio/
  catalog.py               字段解析与寄存器分组（含 RTL 宏名视图）
  component_parse.py       RTL 组件寄存器解析（生成器与「更新控件」共用）
  top_import.py            mix top 组件解析、视图集合与类型元数据
  data/component_catalog.json  生成的各组件类型精确寄存器表（含参数注释与行为表）
  core.py                  配置、数据解析、SSH 与离线模拟
  serial_session.py        串口控制台：收发线程、拔出检测、自动登录、拦截 U-Boot
  console_screen.py        串口终端行模型（80 列 vt102 子集，跟随 readline 行编辑）
  file_scan.py             文件夹内 .bit / top 文件查找
  completion.py            会话终端 Tab 补全（命令与路径）
  window.py                桌面工作台与异步任务调度
  monitor.py               寄存器监视窗口：采样缓冲、曲线绘制、联动高亮、标记与 ZIP / CSV 导出
  widgets.py / theme.py    控件、矢量图标与统一样式
  dialogs.py               批量预览、日志与使用指南
  log_view.py              日志高亮、匹配统计与查找跳转
  taskbar.py               单文件版任务栏标识（固定后重新启动外层 EXE）
  smoke.py                 可在 EXE 内运行的离线验收
assets/                    图标、Windows 版本资源与板端常驻采样程序（regmon/regmon-aarch64）
docs/                      历史变更记录（CHANGELOG.md）、各版本验收记录（VALIDATION_*.md）与在线升级说明
tests/                     核心、RTL、SSH、串口和界面回归
tools/                     图标生成、组件目录生成、单文件封装、隔离 EXE 验证与启动测速
  build.py                 并行构建、测试、隔离验收与发布编排
  run_tests.py             按用时分块的多进程测试入口
  build_singlefile.py      三块 LZMS 并行压缩、压缩缓存与原生 EXE 封装
  check_project.py         锁定依赖、源码编译与组件目录快速检查
  cleanup_workspace.py     预览/清理历史发布包、验收产物与旧日志
  runtime_launcher/        单文件原生启动器（LZMS 解压、缓存校验与清理）
  regmon/                  板端常驻采样程序源码、构建脚本（aarch64-linux-gnu-gcc）与 unicorn 仿真测试
DevmemStudio.spec           运行库打包配置（单文件由 tools/build_singlefile.py 封装）
```

刷新组件寄存器目录（RTL 寄存器定义变更后；schema 2 起同时收割参数注释与行为表）：

```powershell
.\venv\Scripts\python.exe tools\gen_component_catalog.py --rtl <RTL根目录> --out devmem_studio\data\component_catalog.json --check
# 与打包时相同：类型名取组件目录的 module 声明、top 可选、保留旧类型、无变化不改写
.\venv\Scripts\python.exe tools\gen_component_catalog.py --refresh
```
