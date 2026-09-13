"""一键自检：把五道检查按「从便宜到贵」的顺序跑一遍。

    python tools/selfcheck.py            # 全部
    python tools/selfcheck.py --fast     # 只跑静态检查（不启动 Qt）

顺序是有讲究的：静态扫描几毫秒、导入一秒、业务逻辑三秒、界面与启动冒烟要拉起
整个 Qt 与采集后端。前面的先挂，后面就没必要跑了。

| 检查 | 能发现的问题 |
| --- | --- |
| check_attrs | `self.xxx` 调用了但没定义——语法合法，走到那行才崩 |
| selfcheck_imports | 循环导入、语法错误、缺依赖 |
| test_logic | 选区算错、坐标换算错、YOLO 划分泄漏、DB/写盘逻辑错 |
| smoke_ui | 控件构造失败、信号连接错、页面切换崩 |
| smoke_boot | DPI 声明时序、单实例锁、Qt 初始化、异常钩子 |
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

CHECKS = [
    ("静态属性扫描", "tools/check_attrs.py", True),
    ("模块导入", "tools/selfcheck_imports.py", True),
    ("业务逻辑", "tests/test_logic.py", False),
    ("界面冒烟", "tools/smoke_ui.py", False),
    ("启动入口冒烟", "tools/smoke_boot.py", False),
]


def main() -> int:
    fast = "--fast" in sys.argv

    results: list[tuple[str, bool, float, str]] = []
    for title, script, is_static in CHECKS:
        if fast and not is_static:
            continue
        path = ROOT / script
        if not path.exists():
            results.append((title, False, 0.0, f"脚本不存在：{script}"))
            continue

        print(f"\n{'=' * 68}\n▶ {title}  ({script})\n{'=' * 68}", flush=True)
        started = time.perf_counter()
        proc = subprocess.run(
            [PYTHON, str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.perf_counter() - started
        output = (proc.stdout or "") + (proc.stderr or "")
        print(output.rstrip())
        # 静态扫描器永远返回 0，靠输出里的计数判断
        if script.endswith("check_attrs.py"):
            ok = "可疑成员合计 0 处" in output
        elif script.endswith("selfcheck_imports.py"):
            ok = "失败 0 个" in output
        else:
            ok = proc.returncode == 0
        results.append((title, ok, elapsed, "" if ok else output))

    print(f"\n{'=' * 68}\n自检汇总\n{'=' * 68}")
    for title, ok, elapsed, _ in results:
        print(f"  {'通过' if ok else '失败'}  {title:<16} {elapsed:6.2f}s")

    failed = [r for r in results if not r[1]]
    print()
    if failed:
        print(f"{len(failed)} 项未通过，失败项详情见上方输出。")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
