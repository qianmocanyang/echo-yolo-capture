"""图片质量判定与去重指纹。

方案 §10.1 的边界必须守住：

* **完全重复** 用像素哈希判断，可靠，可以用于自动采集时的过滤。
* **相似图** 只用感知哈希"标记"，首版绝不凭相似度直接删除图片——
  因为远处小目标的细微变化在感知哈希上几乎等价于静止画面，删了就再也补不回来。
* 黑屏 / 模糊 / 低亮度也只是**打标记**，留给人工审核。
  夜间场景、运动模糊是合法样本，不能一刀切过滤掉。

阈值一律以"归一化到 320×320 之后"的画面为准，这样不同选区尺寸下
同一个阈值含义一致；否则 1280×720 的图永远比 320×320 的图"更清晰"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import cv2
import numpy as np

# 质量指标的计算基准边长。见模块文档：为了让阈值跨尺寸可比。
ANALYSIS_SIDE = 320

# 阈值（在 ANALYSIS_SIDE 归一化之后）
BLACK_MEAN_LUMA = 8.0
BLACK_STD_LUMA = 4.0
DARK_MEAN_LUMA = 25.0
BLUR_LAPLACIAN_VAR = 12.0

# 实时黑屏检测的采样阈值（配合 quick_luma 使用）。
# 比上面的 BLACK_MEAN_LUMA 严得多：那个是"事后标记疑似黑屏"，这个是"当场判定
# 这帧根本没有内容"。宁可漏判，也不能误伤夜战、洞穴这类正常的暗场景。
BLANK_LUMA = 3.0

FLAG_BLACK = "black"
FLAG_DARK = "dark"
FLAG_BLUR = "blur"

FLAG_LABELS = {
    FLAG_BLACK: "疑似黑屏",
    FLAG_DARK: "低亮度",
    FLAG_BLUR: "疑似模糊",
}


@dataclass(slots=True)
class QualityReport:
    image_hash: str = ""          # 像素级完全重复判断用
    dhash: str = ""               # 差异哈希，16 位十六进制（64 bit）
    ahash: str = ""               # 均值哈希
    phash: str = ""               # DCT 感知哈希
    mean_luma: float = 0.0
    std_luma: float = 0.0
    sharpness: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def flags_csv(self) -> str:
        return ",".join(self.flags)

    def flag_text(self) -> str:
        return "、".join(FLAG_LABELS.get(f, f) for f in self.flags)


def to_gray(image: np.ndarray) -> np.ndarray:
    """转灰度，不缩放。全尺寸的灰度化只该做一次，供下面所有指标复用。"""
    if image is None or image.size == 0:
        return image
    if image.ndim == 3:
        return cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2GRAY)
    return image


def resize_gray(gray: np.ndarray, side: int) -> np.ndarray:
    """把灰度图归一化到 side×side。

    ``INTER_AREA`` 在大比例缩小时要遍历全部源像素，而这正是各指标的成本所在：
    从 1418×997 缩到 9×9 要 ~5 ms，从已经缩好的 320×320 再缩只要 ~0.35 ms。
    """
    height, width = gray.shape[:2]
    if height == 0 or width == 0:
        return np.zeros((side, side), dtype=np.uint8)
    if height != side or width != side:
        interpolation = cv2.INTER_AREA if (height > side or width > side) else cv2.INTER_LINEAR
        gray = cv2.resize(gray, (side, side), interpolation=interpolation)
    return np.ascontiguousarray(gray)


def _to_gray_resized(image: np.ndarray, side: int = ANALYSIS_SIDE) -> np.ndarray:
    """转灰度并归一化尺寸。所有指标都在这个规范化画面上计算。"""
    return resize_gray(to_gray(image), side)


def quick_luma(image: np.ndarray) -> float:
    """极轻量的亮度采样，专供实时黑屏检测用（约 0.02 ms）。

    隔 8 行 8 列抽样，不做灰度转换、不做完整均值——采到 2560×1440 上也只有
    约 22000 个像素参与运算。它不需要精确，只需要判断"是不是全黑"。

    只取前三个通道：``Frame.image`` 按契约是 BGR，但带 alpha 的输入会让
    alpha 的 255 把全黑图抬到均值 64，恰好绕过黑屏判定。
    """
    if image is None or image.size == 0:
        return -1.0
    sample = image[::8, ::8]
    if sample.ndim == 3:
        if sample.shape[2] > 3:
            sample = sample[:, :, :3]
        # BGR 的整数均值与灰度均值在本用途下等价，省掉一次 cvtColor
        return float(sample.mean())
    return float(sample.mean())


def _ahash_from_gray(gray: np.ndarray) -> np.uint64:
    small = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
    return _bits_to_uint64((small > float(small.mean())).flatten())


def _dhash_from_gray(gray: np.ndarray) -> np.uint64:
    small = resize_gray(gray, 9)
    small = cv2.resize(small, (9, 8), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:].astype(np.int16) - small[:, :-1].astype(np.int16)
    return _bits_to_uint64((diff > 0).flatten())


def _phash_from_gray(gray: np.ndarray) -> np.uint64:
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(small)
    low = dct[:8, :8]
    low_flat = low.flatten()
    median = float(np.median(low_flat[1:])) if low_flat.size > 1 else float(low_flat.mean())
    bits = (low_flat > median)
    bits[0] = False
    return _bits_to_uint64(bits)


def pixel_hash(image: np.ndarray) -> str:
    """像素级哈希：对**原始裁剪后**的字节做 BLAKE2b。

    刻意不对归一化后的画面取哈希——那会把两张不同尺寸、缩放后相同的图判成重复，
    而它们其实是不同的采集样本。

    直接把缓冲区交给 blake2b，不走 ``.tobytes()``：后者要先复制一份整图
    （2560×1440 就是 10.5 MB），纯粹白花一次内存带宽。
    """
    if image is None:
        return ""
    array = np.ascontiguousarray(image)
    digest = hashlib.blake2b(memoryview(array).cast("B"), digest_size=16)
    # 把尺寸也混进去，避免 shape 不同的数组字节恰好相同
    digest.update(str(array.shape).encode("ascii"))
    return digest.hexdigest()


def ahash(image: np.ndarray) -> np.uint64:
    """均值哈希。单张调用用它；一批指标请走 ``measure()``，那里只灰度化一次。"""
    return _ahash_from_gray(to_gray(image))


def dhash(image: np.ndarray) -> np.uint64:
    """差异哈希：9×8 灰度，逐行比较相邻像素。对光照变化比 ahash 稳健。"""
    return _dhash_from_gray(to_gray(image))


def phash(image: np.ndarray) -> np.uint64:
    """DCT 感知哈希：32×32 → DCT → 取左上 8×8 低频 → 与中位数比较。"""
    return _phash_from_gray(to_gray(image))


def _bits_to_uint64(bits: np.ndarray) -> np.uint64:
    value = 0
    for bit in bits[:64]:
        value = (value << 1) | int(bool(bit))
    return np.uint64(value)


def to_hex(value: np.uint64) -> str:
    return f"{int(value):016x}"


def from_hex(text: str) -> np.uint64 | None:
    if not text:
        return None
    try:
        return np.uint64(int(text, 16))
    except (ValueError, TypeError):
        return None


def hamming_hex(a: str, b: str) -> int:
    """两个十六进制哈希的汉明距离；任一侧无效时返回一个很大的值。"""
    left, right = from_hex(a), from_hex(b)
    if left is None or right is None:
        return 64
    return bin(int(left) ^ int(right)).count("1")


def measure(image: np.ndarray) -> QualityReport:
    """一次性算出全部指纹与质量指标。

    **全尺寸的灰度化与降采样只做一次**，三个哈希都从这张归一化图（320×320）
    派生。这一点很关键：改之前每个哈希各自把彩色原图重新灰度化、再从全尺寸
    直接降到 8/9/32，等于同一份活干了四遍——1418×997 上实测 26 ms，其中约
    20 ms 是重复劳动。

    代价是 ahash/dhash/phash 变成"两步降采样"，数值与旧版可能有 ±1 bit 差异。
    这三个哈希只用于相似图标记（阈值比较），1 bit 的抖动远小于阈值；精确去重
    走的是 :func:`pixel_hash`，逐字节比对，不受影响。
    """
    report = QualityReport()
    if image is None or image.size == 0:
        return report

    report.image_hash = pixel_hash(image)

    gray = _to_gray_resized(image)
    report.mean_luma = float(gray.mean())
    report.std_luma = float(gray.std())

    # 拉普拉斯方差：经典的清晰度指标。归一化尺寸后阈值才有统一含义。
    laplacian = cv2.Laplacian(gray, cv2.CV_32F)
    report.sharpness = float(laplacian.var())

    report.dhash = to_hex(_dhash_from_gray(gray))
    report.ahash = to_hex(_ahash_from_gray(gray))
    report.phash = to_hex(_phash_from_gray(gray))

    flags: list[str] = []
    if report.mean_luma < BLACK_MEAN_LUMA and report.std_luma < BLACK_STD_LUMA:
        flags.append(FLAG_BLACK)
    elif report.mean_luma < DARK_MEAN_LUMA:
        flags.append(FLAG_DARK)
    if report.sharpness < BLUR_LAPLACIAN_VAR:
        flags.append(FLAG_BLUR)
    report.flags = flags
    return report


# --------------------------------------------------------------------------
# 与既有图片比对
# --------------------------------------------------------------------------

@dataclass(slots=True)
class DedupDecision:
    """去重决策结果。"""

    action: str = "keep"          # keep | skip_exact
    duplicate_of: int | None = None
    similar_to: int | None = None
    distance: int = 64


def decide_duplicate(
    report: QualityReport,
    candidates: list[tuple[int, str]],
) -> int | None:
    """在一批 (image_id, image_hash) 里找完全相同的那张。

    只做精确匹配；相似度不参与"是否保存"的决策。
    """
    for image_id, existing_hash in candidates:
        if existing_hash and existing_hash == report.image_hash:
            return image_id
    return None


def find_similar(
    report: QualityReport,
    candidates: list[tuple[int, str]],
    threshold: int,
) -> tuple[int | None, int]:
    """找最相似的既有图片。返回 (图片ID, 汉明距离)。只用于打标记。"""
    best_id: int | None = None
    best_distance = 65
    for image_id, existing in candidates:
        distance = hamming_hex(report.dhash, existing)
        if distance < best_distance:
            best_distance = distance
            best_id = image_id
    if best_id is not None and best_distance <= threshold:
        return best_id, best_distance
    return None, best_distance
