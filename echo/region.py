"""采集范围（选区）计算。

坐标系约定 —— 这是整个工具最容易出错的地方，所以只在这里定义一次：

* **物理像素**：Win32、DXGI、WGC 以及最终保存的 PNG 都使用物理像素。
  Region 的 x / y / width / height 全部是物理像素，且 **相对于采集源左上角**。
* **采集源（source）**：
  - 显示器模式，采集源 = 该显示器的物理矩形，源尺寸 = 显示器分辨率。
  - 窗口模式，采集源 = 该窗口的 **客户区**，源尺寸 = 客户区宽高。
    这与方案 §1.2「选区限制在单一窗口内部」和 §3.2「以选定游戏客户区中心为中心」一致。
* **逻辑像素**：只出现在 Qt 界面里。界面显示的坐标是物理像素，但控件尺寸是逻辑像素，
  两者通过 :mod:`echo.dpi` 换算，绝不在本模块里混用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

# 方案 §3.1：尺寸为正整数，且不能超出采集源。下限取 16 是为了避免误输入 0 或极小值
# 产生无法训练也看不出问题的图；上限只是防御性约束。
MIN_SIDE = 16
MAX_SIDE = 16384

# 方案 §3.1 的尺寸预设，最后一项「完整客户区」在界面上是动态值，此处用 None 占位。
SIZE_PRESETS: tuple[tuple[str, int | None, int | None], ...] = (
    ("320 × 320", 320, 320),
    ("416 × 416", 416, 416),
    ("640 × 640", 640, 640),
    ("1280 × 720", 1280, 720),
    ("完整客户区", None, None),
)


class RegionMode(str, Enum):
    """方案 §3.2 的三种定位方式。"""

    CENTER = "center"   # 居中裁剪
    DRAG = "drag"       # 拖拽定位
    MANUAL = "manual"   # 精确坐标

    @property
    def label(self) -> str:
        return {
            RegionMode.CENTER: "居中裁剪",
            RegionMode.DRAG: "拖拽定位",
            RegionMode.MANUAL: "精确坐标",
        }[self]


@dataclass(frozen=True, slots=True)
class Region:
    """物理像素下的矩形，原点为采集源左上角。"""

    x: int
    y: int
    width: int
    height: int

    # ---- 派生属性 -------------------------------------------------------
    @property
    def left(self) -> int:
        return self.x

    @property
    def top(self) -> int:
        return self.y

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def crop_window(self) -> tuple[int, int, int, int]:
        """返回 (left, top, right, bottom)，right / bottom 为**开区间**。

        与 numpy 切片和 OpenCV 的 Rect 语义保持一致，避免差一像素。
        """
        return (self.left, self.top, self.right, self.bottom)

    def to_list(self) -> list[int]:
        return [self.x, self.y, self.width, self.height]

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @staticmethod
    def from_dict(data: dict) -> "Region":
        return Region(
            x=int(data.get("x", 0)),
            y=int(data.get("y", 0)),
            width=int(data.get("width", 320)),
            height=int(data.get("height", 320)),
        )

    # ---- 变换 -----------------------------------------------------------
    def moved_to(self, x: int, y: int) -> "Region":
        return Region(int(x), int(y), self.width, self.height)

    def moved_by(self, dx: int, dy: int) -> "Region":
        return Region(self.x + int(dx), self.y + int(dy), self.width, self.height)

    def resized(self, width: int, height: int) -> "Region":
        return Region(self.x, self.y, int(width), int(height))

    def clamp(self, source_width: int, source_height: int) -> "Region":
        """把选区夹回采集源内部，**保持尺寸不变**（方案 §3.3）。

        尺寸本身大于采集源时无法保持，此时退化为「贴左上角、尺寸等于采集源」，
        由调用方负责在此之前先用 :func:`validate_size` 阻止这种情况。
        """
        width = min(self.width, max(MIN_SIDE, source_width))
        height = min(self.height, max(MIN_SIDE, source_height))
        x = min(max(0, self.x), max(0, source_width - width))
        y = min(max(0, self.y), max(0, source_height - height))
        return Region(x, y, width, height)

    def is_inside(self, source_width: int, source_height: int) -> bool:
        return (
            self.x >= 0
            and self.y >= 0
            and self.width > 0
            and self.height > 0
            and self.right <= source_width
            and self.bottom <= source_height
        )

    def fits_within(self, source_width: int, source_height: int) -> bool:
        return self.width <= source_width and self.height <= source_height

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"{self.width}×{self.height} @({self.x},{self.y})"


@dataclass(frozen=True, slots=True)
class SizeValidation:
    """尺寸校验结果。`ok` 为 False 时 `message` 是要展示给用户的原因。"""

    ok: bool
    message: str = ""
    width: int = 0
    height: int = 0

    @property
    def is_degenerate(self) -> bool:
        return not self.ok


def parse_size(raw: str) -> int | None:
    """把输入框文本解析为正整数；解析失败返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # 容忍全角数字与多余空格，避免中文输入法下的挫败感。
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if not text.isdigit():
        return None
    return int(text)


def validate_size(width: int, height: int, source_width: int, source_height: int) -> SizeValidation:
    """校验宽高是否可以应用到给定的采集源。

    方案 §3.3：输入尺寸大于采集源时阻止应用并显示可用最大宽高；
    不黑色填充、不静默缩小。
    """
    if width is None or height is None:
        return SizeValidation(False, "请填写宽度和高度")
    if width <= 0 or height <= 0:
        return SizeValidation(False, "宽度和高度必须是正整数")
    if width < MIN_SIDE or height < MIN_SIDE:
        return SizeValidation(False, f"宽度和高度不能小于 {MIN_SIDE} px")
    if width > MAX_SIDE or height > MAX_SIDE:
        return SizeValidation(False, f"宽度和高度不能大于 {MAX_SIDE} px")
    if source_width <= 0 or source_height <= 0:
        return SizeValidation(False, "尚未选择采集源")
    if width > source_width or height > source_height:
        return SizeValidation(
            False,
            f"超出采集源范围，可用最大为 {source_width} × {source_height}",
        )
    return SizeValidation(True, "", width, height)


def center_region(source_width: int, source_height: int, width: int, height: int) -> Region:
    """居中裁剪（方案 §3.2）。

        left = floor((source_width  - crop_width ) / 2)
        top  = floor((source_height - crop_height) / 2)

    1920×1080 源、320×320 选区 → (800, 380)。
    """
    left = math.floor((source_width - width) / 2)
    top = math.floor((source_height - height) / 2)
    return Region(max(0, left), max(0, top), width, height)


def fit_region_to_source(source_width: int, source_height: int, region: Region) -> Region:
    """把选区收回采集源范围内，换源/恢复源后调用。

    只居中不收缩会留下一个永久越界的选区：选区尺寸本身大于采集源时
    validate_size 一直拦截，采集永远无法开始。这里先把宽高收缩到源内
    （不低于 MIN_SIDE），再按新源重新居中；本就在源内的选区原样返回。
    """
    if region.is_inside(source_width, source_height):
        return region
    width = max(MIN_SIDE, min(region.width, max(MIN_SIDE, source_width)))
    height = max(MIN_SIDE, min(region.height, max(MIN_SIDE, source_height)))
    return center_region(source_width, source_height, width, height)


def apply_ratio_lock(
    width: int,
    height: int,
    changed: str,
    source_width: int,
    source_height: int,
    *,
    ref_width: int = 0,
    ref_height: int = 0,
) -> tuple[int, int]:
    """比例锁定：修改一边时按原比例同步另一边（方案 §3.1）。

    比例必须取自**当前已生效的选区**（``ref_width`` / ``ref_height``），
    而不是调用方传入的 ``width`` / ``height``——后者里有一维是用户刚输进来的新值，
    拿它算比例会得到「另一边原地不动」的假锁定。
    未提供 ref 时退化为按传入值算比例（仅适用于两边都还没改的场景）。

    结果四舍五入并夹到采集源范围内；比例导致另一维超出时以能放下的那一维为准。
    """
    if ref_width > 0 and ref_height > 0:
        ratio = ref_width / ref_height
    elif width > 0 and height > 0:
        ratio = width / height
    else:
        return width, height

    if changed == "width":
        new_w = width
        new_h = int(round(width / ratio)) if ratio else height
    else:
        new_h = height
        new_w = int(round(height * ratio)) if ratio else width

    new_w = max(MIN_SIDE, min(new_w, MAX_SIDE))
    new_h = max(MIN_SIDE, min(new_h, MAX_SIDE))
    if source_width > 0 and new_w > source_width:
        scale = source_width / new_w
        new_w = source_width
        new_h = max(MIN_SIDE, int(round(new_h * scale)))
    if source_height > 0 and new_h > source_height:
        scale = source_height / new_h
        new_h = source_height
        new_w = max(MIN_SIDE, int(round(new_w * scale)))
    return new_w, new_h


def region_from_fields(x: int, y: int, width: int, height: int,
                       source_width: int, source_height: int) -> Region:
    """精确坐标模式：校验并夹取到采集源内部。"""
    return Region(int(x), int(y), int(width), int(height)).clamp(source_width, source_height)


def describe_region(region: Region, source_width: int, source_height: int) -> str:
    """给状态栏用的一行描述。"""
    return (
        f"{region.width} × {region.height} px  偏移 ({region.x}, {region.y})  "
        f"源 {source_width} × {source_height}"
    )
