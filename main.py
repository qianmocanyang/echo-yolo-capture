"""开发运行与 PyInstaller 打包共用的入口脚本。

    python main.py          # 直接从源码运行

PyInstaller 的入口也用这个文件（见 echo.spec），这样开发与打包走同一条路径，
不会出现「源码能跑、打包后行为不一样」。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from echo.main import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())
