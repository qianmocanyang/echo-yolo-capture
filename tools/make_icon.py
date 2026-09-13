"""生成 exe / 窗口图标 assets/echo.ico。

图形与托盘图标同源（``echo/ui/tray.py`` 的 ``make_tray_icon``）：圆环 + 中心点。
区别只在底色——托盘图标要透明地浮在任务栏上，应用图标是独立方块，
加一层深色圆角底能在浅色资源管理器里站得住。

不依赖 Pillow：Qt 渲染出 PNG 后按 ICO 容器格式手工封装（Vista 起 ICO 允许
直接内嵌 PNG 数据），省一个打包期依赖。

    python tools/make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = ROOT / "assets" / "echo.ico"

# 与 echo/ui/theme.py 的 DARK 调色板一致
BACKDROP = "#12151a"
ACCENT = "#4c8dff"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)

    # 圆角底：尺寸越小圆角越收敛，否则 16px 下整个方块会变成椭圆
    radius = size * (0.22 if size >= 32 else 0.16)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(BACKDROP))
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # 圆环：线宽按尺寸缩放，16px 时至少要 2px 才看得见
    width = max(2.0, size * 0.11)
    pen = QPen(QColor(ACCENT))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    margin = size * 0.27
    painter.drawEllipse(
        QRectF(margin, margin, size - margin * 2, size - margin * 2)
    )

    # 中心点：太小就省略，硬塞会糊成一片
    dot = size * 0.15
    if dot >= 2.0:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(ACCENT))
        painter.drawEllipse(
            QRectF(size / 2 - dot / 2, size / 2 - dot / 2, dot, dot)
        )

    painter.end()
    return image


def to_png(image: QImage) -> bytes:
    """QBuffer 必须无参构造。

    写成 ``QBuffer(QByteArray())`` 会让那个临时 QByteArray 立刻被 Python 回收，
    QBuffer 拿着已释放的指针，save() 直接段错误——而且退出码是 0，看起来像
    "跑完了但没输出"，极难排查。
    """
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("PNG 编码失败")
    data = bytes(buffer.data())
    buffer.close()
    return data


def write_ico(chunks: list[tuple[int, bytes]], path: Path) -> None:
    """按 ICO 容器格式封装。宽高为 256 时字段写 0（格式规定）。"""
    header = struct.pack("<HHH", 0, 1, len(chunks))
    offset = len(header) + 16 * len(chunks)

    entries = bytearray()
    body = bytearray()
    for size, png in chunks:
        side = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset
        )
        body += png
        offset += len(png)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(entries) + bytes(body))


def main() -> int:
    app = QApplication.instance() or QApplication([])  # noqa: F841
    chunks = [(size, to_png(render(size))) for size in SIZES]
    write_ico(chunks, OUT)
    print(f"已生成 {OUT}")
    print(f"  尺寸 {', '.join(str(s) for s in SIZES)}")
    print(f"  体积 {OUT.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
