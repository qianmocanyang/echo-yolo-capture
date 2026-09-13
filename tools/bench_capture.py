"""采集链路的耗时基准测试。

目的：优化前先拿到每条路径的真实数字，避免凭感觉改。

    python tools/bench_capture.py            # 走真实屏幕尺寸
    python tools/bench_capture.py 320x320    # 指定裁剪尺寸
"""

from __future__ import annotations

import hashlib
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from echo.quality import measure, pixel_hash  # noqa: E402
from echo.storage.writer import encode_png  # noqa: E402


def make_image(width: int, height: int) -> np.ndarray:
    """造一张有真实纹理的图。纯色会让 PNG 压缩快得不真实。"""
    rng = np.random.default_rng(20260913)
    noise = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
    # 叠一层渐变，避免整图都是高频噪声（那样压缩率失真）
    ramp = np.linspace(0, 120, width, dtype=np.uint8)
    noise[:, :, 0] = np.clip(noise[:, :, 0].astype(int) + ramp, 0, 255)
    return np.ascontiguousarray(noise)


def timeit(label: str, func, repeat: int = 5) -> float:
    """跑 repeat 次取中位数，返回毫秒。"""
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        func()
        samples.append((time.perf_counter() - start) * 1000.0)
    median = statistics.median(samples)
    print(f"  {label:<44} {median:8.2f} ms")
    return median


def current_pixel_hash(image: np.ndarray) -> str:
    """现状：.tobytes() 复制整图。"""
    array = np.ascontiguousarray(image)
    digest = hashlib.blake2b(array.tobytes(), digest_size=16)
    digest.update(str(array.shape).encode("ascii"))
    return digest.hexdigest()


def zero_copy_pixel_hash(image: np.ndarray) -> str:
    """候选：直接把缓冲区交给 blake2b，不做 tobytes 复制。"""
    array = np.ascontiguousarray(image)
    digest = hashlib.blake2b(memoryview(array).cast("B"), digest_size=16)
    digest.update(str(array.shape).encode("ascii"))
    return digest.hexdigest()


def main() -> int:
    size_arg = next((a for a in sys.argv[1:] if "x" in a and a[0].isdigit()), None)
    real = Path(__file__).resolve().parent.parent / "docs" / "screenshot.png"

    if size_arg:
        width, height = (int(v) for v in size_arg.lower().split("x"))
        print(f"来源：合成噪声图 {width}×{height}")
        image = make_image(width, height)
    elif real.exists():
        loaded = cv2.imdecode(np.fromfile(str(real), dtype=np.uint8), cv2.IMREAD_COLOR)
        if loaded is None:
            print("真实截图解码失败，回退到合成图")
            image = make_image(2560, 1440)
        else:
            image = np.ascontiguousarray(loaded)
            print(f"来源：真实截图 {real.name} {image.shape[1]}×{image.shape[0]}")
    else:
        width, height = 2560, 1440
        print(f"来源：合成噪声图 {width}×{height}")
        image = make_image(width, height)

    height, width = image.shape[:2]
    print(f"尺寸 {width}×{height}（{width * height * 3 / 1048576:.1f} MB）\n")

    print("— 指纹与质量 —")
    timeit("pixel_hash 现状（tobytes 复制）", lambda: current_pixel_hash(image))
    timeit("pixel_hash 候选（memoryview 零拷贝）",
           lambda: zero_copy_pixel_hash(image))
    timeit("measure() 全套", lambda: measure(image))

    print("\n— measure 内部拆分 —")
    from echo.quality import _to_gray_resized, ahash, dhash, phash

    timeit("cvtColor 全尺寸（measure 里做了 4 次）",
           lambda: cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    timeit("_to_gray_resized(320) 含 cvtColor",
           lambda: _to_gray_resized(image))
    timeit("dhash(image) 单算", lambda: dhash(image))
    timeit("ahash(image) 单算", lambda: ahash(image))
    timeit("phash(image) 单算", lambda: phash(image))
    gray320 = _to_gray_resized(image)
    timeit("Laplacian(320).var()", lambda: cv2.Laplacian(gray320, cv2.CV_32F).var())

    print("\n— PNG 编码 —")
    results: dict[int, tuple[float, float]] = {}
    for level in (0, 1, 3, 6):
        def enc(level=level):
            ok, buf = cv2.imencode(
                ".png", image, [cv2.IMWRITE_PNG_COMPRESSION, level]
            )
            assert ok
            return buf
        ms = timeit(f"encode_png 压缩级别 {level}", enc)
        results[level] = (ms, len(enc().tobytes()) / 1024)

    base_ms, base_kb = results[3]
    print()
    for level, (ms, kb) in results.items():
        print(f"    级别 {level}: {ms:7.2f} ms  {kb:9.1f} KB  "
              f"速度 {base_ms / ms:5.2f}×  体积 {kb / base_kb:5.2f}×"
              f"{'   ← 当前默认' if level == 3 else ''}")

    print("\n— 预览缩放（长边缩到 640）—")
    scale = 640 / max(width, height)
    target = (max(1, int(width * scale)), max(1, int(height * scale)))
    timeit("cv2.resize INTER_AREA",
           lambda: cv2.resize(image, target, interpolation=cv2.INTER_AREA))
    timeit("cv2.resize INTER_LINEAR",
           lambda: cv2.resize(image, target, interpolation=cv2.INTER_LINEAR))
    timeit("cv2.resize INTER_NEAREST",
           lambda: cv2.resize(image, target, interpolation=cv2.INTER_NEAREST))

    print("\n— 灰度转换（measure 内部）—")
    timeit("cvtColor BGR2GRAY 全尺寸",
           lambda: cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))

    print("\n— QImage 构造（预览跨线程那一跳）—")
    try:
        from echo.ui.imaging import to_qimage
        timeit("to_qimage 预览图",
               lambda: to_qimage(cv2.resize(image, target,
                                            interpolation=cv2.INTER_AREA)))
    except Exception as exc:
        print(f"  跳过（{exc}）")

    print("\n— 结论参考 —")
    print("  定时采集时每保存一张，串行付出：pixel_hash + measure + encode_png")
    return 0


_REF: dict[int, float] = {}

if __name__ == "__main__":
    raise SystemExit(main())
