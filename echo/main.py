"""echo 的启动入口：先做 DPI 声明与单实例检查，再拉起 Qt 应用。

两处时序不能调换，否则会出难查的怪现象：

1. **DPI 声明必须在任何 Qt 对象之前**。Win32 侧的 Per-Monitor V2 决定枚举到的
   显示器 / 窗口矩形是物理像素还是被虚拟化过的逻辑像素。晚一步声明，拿到的
   坐标就是错的，选区整体偏移，而且在小窗口上才看得出来。
2. **单实例检查必须在注册全局热键之前**。RegisterHotKey 是进程级独占资源，
   两个实例会互相抢注，表现成「快捷键时灵时不灵」——用户很难想到是开了两份。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QLockFile, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_DISPLAY_NAME, APP_NAME, __version__
from .logging_setup import get_logger, setup_logging
from .paths import app_data_dir, is_frozen
from .ui import icons
from .ui.main_window import MainWindow
from .ui.theme import DARK
from .winapi import enable_per_monitor_dpi_awareness

log = get_logger("main")


def _acquire_single_instance() -> QLockFile | None:
    """拿到进程锁返回 lock 对象，已被占用返回 None。"""
    lock = QLockFile(str(app_data_dir() / f"{APP_NAME}.lock"))
    # 默认 30 秒的 stale 判定：进程被强杀后锁文件会自己失效，不需要用户手动删。
    if lock.tryLock(200):
        return lock
    return None


def _install_excepthook() -> None:
    """未捕获异常写日志 + 弹窗，避免界面直接消失而用户不知道为什么。"""

    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("未捕获的异常", exc_info=(exc_type, exc_value, exc_tb))
        try:
            box = QMessageBox()
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle(f"{APP_DISPLAY_NAME} 遇到问题")
            box.setText(f"{exc_type.__name__}: {exc_value}")
            box.setInformativeText(
                "详细信息已写入日志。若反复出现，请把 metadata 目录旁的这份日志一并提供。"
            )
            box.exec()
        except Exception:  # pragma: no cover - 弹窗本身失败就别再抛了
            pass

    sys.excepthook = hook


def run(argv: list[str] | None = None) -> int:
    """创建并运行应用，返回进程退出码。"""
    argv = list(sys.argv if argv is None else argv)

    # 1. DPI 声明：必须在 QApplication 之前
    awareness = enable_per_monitor_dpi_awareness()

    # 2. 日志：打包成无控制台窗口后 stderr 为 None，此时只写文件
    setup_logging(to_console=sys.stderr is not None and not is_frozen())
    log.info(
        "%s %s 启动（frozen=%s, dpi_awareness=%s）",
        APP_DISPLAY_NAME, __version__, is_frozen(), awareness,
    )
    if not awareness:
        log.warning("未能开启 Per-Monitor V2 DPI 感知，多显示器下的选区坐标可能偏移")

    # 3. Qt：取整策略设为 PassThrough，让 125% / 150% 缩放下布局不被量化
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    qt_app = QApplication(argv)
    qt_app.setApplicationName(APP_NAME)
    qt_app.setApplicationDisplayName(APP_DISPLAY_NAME)
    qt_app.setApplicationVersion(__version__)
    qt_app.setOrganizationName(APP_NAME)
    qt_app.setWindowIcon(icons.app_icon(DARK.accent))
    # 关闭主窗口是「收起托盘」而不是退出（方案 §6.1），
    # 所以不能让「最后一个窗口关闭」结束进程。
    qt_app.setQuitOnLastWindowClosed(False)

    # 4. 单实例：必须在注册热键之前
    lock = _acquire_single_instance()
    if lock is None:
        log.warning("已有实例在运行，本次启动退出")
        QMessageBox.information(
            None,
            APP_DISPLAY_NAME,
            f"{APP_DISPLAY_NAME} 已经在运行了。\n"
            f"请从任务栏托盘的 {APP_DISPLAY_NAME} 图标打开它，"
            f"或按 Ctrl+Alt+E 唤出主窗口。",
        )
        return 0

    from .app import EchoApp

    app = EchoApp()
    window = MainWindow(app)
    _install_excepthook()
    window.show()

    try:
        return qt_app.exec()
    finally:
        try:
            app.shutdown()
        except Exception:  # pragma: no cover
            log.exception("关闭时出错")
        lock.unlock()
        log.info("%s 已退出", APP_DISPLAY_NAME)


__all__ = ["run"]
