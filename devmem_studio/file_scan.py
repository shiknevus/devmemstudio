# -*- coding: utf-8 -*-
"""Find candidate files (.bit, top .sv/.v) under a picked folder; no Qt."""
from __future__ import annotations

import os
from pathlib import Path

MAX_DEPTH = 8
MAX_FOLDERS = 20000
MAX_RESULTS = 2000


def find_files(root, suffixes, max_depth=MAX_DEPTH, max_folders=MAX_FOLDERS, limit=MAX_RESULTS):
    """[(path, mtime, size)] under `root` with a matching suffix, newest first.

    Hidden folders (.Xil, .git), links and junctions are skipped. Returns
    (entries, complete); complete is False when a depth/size limit cut the walk."""
    suffixes = tuple(suffix.lower() for suffix in suffixes)
    found = []
    complete = True
    pending = [(Path(root), 0)]
    visited = 0
    while pending:
        folder, depth = pending.pop()
        visited += 1
        if visited > max_folders:
            complete = False
            break
        try:
            with os.scandir(folder) as entries:
                entries = list(entries)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.startswith("."):
                        continue
                    if depth >= max_depth:
                        complete = False
                        continue
                    pending.append((Path(entry.path), depth + 1))
                elif entry.name.lower().endswith(suffixes):
                    stat = entry.stat()
                    found.append((Path(entry.path), stat.st_mtime, stat.st_size))
            except OSError:
                continue
    found.sort(key=lambda item: item[1], reverse=True)
    if len(found) > limit:
        complete = False
        del found[limit:]
    return found, complete
