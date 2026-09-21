import ast

data = ast.literal_eval(open("build/DevmemStudio/PKG-00.toc", encoding="utf-8").read())
for dest, src, tc in data[2]:
    s = src.replace("\\", "/").lower()
    if "translations" in s or "plugins" in s:
        print(src.replace("\\", "/").split("site-packages/")[-1])
