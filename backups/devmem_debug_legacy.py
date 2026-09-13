# -*- coding: utf-8 -*-
"""
devmem 调试助手
================
通过 SSH 连接开发板（用户名/密码登录 Linux），以按钮点击的方式读写 devmem 寄存器。

命令格式：
  读: devmem 0xb0102208
  写: devmem 0xb0102314 32 0x01

地址组成：
  全地址 = base(0xb0100000) + 组件地址(0x2200) + 寄存器偏移(0x08) = 0xb0102208
  组件地址可改，寄存器地址（偏移）固定。

地址输入约定：地址一律按十六进制解析，带不带 0x 前缀均可（如 2200 即 0x2200）。
写入值/端口/位宽支持 0x 前缀或十进制。

用法：
  py -3 devmem_debug.py

寄存器/组件配置见 registers.json，可直接增删改。
组件分类(category)：axis / io / ps bus / pl bus，选择类型后组件下拉只显示该类。
只读寄存器(readonly=true)只显示「读取」按钮，不显示写入框。
"""

import json
import os
import random
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

try:
    import paramiko
except ImportError:
    paramiko = None


def _config_path():
    """配置文件路径：优先 exe 旁边（可读可写）；打包后从内置资源初始化。"""
    # 打包后 sys.executable 是 exe 本身；脚本运行时用 __file__
    base_dir = os.path.dirname(os.path.abspath(getattr(sys, "frozen", False) and sys.executable or __file__))
    p = os.path.join(base_dir, "registers.json")
    if getattr(sys, "frozen", False) and not os.path.exists(p):
        # 首次运行：从打包资源复制一份默认配置到 exe 旁边
        try:
            bundled = os.path.join(sys._MEIPASS, "registers.json")
            if os.path.exists(bundled):
                import shutil
                shutil.copy2(bundled, p)
        except Exception:
            pass
    return p


CONFIG_PATH = _config_path()


# 寄存器定义内置（随代码版本管理，不再放 registers.json，避免配置写入覆盖丢失）
DEFAULT_CATEGORIES = ['axis', 'io', 'ps bus', 'pl bus', 'pl ps']

DEFAULT_TYPES = {
    "axis": {
        "registers": [
            {"offset": "0x08", "name": "en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x05c", "name": "a en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x064", "name": "a out", "width": 32, "value": "0x10000"},
            {"offset": "0x068", "name": "a rpt", "width": 32, "readonly": True},
            {"offset": "0x074", "name": "a bhv", "width": 32, "aliases": [{"name": "org", "value": "0x01"}, {"name": "step", "value": "0x02"}, {"name": "target pulse", "value": "0x03"}, {"name": "get point", "value": "0x01E"}]},
            {"offset": "0x084", "name": "b en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x08c", "name": "b out", "width": 32, "value": "0x10000"},
            {"offset": "0x090", "name": "b rpt", "width": 32, "readonly": True},
            {"offset": "0x0ac", "name": "c en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x0b4", "name": "c out", "width": 32, "value": "0x10000"},
            {"offset": "0x0b8", "name": "c rpt", "width": 32, "readonly": True},
            {"offset": "0x0c8", "name": "c gap crl", "width": 20, "value": "100"},
            {"offset": "0x0d8", "name": "max spd", "width": 32, "value": "0x60000"},
            {"offset": "0x0dc", "name": "max acc", "width": 32, "value": "0x60000"},
            {"offset": "0x0e0", "name": "max dcc", "width": 32, "value": "0x60000"},
            {"offset": "0x0e8", "name": "acc", "width": 32, "value": "0x50000"},
            {"offset": "0x114", "name": "dir", "width": 32, "value": "0x01"},
            {"offset": "0x128", "name": "servo en", "width": 32, "buttons": [{"name": "on", "value": "0x1"}, {"name": "off", "value": "0x2"}]},
            {"offset": "0x13c", "name": "pause", "width": 32, "action": True},
            {"offset": "0x140", "name": "stop", "width": 32, "action": True},
            {"offset": "0x144", "name": "resume", "width": 32, "action": True},
            {"offset": "0x148", "name": "reset", "width": 32, "action": True},
            {"offset": "0x158", "name": "touch spd", "width": 32, "value": "1000"},
            {"offset": "0x15c", "name": "dcc", "width": 32, "value": "0x50000"},
            {"offset": "0x160", "name": "spd", "width": 32, "value": "0x50000"},
            {"offset": "0x164", "name": "targetpulse", "width": 32, "value": "0x50000"},
            {"offset": "0x168", "name": "step pulse", "width": 32, "value": "0x50000"},
            {"offset": "0x00", "name": "irq1", "width": 32, "readonly": True},
            {"offset": "0x04", "name": "irq2", "width": 32, "readonly": True},
            {"offset": "0x060", "name": "a ch st", "width": 1, "readonly": True},
            {"offset": "0x06c", "name": "a alm num", "width": 8, "readonly": True},
            {"offset": "0x070", "name": "a tx id", "width": 8, "readonly": True},
            {"offset": "0x088", "name": "b ch st", "width": 1, "readonly": True},
            {"offset": "0x094", "name": "b alm num", "width": 8, "readonly": True},
            {"offset": "0x098", "name": "b tx id", "width": 8, "readonly": True},
            {"offset": "0x09c", "name": "b bhv id", "width": 8, "readonly": True},
            {"offset": "0x0b0", "name": "c ch st", "width": 1, "readonly": True},
            {"offset": "0x0bc", "name": "c alm num", "width": 8, "readonly": True},
            {"offset": "0x0c0", "name": "c tx id", "width": 8, "readonly": True},
            {"offset": "0x0c4", "name": "c bhv id", "width": 8, "readonly": True},
            {"offset": "0x178", "name": "pos", "width": 32, "readonly": True},
            {"offset": "0x1ec", "name": "a fsm", "width": 32, "readonly": True},
            {"offset": "0x1f0", "name": "b fsm", "width": 32, "readonly": True},
            {"offset": "0x1f4", "name": "c fsm", "width": 32, "readonly": True},
        ],
    },
    "io": {
        "registers": [
            {"offset": "0x08", "name": "en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x05c", "name": "a en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x064", "name": "a out", "width": 32, "value": "0x10000"},
            {"offset": "0x084", "name": "b en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x08c", "name": "b out", "width": 32, "value": "0x10000"},
            {"offset": "0x0ac", "name": "c en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x0b4", "name": "c out", "width": 32, "value": "0x10000"},
            {"offset": "0x00", "name": "irq1", "width": 32, "readonly": True},
            {"offset": "0x04", "name": "irq2", "width": 32, "readonly": True},
            {"offset": "0x060", "name": "a ch st", "width": 1, "readonly": True},
            {"offset": "0x088", "name": "b ch st", "width": 1, "readonly": True},
            {"offset": "0x0b0", "name": "c ch st", "width": 1, "readonly": True},
            {"offset": "0x06c", "name": "a alm num", "width": 8, "readonly": True},
            {"offset": "0x094", "name": "b alm num", "width": 8, "readonly": True},
            {"offset": "0x0bc", "name": "c alm num", "width": 8, "readonly": True},
            {"offset": "0x070", "name": "a tx id", "width": 8, "readonly": True},
            {"offset": "0x098", "name": "b tx id", "width": 8, "readonly": True},
            {"offset": "0x0c0", "name": "c tx id", "width": 8, "readonly": True},
            {"offset": "0x09c", "name": "b bhv id", "width": 8, "readonly": True},
            {"offset": "0x0c4", "name": "c bhv id", "width": 8, "readonly": True},
            {"offset": "0x068", "name": "a rpt", "width": 32, "readonly": True},
            {"offset": "0x090", "name": "b rpt", "width": 32, "readonly": True},
            {"offset": "0x0b8", "name": "c rpt", "width": 32, "readonly": True},
            {"offset": "0x1ec", "name": "a fsm", "width": 32, "readonly": True},
            {"offset": "0x1f0", "name": "b fsm", "width": 32, "readonly": True},
        ],
    },
    "ps bus": {
        "registers": [
            {"offset": "0x08", "name": "en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x05c", "name": "a en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x064", "name": "a out", "width": 32, "value": "0x10000"},
            {"offset": "0x084", "name": "b en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x08c", "name": "b out", "width": 32, "value": "0x10000"},
            {"offset": "0x0ac", "name": "c en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x0b4", "name": "c out", "width": 32, "value": "0x10000"},
            {"offset": "0x00", "name": "irq1", "width": 32, "readonly": True},
            {"offset": "0x04", "name": "irq2", "width": 32, "readonly": True},
            {"offset": "0x060", "name": "a ch st", "width": 1, "readonly": True},
            {"offset": "0x088", "name": "b ch st", "width": 1, "readonly": True},
            {"offset": "0x0b0", "name": "c ch st", "width": 1, "readonly": True},
            {"offset": "0x06c", "name": "a alm num", "width": 8, "readonly": True},
            {"offset": "0x094", "name": "b alm num", "width": 8, "readonly": True},
            {"offset": "0x0bc", "name": "c alm num", "width": 8, "readonly": True},
            {"offset": "0x070", "name": "a tx id", "width": 8, "readonly": True},
            {"offset": "0x098", "name": "b tx id", "width": 8, "readonly": True},
            {"offset": "0x0c0", "name": "c tx id", "width": 8, "readonly": True},
            {"offset": "0x09c", "name": "b bhv id", "width": 8, "readonly": True},
            {"offset": "0x0c4", "name": "c bhv id", "width": 8, "readonly": True},
            {"offset": "0x068", "name": "a rpt", "width": 32, "readonly": True},
            {"offset": "0x090", "name": "b rpt", "width": 32, "readonly": True},
            {"offset": "0x0b8", "name": "c rpt", "width": 32, "readonly": True},
            {"offset": "0x1ec", "name": "a fsm", "width": 32, "readonly": True},
            {"offset": "0x1f0", "name": "b fsm", "width": 32, "readonly": True},
            {"offset": "0x1f4", "name": "c fsm", "width": 32, "readonly": True},
        ],
    },
    "pl bus": {
        "registers": [
            {"offset": "0x08", "name": "en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x05c", "name": "a en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x064", "name": "a out", "width": 20, "value": "0x0"},
            {"offset": "0x074", "name": "a bhv id", "width": 8, "value": "0x0"},
            {"offset": "0x0d8", "name": "cfg data", "width": 32, "value": "0x0"},
            {"offset": "0x0dc", "name": "param2", "width": 32, "value": "0x0"},
            {"offset": "0x0ec", "name": "baud rate", "width": 20, "value": "0x0"},
            {"offset": "0x0f0", "name": "resp timeout", "width": 20, "value": "0x0"},
            {"offset": "0x0f4", "name": "param8", "width": 20, "value": "0x0"},
            {"offset": "0x114", "name": "parity", "width": 8, "value": "0x0"},
            {"offset": "0x118", "name": "retry cnt", "width": 8, "value": "0x0"},
            {"offset": "0x11c", "name": "slave addr", "width": 8, "value": "0x0"},
            {"offset": "0x13c", "name": "trigger", "width": 1, "value": "0x0"},
            {"offset": "0x00", "name": "irq1", "width": 32, "readonly": True},
            {"offset": "0x04", "name": "irq2", "width": 32, "readonly": True},
            {"offset": "0x060", "name": "a ch st", "width": 1, "readonly": True},
            {"offset": "0x06c", "name": "a alm num", "width": 8, "readonly": True},
            {"offset": "0x070", "name": "a tx id", "width": 8, "readonly": True},
            {"offset": "0x068", "name": "a rpt", "width": 32, "readonly": True},
            {"offset": "0x178", "name": "rx frame1", "width": 32, "readonly": True},
            {"offset": "0x17c", "name": "rx frame2", "width": 32, "readonly": True},
            {"offset": "0x1ec", "name": "a fsm", "width": 32, "readonly": True},
        ],
    },
    "pl ps": {
        "registers": [
            {"offset": "0x08", "name": "en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x05c", "name": "a en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x064", "name": "a out", "width": 32, "value": "0x10000"},
            {"offset": "0x084", "name": "b en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x08c", "name": "b out", "width": 32, "value": "0x10000"},
            {"offset": "0x0ac", "name": "c en", "width": 32, "aliases": [{"name": "off", "value": "0x0"}, {"name": "on", "value": "0x1"}]},
            {"offset": "0x0b4", "name": "c out", "width": 32, "value": "0x10000"},
            {"offset": "0x114", "name": "dir", "width": 8, "value": "0x01"},
            {"offset": "0x128", "name": "servo en", "width": 8, "buttons": [{"name": "on", "value": "0x1"}, {"name": "off", "value": "0x2"}]},
            {"offset": "0x13c", "name": "pause", "width": 32, "action": True},
            {"offset": "0x140", "name": "stop", "width": 32, "action": True},
            {"offset": "0x144", "name": "resume", "width": 32, "action": True},
            {"offset": "0x148", "name": "reset", "width": 32, "action": True},
            {"offset": "0x00", "name": "irq1", "width": 32, "readonly": True},
            {"offset": "0x04", "name": "irq2", "width": 32, "readonly": True},
            {"offset": "0x060", "name": "a ch st", "width": 1, "readonly": True},
            {"offset": "0x088", "name": "b ch st", "width": 1, "readonly": True},
            {"offset": "0x0b0", "name": "c ch st", "width": 1, "readonly": True},
            {"offset": "0x06c", "name": "a alm num", "width": 8, "readonly": True},
            {"offset": "0x094", "name": "b alm num", "width": 8, "readonly": True},
            {"offset": "0x0bc", "name": "c alm num", "width": 8, "readonly": True},
            {"offset": "0x070", "name": "a tx id", "width": 8, "readonly": True},
            {"offset": "0x098", "name": "b tx id", "width": 8, "readonly": True},
            {"offset": "0x0c0", "name": "c tx id", "width": 8, "readonly": True},
            {"offset": "0x09c", "name": "b bhv id", "width": 8, "readonly": True},
            {"offset": "0x0c4", "name": "c bhv id", "width": 8, "readonly": True},
            {"offset": "0x068", "name": "a rpt", "width": 32, "readonly": True},
            {"offset": "0x090", "name": "b rpt", "width": 32, "readonly": True},
            {"offset": "0x0b8", "name": "c rpt", "width": 32, "readonly": True},
            {"offset": "0x1ec", "name": "a fsm", "width": 32, "readonly": True},
            {"offset": "0x1f0", "name": "b fsm", "width": 32, "readonly": True},
            {"offset": "0x1f4", "name": "c fsm", "width": 32, "readonly": True},
            {"offset": "0x1b4", "name": "axis lim f", "width": 1, "readonly": True},
            {"offset": "0x1b8", "name": "axis zero", "width": 1, "readonly": True},
            {"offset": "0x1bc", "name": "axis lim b", "width": 1, "readonly": True},
        ],
    },
}



def _default_config():
    """状态字段默认值（寄存器定义在 DEFAULT_TYPES，不在此）。"""
    return {
        "host": "", "port": 22, "username": "root", "password": "",
        "base": "0xb0100000", "default_width": 32,
        "last_category": "axis", "last_address": "0x0800",
        "connect_timeout": 2,
    }


def load_config():
    """只从文件读"状态字段"；寄存器定义（types/categories）内置在代码里，不依赖文件。"""
    data = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
    except Exception as e:
        print("警告: 读取 registers.json 失败: %s" % e, file=sys.stderr)
    cfg = _default_config()
    for k in ("host", "port", "username", "password", "base", "default_width",
              "last_category", "last_address", "connect_timeout"):
        if k in data:
            cfg[k] = data[k]
    # write_cache 单独合并（保留未知组件的记忆）
    wc = data.get("write_cache")
    if isinstance(wc, dict):
        cfg["write_cache"] = wc
    return cfg


def save_config(cfg):
    """只把状态字段写入 registers.json；types/categories 不落盘（定义内置在代码里）。"""
    try:
        mutable_keys = ("host", "port", "username", "password", "base", "default_width",
                        "last_category", "last_address", "connect_timeout", "write_cache")
        existing = {}
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if not isinstance(existing, dict):
                    existing = {}
        except Exception:
            existing = {}
        out = {}
        # 保留文件里除 types/categories 外的自定义字段
        for k, v in existing.items():
            if k not in ("types", "categories"):
                out[k] = v
        # 更新状态字段
        for k in mutable_keys:
            if k in cfg:
                out[k] = cfg[k]
        # write_cache 合并保留（不丢其他组件的记忆）
        if "write_cache" in existing and "write_cache" not in out:
            out["write_cache"] = existing["write_cache"]
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def parse_int(s):
    """解析 0x 前缀或十进制数字；失败返回 None（不抛异常）。用于端口/位宽/写入值。"""
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return int(s, 0)
    except ValueError:
        return None


def parse_addr(s):
    """解析地址/偏移：一律按十六进制（2200 即 0x2200），带不带 0x 前缀均可；失败返回 None。"""
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return int(s, 16)
    except ValueError:
        return None


class SshSession:
    """封装一个交互式 SSH 会话，可发送命令并取回输出。"""

    def __init__(self, log):
        self.client = None
        self.chan = None
        self.log = log
        self._lock = threading.Lock()  # 串行化会话读写，防止多线程命令互相穿插
        self.last_exit = None          # 最近一次命令的退出码
        self._stream_stop = True       # 流式读取停止标志
        self._stream_thread = None
        self._stream_on_output = None  # 流输出回调（默认写主日志）
        self._stream_on_stop = None

    def connect(self, host, port, username, password, timeout=8):
        if paramiko is None:
            raise RuntimeError("未安装 paramiko，请运行: py -3 -m pip install paramiko")
        self.close()  # 清理上次失败的残留连接
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host, port=port, username=username,
                password=password, timeout=timeout,
                banner_timeout=timeout, auth_timeout=timeout,
                allow_agent=False, look_for_keys=False,
            )
            chan = client.invoke_shell()
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise
        self.client = client
        self.chan = chan
        # 读取欢迎信息，排空缓冲
        time.sleep(0.5)
        self._drain()
        # 确保有提示符
        self._ensure_prompt()

    def _drain(self):
        out = b""
        try:
            while self.chan is not None and self.chan.recv_ready():
                chunk = self.chan.recv(65536)
                if not chunk:
                    break
                out += chunk
        except Exception:
            pass
        if out:
            self.log(out.decode("utf-8", "replace").replace("\r", ""))
        return out

    def _ensure_prompt(self):
        """发送一个回车，确保 shell 进入交互态并读到提示符。"""
        self.chan.send(b"\n")
        time.sleep(0.3)
        self._drain()

    def _alive(self):
        """通道是否仍有效（设备重启/掉线后通道会被远端关闭）。"""
        try:
            return (self.chan is not None and self.client is not None
                    and not self.chan.closed and not self.chan.exit_status_ready())
        except Exception:
            return False

    def run(self, command, timeout=8):
        """执行命令并返回输出文本。
        通过在命令后拼接一个随机结束标记 + 退出码，可靠地判断命令何时结束。
        通道断开时立即抛 RuntimeError，避免每个命令傻等超时。
        """
        if self.chan is None:
            raise RuntimeError("未连接")
        with self._lock:
            self._drain()
            if not self._alive():
                raise RuntimeError("SSH 连接已断开（设备可能已重启），请重新连接")
            self.log(">> " + command)
            marker = "__PI_DONE_%d_" % random.randrange(100000, 999999)
            end_re = re.compile(re.escape(marker) + r"(\d+)__")
            # 发送命令，后接一个唯一结束标记和退出码
            full = command + "; echo " + marker + "$?__\n"
            self.chan.send(full.encode("utf-8"))
            out = b""
            deadline = time.time() + timeout
            exit_code = None
            while time.time() < deadline:
                try:
                    while self.chan.recv_ready():
                        chunk = self.chan.recv(65536)
                        if not chunk:
                            raise RuntimeError("SSH 连接已断开（设备可能已重启）")
                        out += chunk
                except RuntimeError:
                    raise
                except Exception as e:
                    raise RuntimeError("SSH 读取失败: %s" % e)
                # 检测结束标记
                txt = out.decode("utf-8", "replace")
                m = end_re.search(txt)
                if m:
                    exit_code = int(m.group(1))
                    break
                time.sleep(0.05)
            else:
                self.log("（命令执行超时未收到结束标记，可能板子上该命令卡住或设备掉线）")
            self.last_exit = exit_code
            text = out.decode("utf-8", "replace").replace("\r", "")
            # 调试：把原始收到的数据写入 debug.log（自动轮转，防止无限增长）
            self._debug_append(command, text)
            # 去掉含结束标记的行（回显的命令行与标记结果行），去掉纯提示符行
            lines = [ln for ln in text.splitlines() if marker not in ln]
            cleaned = []
            for ln in lines:
                # 跳过含命令回显的行（通常是第一行）
                if command in ln and len(cleaned) == 0:
                    continue
                # 跳过纯提示符行
                if re.match(r"^[^#]*[#]\s*$", ln):
                    continue
                cleaned.append(ln)
            result = "\n".join(cleaned).strip()
            if exit_code not in (None, 0):
                self.log("（退出码 %d）" % exit_code)
            self.log(result)
            return result

    def send_ctrl_c(self):
        """向远程 shell 发送 Ctrl+C(0x03)，中断当前长驻命令（如 tail -f）。"""
        if self.chan is None:
            raise RuntimeError("未连接")
        with self._lock:
            self.chan.send(b"\x03")
            self.log(">> ^C")

    def start_stream(self, command, on_output=None, on_stop=None):
        """发送长驻命令（如 tail -f），立即返回；后台线程持续把输出交给 on_output 回调。
        on_output(text)/on_stop() 由调用方提供（如写入独立日志窗口）；不传则写主日志。
        用 stop_stream()（配合 send_ctrl_c）停止。"""
        if self.chan is None:
            raise RuntimeError("未连接")
        with self._lock:
            self._drain()
            if not self._alive():
                raise RuntimeError("SSH 连接已断开（设备可能已重启），请重新连接")
            self.log(">> " + command)
            self.chan.send((command + "\n").encode("utf-8"))
        self._stream_stop = False
        self._stream_on_output = on_output or self.log
        self._stream_on_stop = on_stop
        self._stream_thread = threading.Thread(target=self._stream_reader, daemon=True)
        self._stream_thread.start()

    def stop_stream(self):
        """停止流式读取。"""
        self._stream_stop = True

    def _stream_reader(self):
        """后台循环：把通道输出实时回调给 on_output，直到 stop_stream/连接断开。"""
        out_cb = self._stream_on_output or self.log
        buf = b""
        try:
            while not self._stream_stop:
                got = False
                try:
                    while self.chan is not None and self.chan.recv_ready():
                        chunk = self.chan.recv(65536)
                        if not chunk:
                            out_cb("（连接已断开，停止打印日志）")
                            self._stream_stop = True
                            return
                        buf += chunk
                        got = True
                except Exception as e:
                    out_cb("（打印日志停止: %s）" % e)
                    return
                if got:
                    text = buf.decode("utf-8", "replace").replace("\r", "")
                    buf = b""
                    if text.strip():
                        out_cb(text.rstrip("\n"))
                time.sleep(0.2)
        finally:
            out_cb("（已停止打印日志）")
            if self._stream_on_stop:
                try:
                    self._stream_on_stop()
                except Exception:
                    pass

    @staticmethod
    def _debug_append(command, text):
        try:
            # debug.log 写在 exe/脚本旁边（打包后 _MEIPASS 只读，必须用外部路径）
            base_dir = os.path.dirname(os.path.abspath(getattr(sys, "frozen", False) and sys.executable or __file__))
            path = os.path.join(base_dir, "debug.log")
            if os.path.exists(path) and os.path.getsize(path) > 2 * 1024 * 1024:
                try:
                    os.replace(path, path + ".old")
                except Exception:
                    pass
            with open(path, "a", encoding="utf-8") as f:
                f.write("===== RAW for [%s] =====\n%s\n\n" % (command, text))
        except Exception:
            pass

    def close(self):
        self._stream_stop = True  # 停止流式读取线程
        try:
            if self.chan:
                self.chan.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.chan = None
        self.client = None


def parse_devmem_read_output(text, addr_val=None):
    """从 devmem 输出里提取数值。返回 (int值, 原始值串)。
    addr_val: 本次读取的地址(int)，用于排除回显/输出里的地址本身，避免误把地址当值。
    """
    if text is None:
        return None, None
    lines = text.splitlines()
    hexes = []
    for ln in lines:
        if "=" in ln:
            seg = ln.rsplit("=", 1)[-1]
            m = re.search(r"0[xX][0-9a-fA-F]+", seg)
            if m:
                hexes.append(m.group(0))
        else:
            for m in re.finditer(r"0[xX][0-9a-fA-F]+", ln):
                hexes.append(m.group(0))
    # 排除与命令地址相同的那个
    filtered = []
    for h in hexes:
        try:
            hv = int(h, 16)
        except ValueError:
            continue
        if addr_val is not None and hv == addr_val:
            continue
        filtered.append(h)
    if filtered:
        return int(filtered[-1], 16), filtered[-1]
    if hexes:
        return int(hexes[-1], 16), hexes[-1]
    m = re.search(r"\b\d+\b", text)
    if m:
        return int(m.group(0)), m.group(0)
    return None, None


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("devmem 调试助手")
        # 默认最大化窗口
        try:
            self.root.state("zoomed")  # Windows 最大化
        except Exception:
            try:
                self.root.attributes("-zoomed", True)  # Linux/X11 最大化
            except Exception:
                pass
        self.cfg = load_config()
        self.ssh = SshSession(self.log)
        self.register_entries = {}  # 行id -> 控件引用
        self._scroll_panels = []
        self._row_cells = {}      # (panel_id,row) -> [widgets]
        self._row_bg = {}         # widget id -> 原背景
        self._sel_row = None      # 当前选中行 (panel_id,row)
        self._log_win = None      # 板端日志独立窗口
        self._log_text = None
        self._log_state = None

        self._build_ui()
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(seq, self._on_mousewheel, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_fields()

    # ---------- 线程安全辅助 ----------
    def _safe_after(self, ms, fn, *args):
        """跨线程调用 root.after，窗口销毁后静默忽略，防止后台线程崩溃。"""
        try:
            self.root.after(ms, lambda: fn(*args))
        except Exception:
            pass

    @staticmethod
    def _safe_set(var, value):
        """设置 Tk 变量；变量可能已被销毁（刷新界面时），静默忽略。"""
        try:
            if var is not None:
                var.set(value)
        except Exception:
            pass

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 5, "pady": 3}

        # 连接区
        conn = ttk.LabelFrame(self.root, text="SSH 连接")
        conn.pack(fill="x", padx=8, pady=6)

        ttk.Label(conn, text="主机/IP:").grid(row=0, column=0, sticky="w", **pad)
        self.host_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.host_var, width=16).grid(row=0, column=1, **pad)

        ttk.Label(conn, text="端口:").grid(row=0, column=2, sticky="w", **pad)
        self.port_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.port_var, width=6).grid(row=0, column=3, **pad)

        ttk.Label(conn, text="用户名:").grid(row=0, column=4, sticky="w", **pad)
        self.user_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.user_var, width=12).grid(row=0, column=5, **pad)

        ttk.Label(conn, text="密码:").grid(row=0, column=6, sticky="w", **pad)
        self.pwd_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.pwd_var, width=14, show="*").grid(row=0, column=7, **pad)

        self.connect_btn = ttk.Button(conn, text="连接", command=self.on_connect)
        self.connect_btn.grid(row=0, column=8, **pad)

        self.status_var = tk.StringVar(value="● 未连接")
        self.status_lbl = ttk.Label(conn, textvariable=self.status_var, foreground="gray")
        self.status_lbl.grid(row=0, column=9, sticky="w", **pad)

        conn.columnconfigure(9, weight=1)

        # 第二行：连接超时（可设置，默认 2s，有记忆）
        ttk.Label(conn, text="连接超时时间(s):").grid(row=1, column=0, sticky="w", **pad)
        self.timeout_var = tk.StringVar()
        ttk.Entry(conn, textvariable=self.timeout_var, width=6).grid(row=1, column=1, sticky="w", **pad)

        # 组件选择区（类别 + 组件地址）
        comp = ttk.LabelFrame(self.root, text="组件选择")
        comp.pack(fill="x", padx=8, pady=6)

        ttk.Label(comp, text="基地址:").grid(row=0, column=0, sticky="w", **pad)
        self.base_var = tk.StringVar()
        _base_entry = ttk.Entry(comp, textvariable=self.base_var, width=14, state="readonly")
        _base_entry.grid(row=0, column=1, **pad)

        ttk.Label(comp, text="组件类型:").grid(row=0, column=2, sticky="w", **pad)
        self.cat_var = tk.StringVar()
        self.cat_combo = ttk.Combobox(comp, textvariable=self.cat_var, width=10,
                                     values=DEFAULT_CATEGORIES, state="readonly")
        self.cat_combo.grid(row=0, column=3, **pad)
        self.cat_combo.bind("<<ComboboxSelected>>", lambda e: self._on_category_change())

        ttk.Label(comp, text="组件地址:").grid(row=0, column=4, sticky="w", **pad)
        self.comp_var = tk.StringVar()
        # 组件地址：从 0x800 起，步进 0x200，共 256 个（索引 0..255）
        self._comp_addrs = ["0x%04x" % (0x800 + i * 0x200) for i in range(0, 256)]
        self.comp_combo = ttk.Combobox(comp, textvariable=self.comp_var, width=14,
                                      values=self._comp_addrs, state="readonly")
        self.comp_combo.grid(row=0, column=5, **pad)
        self.comp_combo.bind("<<ComboboxSelected>>", self._on_comp_change)

        ttk.Button(comp, text="刷新寄存器", command=self._refresh_registers).grid(row=0, column=6, **pad)
        self.read_all_btn = ttk.Button(comp, text="读取全部", command=self.on_read_all)
        self.read_all_btn.grid(row=0, column=7, **pad)
        self.write_all_btn = ttk.Button(comp, text="写入全部", command=self.on_write_all)
        self.write_all_btn.grid(row=0, column=8, **pad)

        comp.columnconfigure(9, weight=1)

        # 自由命令区
        free = ttk.LabelFrame(self.root, text="自由命令（手动发送任意 shell 命令）")
        free.pack(fill="x", padx=8, pady=6)
        self.cmd_var = tk.StringVar()
        _cmd_entry = ttk.Entry(free, textvariable=self.cmd_var, width=70)
        _cmd_entry.grid(row=0, column=0, padx=5, pady=4, sticky="we")
        # 按回车默认发送
        _cmd_entry.bind("<Return>", lambda e: self.on_send_free())
        ttk.Button(free, text="发送", command=self.on_send_free).grid(row=0, column=1, padx=5, pady=4)
        ttk.Button(free, text="读devmem", command=self.on_free_read).grid(row=0, column=2, padx=5, pady=4)
        free.columnconfigure(0, weight=1)

        # 快捷按钮行
        frow = ttk.Frame(free)
        frow.grid(row=1, column=0, columnspan=3, sticky="we", padx=5, pady=(0, 4))
        ttk.Button(frow, text="重启主板", command=self.on_reboot).pack(side="left", padx=2)
        ttk.Button(frow, text="打印日志", command=self.on_tail_log).pack(side="left", padx=2)
        ttk.Button(frow, text="读取(H)", command=lambda: self.on_quick_read(True)).pack(side="left", padx=2)
        ttk.Button(frow, text="读取(D)", command=lambda: self.on_quick_read(False)).pack(side="left", padx=2)
        ttk.Label(frow, text="读取(H) devmem 0x输入值    读取(D) devmem 输入值").pack(side="left", padx=8)

        # 日志区
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=False, padx=8, pady=6)
        self.log_text = scrolledtext.ScrolledText(logf, height=8, font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True)
        # 保持 normal 态：选中/复制不受插入打断；阻止用户手动输入
        self.log_text.bind("<Key>", self._on_log_key)
        # 右键菜单：复制/全选/清空
        self._log_menu = tk.Menu(self.log_text, tearoff=0)
        self._log_menu.add_command(label="复制", command=self._copy_log)
        self._log_menu.add_command(label="全选", command=self._log_select_all)
        self._log_menu.add_separator()
        self._log_menu.add_command(label="清空日志", command=self._clear_log)
        self.log_text.bind("<Button-3>", lambda e: self._log_menu.tk_popup(e.x_root, e.y_root))

        # 底部说明
        ttk.Label(self.root, text="命令格式: 读 devmem 0xADDR | 写 devmem 0xADDR 32 0xVAL").pack(anchor="w", padx=8)

        # 寄存器按钮区：左右两栏，各自独立滚动
        regf = ttk.LabelFrame(self.root, text="寄存器")
        regf.pack(fill="both", expand=True, padx=8, pady=6)

        paned = ttk.Frame(regf)
        paned.pack(fill="both", expand=True)
        paned.columnconfigure(0, weight=2, uniform="c")
        paned.columnconfigure(1, weight=3, uniform="c")
        paned.rowconfigure(0, weight=1)

        self.reg_inner = paned  # 兼容旧引用

        # 左：只读
        self.ro_panel = self._make_scroll_panel(paned, 0, 0, "只读寄存器（读取）")
        # 右：可读写
        self.rw_panel = self._make_scroll_panel(paned, 0, 1, "可读写寄存器（读取/写入）")

    def _make_scroll_panel(self, parent, r, c, title):
        """建一个带垂直+水平滚动条的 LabelFrame，内部 frame 可滚动。"""
        box = ttk.LabelFrame(parent, text=title)
        box.grid(row=r, column=c, sticky="nsew", padx=4, pady=4)
        canvas = tk.Canvas(box, highlightthickness=0, bg="#f0f0f0")
        vsb = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        hsb = ttk.Scrollbar(box, orient="horizontal", command=canvas.xview)
        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        canvas.pack(side="left", fill="both", expand=True)

        def _on_inner_config(event):
            region = canvas.bbox("all")
            if region:
                canvas.configure(scrollregion=region)
            # 内部 frame 宽度：取需求和 canvas 宽度的较大者，使拉伸列能跟随变宽
            cw = canvas.winfo_width()
            canvas.itemconfig(win_id, width=max(inner.winfo_reqwidth(), cw))

        def _on_canvas_config(event):
            req = inner.winfo_reqwidth()
            canvas.itemconfig(win_id, width=max(req, event.width))

        inner.bind("<Configure>", _on_inner_config)
        canvas.bind("<Configure>", _on_canvas_config)

        box.inner = inner
        box.canvas = canvas
        box._pi_panel = box
        self._scroll_panels.append(box)
        return box

    def _on_mousewheel(self, event):
        """全局滚轮：指针悬停在哪个寄存器面板里就滚动哪个，覆盖面板内的所有子控件。"""
        if not self._scroll_panels:
            return
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            w = None
        node = w
        while node is not None:
            panel = getattr(node, "_pi_panel", None)
            if panel is not None:
                num = getattr(event, "num", None)
                delta = getattr(event, "delta", 0) or 0
                if num == 4:
                    step = -1
                elif num == 5:
                    step = 1
                elif delta:
                    step = int(-delta / 120) or (-1 if delta > 0 else 1)
                else:
                    step = 0
                if step:
                    try:
                        panel.canvas.yview_scroll(step, "units")
                    except Exception:
                        pass
                return "break"
            node = getattr(node, "master", None)

    def _load_fields(self):
        self.host_var.set(self.cfg.get("host", ""))
        self.port_var.set(str(self.cfg.get("port", 22)))
        self.user_var.set(self.cfg.get("username", ""))
        self.pwd_var.set(self.cfg.get("password", ""))
        self.timeout_var.set(str(self.cfg.get("connect_timeout", 2)))
        self.base_var.set(self.cfg.get("base", "0xb0100000"))
        cats = DEFAULT_CATEGORIES
        self.cat_combo["values"] = cats
        last_cat = self.cfg.get("last_category")
        if cats:
            self.cat_var.set(last_cat if last_cat in cats else cats[0])
        # 组件地址记忆：恢复上次关闭前选择的地址
        last_addr = self.cfg.get("last_address")
        if last_addr in self._comp_addrs:
            self.comp_var.set(last_addr)
        elif not self.comp_var.get().strip() or self.comp_var.get().strip() not in self._comp_addrs:
            self.comp_var.set("0x2200")
        self._on_category_change()

    # ---------- 类别 / 组件 ----------
    def _type_registers(self, category):
        """取某类型的寄存器模板（内置在代码里，地址从下拉框选择）。"""
        t = DEFAULT_TYPES.get(category)
        if isinstance(t, dict):
            return t.get("registers", [])
        return []

    def _on_category_change(self):
        # 寄存器集由类型决定，地址是自由输入；切换类型时保留已输入的地址。
        self._refresh_registers()
        self._save_last()

    def _on_comp_change(self, event=None):
        """组件地址下拉选择变化 -> 刷新寄存器并记忆。"""
        self._refresh_registers()
        self._save_last()

    def _save_last(self):
        """记忆当前组件类型/地址，下次打开时恢复。"""
        try:
            self.cfg["last_category"] = self.cat_var.get()
            self.cfg["last_address"] = self.comp_var.get()
            save_config(self.cfg)
        except Exception:
            pass

    # ---------- 寄存器列表 ----------
    @staticmethod
    def _norm_addr(a):
        v = parse_addr(a)
        return ("%#x" % v) if v is not None else str(a)

    def _clear_register_panels(self):
        for p in (self.ro_panel, self.rw_panel):
            for w in p.inner.winfo_children():
                w.destroy()

    def _panel_note(self, panel, text):
        """在面板里居中显示一条说明文字（红色）。"""
        ttk.Label(panel.inner, text=text, justify="left", foreground="#a00",
                  wraplength=560).grid(row=0, column=0, sticky="w", padx=8, pady=8)

    def _refresh_registers(self):
        # 清空旧控件
        self._clear_register_panels()
        self.register_entries = {}
        self._row_cells = {}
        self._row_bg = {}
        self._sel_row = None

        base = parse_addr(self.base_var.get())
        comp_addr = parse_addr(self.comp_var.get())
        if base is None or comp_addr is None:
            if comp_addr is None and base is not None:
                msg = "（请从下拉框选择组件地址）"
            else:
                msg = "基地址或组件地址无效：base=%r, 组件=%r" % (self.base_var.get(), self.comp_var.get())
            self._panel_note(self.ro_panel, msg)
            self._panel_note(self.rw_panel, msg)
            return
        comp_base = base + comp_addr

        # 写入值记忆的组件键（类型@地址）
        self._cur_comp_key = "%s@%s" % (self.cat_var.get(), self.comp_var.get())

        # 寄存器集只取决于所选类型；地址为自由输入，任意地址都用同一模板。
        cat = self.cat_var.get()
        regs = self._type_registers(cat)
        if not regs:
            msg = ("类型「%s」未配置寄存器模板，请在 registers.json 的 types 中添加该类型及其 registers。"
                   % cat)
            self._panel_note(self.ro_panel, msg)
            self._panel_note(self.rw_panel, msg)
            return

        regs = list(regs)
        if not regs:
            msg = "（类型 %s 未定义任何寄存器，请在 registers.json 中为它添加 registers）" % cat
            self._panel_note(self.ro_panel, msg)
            self._panel_note(self.rw_panel, msg)
            return

        # 拆分只读 / 可读写，各自按偏移地址升序排列（方便对照硬件手册）
        ro_regs = [r for r in regs if isinstance(r, dict) and bool(r.get("readonly", False))]
        rw_regs = [r for r in regs if isinstance(r, dict) and not bool(r.get("readonly", False))]
        # 按寄存器种类分组排序（组内按偏移地址升序）
        ro_regs.sort(key=lambda r: self._reg_sort_key(r))
        rw_regs.sort(key=lambda r: self._reg_sort_key(r))

        self._render_table(self.ro_panel.inner, ro_regs, comp_base, readonly=True)
        self._render_table(self.rw_panel.inner, rw_regs, comp_base, readonly=False)

    # 寄存器名称 -> (种类序号, 种类名)；未匹配的归入「其他」排最后
    _NAME_GROUPS = {
        # RO
        "irq1": (0, "中断"), "irq2": (0, "中断"),
        "a ch st": (1, "通道状态"), "b ch st": (1, "通道状态"), "c ch st": (1, "通道状态"),
        "a alm num": (2, "报警"), "b alm num": (2, "报警"), "c alm num": (2, "报警"),
        "a tx id": (3, "事务"), "b tx id": (3, "事务"), "c tx id": (3, "事务"),
        "a bhv id": (4, "行为"), "b bhv id": (4, "行为"), "c bhv id": (4, "行为"),
        "a rpt": (5, "上报"), "b rpt": (5, "上报"), "c rpt": (5, "上报"),
        "pos": (6, "数据"), "rx frame1": (6, "数据"), "rx frame2": (6, "数据"),
        "a fsm": (7, "状态机"), "b fsm": (7, "状态机"), "c fsm": (7, "状态机"),
        "axis lim f": (8, "限位"), "axis zero": (8, "限位"), "axis lim b": (8, "限位"),
        # RW
        "en": (0, "使能"), "a en": (0, "使能"), "b en": (0, "使能"), "c en": (0, "使能"),
        "a out": (1, "输出"), "b out": (1, "输出"), "c out": (1, "输出"),
        "dir": (2, "伺服"), "servo en": (2, "伺服"),
        "spd": (3, "运动参数"), "acc": (3, "运动参数"), "dcc": (3, "运动参数"),
        "max spd": (3, "运动参数"), "max acc": (3, "运动参数"), "max dcc": (3, "运动参数"),
        "touch spd": (3, "运动参数"),
        "targetpulse": (4, "脉冲"), "step pulse": (4, "脉冲"),
        "c gap crl": (5, "任务"),
        "cfg data": (6, "通信参数"), "param2": (6, "通信参数"),
        "baud rate": (6, "通信参数"), "resp timeout": (6, "通信参数"), "param8": (6, "通信参数"),
        "parity": (6, "通信参数"), "retry cnt": (6, "通信参数"), "slave addr": (6, "通信参数"),
        "pause": (7, "动作"), "stop": (7, "动作"), "resume": (7, "动作"),
        "reset": (7, "动作"), "trigger": (7, "动作"),
    }

    def _reg_sort_key(self, r):
        """排序键：(种类序号, 偏移地址)。未登记名称归「其他」排最后。"""
        name = (r.get("name") or "")
        grp = self._NAME_GROUPS.get(name, (98, "其他"))
        return (grp[0], parse_addr(r.get("offset")) or 0)

    # ---------- 写入值记忆 ----------
    def _comp_key(self):
        return "%s@%s" % (self.cat_var.get(), self.comp_var.get())

    def _cache_write(self, name, value, fmt=None):
        """记住某寄存器的写入值（含别名选择）；按组件（类型@地址）分开存。"""
        try:
            ck = self._comp_key()
            entry = {"v": value}
            if fmt:
                entry["f"] = fmt
            self.cfg.setdefault("write_cache", {}).setdefault(ck, {})[name] = entry
            save_config(self.cfg)
        except Exception:
            pass

    def _load_write(self, name):
        """取回某寄存器上次写入值；无则返回 None。"""
        try:
            return self.cfg.get("write_cache", {}).get(self._comp_key(), {}).get(name)
        except Exception:
            return None

    def _render_table(self, parent, regs, comp_base, readonly):
        # 表头
        if readonly:
            headers = ["名称", "偏移", "全地址", "当前值(H)", "当前值(D)", "解析", "操作"]
            colw = [14, 6, 12, 14, 14, 24, 8]
        else:
            headers = ["名称", "偏移", "全地址", "当前值(H)", "当前值(D)", "解析", "写入值", "格式", "位宽", "操作"]
            colw = [14, 6, 12, 14, 14, 24, 10, 6, 4, 10]
        for ci, (h, w) in enumerate(zip(headers, colw)):
            lbl = ttk.Label(parent, text=h, width=w)
            lbl.grid(row=0, column=ci, sticky="w", padx=4, pady=(2, 4))
            lbl.configure(font=("", 10, "bold"))

        # 当前值(H)(3) 与 当前值(D)(4) 同时拉伸、等权重 -> 两列等宽；解析列(5)也拉伸
        stretch_cols = (3, 4, 5)
        for ci in range(len(headers)):
            parent.columnconfigure(ci, weight=1 if ci in stretch_cols else 0)

        if not regs:
            ttk.Label(parent, text="（无）", width=20).grid(row=1, column=0, columnspan=len(headers),
                                                 sticky="w", padx=4, pady=8)
            return

        for i, r in enumerate(regs, start=1):
            off = parse_addr(r.get("offset"))
            name = r.get("name", "")
            width = parse_int(r.get("width")) or self.cfg.get("default_width", 32)
            preset = r.get("value", "")
            aliases = r.get("aliases", [])
            if off is None:
                continue
            full = comp_base + off
            full_str = "0x%08x" % full

            # 固定列：0 名称 / 1 偏移 / 2 全地址 / 3 当前值(hex) / 4 十进制
            ttk.Label(parent, text=name, width=14).grid(row=i, column=0, sticky="w", padx=4, pady=2)
            ttk.Label(parent, text=("0x%03x" % off), width=6).grid(row=i, column=1, sticky="w", padx=4, pady=2)
            ttk.Label(parent, text=full_str, width=12).grid(row=i, column=2, sticky="w", padx=4, pady=2)
            cur_var = tk.StringVar(value="-")
            ttk.Label(parent, textvariable=cur_var, width=14, foreground="blue").grid(
                row=i, column=3, sticky="ew", padx=4, pady=2)
            dec_var = tk.StringVar(value="-")
            ttk.Label(parent, textvariable=dec_var, width=14, foreground="#444444").grid(
                row=i, column=4, sticky="ew", padx=4, pady=2)
            # 解析数据列（仅 irq1/irq2/a b c rpt 有值）
            rpt_var = tk.StringVar(value="-")
            ttk.Label(parent, textvariable=rpt_var, width=24, foreground="#006400").grid(
                row=i, column=5, sticky="ew", padx=4, pady=2)

            row_id = id(r)
            writer = None

            # rw 列：6 写入值 / 7 格式 / 8 位宽 / 9 操作；ro 列：6 操作
            if readonly:
                bf = ttk.Frame(parent)
                bf.grid(row=i, column=6, sticky="w", padx=4, pady=2)
                ttk.Button(bf, text="读取", width=6,
                           command=lambda f=full_str, v=cur_var, d=dec_var, r=rpt_var, nm=name: self._read_one(f, v, d, r, name=nm)).pack(side="left", padx=2)
            elif r.get("action"):
                # action寄存器：点击即写一次 0x1，无写入框/位宽框
                bf = ttk.Frame(parent)
                bf.grid(row=i, column=9, sticky="w", padx=4, pady=2)
                ttk.Button(bf, text="触发", width=8,
                           command=lambda f=full_str, w=width, c=cur_var, d=dec_var, r=rpt_var, nm=name: self._write_action(f, w, c, d, r, nm)).pack(side="left", padx=2)
                writer = lambda silent=False, f=full_str, w=width, c=cur_var, d=dec_var, r=rpt_var, nm=name: self._write_action(f, w, c, d, r, nm, silent=silent)
            elif r.get("buttons"):
                # 多按钮寄存器：每个按钮写一个固定值
                btns = [b for b in r.get("buttons", []) if isinstance(b, dict)]
                bf = ttk.Frame(parent)
                bf.grid(row=i, column=9, sticky="w", padx=4, pady=2)
                for b in btns:
                    bname = b.get("name", "")
                    bval = b.get("value", "0x0")
                    ttk.Button(bf, text=bname, width=6,
                               command=lambda f=full_str, w=width, v=bval, c=cur_var, d=dec_var, r=rpt_var, nm=name: self._write_button(f, w, v, c, d, r, nm)).pack(side="left", padx=2)
                if btns:
                    first_value = btns[0].get("value", "0x0")
                    writer = lambda silent=False, f=full_str, w=width, v=first_value, c=cur_var, d=dec_var, r=rpt_var, nm=name: self._write_button(f, w, v, c, d, r, nm, silent=silent)
            else:
                if aliases:
                    alias_labels = ["%s (%s)" % (a.get("name", ""), a.get("value", "")) for a in aliases]
                    alias_var = tk.StringVar(value=alias_labels[0] if alias_labels else "")
                    # 恢复上次写入时选择的别名
                    _cached = self._load_write(name)
                    if isinstance(_cached, dict) and _cached.get("v") in alias_labels:
                        alias_var.set(_cached["v"])
                    ttk.Combobox(parent, textvariable=alias_var, width=10,
                                 values=alias_labels, state="readonly").grid(
                        row=i, column=6, sticky="w", padx=4, pady=2)
                    # 第 7 列（格式）留空：别名为固定预设值，无需格式选择
                    w_var = tk.StringVar(value=str(width))
                    ttk.Combobox(parent, textvariable=w_var, width=4,
                                 values=["8", "16", "32"], state="readonly").grid(row=i, column=8, sticky="w", padx=4, pady=2)
                    bf = ttk.Frame(parent)
                    bf.grid(row=i, column=9, sticky="w", padx=4, pady=2)
                    ttk.Button(bf, text="读取", width=6,
                               command=lambda f=full_str, v=cur_var, d=dec_var, r=rpt_var, nm=name: self._read_one(f, v, d, r, name=nm)).pack(side="left", padx=2)
                    ttk.Button(bf, text="写入", width=6,
                               command=lambda f=full_str, av=alias_var, al=aliases, ww=w_var, c=cur_var, d=dec_var, r=rpt_var, nm=name: self._write_one_alias(f, av, al, ww, c, d, r, nm)).pack(side="left", padx=2)
                    writer = lambda silent=False, f=full_str, av=alias_var, al=aliases, ww=w_var, c=cur_var, d=dec_var, r=rpt_var, nm=name: self._write_one_alias(f, av, al, ww, c, d, r, nm, silent=silent)
                else:
                    # 默认值按默认格式 D(十进制) 显示，不带 0x；若有上次写入值记忆则恢复
                    _pv = parse_int(preset)
                    val_var = tk.StringVar(value=(str(_pv) if _pv is not None else (preset or "")))
                    _cached = self._load_write(name)
                    if isinstance(_cached, dict) and _cached.get("v") is not None:
                        val_var.set(str(_cached["v"]))
                    ttk.Entry(parent, textvariable=val_var, width=10).grid(row=i, column=6, sticky="w", padx=4, pady=2)
                    # 写入格式选择（默认 D=十进制，H=十六进制），位于位宽左边；切换时自动转换显示值
                    fmt_var = tk.StringVar(value="D")
                    if isinstance(_cached, dict) and _cached.get("f") in ("D", "H"):
                        fmt_var.set(_cached["f"])
                    _fmt_prev = [fmt_var.get()]
                    fmt_combo = ttk.Combobox(parent, textvariable=fmt_var, width=4,
                                 values=["D", "H"], state="readonly")
                    fmt_combo.grid(row=i, column=7, sticky="w", padx=4, pady=2)
                    fmt_combo.bind("<<ComboboxSelected>>",
                                   lambda e, fv=fmt_var, vv=val_var, p=_fmt_prev: self._on_fmt_change(fv, vv, p))
                    # 禁用滚轮切换 D/H：悬停滚轮不改变格式
                    for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                        fmt_combo.bind(_seq, lambda e: "break")
                    w_var = tk.StringVar(value=str(width))
                    ttk.Combobox(parent, textvariable=w_var, width=4,
                                 values=["8", "16", "32"], state="readonly").grid(row=i, column=8, sticky="w", padx=4, pady=2)
                    bf = ttk.Frame(parent)
                    bf.grid(row=i, column=9, sticky="w", padx=4, pady=2)
                    ttk.Button(bf, text="读取", width=6,
                               command=lambda f=full_str, v=cur_var, d=dec_var, r=rpt_var, nm=name: self._read_one(f, v, d, r, name=nm)).pack(side="left", padx=2)
                    ttk.Button(bf, text="写入", width=6,
                               command=lambda f=full_str, vv=val_var, ww=w_var, ff=fmt_var, c=cur_var, d=dec_var, r=rpt_var, nm=name: self._write_one(f, vv, ww, ff, c, d, r, nm)).pack(side="left", padx=2)
                    writer = lambda silent=False, f=full_str, vv=val_var, ww=w_var, ff=fmt_var, c=cur_var, d=dec_var, r=rpt_var, nm=name: self._write_one(f, vv, ww, ff, c, d, r, nm, silent=silent)

            self.register_entries[row_id] = {
                "full": full, "full_str": full_str, "cur": cur_var, "dec": dec_var, "rpt": rpt_var,
                "off": off, "name": name, "readonly": readonly, "writer": writer,
            }

        # 行选择高亮：点击行内任意单元格，整行变灰高亮
        self._setup_row_selection(parent)

    def _setup_row_selection(self, parent):
        """按 grid row 分组记录单元格，并绑定点击选中整行高亮。
        跳过 TFrame 等不支持 background 的容器控件。"""
        panel_id = id(parent)
        cells_by_row = {}
        for w in parent.winfo_children():
            info = w.grid_info()
            if not info:
                continue
            r = int(info.get("row", 0))
            if r == 0:
                continue  # 跳过表头
            # 跳过不支持 -background 的控件（TFrame 等）
            try:
                w.cget("background")
            except Exception:
                continue
            cells_by_row.setdefault(r, []).append(w)
        for r, ws in cells_by_row.items():
            self._row_cells[(panel_id, r)] = ws
            for w in ws:
                try:
                    if id(w) not in self._row_bg:
                        self._row_bg[id(w)] = w.cget("background")
                except Exception:
                    pass
                w.bind("<Button-1>", lambda e, p=panel_id, rr=r: self._select_row(p, rr), add="+")

    def _select_row(self, panel_id, row):
        """选中某行高亮（灰色），清除上一选中行。"""
        cur = (panel_id, row)
        prev = self._sel_row
        if prev == cur:
            return
        # 清除旧高亮
        if prev is not None:
            for w in self._row_cells.get(prev, []):
                try:
                    w.configure(background=self._row_bg.get(id(w), ""))
                except Exception:
                    pass
        # 设置新高亮
        for w in self._row_cells.get(cur, []):
            try:
                w.configure(background="#c8c8c8")
            except Exception:
                pass
        self._sel_row = cur

    def _snapshot_entries(self):
        return list(self.register_entries.values())

    def _set_batch_buttons(self, running):
        try:
            state = "disabled" if running else "normal"
            for b in (self.read_all_btn, self.write_all_btn):
                b.config(state=state)
        except Exception:
            pass

    # ---------- 操作 ----------
    def _set_status(self, connected):
        if connected:
            self.status_var.set("● 已连接")
            self.status_lbl.config(foreground="green")
            self.connect_btn.config(state="normal", text="断开")
        else:
            self.status_var.set("● 未连接")
            self.status_lbl.config(foreground="gray")
            self.connect_btn.config(state="normal", text="连接")

    def _set_err(self, msg):
        self.status_var.set("● " + msg[:60])
        self.status_lbl.config(foreground="red")
        self.connect_btn.config(state="normal", text="连接")

    def on_connect(self):
        if self.ssh.client is not None:
            self.ssh.close()
            self._set_status(False)
            self.log("已断开连接")
            return

        host = self.host_var.get().strip()
        port = parse_int(self.port_var.get()) or 22
        user = self.user_var.get().strip()
        pwd = self.pwd_var.get()
        # 连接超时时间（s），非法输入回退默认 2，限制 1~120
        timeout = parse_int(self.timeout_var.get())
        if timeout is None or timeout <= 0:
            timeout = 2
        timeout = max(1, min(timeout, 120))

        if not host or not user:
            messagebox.showerror("错误", "请填写主机和用户名")
            return

        self.log("正在连接 %s@%s:%s ..." % (user, host, port))
        self.connect_btn.config(state="disabled", text="连接中…")
        self.status_var.set("● 连接中…")
        self.status_lbl.config(foreground="orange")

        # 保存配置便于下次使用
        self.cfg["host"] = host
        self.cfg["port"] = port
        self.cfg["username"] = user
        self.cfg["password"] = pwd
        self.cfg["base"] = self.base_var.get()
        self.cfg["connect_timeout"] = timeout
        save_config(self.cfg)

        def worker():
            try:
                self.ssh.connect(host, port, user, pwd, timeout=timeout)
                self._safe_after(0, lambda: self._set_status(True))
                self.log("连接成功")
            except Exception as e:
                msg = "连接失败: " + str(e)
                self.log(msg)
                self._safe_after(0, lambda m=msg: self._set_err(m))
        threading.Thread(target=worker, daemon=True).start()

    def _check_connected(self, quiet=False):
        if self.ssh.client is None:
            if not quiet:
                messagebox.showerror("未连接", "请先连接主板")
            return False
        return True

    def _ask_error(self, msg, silent):
        if silent:
            self.log("跳过: " + msg)
        else:
            try:
                messagebox.showerror("提示", msg)
            except Exception:
                pass

    @staticmethod
    def _parse_data(name, value):
        """把 irq1/irq2/a b c rpt 的 32 位值解析成可读字段串；非这些寄存器返回 None。
        各项之间用较宽间隔分隔，便于阅读。"""
        sep = "    "  # 项间隔：4 空格
        if value is None:
            return None
        v = value & 0xFFFFFFFF
        if name == "irq1":
            ec_id = (v >> 18) & 0x3FFF
            sc_id = (v >> 8) & 0x3FF
            bhv_id = v & 0xFF
            return sep.join(["ec=%d" % ec_id, "sc=%d" % sc_id, "bhv=%d" % bhv_id])
        if name == "irq2":
            tx_id = (v >> 24) & 0xFF
            alm_num = (v >> 16) & 0xFF
            return sep.join(["tx=%d" % tx_id, "alm=%d" % alm_num])
        if name in ("a rpt", "b rpt", "c rpt"):
            beh = (v >> 24) & 0xFF
            tx = (v >> 16) & 0xFF
            res = (v >> 8) & 0xFF
            alm = v & 0xFF
            return sep.join(["beh=%d" % beh, "tx=%d" % tx, "res=%d" % res, "alm=%d" % alm])
        return None

    def _show_read_result(self, full_str, cur_var, dec_var, out, rpt_var=None, name=None):
        """把一次 devmem 读取的输出解析成显示值（含超时/出错分支）。
        十六进制进当前值列，十进制进右侧十进制列；irq/rpt 进解析列。"""
        exit_code = getattr(self.ssh, "last_exit", None)
        if exit_code is None:
            self._safe_set(cur_var, "超时/无响应")
            self._safe_set(dec_var, "-")
            self._safe_set(rpt_var, "-")
            return
        if exit_code != 0:
            first = next((ln for ln in out.splitlines() if ln.strip()), out or "无输出")
            self._safe_set(cur_var, ("出错:%s" % first)[:18])
            self._safe_set(dec_var, "-")
            self._safe_set(rpt_var, "-")
            return
        addr_val = parse_addr(full_str)
        value, raw = parse_devmem_read_output(out, addr_val)
        if raw:
            self._safe_set(cur_var, raw)
            self._safe_set(dec_var, str(value) if value is not None else "-")
            # 解析列
            pd = self._parse_data(name, value) if name else None
            self._safe_set(rpt_var, pd if pd else ("-" if name in ("irq1", "irq2", "a rpt", "b rpt", "c rpt") else "-"))
        elif out.strip():
            lines = [ln for ln in out.splitlines() if ln.strip()]
            self._safe_set(cur_var, (lines[-1][:16] if lines else "-"))
            self._safe_set(dec_var, "-")
            self._safe_set(rpt_var, "-")
        else:
            self._safe_set(cur_var, "无输出")
            self._safe_set(dec_var, "-")
            self._safe_set(rpt_var, "-")

    def _read_one(self, full_str, cur_var, dec_var, rpt_var=None, quiet=False, name=None):
        if not self._check_connected(quiet):
            return
        cmd = "devmem %s" % full_str
        try:
            out = self.ssh.run(cmd)
            self._show_read_result(full_str, cur_var, dec_var, out, rpt_var, name)
        except Exception as e:
            self._safe_set(cur_var, "错误")
            self._safe_set(dec_var, "-")
            self._safe_set(rpt_var, "-")
            self.log("读取失败: " + str(e))

    def _readback(self, full_str, cur_var=None, dec_var=None, rpt_var=None, name=None):
        """写完后回读并刷新“当前值/十进制/解析”三列。"""
        try:
            out = self.ssh.run("devmem %s" % full_str)
            if cur_var is not None:
                self._show_read_result(full_str, cur_var, dec_var, out, rpt_var, name)
        except Exception as e:
            self.log("回读失败: " + str(e))

    def _write_button(self, full_str, width, value, cur_var=None, dec_var=None, rpt_var=None, name=None, silent=False):
        """多按钮寄存器：写入指定值。"""
        if not self._check_connected(quiet=silent):
            return
        ival = parse_int(value)
        if ival is None:
            self._ask_error("按钮值无效: " + str(value), silent)
            return
        width = parse_int(width) or 32
        cmd = "devmem %s %d 0x%x" % (full_str, width, ival)
        try:
            self.ssh.run(cmd)
            self.log("写入完成: %s = 0x%x" % (full_str, ival))
            self._readback(full_str, cur_var, dec_var, rpt_var, name)
        except Exception as e:
            self.log("写入失败: " + str(e))

    def _write_action(self, full_str, width, cur_var=None, dec_var=None, rpt_var=None, name=None, silent=False):
        """action寄存器：写一次 0x1。"""
        if not self._check_connected(quiet=silent):
            return
        width = parse_int(width) or 32
        cmd = "devmem %s %d 0x1" % (full_str, width)
        try:
            self.ssh.run(cmd)
            self.log("触发完成: %s" % full_str)
            self._readback(full_str, cur_var, dec_var, rpt_var, name)
        except Exception as e:
            self.log("触发失败: " + str(e))

    def _on_fmt_change(self, fmt_var, val_var, prev):
        """切换写入格式时，把当前值按旧进制解析、按新进制重显（不带 0x），保证两种格式下都能正确写入。"""
        new = fmt_var.get()
        old = prev[0]
        prev[0] = new
        if new == old:
            return
        s = (val_var.get() or "").strip()
        if not s:
            return
        v = None
        try:
            v = int(s, 16) if old == "H" else int(s, 10)
        except ValueError:
            try:
                v = int(s, 0)
            except ValueError:
                v = None
        if v is None:
            return
        try:
            val_var.set(("%x" % v) if new == "H" else str(v))
        except Exception:
            pass

    def _write_one(self, full_str, val_var, w_var, fmt_var, cur_var=None, dec_var=None, rpt_var=None, name=None, silent=False):
        if not self._check_connected(quiet=silent):
            return
        val = val_var.get().strip() if val_var.get() else ""
        if not val:
            self._ask_error("请填写写入值", silent)
            return
        fmt = fmt_var.get() if fmt_var else "D"
        try:
            ival = int(val, 16) if fmt == "H" else int(val, 10)
        except ValueError:
            ival = None
        if ival is None:
            self._ask_error("写入值格式错误（当前格式: %s）: %s" % (fmt, val), silent)
            return
        width = parse_int(w_var.get()) or 32
        cmd = "devmem %s %d 0x%x" % (full_str, width, ival)
        try:
            self.ssh.run(cmd)
            self.log("写入完成: %s = 0x%x" % (full_str, ival))
            # 记住本次写入值（按组件/寄存器）
            if name:
                self._cache_write(name, val, fmt)
            # 写完后回读，刷新当前值/十进制/解析
            self._readback(full_str, cur_var, dec_var, rpt_var, name)
        except Exception as e:
            self.log("写入失败: " + str(e))

    def _write_one_alias(self, full_str, alias_var, aliases, w_var, cur_var=None, dec_var=None, rpt_var=None, name=None, silent=False):
        """别名下拉写入：从下拉选项里取对应预设值写入。"""
        if not self._check_connected(quiet=silent):
            return
        sel = alias_var.get()
        # 解析下拉文本 "name (value)" 里的 value（锚定结尾，避免名字本身含括号）
        m = re.search(r"\((0x[0-9a-fA-F]+|\d+)\)\s*$", sel)
        if not m:
            self._ask_error("请选择一个写入项", silent)
            return
        ival = parse_int(m.group(1))
        if ival is None:
            self._ask_error("写入值格式错误: " + m.group(1), silent)
            return
        width = parse_int(w_var.get()) or 32
        cmd = "devmem %s %d 0x%x" % (full_str, width, ival)
        try:
            self.ssh.run(cmd)
            self.log("写入完成: %s" % sel)
            # 记住本次选择的别名
            if name:
                self._cache_write(name, sel)
            self._readback(full_str, cur_var, dec_var, rpt_var, name)
        except Exception as e:
            self.log("写入失败: " + str(e))

    def on_read_all(self):
        if not self._check_connected():
            return
        self._set_batch_buttons(True)
        self.log("开始读取全部寄存器…")
        entries = self._snapshot_entries()

        def worker():
            try:
                for e in entries:
                    self._read_one(e["full_str"], e["cur"], e["dec"], e.get("rpt"), quiet=True, name=e.get("name"))
                self._safe_after(0, lambda: self.log("读取全部完成"))
            finally:
                self._safe_after(0, self._set_batch_buttons, False)
        threading.Thread(target=worker, daemon=True).start()

    def on_write_all(self):
        """把所有可读写寄存器（含预设值/别名/action）按列表顺序写入。"""
        if not self._check_connected():
            return
        ok = messagebox.askyesno("确认", "将把当前组件所有可读写寄存器按预设值依次写入，确认？")
        if not ok:
            return
        self._set_batch_buttons(True)
        self.log("开始写入全部寄存器…")
        entries = self._snapshot_entries()

        def worker():
            try:
                for e in entries:
                    w = e.get("writer")
                    if w:
                        try:
                            w(silent=True)
                        except Exception as ex:
                            self.log("写入失败 %s: %s" % (e.get("name", ""), ex))
                self._safe_after(0, lambda: self.log("写入全部完成"))
            finally:
                self._safe_after(0, self._set_batch_buttons, False)
        threading.Thread(target=worker, daemon=True).start()

    def on_send_free(self):
        if not self._check_connected():
            return
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return
        try:
            self.ssh.run(cmd)
        except Exception as e:
            self.log("执行失败: " + str(e))

    # ---------- 自由命令快捷按钮 ----------
    def on_reboot(self):
        """重启主板（带确认弹窗）。"""
        if not self._check_connected():
            return
        if not messagebox.askyesno("确认", "确认重启主板？\n重启后需要重新连接。"):
            return
        try:
            self.ssh.run("reboot", timeout=3)
        except Exception as e:
            self.log("reboot: " + str(e))
        self.log("已发送 reboot，设备重启中，连接将断开，请稍后重新连接")
        try:
            self.ssh.close()
        except Exception:
            pass
        self._set_status(False)

    def on_tail_log(self):
        """打印板端日志：弹出独立小窗口显示，不影响主窗口操作；流式不卡 UI。"""
        if not self._check_connected():
            return
        self._open_log_window()
        if getattr(self.ssh, "_stream_stop", True) is False:
            return  # 已在打印，窗口已置前
        try:
            self.ssh.start_stream(
                "tail -f /run/media/sda/sunny.log",
                on_output=lambda t: self._safe_after(0, self._win_append, t))
            self._win_append("（日志持续输出中，点「停止」或关闭窗口结束）")
        except Exception as e:
            self._win_append("执行失败: " + str(e))
            self.log("打印日志失败: " + str(e))

    # ---------- 板端日志独立窗口 ----------
    def _open_log_window(self):
        """创建/置前板端日志小窗口。返回 True 表示窗口此前已存在。"""
        if self._log_win is not None and self._log_win.winfo_exists():
            self._log_win.deiconify()
            self._log_win.lift()
            return True
        win = tk.Toplevel(self.root)
        win.title("板端日志（tail -f /run/media/sda/sunny.log）")
        win.geometry("900x500")
        self._log_win = win
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="停止", command=self._stop_log_stream).pack(side="left", padx=2)
        ttk.Button(bar, text="清空", command=self._clear_win_log).pack(side="left", padx=2)
        self._log_state = tk.StringVar(value="● 未开始")
        ttk.Label(bar, textvariable=self._log_state, foreground="gray").pack(side="left", padx=8)
        self._log_text = scrolledtext.ScrolledText(win, font=("Consolas", 10))
        self._log_text.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        # 关闭窗口 = 停止拉流 + 发 Ctrl+C 停掉板端 tail
        win.protocol("WM_DELETE_WINDOW", self._close_log_window)
        return False

    def _win_append(self, text):
        """向日志小窗口追加文本（跨线程经 _safe_after 调用）。"""
        try:
            if self._log_text is not None and self._log_text.winfo_exists():
                self._log_text.insert("end", text + "\n")
                self._log_text.see("end")
                if text.startswith("（"):
                    self._log_state.set(text)
                elif getattr(self.ssh, "_stream_stop", True) is False:
                    self._log_state.set("● 打印中")
        except Exception:
            pass

    def _stop_log_stream(self):
        """停止拉流并发 Ctrl+C 停掉板端 tail。"""
        try:
            self.ssh.send_ctrl_c()
        except Exception:
            pass
        self.ssh.stop_stream()

    def _clear_win_log(self):
        try:
            if self._log_text is not None and self._log_text.winfo_exists():
                self._log_text.delete("1.0", "end")
        except Exception:
            pass

    def _close_log_window(self):
        self._stop_log_stream()
        try:
            if self._log_win is not None and self._log_win.winfo_exists():
                self._log_win.destroy()
        except Exception:
            pass
        self._log_win = None
        self._log_text = None

    def on_quick_read(self, hex_mode):
        """快捷读取：H=devmem 0x+输入的数；D=devmem 输入的数。"""
        if not self._check_connected():
            return
        s = (self.cmd_var.get() or "").strip()
        if not s:
            messagebox.showinfo("提示", "请先在输入框填写地址")
            return
        if hex_mode:
            # 输入已带 0x 则不重复加前缀
            if s.lower().startswith("0x"):
                cmd = "devmem " + s
            else:
                cmd = "devmem 0x" + s
        else:
            cmd = "devmem " + s
        self.cmd_var.set(cmd)
        self.log("快捷读取: " + cmd)
        try:
            self.ssh.run(cmd)
        except Exception as e:
            self.log("执行失败: " + str(e))

    def on_free_read(self):
        if not self._check_connected():
            return
        cmd = self.cmd_var.get().strip()
        if not cmd:
            messagebox.showinfo("提示", "请在输入框填写 devmem 命令或地址")
            return
        if not cmd.lower().startswith("devmem"):
            if parse_addr(cmd) is not None:
                cmd = "devmem %s" % cmd
        try:
            self.ssh.run(cmd)
        except Exception as e:
            self.log("执行失败: " + str(e))

    # ---------- 日志 ----------
    def log(self, text):
        if not text:
            return
        # 线程安全：切回主线程执行
        self._safe_after(0, lambda t=str(text): self._do_log(t))

    def _do_log(self, text):
        # 保持 normal 态，选中/复制不会被插入打断
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def _on_log_key(self, event):
        """阻止用户手动输入文字，但放行复制/全选/选择导航等。"""
        # Ctrl/Cmd 组合键放行（Ctrl+C / Ctrl+A 等）
        if event.state & 0x4:
            return None
        keysym = event.keysym
        if keysym in ("Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
                      "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
                      "Caps_Lock", "Escape", "Tab"):
            return None
        return "break"

    def _copy_log(self):
        """复制选中文本；无选中则复制全部。"""
        try:
            sel = self.log_text.get("sel.first", "sel.last")
        except Exception:
            sel = ""
        if not sel:
            sel = self.log_text.get("1.0", "end-1c")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(sel)
        except Exception:
            pass

    def _log_select_all(self):
        try:
            self.log_text.tag_remove("sel", "1.0", "end")
            self.log_text.tag_add("sel", "1.0", "end")
            self.log_text.focus_set()
        except Exception:
            pass

    def _clear_log(self):
        try:
            self.log_text.delete("1.0", "end")
        except Exception:
            pass

    def _on_close(self):
        self._save_last()
        # 关闭板端日志窗口（停止拉流）
        try:
            self.ssh.stop_stream()
            if self._log_win is not None and self._log_win.winfo_exists():
                self._log_win.destroy()
        except Exception:
            pass
        self._log_win = None
        self._log_text = None
        try:
            self.ssh.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def _hide_console():
    """Windows 下隐藏控制台窗口，避免 cmd 后台一直显示。"""
    if sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass


def main():
    _hide_console()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    try:
        app = App(root)
        root.mainloop()
    except Exception as e:
        try:
            from tkinter import messagebox
            messagebox.showerror("运行错误", str(e))
        except Exception:
            pass


if __name__ == "__main__":
    main()