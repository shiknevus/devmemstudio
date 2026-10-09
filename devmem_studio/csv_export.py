"""Stream the same UTF-8 CSV into a plain file or a lossless ZIP archive."""
from contextlib import contextmanager
import io
import os
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

ZIP_FILTER = "无损压缩 CSV (*.zip)"
CSV_FILTER = "CSV 文件 (*.csv)"
EXPORT_FILTERS = f"{ZIP_FILTER};;{CSV_FILTER}"


def export_path(path, selected_filter):
    """Honor an explicit extension, otherwise add the selected format's extension."""
    target = Path(path)
    if target.suffix.lower() not in (".csv", ".zip"):
        path += ".csv" if selected_filter == CSV_FILTER else ".zip"
    return path


@contextmanager
def csv_export(path):
    """Commit only a complete export; leave an existing file intact on failure."""
    target = Path(path)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".csv-export-", suffix=".tmp", delete=False) as temp:
        temporary = Path(temp.name)
    try:
        if target.suffix.lower() == ".zip":
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
                with archive.open(target.with_suffix(".csv").name, "w", force_zip64=True) as entry:
                    with io.TextIOWrapper(entry, encoding="utf-8-sig", newline="") as handle:
                        yield handle
        else:
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                yield handle
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
