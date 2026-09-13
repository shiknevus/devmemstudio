"""Validate a copied single EXE with Python and venv removed from its environment."""
import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--output-name", default="exe-isolated")
parser.add_argument("--native", action="store_true")
parser.add_argument("--scale", default="1")
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
artifacts = (root / "artifacts").resolve()
target = (artifacts / args.output_name).resolve()
if not target.is_relative_to(artifacts) or target == artifacts:
    raise ValueError("Output must remain inside the project's artifacts directory")
target.mkdir(parents=True, exist_ok=True)
executable = target / "DevmemStudio.exe"
shutil.copy2(root / "dist/DevmemStudio.exe", executable)
env = os.environ.copy()
env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_PLATFORM"):
    env.pop(key, None)
env["QT_SCALE_FACTOR"] = args.scale
output = target / "acceptance"
command = [str(executable), "--smoke-test", str(output)]
if not args.native:
    command.append("--offscreen")
result = subprocess.run(command, cwd=target, env=env, capture_output=True, timeout=60,
                        creationflags=subprocess.CREATE_NO_WINDOW)
(target / "stdout.log").write_bytes(result.stdout)
(target / "stderr.log").write_bytes(result.stderr)
report_path = output / "report.json"
if result.returncode or not report_path.exists():
    raise RuntimeError(f"EXE acceptance failed (exit {result.returncode}). See {target}")
report = json.loads(report_path.read_text(encoding="utf-8"))
if not report["passed"] or not report["frozen"]:
    raise RuntimeError(f"EXE acceptance failed: {report_path}")
if report.get("paramiko") != version("paramiko"):
    raise RuntimeError(f"The EXE SSH library does not match the project venv: {report_path}")
print(json.dumps({"passed": True, "bundled_runtime": True, "checks": len(report["checks"]),
                  "paramiko": report["paramiko"],
                  "python_path_removed": True, "native": args.native, "scale": args.scale,
                  "report": str(report_path)}, ensure_ascii=False))
