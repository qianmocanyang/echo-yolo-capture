"""YOLO 数据集导出。

方案 §10.3 的每一条约束都有对应实现：

| 约束 | 实现 |
| --- | --- |
| 导出 images/train、val、test 及对应 labels 与 data.yaml | :func:`write_export` |
| 按批次约 80% / 10% / 10% 划分 | :func:`plan_splits` |
| 相邻帧、同源裁剪图、增强图不跨集合 | 整批次划分；单批次时用连续分块 + 边界保护带 |
| 仅导出审核通过的数据 | 只收 :attr:`ReviewStatus.exportable` |
| 已确认无目标的可生成空标签；未标注不可冒充背景样本 | 空标签与"未标注"在数据模型里就是两回事 |
| 校验类别编号、非有限数值、零面积框、越界框、尺寸与路径 | :func:`validate_plan` |
| 冻结类别映射与划分清单，便于复现 | `classes.json` + `split_manifest.json` |
| 导出原图不带任何水印 | 直接字节复制原 PNG，不重新编码 |

关于"相邻帧不跨集合"的落地方式值得说明：同一批次内的图按时间连续，
如果只在批次内按 80/10/10 切两刀，刀口两侧的两帧几乎一样，
会造成实打实的训练/验证泄漏。所以单批次划分时会在每个刀口留一条**保护带**
（默认丢弃边界各 2 帧），这些帧不进入任何集合，并在报告里如实列出。
宁可少几张，也不要一个虚高的验证指标。
"""

from __future__ import annotations

import json
import os
import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..logging_setup import get_logger
from ..storage import Database, ProjectLayout, ReviewStatus, read_image_size
from .labels import YoloBox, read_label_file, validate_boxes, write_label_file

log = get_logger("dataset.exporter")

SPLITS = ("train", "val", "test")

# 单批次划分时，边界两侧各丢弃几帧作为保护带。
DEFAULT_GUARD_FRAMES = 2

# 少于这个数量就不做划分了：切出来的 val/test 只有一两张，还不如全部给 train。
MIN_ITEMS_FOR_SPLIT = 6


@dataclass(slots=True)
class ExportItem:
    """一个待导出的样本。"""

    image_id: int
    rel_path: str
    abs_path: Path
    session_uid: str
    captured_at: datetime
    boxes: list[YoloBox] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    output_w: int = 0
    output_h: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.boxes

    @property
    def stem(self) -> str:
        return Path(self.rel_path).stem


@dataclass(slots=True)
class ExportPlan:
    """导出计划。先规划再落盘，让校验能在写任何文件之前完成。"""

    root: Path
    out_dir: Path
    class_names: list[str]
    splits: dict[str, list[ExportItem]] = field(default_factory=dict)
    purged: list[ExportItem] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    strategy: str = ""
    split_seed: int = 0

    @property
    def total(self) -> int:
        return sum(len(items) for items in self.splits.values())

    @property
    def blocked(self) -> bool:
        return bool(self.problems) or self.total == 0

    def counts(self) -> dict[str, int]:
        return {name: len(self.splits.get(name, [])) for name in SPLITS}


@dataclass(slots=True)
class ExportResult:
    export_uid: str
    out_dir: Path
    counts: dict[str, int]
    n_boxes: int
    n_empty: int
    n_purged: int
    problems: list[str]
    report: str

    @property
    def ok(self) -> bool:
        return not self.problems


# --------------------------------------------------------------------------
# 收集样本
# --------------------------------------------------------------------------

def collect_items(
    db: Database,
    layout: ProjectLayout,
    class_names: list[str],
    *,
    session_uids: list[str] | None = None,
) -> tuple[list[ExportItem], list[tuple[str, str]]]:
    """从数据库收集可导出的样本。

    返回 ``(样本, 被跳过的记录及原因)``。只有"已标注"与"已确认无目标"会进来，
    方案 §10.3 最后一条就说得很直白：**未标注图片不可冒充背景样本**。
    """
    num_classes = len(class_names)
    items: list[ExportItem] = []
    skipped: list[tuple[str, str]] = []

    rows = db.query_images(statuses=None, order="id ASC")
    for row in rows:
        status = ReviewStatus.parse(row["review_status"])
        if status is ReviewStatus.PENDING:
            continue  # 待审核：连跳过都不必记录，它本来就没打算导出
        if status is ReviewStatus.TO_LABEL:
            skipped.append((row["rel_path"], "尚未标注"))
            continue
        if status is ReviewStatus.EXCLUDED:
            continue
        if session_uids and row["session_uid"] not in session_uids:
            continue
        if not status.exportable:
            continue

        absolute = layout.absolute(row["rel_path"])
        if not absolute.exists():
            skipped.append((row["rel_path"], "原图文件不存在"))
            continue

        size = read_image_size(absolute)
        if size is None:
            skipped.append((row["rel_path"], "原图无法读取"))
            continue
        width, height = size
        recorded_w = int(row["output_w"] or 0)
        recorded_h = int(row["output_h"] or 0)
        if recorded_w and recorded_h and (width, height) != (recorded_w, recorded_h):
            skipped.append(
                (row["rel_path"], f"实际尺寸 {width}×{height} 与记录 {recorded_w}×{recorded_h} 不符")
            )
            continue

        boxes: list[YoloBox] = []
        if status is ReviewStatus.LABELED:
            label_path = _resolve_label(layout, row)
            if label_path is None:
                skipped.append((row["rel_path"], "标注文件缺失"))
                continue
            parsed = read_label_file(label_path, num_classes)
            fatal = [p for p in parsed.problems if p.fatal]
            if fatal:
                skipped.append(
                    (row["rel_path"], f"标注文件有 {len(fatal)} 处问题：{fatal[0].message}")
                )
                continue
            if not parsed.boxes:
                # 标成"已标注"却没有任何框：这是记录与实际不一致，不能当背景样本
                skipped.append(
                    (row["rel_path"], "状态为已标注但标注文件为空；如确无目标请改判「已确认无目标」")
                )
                continue
            problems = validate_boxes(parsed.boxes, num_classes)
            if problems:
                skipped.append((row["rel_path"], f"标注校验未通过：{problems[0]}"))
                continue
            boxes = parsed.boxes

        items.append(
            ExportItem(
                image_id=int(row["id"]),
                rel_path=row["rel_path"],
                abs_path=absolute,
                session_uid=row["session_uid"],
                captured_at=_parse_dt(row["captured_at"]),
                boxes=boxes,
                flags=[f for f in (row["quality_flags"] or "").split(",") if f],
                output_w=width,
                output_h=height,
            )
        )
    return items, skipped


def _resolve_label(layout: ProjectLayout, row) -> Path | None:
    """找出某张图对应的标签文件。

    查找顺序体现导入时优先落在统一目录、其次允许与原图同目录：
    1. 数据库里记录的 ``label_path``（相对保存目录）
    2. ``labels/<与原图同名>.txt``
    3. 与原图同目录的同名 .txt
    """
    recorded = (row["label_path"] or "").strip()
    if recorded:
        candidate = layout.absolute(recorded)
        if candidate.exists():
            return candidate

    stem = Path(row["rel_path"]).stem
    candidate = layout.labels_dir / f"{stem}.txt"
    if candidate.exists():
        return candidate

    sibling = layout.absolute(row["rel_path"]).with_suffix(".txt")
    if sibling.exists():
        return sibling
    return None


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now()


# --------------------------------------------------------------------------
# 划分
# --------------------------------------------------------------------------

def plan_splits(
    items: list[ExportItem],
    *,
    train: float = 0.8,
    val: float = 0.1,
    test: float = 0.1,
    seed: int = 20260913,
    guard_frames: int = DEFAULT_GUARD_FRAMES,
) -> tuple[dict[str, list[ExportItem]], list[ExportItem], str]:
    """把样本划分成 train/val/test。

    返回 ``(划分结果, 被保护带淘汰的样本, 策略说明)``。

    两种策略：

    * ``session``：批次数量 ≥ 3 时整批分配。相邻帧天然同属一个集合，
      这是最干净的划分方式。
    * ``contiguous``：批次不足 3 个时，在批次内部按时间做连续分块，
      刀口留保护带。
    """
    result: dict[str, list[ExportItem]] = {name: [] for name in SPLITS}
    purged: list[ExportItem] = []

    if not items:
        return result, purged, "无样本"
    if len(items) < MIN_ITEMS_FOR_SPLIT:
        return (
            {**result, "train": list(items)},
            purged,
            f"样本仅 {len(items)} 张，不足 {MIN_ITEMS_FOR_SPLIT} 张，全部放入 train（不划分验证集）",
        )

    by_session: dict[str, list[ExportItem]] = {}
    for item in items:
        by_session.setdefault(item.session_uid, []).append(item)
    for bucket in by_session.values():
        bucket.sort(key=lambda i: (i.captured_at, i.image_id))
    session_ids = sorted(by_session, key=lambda s: by_session[s][0].captured_at)

    if len(session_ids) >= 3:
        return _split_by_session(by_session, session_ids, train, val, test, seed), purged, "session"

    return _split_contiguous(by_session, session_ids, train, val, test, guard_frames)


def _split_by_session(
    by_session: dict[str, list[ExportItem]],
    session_ids: list[str],
    train: float, val: float, test: float,
    seed: int,
) -> dict[str, list[ExportItem]]:
    """整批次分配。相邻帧天然同属一个集合，不存在泄漏。"""
    result: dict[str, list[ExportItem]] = {name: [] for name in SPLITS}
    total = sum(len(by_session[s]) for s in session_ids)
    target = {"train": total * train, "val": total * val, "test": total * test}

    rng = random.Random(seed)
    shuffled = list(session_ids)
    rng.shuffle(shuffled)

    # 先给 val / test 各留一个批次，保证它们不为空（小数据集上很容易被贪心吃光）
    for name in ("test", "val"):
        if len(shuffled) > 1:
            result[name].extend(by_session[shuffled.pop()])

    # 其余批次按"离目标还差多少"贪心分配
    for session_id in shuffled:
        deficits = {name: target[name] - len(result[name]) for name in SPLITS}
        best = max(SPLITS, key=lambda name: deficits[name])
        result[best].extend(by_session[session_id])
    return result


def _split_contiguous(
    by_session: dict[str, list[ExportItem]],
    session_ids: list[str],
    train: float, val: float, test: float,
    guard_frames: int,
) -> tuple[dict[str, list[ExportItem]], list[ExportItem], str]:
    """批次不足 3 个时，在批次内部按时间做连续分块，刀口留保护带。

    保护带只是防泄漏的手段，不能反过来让划分失效：val / test 段本来就可能只有
    一两帧，此时保护带必须按可用余量收缩（每段至少留 1 帧）。早先用固定宽度，
    两个刀口的保护带会交叉，val 段被吃空，于是整批退回 train——小数据集因此
    永远拿不到验证集，而 validate_plan 只把它当成一句"样本太少"的提示。
    """
    result: dict[str, list[ExportItem]] = {name: [] for name in SPLITS}
    purged: list[ExportItem] = []
    guard = max(0, int(guard_frames))

    for session_id in session_ids:
        bucket = by_session[session_id]
        count = len(bucket)
        if count < 5:
            # 太短了，切出来的 val/test 只有一两张，还不如整批进 train
            result["train"].extend(bucket)
            continue

        n_train = max(1, int(round(count * train)))
        n_val = max(1, int(round(count * val)))
        # 三段都得有样本：先给 test 留一帧，必要时再压缩 train
        if count - n_train - n_val < 1:
            n_val = max(1, count - n_train - 1)
        if count - n_train - n_val < 1:
            n_train = max(1, count - 2)
            n_val = 1
        n_test = count - n_train - n_val

        def spare(length: int) -> int:
            """这一段最多能让出多少帧（至少给自己留 1 帧）。"""
            return max(0, length - 1)

        # val 段夹在两个刀口之间，两侧的牺牲加起来不能把它吃空，
        # 所以先定后一个刀口（val|test）的宽度，再定前一个。
        b1 = n_train
        b2 = n_train + n_val
        g_bc = min(guard, spare(n_test), spare(n_val))
        g_ab = max(0, min(guard, spare(n_train), spare(n_val) - g_bc))

        train_part = bucket[: b1 - g_ab]
        val_part = bucket[b1 + g_ab : b2 - g_bc]
        test_part = bucket[b2 + g_bc :]
        band = bucket[b1 - g_ab : b1 + g_ab] + bucket[b2 - g_bc : b2 + g_bc]

        if not train_part or not val_part or not test_part:
            # 理论上不该走到这里；真到了就整批进 train，宁可少划分也不要错划分
            log.warning("批次 %s 的保护带计算异常，整批归入 train", session_id)
            result["train"].extend(bucket)
            continue

        result["train"].extend(train_part)
        result["val"].extend(val_part)
        result["test"].extend(test_part)
        purged.extend(band)

    return result, purged, "contiguous"


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def validate_plan(plan: ExportPlan) -> list[str]:
    """在写任何文件之前把能查的都查一遍（方案 §10.3）。"""
    problems: list[str] = []

    if not plan.class_names:
        problems.append("类别表为空。请先设置类别名称，类别编号从 0 开始。")
    seen: set[str] = set()
    for name in plan.class_names:
        if name in seen:
            problems.append(f"类别表存在重复名称：{name}")
        seen.add(name)

    if plan.total == 0:
        problems.append("没有可导出的样本。请确认图片审核状态为「已标注」或「已确认无目标」。")

    num_classes = len(plan.class_names)
    split_of: dict[int, str] = {}
    for split_name, items in plan.splits.items():
        for item in items:
            previous = split_of.get(item.image_id)
            if previous is not None and previous != split_name:
                problems.append(f"样本 {item.rel_path} 同时出现在 {previous} 与 {split_name}")
            split_of[item.image_id] = split_name

            if not item.abs_path.exists():
                problems.append(f"原图不存在：{item.rel_path}")
                continue
            size = read_image_size(item.abs_path)
            if size is None:
                problems.append(f"原图无法读取：{item.rel_path}")
                continue
            if item.output_w and item.output_h and size != (item.output_w, item.output_h):
                problems.append(
                    f"原图尺寸与记录不符：{item.rel_path}（实际 {size[0]}×{size[1]}）"
                )

            for message in validate_boxes(item.boxes, num_classes):
                problems.append(f"{item.rel_path}：{message}")

            # 重复文件名的样本会被写进同一个目录互相覆盖
    stems: dict[str, str] = {}
    for items in plan.splits.values():
        for item in items:
            other = stems.get(item.stem)
            if other is not None and other != item.rel_path:
                problems.append(f"文件名冲突：{other} 与 {item.rel_path} 同名")
            stems[item.stem] = item.rel_path

    for split_name in ("val", "test"):
        if plan.total >= MIN_ITEMS_FOR_SPLIT and not plan.splits.get(split_name):
            problems.append(
                f"{split_name} 集合为空。若样本太少，可先累积更多批次再导出。"
            )
    return problems


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------

def write_export(
    plan: ExportPlan,
    db: Database | None = None,
    *,
    export_uid: str | None = None,
) -> ExportResult:
    """把计划写成一份固定的 YOLO Detection 数据集。"""
    out_dir = Path(plan.out_dir)
    uid = export_uid or f"export_{datetime.now():%Y%m%d_%H%M%S}"
    problems = list(plan.problems)

    # 兜底：plan.problems 由调用方填（UI 层会填 validate_plan 的结果）。
    # 万一漏填，至少不能让「导出空数据集」静默报告成功。
    if not problems and plan.total == 0:
        problems = validate_plan(plan)

    if problems:
        report = _format_report(uid, plan, {}, 0, 0, problems, written=False)
        return ExportResult(uid, out_dir, {}, 0, 0, len(plan.purged), problems, report)

    for split_name in SPLITS:
        (out_dir / "images" / split_name).mkdir(parents=True, exist_ok=True)
        (out_dir / "labels" / split_name).mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    total_boxes = 0
    empty_labels = 0

    for split_name in SPLITS:
        items = plan.splits.get(split_name, [])
        counts[split_name] = len(items)
        for item in items:
            target_image = out_dir / "images" / split_name / item.abs_path.name
            if target_image.exists():
                problems.append(f"导出目录已有同名文件，已跳过：{target_image.name}")
                continue
            try:
                _copy_atomic(item.abs_path, target_image)
            except OSError as exc:
                problems.append(f"复制原图失败 {item.rel_path}：{exc}")
                continue

            label_target = out_dir / "labels" / split_name / f"{item.abs_path.stem}.txt"
            # 空标签是合法的背景样本，会写成 0 字节文件（方案 §10.3）
            write_label_file(label_target, item.boxes)
            total_boxes += len(item.boxes)
            if not item.boxes:
                empty_labels += 1

    data_yaml = out_dir / "data.yaml"
    data_yaml.write_text(
        _render_data_yaml(out_dir, plan.class_names), encoding="utf-8", newline="\n"
    )

    # 冻结类别映射与划分清单，便于复现同一次训练（方案 §10.3）
    (out_dir / "classes.json").write_text(
        json.dumps(
            {"class_id_to_name": {i: n for i, n in enumerate(plan.class_names)},
             "frozen_at": datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "split_manifest.json").write_text(
        json.dumps(
            {
                "export_uid": uid,
                "strategy": plan.strategy,
                "split_seed": plan.split_seed,
                "ratios": {"train": 0.8, "val": 0.1, "test": 0.1},
                "splits": {
                    name: [
                        {
                            "rel_path": item.rel_path,
                            "session_uid": item.session_uid,
                            "captured_at": item.captured_at.isoformat(timespec="milliseconds"),
                            "n_boxes": len(item.boxes),
                        }
                        for item in plan.splits.get(name, [])
                    ]
                    for name in SPLITS
                },
                "purged_guard_band": [
                    {"rel_path": item.rel_path, "reason": "划分边界保护带"}
                    for item in plan.purged
                ],
                "skipped": [{"rel_path": p, "reason": r} for p, r in plan.skipped],
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    report = _format_report(uid, plan, counts, total_boxes, empty_labels, problems, written=True)
    (out_dir / "export_report.txt").write_text(report, encoding="utf-8", newline="\n")

    if db is not None:
        try:
            db.record_export(
                export_uid=uid,
                out_dir=str(out_dir),
                class_map=json.dumps(plan.class_names, ensure_ascii=False),
                split_seed=plan.split_seed,
                n_train=counts.get("train", 0),
                n_val=counts.get("val", 0),
                n_test=counts.get("test", 0),
                n_empty=empty_labels,
                report=report,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("登记导出记录失败：%s", exc)

    log.info(
        "导出完成 %s：train=%d val=%d test=%d boxes=%d",
        uid, counts.get("train", 0), counts.get("val", 0), counts.get("test", 0), total_boxes,
    )
    return ExportResult(
        export_uid=uid,
        out_dir=out_dir,
        counts=counts,
        n_boxes=total_boxes,
        n_empty=empty_labels,
        n_purged=len(plan.purged),
        problems=problems,
        report=report,
    )


def _copy_atomic(source: Path, target: Path) -> None:
    """先把原图复制成 .part，再重命名。绝不重新编码，保证像素完全一致。"""
    tmp = target.with_name(target.name + ".part")
    try:
        shutil.copyfile(str(source), str(tmp))
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _render_data_yaml(out_dir: Path, class_names: list[str]) -> str:
    """手写 data.yaml，避免为一个 6 行的文件引入 YAML 依赖。

    路径一律用正斜杠，Ultralytics 在 Windows 上也能正确读取。
    """
    root = str(out_dir).replace("\\", "/")
    lines = [
        "# echo 导出的 Ultralytics YOLO Detection 数据集",
        f"# 生成时间 {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"path: {root}",
        "train: images/train",
        "val: images/val",
        "test: images/test",
        "",
        f"nc: {len(class_names)}",
        "names:",
    ]
    lines.extend(f"  {index}: {json.dumps(name, ensure_ascii=False)}" for index, name in enumerate(class_names))
    lines.append("")
    return "\n".join(lines)


def _format_report(
    uid: str,
    plan: ExportPlan,
    counts: dict[str, int],
    total_boxes: int,
    empty_labels: int,
    problems: list[str],
    *,
    written: bool,
) -> str:
    lines = [
        f"echo YOLO 数据集导出报告",
        f"导出版本：{uid}",
        f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"输出目录：{plan.out_dir}",
        f"划分策略：{plan.strategy}（种子 {plan.split_seed}）",
        "",
        "== 数量 ==",
        f"train {counts.get('train', 0)} / val {counts.get('val', 0)} / test {counts.get('test', 0)}",
        f"目标框总数 {total_boxes}，其中空标签（背景样本）{empty_labels}",
        f"边界保护带未导出 {len(plan.purged)} 张",
        f"类别：{', '.join(f'{i}={n}' for i, n in enumerate(plan.class_names)) or '（空）'}",
        "",
    ]
    if not written:
        lines.append("== 结果：未写出任何文件 ==")
    else:
        lines.append("== 结果：已写出 ==")
    lines.append("")

    if problems:
        lines.append(f"== 问题（{len(problems)}）==")
        lines.extend(f"- {p}" for p in problems[:200])
        if len(problems) > 200:
            lines.append(f"- …… 另有 {len(problems) - 200} 条")
        lines.append("")

    if plan.skipped:
        lines.append(f"== 跳过的图片（{len(plan.skipped)}）==")
        for rel, reason in plan.skipped[:200]:
            lines.append(f"- {rel}：{reason}")
        if len(plan.skipped) > 200:
            lines.append(f"- …… 另有 {len(plan.skipped) - 200} 条")
        lines.append("")

    lines.append("== 提醒 ==")
    lines.append("- 未标注图片没有进入本次导出；如其中确实没有目标，请把审核状态改为「已确认无目标」。")
    lines.append("- 320×320 的裁剪样本应与后续实际推理输入范围一致；需要全屏识别时请补采相应范围的样本。")
    return "\n".join(lines)
