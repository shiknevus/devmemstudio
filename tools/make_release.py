"""Create the release archive: the single EXE only, never local settings or credentials."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from devmem_studio import __version__

exe = ROOT / "dist" / "DevmemStudio.exe"
if not exe.is_file():
    raise SystemExit("Build dist/DevmemStudio.exe first")
archive = ROOT / "dist" / f"DevmemStudio-{__version__}-win64.zip"
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
    package.write(exe, "DevmemStudio/DevmemStudio.exe")
print(json.dumps({"version": __version__, "executable": str(exe), "bytes": exe.stat().st_size,
                  "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(), "archive": str(archive),
                  "archive_bytes": archive.stat().st_size, "settings_excluded": True}, ensure_ascii=False))
