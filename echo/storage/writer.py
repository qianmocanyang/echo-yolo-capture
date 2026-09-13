"""保存目录布局与原子写盘。

方案 §5.2 明确要求「保存采用临时文件编码、成功后同目录重命名、再提交数据库记录」。
这个顺序保证了任何时刻崩溃都不会产生"数据库说有这张图、但文件是半个"的状态。

中文与空格路径是硬需求（方案 §5.1），所以这里刻意 **不用** `cv2.imwrite`：
OpenCV 在 Windows 上对非 ASCII 路径会静默失败，只返回一个 False。
统一走 `cv2.imencode` + `Path.write_bytes`，由 Python 负责文件名为 Unicode 的写入。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..logging_setup import get_logger
from .naming import TMP_SUFFIX

log = get_logger("storage.writer")

# PNG 压缩级别。0 最快/最大，9 最慢/最小。3 是截图的合理折中：
# 320×320 游戏画面约 60～150 KB，编码耗时 1 ms 量级，不会拖累连拍。
PNG_COMPRESSION = 3

# 低于这个剩余空间就停止自动保存并提示（方案 §5.1）。
MIN_FREE_BYTES = 200 * 1024 * 1024


class SaveError(RuntimeError):
    """保存失败。上层据此进入「保存异常」状态并停止继续堆积任务。"""

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    """保存目录的固定结构（方案 §5.2）。"""

    root: Path

    @property
    def images_dir(self) -> Path:
        return self.root / "images"

    @property
    def metadata_dir(self) -> Path:
        return self.root / "metadata"

    @property
    def labels_dir(self) -> Path:
        return self.root / "labels"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def manifest_path(self) -> Path:
        return self.metadata_dir / "manifest.jsonl"

    @property
    def db_path(self) -> Path:
        return self.metadata_dir / "dataset.db"

    def session_dir(self, session_uid: str) -> Path:
        return self.images_dir / session_uid

    def session_rel(self, session_uid: str, filename: str) -> str:
        """写进数据库的相对路径，一律用正斜杠，跨平台可移植。"""
        return f"images/{session_uid}/{filename}"

    def absolute(self, rel_path: str) -> Path:
        return self.root / Path(rel_path.replace("\\", "/"))

    def ensure(self) -> "ProjectLayout":
        for directory in (self.images_dir, self.metadata_dir, self.labels_dir, self.exports_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def check_directory_writable(path: Path) -> tuple[bool, str]:
    """选择目录后执行一次小文件写入与删除检查（方案 §5.1）。

    只写一个随机名的小文件，不碰目录里已有的任何东西。
    """
    try:
        path = Path(path)
    except (TypeError, ValueError) as exc:
        return False, f"路径无效：{exc}"

    if not path.exists():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"目录不存在且无法创建：{exc.strerror or exc}"
    if not path.is_dir():
        return False, "该路径不是文件夹"

    probe: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".echo_write_test_", dir=str(path))
        probe = Path(name)
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"echo")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        return False, f"目录不可写：{exc.strerror or exc}"
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass

    return True, ""


def free_space_bytes(path: Path) -> int:
    try:
        return int(shutil.disk_usage(str(path)).free)
    except OSError:
        return -1


def check_space(path: Path) -> tuple[bool, str]:
    free = free_space_bytes(path)
    if free < 0:
        return True, ""  # 读不到就不拦，交给真正的写入失败去报错
    if free < MIN_FREE_BYTES:
        return False, f"磁盘剩余空间不足（剩 {free / 1024 / 1024:.0f} MB）"
    return True, ""


def encode_png(image: np.ndarray) -> bytes:
    """把 BGR/BGRA 图像编码为 PNG 字节。**不做任何缩放或色彩变换**。

    方案 §3.1「原图保存默认保持裁剪后的真实尺寸，不做插值缩放」——
    这里唯一的操作是丢弃无意义的 alpha 通道。
    """
    if image is None or image.size == 0:
        raise SaveError("图像为空")
    if image.dtype != np.uint8:
        image = np.clip(image, 0, 255).astype(np.uint8)
    if image.ndim == 3 and image.shape[2] == 4:
        image = np.ascontiguousarray(image[:, :, :3])
    elif image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

    ok, buffer = cv2.imencode(
        ".png", image, [cv2.IMWRITE_PNG_COMPRESSION, PNG_COMPRESSION]
    )
    if not ok:
        raise SaveError("PNG 编码失败")
    return buffer.tobytes()


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """临时文件 → 同目录重命名。禁止覆盖已有文件（方案 §5.2）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SaveError(f"目标文件已存在，拒绝覆盖：{path.name}")

    tmp_path = path.parent / (path.name + TMP_SUFFIX)
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():  # 极小概率的并发写
            raise SaveError(f"目标文件已存在，拒绝覆盖：{path.name}")
        os.replace(tmp_path, path)
    except SaveError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SaveError(f"写入失败：{exc.strerror or exc}", fatal=True) from exc


def save_png(image: np.ndarray, path: Path) -> int:
    """编码并原子落盘，返回写入字节数。"""
    payload = encode_png(image)
    atomic_write_bytes(path, payload)
    return len(payload)


def read_image(path: Path) -> np.ndarray | None:
    """读取图片，兼容中文路径与空格路径。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError as exc:
        log.debug("读取 %s 失败：%s", path, exc)
        return None
    if data.size == 0:
        return None
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return image


def read_image_size(path: Path) -> tuple[int, int] | None:
    """只读尺寸，用于导出前校验（不整图解码，省内存也快得多）。

    PNG 的 IHDR 里就有宽高，直接读 24 字节即可；其他格式退回到整图解码。
    """
    import struct

    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None

    head = data[:24].tobytes()
    if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        try:
            width, height = struct.unpack(">II", head[16:24])
            return int(width), int(height)
        except struct.error:  # pragma: no cover
            return None

    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        return None
    return int(image.shape[1]), int(image.shape[0])
