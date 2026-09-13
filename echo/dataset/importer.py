"""标注导入：把 CVAT / Ultralytics 的 YOLO 标注接回本工具的批次。

方案 §10.2 的第 4 步是「导入或整理标注，校验图片与标签对应关系」。
"校验对应关系"是这里最容易做假的一环，所以本模块的原则是：

* **按文件名单值匹配，绝不按顺序猜**。CVAT 的 YOLO 1.1 格式会把图重命名成
  ``obj_000001.jpg``，此时与原图的对应关系只存在于 ``train.txt`` 里。
  如果连 ``train.txt`` 都指不回原始文件名，本模块**如实报告无法对应**，
  并给出具体该怎么做（导出时保留原始文件名），而不是按顺序硬凑。
* 类别表不一致时**导入但仍显著告警**。按索引映射是安全的，
  但"第 0 类在两边是不同东西"这种事故必须让人看见。
* 导入是**可重复的**：同一个标签文件重复导入只是覆盖，不会产生重复记录。

支持两种目录形态：

```
A. Ultralytics 风格                 B. CVAT YOLO 1.1 风格
   labels/*.txt                        obj.names
   images/*.png                        train.txt
   （或 labels/ 与 obj.names）          obj_*.txt + images/
```
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..logging_setup import get_logger
from ..storage import Database, ProjectLayout, ReviewStatus, read_image_size
from .labels import classes_from_names_file, read_label_file

log = get_logger("dataset.importer")

FMT_ULTRALYTICS = "ultralytics"
FMT_CVAT = "cvat_yolo"
FMT_UNKNOWN = "unknown"

NAMES_FILES = ("obj.names", "classes.txt", "names.txt")
DATA_FILES = ("obj.data", "data.yaml", "dataset.yaml")

_INDEXED_STEM = re.compile(r"^(?P<prefix>.*?)[_-]?(?P<index>\d+)$")


@dataclass(slots=True)
class ImportReport:
    source: Path
    fmt: str = FMT_UNKNOWN
    classes: list[str] = field(default_factory=list)
    class_map_note: str = ""
    n_files: int = 0
    n_boxes: int = 0
    n_matched: int = 0
    n_unmatched: int = 0
    n_rejected: int = 0
    problems: list[str] = field(default_factory=list)
    unmatched_samples: list[str] = field(default_factory=list)
    applied: bool = False

    def summary(self) -> str:
        parts = [
            f"扫描标签文件 {self.n_files} 个",
            f"成功导入 {self.n_matched} 个（共 {self.n_boxes} 个目标框）",
        ]
        if self.n_unmatched:
            parts.append(f"未能对应到原图 {self.n_unmatched} 个")
        if self.n_rejected:
            parts.append(f"校验未通过 {self.n_rejected} 个")
        if not self.applied:
            parts.insert(0, "（仅检查，未写入）")
        return "；".join(parts)


# --------------------------------------------------------------------------
# 格式探测
# --------------------------------------------------------------------------

def detect_format(source: Path) -> str:
    source = Path(source)
    if not source.is_dir():
        return FMT_UNKNOWN

    has_images = (source / "images").is_dir()
    has_labels = (source / "labels").is_dir()
    txt_files = list(source.glob("*.txt"))
    has_names = any((source / name).exists() for name in NAMES_FILES)
    has_train_list = (source / "train.txt").exists()

    if has_labels and (has_images or has_names or txt_files):
        return FMT_ULTRALYTICS
    if has_train_list or (has_names and txt_files):
        return FMT_CVAT
    if txt_files:
        return FMT_ULTRALYTICS  # 只有一堆 .txt，按扁平标签目录处理
    return FMT_UNKNOWN


def read_class_names(source: Path) -> list[str]:
    """从各种常见文件里读出类别名。"""
    source = Path(source)
    for name in NAMES_FILES:
        candidate = source / name
        if candidate.exists():
            names = classes_from_names_file(candidate)
            if names:
                return names

    for name in DATA_FILES:
        candidate = source / name
        if not candidate.exists():
            continue
        names = _names_from_data_file(candidate)
        if names:
            return names

    for candidate in (source / "labels").glob("*.txt"):
        # 兜底：有的导出会附一份 classes.txt 放在 labels/ 里
        if candidate.name.lower() in NAMES_FILES:
            names = classes_from_names_file(candidate)
            if names:
                return names
    return []


def _names_from_data_file(path: Path) -> list[str]:
    """从 obj.data / data.yaml 里抠出 names。刻意不引入 YAML 依赖，按行解析够用。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    names: list[str] = []
    in_names_block = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if not in_names_block:
            if stripped.startswith("names"):
                if "[" in stripped:
                    body = stripped[stripped.index("[") + 1:]
                    body = body.rsplit("]", 1)[0]
                    for token in body.split(","):
                        token = token.strip().strip("'\"")
                        if token:
                            names.append(token)
                    return names
                in_names_block = True
                continue
            # obj.data 里的 names 是一个文本文件路径
            if stripped.startswith("names") and "=" in stripped:
                target = stripped.split("=", 1)[1].strip()
                candidate = path.parent / target
                if candidate.exists():
                    return classes_from_names_file(candidate)
            continue

        # names: 块形式，形如 "  0: person"
        if stripped and (stripped[0].isdigit() or stripped.startswith("-")):
            token = re.sub(r"^[-\d]+\s*[:.)]?\s*", "", stripped)
            token = token.strip().strip("'\"")
            if token:
                names.append(token)
            continue
        in_names_block = False
    return names


# --------------------------------------------------------------------------
# 标签文件收集
# --------------------------------------------------------------------------

@dataclass(slots=True)
class _LabelSource:
    label_path: Path
    stem: str
    original_name: str | None = None   # train.txt 里给的原图名（如果可解析）


def _collect_label_files(source: Path, fmt: str) -> tuple[list[_LabelSource], list[str]]:
    problems: list[str] = []
    entries: list[_LabelSource] = []

    if fmt == FMT_ULTRALYTICS:
        candidates: list[Path] = []
        if (source / "labels").is_dir():
            candidates.extend(sorted((source / "labels").glob("*.txt")))
        candidates.extend(sorted(p for p in source.glob("*.txt") if p.name not in _SKIP_TXT))
        seen: set[Path] = set()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            entries.append(_LabelSource(label_path=path, stem=path.stem))
        return entries, problems

    # CVAT 风格：obj_*.txt + train.txt
    candidates = sorted(source.glob("*.txt"))
    candidates = [p for p in candidates if p.name not in _SKIP_TXT]

    train_list = _read_split_list(source, "train.txt")
    if train_list:
        # train.txt 里是原图路径（可能含 images/ 子目录），取文件名做对应
        by_stem: dict[str, str] = {}
        for rel in train_list:
            base = Path(rel.replace("\\", "/")).name
            by_stem[Path(base).stem] = base

        for path in candidates:
            stem = path.stem
            # obj_000001 这种名字先尝试直接命中，再尝试去掉公共前缀后的编号
            original = by_stem.get(stem)
            if original is None:
                match = _INDEXED_STEM.match(stem)
                if match:
                    lowered = {
                        s.lstrip("0") or "0": v for s, v in by_stem.items()
                    }
                    original = lowered.get(match.group("index").lstrip("0") or "0")
                if original is None:
                    original = by_stem.get(stem)
            entries.append(
                _LabelSource(label_path=path, stem=stem, original_name=original)
            )
        if not any(e.original_name for e in entries):
            problems.append(
                "train.txt 里列出的图片名（如 obj_000001.jpg）与本工具的图片名无法对应。"
                "请在 CVAT 导出时保留原始文件名，或改用 Ultralytics 目录结构。"
            )
        return entries, problems

    for path in candidates:
        entries.append(_LabelSource(label_path=path, stem=path.stem))
    if not entries:
        problems.append("目录里没有找到任何标签文件（*.txt）")
    return entries, problems


# 这些 .txt 是清单/说明文件，不是标签文件
_SKIP_TXT = {
    "train.txt", "val.txt", "test.txt", "classes.txt", "names.txt",
    "obj.names", "labels.txt", "readme.txt", "notes.txt",
}


def _read_split_list(source: Path, name: str) -> list[str]:
    for candidate in (source / name, source / "ImageSets" / name):
        if candidate.exists():
            try:
                return [
                    line.strip()
                    for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip()
                ]
            except OSError:
                return []
    return []


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def import_labels(
    source: Path,
    db: Database,
    layout: ProjectLayout,
    class_names: list[str],
    *,
    apply: bool = True,
) -> ImportReport:
    """导入标注。

    ``apply=False`` 时只做检查（dry run），不改动数据库，便于让用户先看清楚再决定。
    """
    source = Path(source)
    report = ImportReport(source=source)

    if not source.is_dir():
        report.problems.append("请选择一个包含标注文件的文件夹")
        return report

    fmt = detect_format(source)
    report.fmt = fmt
    if fmt is FMT_UNKNOWN:
        report.problems.append(
            "无法识别标注目录结构。支持 Ultralytics 结构（images/ + labels/）"
            "或 CVAT 的 YOLO 1.1 导出（obj.names + train.txt + obj_*.txt）。"
        )
        return report

    imported_classes = read_class_names(source)
    effective_classes = list(class_names)
    if imported_classes:
        report.classes = imported_classes
        if not effective_classes:
            effective_classes = imported_classes
            report.class_map_note = f"项目类别表为空，已按导入文件建立 {len(imported_classes)} 个类别"
        elif imported_classes != effective_classes:
            report.class_map_note = (
                "类别表与项目不一致，已按**索引**映射："
                f"项目={effective_classes} / 导入={imported_classes}。"
                "请确认同一索引在两边指的是同一类，否则标注会错位。"
            )
            report.problems.append(report.class_map_note)
    elif effective_classes:
        report.class_map_note = "导入目录没有类别表，按项目的类别表校验（只检查编号范围）"
    else:
        report.problems.append(
            "既没有类别表文件，项目里也没有设置类别。无法校验类别编号，请先设置类别。"
        )

    num_classes = len(effective_classes)

    entries, collect_problems = _collect_label_files(source, fmt)
    report.problems.extend(collect_problems)
    report.n_files = len(entries)
    if not entries:
        return report

    # 建立"文件名 stem → 数据库记录"的索引，一次性查库，避免逐个文件查
    stem_index: dict[str, object] = {}
    for row in db.query_images(order="id ASC"):
        stem_index.setdefault(Path(row["rel_path"]).stem, row)

    labels_dir = layout.labels_dir
    if apply:
        labels_dir.mkdir(parents=True, exist_ok=True)

    matched_stems: set[str] = set()

    for entry in entries:
        target_row = None
        resolved_stem = entry.original_name and Path(entry.original_name).stem or entry.stem
        target_row = stem_index.get(resolved_stem)
        if target_row is None and entry.original_name:
            target_row = stem_index.get(entry.stem)

        if target_row is None:
            report.n_unmatched += 1
            if len(report.unmatched_samples) < 30:
                report.unmatched_samples.append(entry.label_path.name)
            continue

        row_stem = Path(target_row["rel_path"]).stem
        parsed = read_label_file(entry.label_path, num_classes)
        fatal = [p for p in parsed.problems if p.fatal]
        if fatal:
            report.n_rejected += 1
            if len(report.problems) < 60:
                report.problems.append(
                    f"{entry.label_path.name}：{len(fatal)} 条标注无效，例如第 "
                    f"{fatal[0].line_number} 行（{fatal[0].message}）"
                )
            continue

        # 原图还在不在，以及尺寸/比例是否与导出时一致
        absolute = layout.absolute(target_row["rel_path"])
        if not absolute.exists():
            report.n_rejected += 1
            report.problems.append(f"{target_row['rel_path']}：原图文件不存在，未导入其标注")
            continue
        source_image = _find_source_image(source, entry)
        if source_image is not None:
            ours = read_image_size(absolute)
            theirs = read_image_size(source_image)
            if ours and theirs:
                ratio_ours = ours[0] / max(1, ours[1])
                ratio_theirs = theirs[0] / max(1, theirs[1])
                if abs(ratio_ours - ratio_theirs) > 0.01:
                    report.n_rejected += 1
                    report.problems.append(
                        f"{entry.label_path.name}：标注所用图片长宽比（{theirs[0]}×{theirs[1]}）"
                        f"与原图（{ours[0]}×{ours[1]}）不一致，归一化坐标会对不上，已跳过"
                    )
                    continue

        if not apply:
            report.n_matched += 1
            report.n_boxes += len(parsed.boxes)
            continue

        destination = labels_dir / f"{row_stem}.txt"
        try:
            destination.write_text(
                "\n".join(box.to_line() for box in parsed.boxes) + ("\n" if parsed.boxes else ""),
                encoding="utf-8",
                newline="\n",
            )
        except OSError as exc:
            report.n_rejected += 1
            report.problems.append(f"写入 {destination.name} 失败：{exc}")
            continue

        rel_label = f"labels/{row_stem}.txt"
        status = ReviewStatus.LABELED if parsed.boxes else ReviewStatus.VERIFIED_EMPTY
        try:
            db.set_label_path(int(target_row["id"]), rel_label, status)
        except Exception as exc:  # pragma: no cover
            report.n_rejected += 1
            report.problems.append(f"更新 {row_stem} 的审核状态失败：{exc}")
            continue

        report.n_matched += 1
        report.n_boxes += len(parsed.boxes)
        matched_stems.add(row_stem)

    report.applied = apply and report.n_matched > 0

    if report.n_unmatched:
        report.problems.append(
            f"有 {report.n_unmatched} 个标签文件找不到对应的原图。"
            "本工具按文件名对应（不含扩展名），请确认导出时保留了原始文件名。"
        )

    if apply and report.n_matched:
        try:
            db.record_import(
                source_path=str(source), fmt=fmt,
                n_files=report.n_files, n_boxes=report.n_boxes,
                n_matched=report.n_matched, n_unmatched=report.n_unmatched,
                n_rejected=report.n_rejected,
                problems="\n".join(report.problems[:50]),
            )
        except Exception as exc:  # pragma: no cover
            log.warning("登记导入记录失败：%s", exc)

    log.info(
        "标注导入：%s matched=%d unmatched=%d rejected=%d boxes=%d",
        fmt, report.n_matched, report.n_unmatched, report.n_rejected, report.n_boxes,
    )
    return report


def _find_source_image(source: Path, entry: _LabelSource) -> Path | None:
    """在导入目录里找与某个标签文件同名的那张图，用来核对长宽比。"""
    names = []
    if entry.original_name:
        names.append(entry.original_name)
    names.append(entry.stem)
    search_dirs = [source / "images", source]
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for name in names:
            stem = Path(name).stem
            for suffix in (".png", ".PNG", ".jpg", ".JPG", ".jpeg", ".bmp", ".webp"):
                candidate = directory / f"{stem}{suffix}"
                if candidate.exists():
                    return candidate
    return None


def export_classes_for_cvat(class_names: list[str], target: Path) -> Path:
    """生成一份 CVAT 需要的类别表，方便让标注同事按同一个类目表开工。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(class_names) + "\n", encoding="utf-8", newline="\n")
    return target
