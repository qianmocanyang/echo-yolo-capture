"""YOLO 标签的解析、校验与坐标换算。

方案 §10.2 / §10.3 的格式与约束在这里落地：

```
class_id x_center y_center width height     # 均为相对该图片的归一化值
```

关于裁剪图最重要的一条（§10.2 末段）：**标注必须以裁剪图为基准**。
如果手上的标注是全图坐标（比如别人标的是整屏 1920×1080），
必须按这个顺序转换，缺一步就会得到错位的框：

1. 减去裁剪偏移 ``(crop_x, crop_y)``；
2. 截断到 ``[0, crop_w] × [0, crop_h]``；
3. 处理被裁掉的目标——只露一小角的框不该当作有效正样本，默认丢弃；
4. 再用**裁剪后的**宽高做归一化。

第 3 步容易被忽略。一个只在边缘露了 3% 面积的框，
归一化之后坐标依然"合法"，但它对训练是噪声而不是信号。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

# 只露出一小角的目标默认丢弃。0.30 表示至少要有 30% 的原始面积落在裁剪区内。
MIN_VISIBLE_RATIO = 0.30

# 归一化后允许的极小越界，抵消浮点误差。
COORD_EPSILON = 1e-6

# 导出时的坐标精度。6 位小数对 4K 图也足够（1/1000000 ≈ 0.004 像素）。
COORD_DECIMALS = 6


@dataclass(slots=True)
class YoloBox:
    """一个归一化的 YOLO 目标框。"""

    class_id: int
    x_center: float
    y_center: float
    width: float
    height: float

    # ---- 派生 -----------------------------------------------------------
    @property
    def x1(self) -> float:
        return self.x_center - self.width / 2.0

    @property
    def y1(self) -> float:
        return self.y_center - self.height / 2.0

    @property
    def x2(self) -> float:
        return self.x_center + self.width / 2.0

    @property
    def y2(self) -> float:
        return self.y_center + self.height / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def to_line(self) -> str:
        return (
            f"{self.class_id} "
            f"{self.x_center:.{COORD_DECIMALS}f} "
            f"{self.y_center:.{COORD_DECIMALS}f} "
            f"{self.width:.{COORD_DECIMALS}f} "
            f"{self.height:.{COORD_DECIMALS}f}"
        )

    def to_pixels(self, image_w: int, image_h: int) -> tuple[float, float, float, float]:
        """转回像素坐标 (x1, y1, x2, y2)。"""
        return (
            self.x1 * image_w,
            self.y1 * image_h,
            self.x2 * image_w,
            self.y2 * image_h,
        )

    @staticmethod
    def from_pixels(
        class_id: int,
        x1: float, y1: float, x2: float, y2: float,
        image_w: int, image_h: int,
    ) -> "YoloBox":
        """像素坐标 (x1,y1,x2,y2) → 归一化框。"""
        left, right = sorted((float(x1), float(x2)))
        top, bottom = sorted((float(y1), float(y2)))
        width = right - left
        height = bottom - top
        return YoloBox(
            class_id=int(class_id),
            x_center=(left + right) / 2.0 / image_w,
            y_center=(top + bottom) / 2.0 / image_h,
            width=width / image_w,
            height=height / image_h,
        )


@dataclass(slots=True)
class LabelProblem:
    """一条校验问题。``fatal`` 为真表示该行不可用，必须丢弃。"""

    line_number: int
    raw: str
    message: str
    fatal: bool = True


@dataclass(slots=True)
class LabelFile:
    """一个标签文件的解析结果。"""

    boxes: list[YoloBox] = field(default_factory=list)
    problems: list[LabelProblem] = field(default_factory=list)
    empty: bool = False

    @property
    def ok(self) -> bool:
        return not any(p.fatal for p in self.problems)


def parse_label_line(raw: str, num_classes: int) -> tuple[YoloBox | None, str]:
    """解析一行标签。返回 ``(框 或 None, 错误说明)``。"""
    parts = raw.split()
    if not parts:
        return None, "空行"
    if len(parts) != 5:
        return None, f"应包含 5 个字段，实际 {len(parts)} 个"

    try:
        class_id = int(float(parts[0]))
    except ValueError:
        return None, f"类别编号不是数字：{parts[0]}"

    if class_id < 0:
        return None, f"类别编号为负：{class_id}"
    if num_classes > 0 and class_id >= num_classes:
        return None, f"类别编号 {class_id} 超出类别表范围（共 {num_classes} 类）"

    try:
        values = [float(v) for v in parts[1:]]
    except ValueError:
        return None, "坐标不是合法的数字"

    if not all(math.isfinite(v) for v in values):
        return None, "坐标包含 NaN 或无穷大"

    x_center, y_center, width, height = values

    if width <= 0 or height <= 0:
        return None, f"框的宽或高为 0（{width}, {height}）"

    # 越界：YOLO 允许框略微超出画面（目标被裁切），但中心点必须在图内，
    # 且不能整框跑到图外。这里按"中心在图内 + 面积有交集"来判断。
    if not (0.0 <= x_center <= 1.0 and 0.0 <= y_center <= 1.0):
        return None, f"框中心 ({x_center:.4f}, {y_center:.4f}) 落在画面之外"

    if width > 1.0 + COORD_EPSILON or height > 1.0 + COORD_EPSILON:
        return None, f"归一化宽高超过 1（{width:.4f}, {height:.4f}）"

    box = YoloBox(
        class_id=class_id,
        x_center=x_center,
        y_center=y_center,
        width=width,
        height=height,
    )
    if box.area <= COORD_EPSILON:
        return None, "框面积近似为 0"
    return box, ""


def parse_label_text(text: str, num_classes: int) -> LabelFile:
    result = LabelFile()
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        box, message = parse_label_line(stripped, num_classes)
        if box is None:
            result.problems.append(
                LabelProblem(line_number=number, raw=stripped, message=message)
            )
            continue
        result.boxes.append(box)
    result.empty = not result.boxes
    return result


def read_label_file(path: Path, num_classes: int) -> LabelFile:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result = LabelFile()
        result.problems.append(
            LabelProblem(line_number=0, raw="", message=f"无法读取标签文件：{exc}")
        )
        return result
    return parse_label_text(text, num_classes)


def write_label_file(path: Path, boxes: list[YoloBox]) -> None:
    """写标签文件。空列表会写一个 0 字节文件——这是合法的"背景样本"标签。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(box.to_line() for box in boxes)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# 全图标注 → 裁剪图标注（方案 §10.2 末段）
# --------------------------------------------------------------------------

@dataclass(slots=True)
class ConvertedBox:
    box: YoloBox | None
    dropped_reason: str = ""


def convert_full_frame_box(
    class_id: int,
    x1: float, y1: float, x2: float, y2: float,
    *,
    crop_x: int, crop_y: int, crop_w: int, crop_h: int,
    full_w: int, full_h: int,
    min_visible_ratio: float = MIN_VISIBLE_RATIO,
) -> ConvertedBox:
    """把全图坐标的框转换成裁剪图坐标。

    严格按方案 §10.2 的四步：减偏移 → 截断 → 处理裁掉的目标 → 用裁剪尺寸归一化。
    """
    if full_w <= 0 or full_h <= 0 or crop_w <= 0 or crop_h <= 0:
        return ConvertedBox(None, "图片或裁剪尺寸无效")

    left, right = sorted((float(x1), float(x2)))
    top, bottom = sorted((float(y1), float(y2)))
    original_area = max(0.0, right - left) * max(0.0, bottom - top)
    if original_area <= 0:
        return ConvertedBox(None, "原始框面积为 0")

    # 1. 减偏移
    left -= crop_x
    right -= crop_x
    top -= crop_y
    bottom -= crop_y

    # 2. 截断到裁剪区
    clipped_left = max(0.0, left)
    clipped_top = max(0.0, top)
    clipped_right = min(float(crop_w), right)
    clipped_bottom = min(float(crop_h), bottom)

    clipped_w = clipped_right - clipped_left
    clipped_h = clipped_bottom - clipped_top
    if clipped_w <= 0 or clipped_h <= 0:
        return ConvertedBox(None, "目标完全落在裁剪范围之外")

    # 3. 处理被裁掉的目标
    kept_area = clipped_w * clipped_h
    if kept_area / original_area < min_visible_ratio:
        return ConvertedBox(
            None,
            f"目标仅有 {kept_area / original_area * 100:.0f}% 面积落在裁剪范围内，已丢弃",
        )

    # 4. 用裁剪后的宽高归一化
    box = YoloBox.from_pixels(
        class_id, clipped_left, clipped_top, clipped_right, clipped_bottom,
        crop_w, crop_h,
    )
    return ConvertedBox(box)


def validate_boxes(boxes: list[YoloBox], num_classes: int) -> list[str]:
    """对一组框做导出前校验，返回问题描述列表。"""
    problems: list[str] = []
    for index, box in enumerate(boxes, 1):
        if box.class_id < 0 or (num_classes > 0 and box.class_id >= num_classes):
            problems.append(f"第 {index} 个框：类别编号 {box.class_id} 越界")
        values = (box.x_center, box.y_center, box.width, box.height)
        if not all(math.isfinite(v) for v in values):
            problems.append(f"第 {index} 个框：坐标包含非有限数值")
        if box.width <= 0 or box.height <= 0:
            problems.append(f"第 {index} 个框：宽或高为 0")
        if box.x_center < -COORD_EPSILON or box.x_center > 1 + COORD_EPSILON:
            problems.append(f"第 {index} 个框：中心 X 越界（{box.x_center:.6f}）")
        if box.y_center < -COORD_EPSILON or box.y_center > 1 + COORD_EPSILON:
            problems.append(f"第 {index} 个框：中心 Y 越界（{box.y_center:.6f}）")
        if box.width > 1 + COORD_EPSILON or box.height > 1 + COORD_EPSILON:
            problems.append(f"第 {index} 个框：归一化宽高大于 1")
    return problems


def classes_from_names_file(path: Path) -> list[str]:
    """读取 `obj.names` / `classes.txt` 这类纯文本类别表。"""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip()]


def boxes_from_xywh_yolo(text: str, num_classes: int) -> list[YoloBox]:
    return parse_label_text(text, num_classes).boxes
