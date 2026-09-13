"""Create the redistributable archive without local device settings or credentials."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from devmem_studio import __version__
dist = root / "dist"
exe = dist / "DevmemStudio.exe"
if not exe.is_file():
    raise SystemExit("Build dist/DevmemStudio.exe first")
readme = f"""寄存器调试工作台 {__version__}

双击 DevmemStudio.exe 即可运行，无需安装 Python 或 Qt。
本包为 Windows x64 单文件应用。

1. 填写 SSH 主机、端口、用户名和密码，连接 Linux 开发板。
   若重启后提示主机密钥变化，核对设备地址与指纹，再点击“信任新密钥并重连”。
   原密钥记录自动备份；取消则保留原记录并停止连接。
2. 左侧选择模块，设置基地址与组件地址。
   新增“基础”组件，含 EC_ID、SC_ID 等 15 个可读写寄存器，默认 32 位访问。
3. 读取寄存器；在右侧检查器中修改数值，再点击“写入并回读”。
   irq1/irq2 字段解析显示十进制。a/b/c rpt 中 ack_tx_result 显示十六进制，
   ack_beh_id、ack_tx_id、ack_ps_alart_num 显示十进制；在“解析与位状态”中查看。
4. 点击“打印日志”，新窗口直接执行 tail -f /run/media/sda/sunny.log。
   日志启动及打印期间可继续读写寄存器；关闭日志窗口只停止日志通道。
5. 日志按错误、告警、成功、调试高亮。Ctrl+F 查找，Enter/F3 跳到下一处，
   Shift+Enter/Shift+F3 跳到上一处；Ctrl+G 输入行号并按 Enter 跳转。
   查找时继续接收日志，点击“回到最新”恢复自动滚动。缓存保留最近 5000 行。
6. 日志默认开启“自动换行”和“显示行号”，可分别勾选切换。
   长日志折行显示，续行不重复编号；左侧行号与 Ctrl+G 的缓存原始行号一致。
   旧行淘汰或清空后按当前缓存重新编号。复制、保存仍保留日志原文。
7. 侧栏“导入 top”可选择 emcc mix 顶层文件：组件按类型分组列出，
   点击组件即加载该类型的精确寄存器表（寄存器名与 RTL 宏一致），
   再用「全部 / 基础 / A通道 / B通道 / C通道 / 中断 / 参数 / 调试」页签切换视图；
   A/B/C_BHV_ID 写入预设来自 RTL 行为表，参数寄存器名带实际信号名
   （如 PARAM1 · rcfg_spd_max），中断页含上报应答，调试页按字节解码状态机历史。
   寄存器表随软件内置生成；RTL 变更后需用源码工程的
   tools/gen_component_catalog.py 重新生成。

F5 读取全部；Ctrl+F 搜索；Ctrl+L 命令输入；Esc 停止后续操作。
批量写入先预览确认；板端日志使用独立通道。
详细操作说明在软件右上角“使用指南”。

设置文件：EXE 旁 registers.json；目录不可写时使用
%LOCALAPPDATA%\\DevmemStudio\\registers.json。
日志与 SSH 已知主机信息保存在 %LOCALAPPDATA%\\DevmemStudio。

本发布包不包含任何本机设备地址或登录密码。
如要沿用旧配置，可自行复制原 registers.json 到 EXE 旁边。
勾选“记住密码”时密码会保存于本机配置，请妥善保管该文件。

已完成离线验收及本机 SSH 回环测试。
实际开发板上的寄存器访问能力和副作用需在现场验证。

作者：szzhang / cgliu / bxli
"""
(dist / "使用说明.txt").write_text(readme, encoding="utf-8-sig")
shutil.copy2(root / "THIRD_PARTY_NOTICES.md", dist / "THIRD_PARTY_NOTICES.md")
digest = hashlib.sha256(exe.read_bytes()).hexdigest()
(dist / "SHA256SUMS.txt").write_text(f"{digest}  DevmemStudio.exe\n", encoding="ascii")
archive = dist / f"DevmemStudio-{__version__}-win64.zip"
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
    for name in ("DevmemStudio.exe", "使用说明.txt", "THIRD_PARTY_NOTICES.md", "SHA256SUMS.txt"):
        package.write(dist / name, f"DevmemStudio/{name}")
print(json.dumps({"executable": str(exe), "bytes": exe.stat().st_size,
                  "sha256": digest, "archive": str(archive)}, ensure_ascii=False))
