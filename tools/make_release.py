"""Create the release archive: the single EXE only, never local settings or credentials."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from devmem_studio import __version__


def exe_version(exe):
    """a.b.c from the EXE's VERSIONINFO, or None when it has none."""
    import pefile   # PyInstaller dependency, always in the build venv

    with pefile.PE(str(exe), fast_load=True) as image:
        image.parse_data_directories([pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        fixed = getattr(image, "VS_FIXEDFILEINFO", None)
        if not fixed:
            return None
        info = fixed[0]
        return f"{info.FileVersionMS >> 16}.{info.FileVersionMS & 0xFFFF}.{info.FileVersionLS >> 16}"


def build_release(exe, version=__version__):
    exe = Path(exe)
    if not exe.is_file():
        raise SystemExit(f"Build {exe} first")
    found = exe_version(exe)
    if found != version:   # an old EXE in a new-version zip makes clients offer the update forever
        raise SystemExit(f"{exe} is version {found}, not {version}; rebuild it before releasing")
    archive = exe.with_name(f"DevmemStudio-{version}-win64.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        package.write(exe, "DevmemStudio/DevmemStudio.exe")
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_name(archive.name + ".sha256")
    checksum.write_text(f"{archive_sha256}  {archive.name}\n", encoding="utf-8")
    return {"version": version, "executable": str(exe), "bytes": exe.stat().st_size,
            "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(), "archive": str(archive),
            "archive_sha256": archive_sha256, "checksum": str(checksum),
            "archive_bytes": archive.stat().st_size, "settings_excluded": True}


if __name__ == "__main__":
    print(json.dumps(build_release(ROOT / "dist" / "DevmemStudio.exe"), ensure_ascii=False))
