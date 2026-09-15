"""Create the redistributable archive without local device settings or credentials."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from devmem_studio import __version__

dist = ROOT / "dist"
exe = dist / "DevmemStudio.exe"
if not exe.is_file():
    raise SystemExit("Build dist/DevmemStudio.exe first")
# The archive ships the single EXE only; extra files were trimmed on purpose.
archive = dist / f"DevmemStudio-{__version__}-win64.zip"
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
    package.write(exe, "DevmemStudio/DevmemStudio.exe")
digest = hashlib.sha256(exe.read_bytes()).hexdigest()
print(json.dumps({"executable": str(exe), "bytes": exe.stat().st_size,
                  "sha256": digest, "archive": str(archive)}, ensure_ascii=False))
