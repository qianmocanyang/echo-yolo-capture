"""采集链路诊断（命令行入口）。

用在「抓不到游戏画面」的那台机器上：跑一遍，把输出整段发回来即可定位原因。
界面上的「设置 → 采集诊断」走的是同一份实现（``echo/diagnose.py``），
这里只是把它包一层命令行。

    python tools/diagnose_capture.py                    # 默认探测
    python tools/diagnose_capture.py --window Rust      # 只测标题/进程名含 Rust 的窗口
    python tools/diagnose_capture.py --window ""        # 列出全部窗口

整个流程只读：不写图片、不改配置、不注册热键。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from echo.diagnose import collect_report  # noqa: E402


def main() -> int:
    argv = sys.argv[1:]
    keyword: str | None = None
    for index, arg in enumerate(argv):
        if arg == "--window":
            keyword = argv[index + 1] if index + 1 < len(argv) else ""

    print(collect_report(keyword))
    print()
    print("把以上输出整段发回来即可。详细日志：")
    print(r"  %APPDATA%\echo\logs\echo.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
