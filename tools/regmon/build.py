"""Build assets/regmon/regmon-aarch64 (the monitor's board-side sampler) from regmon.c.

Uses the Linaro aarch64-linux-gnu-gcc that ships with Xilinx SDK/Vitis; set REGMON_GCC to
use another one. The output is static and libc-free, so it runs on any aarch64 Linux.
Verify it afterwards with tools/regmon/emulate_test.py.
"""
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
OUTPUT = HERE.parents[1] / "assets" / "regmon" / "regmon-aarch64"
FLAGS = ["-std=gnu11", "-Os", "-Wall", "-Wextra", "-Werror", "-ffreestanding", "-fno-builtin",
         "-fno-tree-loop-distribute-patterns", "-fno-stack-protector", "-fno-asynchronous-unwind-tables",
         "-fno-unwind-tables", "-fno-pie", "-no-pie", "-static", "-nostdlib", "-ffunction-sections",
         "-fdata-sections", "-Wl,--gc-sections", "-Wl,--build-id=none", "-s"]


def find_gcc():
    if os.environ.get("REGMON_GCC"):
        return Path(os.environ["REGMON_GCC"])
    for drive in "CDE":
        for root in sorted(Path(f"{drive}:/Xilinx").glob("*/*/gnu/aarch64/nt/aarch64-linux/bin"), reverse=True):
            gcc = root / "aarch64-linux-gnu-gcc.exe"
            if gcc.exists():
                return gcc
    sys.exit("aarch64-linux-gnu-gcc not found; set REGMON_GCC")


def main():
    gcc = find_gcc()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(gcc), *FLAGS, "-o", str(OUTPUT), str(HERE / "regmon.c")], check=True)
    data = OUTPUT.read_bytes()
    if data[:4] != b"\x7fELF" or data[18:20] != b"\xb7\x00":
        sys.exit(f"{OUTPUT} is not an aarch64 ELF")
    print(f"{OUTPUT} ({len(data)} bytes) built with {gcc}")


if __name__ == "__main__":
    main()
