"""Render the project's vector icon into a multi-resolution Windows ICO."""
import os
from pathlib import Path
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QBuffer, QIODevice, QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

root = Path(__file__).resolve().parent.parent
app = QApplication(sys.argv[:1])
renderer = QSvgRenderer(str(root / "assets/logo.svg"))
frames = []
for size in (16, 24, 32, 48, 64, 128, 256):
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    frames.append((size, bytes(buffer.data())))
offset = 6 + 16 * len(frames)
header = struct.pack("<HHH", 0, 1, len(frames))
body = b""
for size, data in frames:
    header += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
    body += data
    offset += len(data)
(root / "assets/devmem.ico").write_bytes(header + body)
