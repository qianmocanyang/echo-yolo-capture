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


def _to_gray_resized(image: np.ndarray, side: int = ANALYSIS_SIDE) -> np.ndarray:
    """转灰度并归一化尺寸。所有指标都在这个规范化画面上计算。"""
    if image.ndim == 3:
        gray = cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    height, width = gray.shape[:2]
    if height == 0 or width == 0:
        return np.zeros((side, side), dtype=np.uint8)
    if height != side or width != side:
        interpolation = cv2.INTER_AREA if (height > side or width > side) else cv2.INTER_LINEAR
        gray = cv2.resize(gray, (side, side), interpolation=interpolation)
    return np.ascontiguousarray(gray)


def pixel_hash(image: np.ndarray) -> str:
    """像素级哈希：对**原始裁剪后**的字节做 BLAKE2b。

    刻意不对归一化后的画面取哈希——那会把两张不同尺寸、缩放后相同的图判成重复，
    而它们其实是不同的采集样本。
    """
    if image is None:
        return ""
    array = np.ascontiguousarray(image)
    digest = hashlib.blake2b(array.tobytes(), digest_size=16)
    # 把尺寸也混进去，避免 shape 不同的数组字节恰好相同
    digest.update(str(array.shape).encode("ascii"))
    return digest.hexdigest()


def ahash(image: np.ndarray) -> np.uint64:
    gray = _to_gray_resized(image, 8)
    mean = float(gray.mean())
    bits = (gray > mean).flatten()
    return _bits_to_uint64(bits)


def dhash(image: np.ndarray) -> np.uint64:
    """差异哈希：9×8 灰度，逐行比较相邻像素。对光照变化比 ahash 稳健。"""
    gray = _to_gray_resized(image, 9)
    gray = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    diff = gray[:, 1:].astype(np.int16) - gray[:, :-1].astype(np.int16)
    return _bits_to_uint64((diff > 0).flatten())


def phash(image: np.ndarray) -> np.uint64:
    """DCT 感知哈希：32×32 → DCT → 取左上 8×8 低频 → 与中位数比较。"""
    gray = _to_gray_resized(image, 32).astype(np.float32)
    dct = cv2.dct(gray)
    low = dct[:8, :8]
    # 去掉直流分量，否则整体亮度会主导中位数
    low_flat = low.flatten()
    median = float(np.median(low_flat[1:])) if low_flat.size > 1 else float(low_flat.mean())
    bits = (low_flat > median)
    bits[0] = False
    return _bits_to_uint64(bits)


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

    在采集线程里对**裁剪后**的小图执行，320×320 约 0.3～0.8 ms，
    远低于 PNG 编码耗时，所以不会成为连拍的瓶颈。
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

    report.dhash = to_hex(dhash(image))
    report.ahash = to_hex(ahash(image))
    report.phash = to_hex(phash(image))

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
