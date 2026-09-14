"""界面布局校验：用离屏渲染 + 断言，拦住「控件被静默裁掉」这类问题。

    python tools/check_layout.py

前三轮 UI bug（图片库首开挤成横带、缩略图 125% 下发虚、右栏右侧整片不显示）
都是**离屏探针**定位出来的，但探针每次都是临时脚本、用完就删，所以同样的问题
换个地方又出现了一次。这个脚本把三条判据固化下来：

1. **右栏不能被撑破**——``ScrollColumn`` 关掉了水平滚动条，所以只要内容比视口宽，
   超出的部分就是**看不见**（不是变窄、不是出滚动条）。判据：
   内层容器的最小宽度必须 ≤ 视口宽度。
2. **图片库格子不能被压扁**——QGridLayout 在空间不足时会强压行高，
   格子会从 174px 挤成 52px 的横带。判据：格子高度必须等于设计值。
3. **缩略图必须按物理像素解码**——125% 缩放下按逻辑尺寸解码会被系统放大而发虚。
   判据：``QPixmap.devicePixelRatio()`` 必须等于屏幕缩放比。

退出码 0 表示全部通过。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="echo-layout-")
os.environ["ECHO_DATA_DIR"] = str(Path(_TMP) / "appdata")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget  # noqa: E402

PROBLEMS: list[str] = []

# 覆盖最小窗口、设计默认、以及用户实际最大化的尺寸（2048×1104 逻辑像素）
WINDOW_SIZES = [(900, 640), (1120, 760), (1600, 900), (2048, 1104)]


def note(text: str) -> None:
    print(f"  {text}")


def check_scroll_columns(window, size: tuple[int, int]) -> None:
    """右栏内容不能比视口宽——超出的部分是直接看不见的。"""
    for page_name, page in (
        ("采集页", window.capture_page),
        ("图片库", window.library_page),
        ("设置页", window.settings_page),
    ):
        for index, scroll in enumerate(page.findChildren(QScrollArea)):
            inner = scroll.widget()
            if inner is None:
                continue
            viewport = scroll.viewport().width()
            need = inner.minimumSizeHint().width()
            actual = inner.width()
            label = f"{page_name}#{index}"

            if need > viewport:
                # 找出是哪个直接子控件把最小宽度顶上去的，报出名字好修
                worst = ""
                worst_w = 0
                for kid in inner.children():
                    if not hasattr(kid, "minimumSizeHint"):
                        continue
                    try:
                        kid_w = kid.minimumSizeHint().width()
                    except Exception:
                        continue
                    if kid_w > worst_w:
                        worst_w, worst = kid_w, type(kid).__name__
                PROBLEMS.append(
                    f"{size[0]}×{size[1]} {label}: 内容最小宽 {need} > 视口 {viewport}"
                    f"（缺口 {need - viewport}px，最宽子控件 {worst} {worst_w}px）"
                    " —— 多余部分会被静默裁掉"
                )
                note(f"[失败] {label} 内容{need} > 视口{viewport}")
            elif actual > viewport:
                PROBLEMS.append(
                    f"{size[0]}×{size[1]} {label}: 容器实宽 {actual} > 视口 {viewport}"
                )
                note(f"[失败] {label} 实宽{actual} > 视口{viewport}")
            else:
                note(f"[通过] {label} 内容最小{need} / 实宽{actual} ≤ 视口{viewport}"
                     f"（余量 {viewport - need}px）")


def check_library_grid(window, size: tuple[int, int]) -> None:
    """图片库格子高度与缩略图 DPR。"""
    from echo.ui import imaging
    from echo.ui.pages.library_page import GRID_CELL

    page = window.library_page
    cells = page._cells
    if not cells:
        note("[跳过] 图片库：本页没有数据（未插入测试图）")
        return

    expected_height = GRID_CELL + 26
    squashed = [c for c in cells if c.height() != expected_height]
    if squashed:
        heights = sorted({c.height() for c in squashed})
        PROBLEMS.append(
            f"{size[0]}×{size[1]} 图片库: {len(squashed)}/{len(cells)} 个格子高"
            f"{heights}，期望 {expected_height}（被布局压扁成横带）"
        )
        note(f"[失败] 格子高度 {heights} ≠ {expected_height}")
    else:
        note(f"[通过] {len(cells)} 个格子高均为 {expected_height}")

    # 网格必须在滚动区里，否则行高不足时无处可去
    scroll_ancestor = page.grid.parent()
    found_scroll = False
    while scroll_ancestor is not None:
        if isinstance(scroll_ancestor, QScrollArea):
            found_scroll = True
            break
        scroll_ancestor = scroll_ancestor.parent()
    if not found_scroll:
        PROBLEMS.append(f"{size[0]}×{size[1]} 图片库: 网格不在滚动容器里")
        note("[失败] 网格不在滚动容器里")
    else:
        note("[通过] 网格位于滚动容器内")

    # 缩略图 DPR：这是「125% 缩放下发虚」的判据
    dpr = imaging.screen_dpr()
    thumbs = [c for c in cells if not c._image.pixmap().isNull()]
    if not thumbs:
        note("[跳过] 缩略图尚未解码完成")
        return
    bad = [t for t in thumbs if abs(t._image.pixmap().devicePixelRatio() - dpr) > 1e-6]
    if bad:
        PROBLEMS.append(
            f"{size[0]}×{size[1]} 图片库: {len(bad)}/{len(thumbs)} 张缩略图 DPR "
            f"{bad[0]._image.pixmap().devicePixelRatio()} ≠ 屏幕 {dpr}"
        )
        note(f"[失败] 缩略图 DPR 与屏幕 {dpr} 不一致")
    else:
        sample = thumbs[0]._image.pixmap()
        note(f"[通过] {len(thumbs)} 张缩略图 DPR={sample.devicePixelRatio()}"
             f"（物理 {sample.width()}px / 逻辑 "
             f"{sample.deviceIndependentSize().width():.0f}px）")


def seed_images(app, qt_app, count: int = 12) -> None:
    """往库里塞几张纯色图，让图片库有东西可排。"""
    from datetime import datetime, timedelta

    from echo.storage import ImageRecord, next_session_id
    from echo.storage.naming import image_filename

    layout = app.layout()
    db = app.db()
    if layout is None or db is None:
        return

    session = next_session_id([], datetime(2026, 9, 14, 10, 0, 0))
    layout.session_dir(session.uid).mkdir(parents=True, exist_ok=True)
    base = datetime(2026, 9, 14, 10, 0, 0)
    for index in range(count):
        moment = base + timedelta(seconds=index)
        rel = layout.session_rel(session.uid, image_filename(session, moment, index))
        target = layout.absolute(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        img = QImage(320, 320, QImage.Format_RGB32)
        img.fill(QColor.fromHsv((index * 37) % 360, 160, 220))
        if not img.save(str(target)):
            continue
        db.insert_image(
            ImageRecord(
                rel_path=rel,
                session_uid=session.uid,
                captured_at=moment,
                output_w=320,
                output_h=320,
                image_hash=f"layout{index}",
            )
        )


def drain(qt_app, ms: int = 150) -> None:
    """跑一小段真实事件循环。

    不能用 ``processEvents()`` 空转：后台解码线程通过队列信号回包，
    需要事件循环**真正转起来**才会派发。这里用 QEventLoop + 单次定时器
    等一小段，既让回包送达，又不会挂住。
    """
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> int:
    from echo.app import EchoApp
    from echo.logging_setup import setup_logging
    from echo.ui.main_window import MainWindow

    setup_logging()
    qt_app = QApplication.instance() or QApplication(sys.argv[:1])

    app = EchoApp()
    app.config.storage.save_directory = str(Path(_TMP) / "proj")

    window = MainWindow(app)
    window.show()
    qt_app.processEvents()

    seed_images(app, qt_app)
    window.library_page.refresh()
    qt_app.processEvents()

    for size in WINDOW_SIZES:
        print(f"\n=== 窗口 {size[0]}×{size[1]} ===")
        window.resize(*size)
        qt_app.processEvents()
        window._refresh_compact()
        drain(qt_app, 10)

        for nav in (0, 1, 2):
            window._on_nav(nav)
            qt_app.processEvents()
            drain(qt_app, 6)

        check_scroll_columns(window, size)
        window._on_nav(1)
        drain(qt_app, 20)
        check_library_grid(window, size)
        window._on_nav(0)
        qt_app.processEvents()

    app.shutdown()
    qt_app.processEvents()

    print(f"\n{'=' * 60}")
    if PROBLEMS:
        print(f"发现 {len(PROBLEMS)} 处布局问题：")
        for item in dict.fromkeys(PROBLEMS):
            print(f"  - {item}")
        return 1
    print("布局校验全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
