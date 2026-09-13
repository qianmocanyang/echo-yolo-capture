"""无头（offscreen）冒烟测试：真实实例化 EchoApp + MainWindow 并跑事件循环。

这是 UI 层的主要验证手段——导入通过不代表能构造，能构造不代表能启动。

用法：
    python tools/smoke_ui.py

脚本会：
  1. 把配置目录指向临时目录（ECHO_DATA_DIR），不碰用户真实配置；
  2. 用 Qt offscreen 平台插件创建应用；
  3. 构造 EchoApp 与 MainWindow，show() 后跑事件循环，触发 _after_show -> app.start()；
  4. 依次切到采集 / 图片库 / 设置 三个页面；
  5. 收集全部 warning/critical 日志与未捕获异常，最后打印判定结果。

退出码 0 表示无异常。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 PySide6 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TMP = tempfile.mkdtemp(prefix="echo-smoke-")
os.environ["ECHO_DATA_DIR"] = str(Path(_TMP) / "appdata")

PROBLEMS: list[str] = []


class _Collector(logging.Handler):
    """把 WARNING 及以上级别的日志收进 PROBLEMS。"""

    IGNORE = (
        # offscreen 平台插件下必然出现、与应用逻辑无关的噪音
        "propagateSizeHints",
        "QStandardPaths",
    )

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            text = self.format(record)
        except Exception:
            return
        if any(token in text for token in self.IGNORE):
            return
        PROBLEMS.append(f"[{record.levelname}] {text}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from echo import APP_DISPLAY_NAME, __version__
    from echo.app import EchoApp
    from echo.logging_setup import setup_logging

    setup_logging()
    root_logger = logging.getLogger()
    handler = _Collector()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root_logger.addHandler(handler)

    section("环境")
    print(f"应用      : {APP_DISPLAY_NAME} {__version__}")
    print(f"Qt 平台   : {os.environ['QT_QPA_PLATFORM']}")
    print(f"数据目录  : {os.environ['ECHO_DATA_DIR']}")

    qt_app = QApplication.instance() or QApplication(sys.argv[:1])

    section("构造 EchoApp")
    try:
        app = EchoApp()
    except Exception:
        print("构造 EchoApp 失败：")
        traceback.print_exc()
        return 1
    print("  ok  EchoApp()")

    # 收集应用层 notice（这是应用主动告诉用户的问题）
    notices: list[tuple[str, str]] = []
    app.notice.connect(lambda level, text: notices.append((level, text)))
    app.hotkey_error.connect(
        lambda action, combo, msg: notices.append(("hotkey", f"{action} {combo}: {msg}"))
    )
    app.backend_notice.connect(lambda text: notices.append(("backend", text)))

    section("构造 MainWindow")
    try:
        from echo.ui.main_window import MainWindow

        window = MainWindow(app)
        window.show()
    except Exception:
        print("构造 MainWindow 失败：")
        traceback.print_exc()
        return 1
    print("  ok  MainWindow() + show()")

    section("事件循环（触发 _after_show -> app.start()）")
    QTimer.singleShot(2500, qt_app.quit)
    qt_app.exec()
    print("  ok  事件循环正常退出")
    print(f"  当前状态: {app.state_value}")

    section("页面切换")
    for index in range(3):
        try:
            window._on_nav(index)
            qt_app.processEvents()
        except Exception:
            print(f"  切换第 {index} 页失败：")
            traceback.print_exc()
            return 1
    print("  ok  采集 / 图片库 / 设置 三页均可切换")

    section("窄窗布局（< 1000px 收拢侧栏）")
    try:
        print(f"  初始宽度 {window.width()}px，compact={window._compact}")
        window.resize(880, 700)
        qt_app.processEvents()
        # offscreen 平台不派发 resizeEvent，手动驱动一次以验证分支逻辑
        window._refresh_compact()
        print(f"  缩到 880 后宽度 {window.width()}px，compact={window._compact}")
        if not window._compact:
            PROBLEMS.append("窗口收窄到 900px 时侧栏未收拢")
        # 用 isHidden() 而非 isVisible()：offscreen 平台窗口未真正 mapped，
        # isVisible() 对所有子控件都返回 False，只有隐藏标志是可判定的。
        if window.echo_mark_small.isHidden():
            PROBLEMS.append("侧栏收拢后状态栏的 echo 字标未出现")

        window.resize(1280, 820)
        qt_app.processEvents()
        window._refresh_compact()
        print(f"  放大后宽度 {window.width()}px，compact={window._compact}")
        if window._compact:
            PROBLEMS.append("放大到 1280px 后侧栏仍未展开")
        if not window.echo_mark_small.isHidden():
            PROBLEMS.append("侧栏展开后状态栏的 echo 字标未隐藏")
    except Exception:
        print("  窄窗切换失败：")
        traceback.print_exc()
        return 1

    section("手动截图请求（无采集源时应给出提示而非崩溃）")
    try:
        app.request_manual()
        qt_app.processEvents()
        print("  ok  request_manual() 未抛异常")
    except Exception:
        print("  request_manual() 失败：")
        traceback.print_exc()
        return 1

    section("关闭")
    try:
        app.shutdown()
        qt_app.processEvents()
    except Exception:
        print("  shutdown 失败：")
        traceback.print_exc()
        return 1
    print("  ok  shutdown()")

    section("应用层提示（notice）")
    if notices:
        for level, text in notices:
            print(f"  {level:<8} {text}")
    else:
        print("  （无）")

    section("日志中的 warning/critical")
    if PROBLEMS:
        for item in PROBLEMS:
            print(f"  {item}")
    else:
        print("  （无）")

    print("\n结论：", "通过" if not PROBLEMS else f"{len(PROBLEMS)} 条警告待确认")
    return 0 if not PROBLEMS else 2


if __name__ == "__main__":
    raise SystemExit(main())
