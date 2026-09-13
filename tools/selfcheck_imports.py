"""导入自检：逐个导入全部模块，报告失败项。

用法：
    python tools/selfcheck_imports.py

不带 Qt 事件循环，仅验证模块可导入、语法与循环依赖正常。
"""
from __future__ import annotations

import importlib
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CORE_MODULES = [
    "echo",
    "echo.paths",
    "echo.logging_setup",
    "echo.winapi",
    "echo.region",
    "echo.dpi",
    "echo.config",
    "echo.quality",
    "echo.hotkeys",
    "echo.pipeline",
    "echo.app",
]

CAPTURE_MODULES = [
    "echo.capture",
    "echo.capture.base",
    "echo.capture.frame",
    "echo.capture.sources",
    "echo.capture.dxgi_monitor",
    "echo.capture.wgc_window",
    "echo.capture.factory",
]

STORAGE_MODULES = [
    "echo.storage",
    "echo.storage.naming",
    "echo.storage.db",
    "echo.storage.writer",
    "echo.storage.manifest",
]

DATASET_MODULES = [
    "echo.dataset",
    "echo.dataset.labels",
    "echo.dataset.exporter",
    "echo.dataset.importer",
]

UI_MODULES = [
    "echo.ui",
    "echo.ui.theme",
    "echo.ui.icons",
    "echo.ui.widgets",
    "echo.ui.hotkey_edit",
    "echo.ui.region_picker",
    "echo.ui.imaging",
    "echo.ui.preview",
    "echo.ui.pages",
    "echo.ui.pages.capture_page",
    "echo.ui.pages.library_page",
    "echo.ui.pages.settings_page",
    "echo.ui.tray",
    "echo.ui.main_window",
]

GROUPS = [
    ("core", CORE_MODULES),
    ("capture", CAPTURE_MODULES),
    ("storage", STORAGE_MODULES),
    ("dataset", DATASET_MODULES),
    ("ui", UI_MODULES),
]


def main() -> int:
    total = 0
    failed: list[tuple[str, str]] = []

    for group, modules in GROUPS:
        print(f"\n=== {group} ===")
        for name in modules:
            total += 1
            try:
                importlib.import_module(name)
            except Exception:
                failed.append((name, traceback.format_exc(limit=6)))
                print(f"  FAIL  {name}")
            else:
                print(f"  ok    {name}")

    print(f"\n合计 {total} 个模块，失败 {len(failed)} 个")
    for name, tb in failed:
        print(f"\n--- {name} ---\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
