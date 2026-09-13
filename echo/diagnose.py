"""采集链路诊断。

「抓不到游戏画面」这类问题的排查现场在**出问题的那台机器上**，而原因往往不在
代码里：独占全屏、HDR、双显卡、反作弊——每一种的表现都是「API 一切正常，但
拿到的画面是黑的」。这个模块把该问的问题一次性问完：

1. 环境（系统 / Python / DPI 感知）；
2. 每个后端**自报**的能力与已知边界（probe_all）；
3. 实际打开再抓若干帧，看能不能拿到、尺寸对不对、耗时多少、**是不是全黑**；
4. 按结果给出可执行的排查建议。

界面上的「采集诊断」按钮和 ``tools/diagnose_capture.py`` 共用这一份实现，
避免两处逻辑漂移。整个流程只读：不写图片、不改配置、不注册热键。
"""

from __future__ import annotations

import platform
import sys
import time
from dataclasses import dataclass

from .capture.base import SourceKind, SourceSpec
from .capture.factory import create_backend, probe_all
from .capture.sources import is_source_usable, list_monitors, list_windows
from .quality import BLANK_LUMA, quick_luma
from .region import center_region
from .winapi import enable_per_monitor_dpi_awareness

PROBE_FRAMES = 6            # 每个源抓几帧
PROBE_TIMEOUT = 0.35        # 等"新帧"的上限
PROBE_SIZE = 320            # 试验选区取源中央 320×320
MAX_WINDOWS = 6             # 不带关键词时最多测几个窗口


@dataclass(slots=True)
class Trial:
    """一次「打开 + 抓若干帧」的试验结果。"""

    label: str
    backend_key: str
    opened: bool = False
    frames: int = 0
    black: int = 0
    size: str = ""
    luma: float = 0.0
    per_frame_ms: float = 0.0
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.frames > 0 and self.black < self.frames

    @property
    def blank_only(self) -> bool:
        return self.frames > 0 and self.black == self.frames

    @property
    def no_frame(self) -> bool:
        return self.opened and self.frames == 0

    @property
    def verdict(self) -> str:
        if not self.opened:
            return "打开失败"
        if self.frames == 0:
            return "取不到帧"
        if self.black == self.frames:
            return "全是黑屏"
        if self.black:
            return "部分黑屏"
        return "正常"

    def lines(self, indent: str = "   ") -> list[str]:
        head = f"{indent}{self.backend_key:<5} {self.label}"
        if not self.opened:
            return [head, f"{indent}      → {self.verdict}：{self.reason}"]
        out = [
            head,
            f"{indent}      → {self.verdict}  "
            f"取到 {self.frames}/{PROBE_FRAMES} 帧  "
            f"尺寸 {self.size or '—'}  "
            f"平均亮度 {self.luma:.1f}  "
            f"黑屏 {self.black}/{self.frames}  "
            f"每帧 {self.per_frame_ms:.1f} ms",
        ]
        if self.reason:
            out.append(f"{indent}        注：{self.reason}")
        return out


def _run_trial(spec: SourceSpec, backend_key: str) -> Trial:
    trial = Trial(label=spec.title or spec.label, backend_key=backend_key)

    backend = None
    try:
        backend = create_backend(backend_key)
        backend.open(spec)
        trial.opened = True
    except Exception as exc:
        trial.reason = str(exc)
        return trial

    width = min(PROBE_SIZE, max(16, spec.source_width))
    height = min(PROBE_SIZE, max(16, spec.source_height))
    region = center_region(spec.source_width, spec.source_height, width, height)

    luma_total = 0.0
    started = time.perf_counter()
    try:
        for _ in range(PROBE_FRAMES):
            try:
                frame = backend.grab(region, timeout=PROBE_TIMEOUT)
            except Exception as exc:
                trial.reason = f"抓帧抛错：{exc}"
                break
            if frame is None:
                continue
            trial.frames += 1
            if not trial.size:
                trial.size = f"{frame.image.shape[1]}×{frame.image.shape[0]}"
            luma = quick_luma(frame.image)
            if luma < 0:
                continue
            luma_total += luma
            if luma < BLANK_LUMA:
                trial.black += 1
    finally:
        try:
            backend.close()
        except Exception:
            pass

    trial.per_frame_ms = (time.perf_counter() - started) * 1000.0 / max(1, PROBE_FRAMES)
    if trial.frames:
        trial.luma = luma_total / trial.frames
    elif not trial.reason:
        trial.reason = (
            f"{PROBE_FRAMES} 次抓取都在 {PROBE_TIMEOUT}s 内没等到新帧"
            "（窗口捕获只在重绘时交付，静止窗口不出帧是正常的）"
        )
    return trial


def _env_lines() -> list[str]:
    frozen = "（打包运行）" if getattr(sys, "frozen", False) else ""
    return [
        "① 运行环境",
        f"   平台      {platform.platform()}",
        f"   Python    {platform.python_version()} "
        f"({'64' if sys.maxsize > 2 ** 32 else '32'} 位){frozen}",
        f"   DPI 感知  {enable_per_monitor_dpi_awareness()}",
    ]


def _backend_lines() -> tuple[list[str], dict[str, list[SourceKind]]]:
    out = ["", "② 后端自报能力"]
    supported: dict[str, list[SourceKind]] = {}
    for key, cap in probe_all().items():
        supported[key] = list(cap.kinds)
        out.append(f"   {key:<5} available={cap.available}  "
                   f"支持={sorted(k.value for k in cap.kinds) or '无'}")
        out.append(f"         {cap.summary}")
        if not cap.available and cap.reason:
            out.append(f"         不可用原因：{cap.reason}")
        for note in cap.notes:
            out.append(f"         · {note}")
    return out, supported


def _source_lines(keyword: str | None) -> tuple[list[str], list[SourceSpec],
                                                list[SourceSpec]]:
    out = ["", "③ 采集源"]
    monitors = list_monitors()
    out.append(f"   显示器 {len(monitors)} 个")
    for mon in monitors:
        ok, why = is_source_usable(mon)
        out.append(f"     · {mon.label}")
        out.append(f"       {mon.source_width}×{mon.source_height}  "
                   f"起点 ({mon.origin_left},{mon.origin_top})  "
                   f"可用={ok}{'' if ok else '  原因：' + why}")

    windows = list_windows()
    if keyword is None:
        picked = windows[:MAX_WINDOWS]
        note = f"（只取前 {MAX_WINDOWS} 个）"
    elif keyword == "":
        picked = windows
        note = "（全部）"
    else:
        low = keyword.lower()
        picked = [w for w in windows
                  if low in (w.title or "").lower()
                  or low in (w.process_name or "").lower()]
        note = f"（筛选“{keyword}”，命中 {len(picked)} 个）"

    out.append(f"   窗口 {len(windows)} 个 {note}")
    for win in picked:
        ok, why = is_source_usable(win)
        out.append(f"     · {win.title or '(无标题)'}")
        out.append(f"       {win.process_name or '?'}  "
                   f"客户区 {win.source_width}×{win.source_height}  "
                   f"可用={ok}{'' if ok else '  原因：' + why}")
    return out, monitors, picked


def _trial_lines(monitors: list[SourceSpec], windows: list[SourceSpec],
                 supported: dict[str, list[SourceKind]]) -> tuple[list[str],
                                                                  list[Trial]]:
    out = ["", f"④ 实际抓帧试验（抓 {PROBE_FRAMES} 帧，选区为源中央 "
               f"{PROBE_SIZE}×{PROBE_SIZE}）"]
    results: list[Trial] = []

    for spec in monitors:
        for key in ("dxgi", "wgc"):
            if SourceKind.MONITOR not in supported.get(key, []):
                out.append(f"   {key:<5} {spec.label}")
                out.append("         → 跳过：该后端不支持显示器采集")
                continue
            trial = _run_trial(spec, key)
            results.append(trial)
            out.extend(trial.lines())

    for spec in windows:
        if SourceKind.WINDOW not in supported.get("wgc", []):
            out.append("   wgc   跳过：不支持窗口采集")
            break
        trial = _run_trial(spec, "wgc")
        results.append(trial)
        out.extend(trial.lines())

    return out, results


def _verdict_lines(results: list[Trial]) -> list[str]:
    out = ["", "⑤ 结论"]

    usable = [t for t in results if t.usable]
    blank_only = [t for t in results if t.blank_only]
    no_frame = [t for t in results if t.no_frame]
    failed = [t for t in results if not t.opened]

    if usable:
        out.append("   能取到正常画面的链路：")
        for t in usable:
            out.append(f"     ✓ {t.backend_key} / {t.label}  "
                       f"尺寸 {t.size}  亮度 {t.luma:.1f}  "
                       f"每帧 {t.per_frame_ms:.1f} ms")
    else:
        out.append("   ✗ 没有任何链路能取到正常画面。")

    if blank_only:
        out += [
            "",
            "   ⚠ 抓到了画面但**全是黑的**——这是最容易误判的一类故障：后端没有",
            "     报错、API 全部成功，只是每帧都没有内容。按可能性排查：",
            "     1. 游戏是「独占全屏」：改成「无边框窗口」或「窗口化」。桌面复制",
            "        拿不到独占全屏的内容，这是 Windows 的机制限制，不是工具的问题。",
            "     2. 游戏开了 HDR：先关掉。HDR 会让输出变成 10bit/16bit，拿到的",
            "        缓冲区无法直接解读。",
            "     3. 笔记本双显卡：游戏跑在独显、而采集到的是核显输出。可在系统",
            "        「显示设置 → 图形」里把游戏指定到同一块 GPU。",
            "     4. 游戏已最小化或停止渲染：先切回游戏。",
            "     提示：优先用「窗口模式」直接选游戏窗口，它对独占全屏更宽容。",
        ]

    if no_frame:
        out += ["", "   ⚠ 后端能打开但取不到新帧："]
        out += [f"     ✗ {t.backend_key} / {t.label}" for t in no_frame]
        out += [
            "     窗口捕获只在窗口重绘时交付新帧，静止窗口不出帧是正常的；",
            "     若游戏正在动也不出帧，换一个采集源或后端再试。",
        ]

    if failed:
        out += ["", "   ✗ 打开就失败的链路："]
        out += [f"     ✗ {t.backend_key} / {t.label}：{t.reason}" for t in failed]

    if not results:
        out.append("   没有可试验的采集源：请确认显示器枚举正常、游戏正在运行。")

    if usable and not blank_only:
        out += ["", "   采集后端本身工作正常。若游戏里仍抓不到，问题在游戏侧的",
                "   渲染模式（独占全屏 / HDR），按上面第 1、2 条处理。"]
    return out


def collect_report(keyword: str | None = None) -> str:
    """跑一遍完整诊断并返回可读报告。

    ``keyword`` 为 ``None`` 时只测前几个窗口；给字符串则按标题/进程名筛选；
    给空串则列出全部窗口。整个流程只读，可以随时中断。
    """
    enable_per_monitor_dpi_awareness()

    lines: list[str] = ["=" * 68, "echo 采集链路诊断", "=" * 68]
    lines += _env_lines()
    backend_lines, supported = _backend_lines()
    lines += backend_lines
    source_lines, monitors, windows = _source_lines(keyword)
    lines += source_lines
    trial_lines, results = _trial_lines(monitors, windows, supported)
    lines += trial_lines
    lines += _verdict_lines(results)
    return "\n".join(lines)
