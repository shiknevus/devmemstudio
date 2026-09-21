"""Analyze PyInstaller bundle size distribution from PKG-00.toc."""
import ast
import collections
import os
import sys

toc_path = sys.argv[1] if len(sys.argv) > 1 else "build/DevmemStudio/PKG-00.toc"

data = ast.literal_eval(open(toc_path, encoding="utf-8").read())

# PKG-00.toc = (pkg_name, {'BINARY':..., ...}, [ (dest, src, typecode), ... ])
entries = data[2]

agg = collections.Counter()
missing = 0
by_type = collections.Counter()
for dest, src, typecode in entries:
    src = src.replace("\\", "/")
    by_type[typecode] += 1
    if not os.path.exists(src):
        missing += 1
        continue
    try:
        sz = os.path.getsize(src)
    except OSError:
        continue
    marker = "/site-packages/"
    if marker in src:
        rest = src.split(marker, 1)[1]
        parts = rest.split("/")
        key = parts[0] + ("/" + parts[1] if len(parts) > 1 else "")
    else:
        key = src
    agg[key] += sz

print("entries:", len(entries), "missing:", missing)
print("types:", dict(by_type))
total = sum(agg.values())
print("=== top-level size buckets (MB) ===")
for k, v in agg.most_common(40):
    print(f"{v/1e6:9.2f} MB  {v/total*100:5.1f}%  {k}")
print(f"{total/1e6:9.2f} MB  total")
