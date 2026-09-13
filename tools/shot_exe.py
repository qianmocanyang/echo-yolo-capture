"""启动打包后的 exe，把主窗口提到前台并截图。

源码运行正常不代表打包后正常：Qt 插件、字体、QSS 里的相对路径在冻结环境
里都可能变。这个脚本是唯一能覆盖「打包产物真实渲染」的检查。

    python tools/shot_exe.py [输出路径]
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "echo" / "echo.exe"

_flags = [a for a in sys.argv[1:] if a.startswith("--")]
_positional = [a for a in sys.argv[1:] if not a.startswith("--")]
SOURCE_MODE = "--src" in _flags
CLEAN_MODE = "--clean" in _flags
OUT = (
    Path(_positional[0]) if _positional
    else ROOT / "assets" / ("_src_shot.png" if SOURCE_MODE else "_exe_shot.png")
)

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


def find_window(pid: int, title_keyword: str | None = None) -> int | None:
    """找该进程的可见顶层窗口，``title_keyword`` 作为兜底条件。

    一开始按标题关键词匹配，结果匹配到了资源管理器——它的地址栏正好是
    ``AppData\\Roaming\\echo``，字符串里带 "echo"。所以主用 PID 认。

    但源码模式要留标题兜底：venv 里的 ``Scripts\\python.exe`` 是个转发器，
    会再起一个子进程跑真正的解释器，窗口属于子进程，Popen 拿到的 PID 对不上。
    """
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True

        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.append(hwnd)
            return True

        if title_keyword:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title_keyword in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def grab_window(hwnd: int, width: int, height: int):
    """用 PrintWindow 抓窗口自身内容。

    比 ``QScreen.grabWindow`` 可靠两处：

    * 后者在 Windows 上就是对屏幕 DC 做 BitBlt，窗口被遮挡的地方会抓到
      别的窗口的内容——验证时正好撞上，截出一张"左半边是资源管理器"的图。
    * 后者区域参数按逻辑像素算、要自己除 dpr，容易搞反。

    PrintWindow 直接让窗口把内容画到我们给的 DC 上，与遮挡、DPI 都无关。
    """
    gdi32 = ctypes.windll.gdi32
    PW_RENDERFULLCONTENT = 0x00000002

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    previous = gdi32.SelectObject(mem_dc, bitmap)

    try:
        if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
            raise RuntimeError("PrintWindow 失败")

        info = BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.biWidth = width
        info.biHeight = -height        # 负值 = top-down，省得再翻转
        info.biPlanes = 1
        info.biBitCount = 32
        info.biCompression = 0         # BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(
            mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0
        ):
            raise RuntimeError("GetDIBits 失败")

        from PySide6.QtGui import QImage

        # 32 位 BI_RGB 在小端机器上的字节序就是 BGRA，与 Format_ARGB32 一致
        image = QImage(
            buffer, width, height, width * 4, QImage.Format_ARGB32
        ).copy()                        # copy() 让 QImage 脱离 buffer 生命周期
        return image
    finally:
        gdi32.SelectObject(mem_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def kill() -> None:
    subprocess.run(
        ["taskkill", "/IM", "echo.exe", "/F"],
        capture_output=True, check=False,
    )


def main() -> int:
    if not SOURCE_MODE and not EXE.exists():
        print(f"找不到 {EXE}，先跑 pyinstaller echo.spec")
        return 1

    if SOURCE_MODE:
        # 对照用：同样的截图流程跑源码版，用来区分「打包特有」和「代码本身」的问题
        cmd = [sys.executable, str(ROOT / "main.py")]
        cwd = str(ROOT)
        label = "main.py（源码版）"
    else:
        kill()  # 清掉残留实例，否则新实例会因单实例锁直接退出
        time.sleep(1)
        cmd = [str(EXE)]
        cwd = str(EXE.parent)
        label = f"{EXE.name}（打包版）"

    print(f"启动 {label} ...")

    env = os.environ.copy()
    if CLEAN_MODE:
        # 用全新配置启动，既能验证「首次运行」的样子，也不动用户真实配置
        data_dir = tempfile.mkdtemp(prefix="echo-shot-")
        env["ECHO_DATA_DIR"] = data_dir
        print(f"数据目录 : {data_dir}（隔离）")

    proc = subprocess.Popen(cmd, cwd=cwd, env=env)

    try:
        hwnd = None
        for _ in range(40):          # 最多等 20 秒
            time.sleep(0.5)
            hwnd = find_window(
                proc.pid,
                None if not SOURCE_MODE else "游戏截图与数据集采集",
            )
            if hwnd:
                break

        if not hwnd:
            print("失败：没等到主窗口。检查 %APPDATA%\\echo\\logs\\echo.log")
            return 1

        # 稍微多等一会，让首帧渲染完、异步的源枚举收尾
        time.sleep(4)

        # SetForegroundWindow 常被 Windows 的前台锁定策略拒绝（尤其从后台脚本调用时），
        # 表现为截到的图里窗口被别的窗口压住。设成 TOPMOST 才稳，截完再摘掉。
        HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
        SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
        flags = SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        time.sleep(2)

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top

        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])  # noqa: F841
        screen = QGuiApplication.primaryScreen()
        geo = screen.geometry()
        dpr = screen.devicePixelRatio()
        print(f"窗口 {width}×{height} 物理像素 @ ({rect.left},{rect.top})")
        print(f"主屏 {geo.width()}×{geo.height()} 逻辑像素，缩放 {dpr}")
        print(f"窗口折算逻辑尺寸约 {round(width / dpr)}×{round(height / dpr)}"
              f"（设计默认 1120×760，900×640 以下才收拢侧栏）")

        shot = grab_window(hwnd, width, height)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        shot.save(str(OUT), "PNG")

        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        print(f"已截图 {OUT}（{shot.width()}×{shot.height()}）")
        return 0
    finally:
        proc.terminate()
        time.sleep(1)
        if not SOURCE_MODE:
            kill()


if __name__ == "__main__":
    raise SystemExit(main())
