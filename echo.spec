# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

    python tools/make_icon.py          # 先出图标（assets/echo.ico）
    pyinstaller echo.spec --noconfirm

产物：``dist/echo/echo.exe``

**为什么用目录模式（COLLECT）而不是单文件**：

* 单文件每次启动都要把几百 MB 的 Qt 解压到临时目录，冷启动要好几秒；
  这是个常驻托盘的工具，启动速度是每天都在付的成本。
* 采集后端带原生 DLL（DXCam / WindowsCapture），目录模式下排障时能直接
  替换单个文件，单文件模式只能整体重打。
* 单文件会把内容解到 ``%TEMP%\\_MEIxxxx``，而部分反作弊会盯着这类
  临时目录里的注入行为——常驻工具没必要冒这个风险。

``console=False``：无控制台窗口，日志写 ``%APPDATA%\\echo\\logs``。
"""

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# 静态分析看不到的模块：编译扩展、动态导入的 COM 包
hiddenimports = [
    "windows_capture",   # WGC 后端是 .pyd 扩展
    "comtypes",          # dxcam 靠它动态生成 COM 包装
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
]

# 自己的包也整体收进来：UI 页面是按需 import 的，只靠入口追踪容易漏模块，
# 而漏掉的后果是打包后点某个页面才崩。
hiddenimports += collect_submodules("echo")
hiddenimports += collect_submodules("dxcam")

# 采集后端自带的原生库
binaries = collect_dynamic_libs("dxcam") + collect_dynamic_libs("windows_capture")

# 只读资源。注意 PyInstaller 6 会把它们放进 dist/echo/_internal/，
# 运行时代码走 paths.resource_root()（即 sys._MEIPASS）定位，不要用 exe 所在目录。
datas = [("assets/echo.ico", "assets")]

# 界面用不到的 Qt 模块与科学计算包：不排除的话产物体积接近翻倍
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.QtQuickWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtRemoteObjects",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSensors", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest", "setuptools",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="echo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX 压缩过的原生库容易被反作弊误判，不开
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/echo.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="echo",
)
