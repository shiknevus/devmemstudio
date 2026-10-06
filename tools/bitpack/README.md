# 内置 Bit_pack 引擎

从本机 `D:\bitfile` 的 Bit_pack 2.0.1 复制的独立 C 工程；原工程不作修改。
工作台使用统一 PySide6 弹窗，后台仅调用此引擎的 CLI，不显示原生 GUI。

集成补丁：`--scan <目录>` 复用原核心的安全递归扫描（输出 FILE/SCAN 记录）；`--progress` 流式输出阶段；`--cancel-event <名称>` 打开工作台创建的
Windows 命名事件，执行核心原有的安全取消与临时文件清理。其它 CLI / 原生 GUI 保持兼容。

重建（需要 MinGW-w64 gcc / windres）：

```powershell
.\tools\build_bitpack.ps1
```

生成并更新 `assets/bitpack/pack_bit.exe`。普通工作台构建使用已打包的引擎，
不需要 MinGW，也不依赖 `D:\bitfile`。核心实现仍负责源文件保留、快照、
ZIP 内容校验、防重名、仅复制覆盖确认、超时与取消清理。
