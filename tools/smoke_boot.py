"""启动入口冒烟：真实执行 ``main.py``，看它能不能活着起来、干净地退出。

前面的 smoke_ui 是「手工拼装 EchoApp + MainWindow」，验证的是组件；
这里走的是用户实际点开的那个路径——DPI 声明、日志、Qt 初始化、单实例锁、
异常钩子全套。两者挂掉的原因往往不一样。

用 offscreen 平台插件跑，几秒后主动终止；只要这段时间里没有 Traceback
且日志里出现了启动完成标记，就算通过。

用法：
    python tools/smoke_boot.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
RUN_SECONDS = 4.0


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="echo-boot-")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["ECHO_DATA_DIR"] = str(Path(tmp) / "appdata")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    print("=== 启动入口 ===")
    print(f"脚本     : {ROOT / 'main.py'}")
    print(f"Qt 平台  : {env['QT_QPA_PLATFORM']}")
    print(f"数据目录 : {env['ECHO_DATA_DIR']}")
    print(f"观察时长 : {RUN_SECONDS}s")

    proc = subprocess.Popen(
        [PYTHON, str(ROOT / "main.py")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        output, _ = proc.communicate(timeout=RUN_SECONDS)
        exit_note = f"进程在 {RUN_SECONDS}s 内自行退出（退出码 {proc.returncode}）"
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
        exit_note = f"运行中，已在 {RUN_SECONDS}s 后主动终止（符合预期）"

    print(f"\n{exit_note}\n")

    problems: list[str] = []
    if "Traceback" in output:
        problems.append("启动过程中出现 Traceback")
    if "已启动" not in output:
        problems.append("日志里没有出现启动完成标记")
    if "Traceback" not in output and proc.returncode not in (0, None, 1, -15, 15, 143):
        # terminate() 在 Windows 上返回 1，POSIX 上返回 -15，都属正常
        problems.append(f"异常退出码：{proc.returncode}")

    print("=== 进程输出 ===")
    print(output.strip() or "（无输出）")

    print("\n=== 结论 ===")
    if problems:
        for item in problems:
            print(f"  失败：{item}")
        return 1
    print("  通过：入口可正常启动并保持运行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
