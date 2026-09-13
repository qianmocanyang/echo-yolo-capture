"""无 GUI 业务逻辑冒烟测试。

这些模块是工具的地基——选区算错会截错地方，坐标换换算错会让标注整体偏移，
原子写盘漏一步会在断电时留下坏文件。UI 跑不起来只是难受，这些错了是数据不可信。

全程不依赖 Qt，可直接跑：
    python tests/test_logic.py
也可以用 unittest 发现机制：
    python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from echo.dataset.exporter import (  # noqa: E402
    ExportPlan,
    collect_items,
    plan_splits,
    validate_plan,
    write_export,
)
from echo.dataset.importer import detect_format, import_labels  # noqa: E402
from echo.dataset.labels import (  # noqa: E402
    YoloBox,
    convert_full_frame_box,
    parse_label_text,
    read_label_file,
    write_label_file,
)
from echo.quality import measure, pixel_hash  # noqa: E402
from echo.region import (  # noqa: E402
    MIN_SIDE,
    Region,
    apply_ratio_lock,
    center_region,
    fit_region_to_source,
    parse_size,
    validate_size,
)
from echo.storage.db import (  # noqa: E402
    Database,
    ImageRecord,
    ReviewStatus,
    recover_orphans,
)
from echo.storage.naming import (  # noqa: E402
    image_filename,
    is_echo_filename,
    next_session_id,
    parse_filename,
)
from echo.storage.writer import (  # noqa: E402
    ProjectLayout,
    atomic_write_bytes,
    check_directory_writable,
    encode_png,
    read_image_size,
    save_png,
)


def make_image(w: int = 320, h: int = 320, seed: int = 0) -> np.ndarray:
    """造一张有内容的 BGR 图，避免纯色触发去重。"""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    # 画一块高对比方框，让 dhash/phash 有稳定结构
    img[20:120, 20:120] = 240
    return img


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="echo-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ======================================================================
# 选区计算
# ======================================================================
class TestRegion(unittest.TestCase):
    def test_center_matches_doc_example(self):
        """方案里的例子：1920×1080 源、320×320 选区 → (800, 380)。"""
        region = center_region(1920, 1080, 320, 320)
        self.assertEqual((region.x, region.y, region.width, region.height), (800, 380, 320, 320))

    def test_center_never_negative(self):
        """选区大于源时居中会算出负数，必须夹到 0。"""
        region = center_region(200, 200, 320, 320)
        self.assertEqual((region.x, region.y), (0, 0))

    def test_clamp_keeps_size(self):
        """§3.3：夹取只挪位置，不改尺寸。"""
        region = Region(1900, 1000, 320, 320).clamp(1920, 1080)
        self.assertEqual((region.width, region.height), (320, 320))
        self.assertEqual((region.x, region.y), (1600, 760))
        self.assertTrue(region.is_inside(1920, 1080))

    def test_fit_keeps_inside_region(self):
        """本就在源内的选区原样返回，不挪不改。"""
        region = Region(800, 380, 320, 320)
        fitted = fit_region_to_source(1920, 1080, region)
        self.assertEqual(fitted, region)

    def test_fit_shrinks_oversized_region(self):
        """大窗口选区换到小屏幕：尺寸收缩并居中，validate_size 不再拦截。"""
        fitted = fit_region_to_source(1366, 768, Region(0, 0, 1920, 1080))
        self.assertTrue(fitted.is_inside(1366, 768))
        self.assertTrue(validate_size(fitted.width, fitted.height, 1366, 768).ok)

    def test_fit_smaller_than_min_source(self):
        """源比 MIN_SIDE 还小（极端情况）也不能产出非法选区。"""
        fitted = fit_region_to_source(8, 8, Region(0, 0, 320, 320))
        self.assertEqual((fitted.width, fitted.height), (MIN_SIDE, MIN_SIDE))
        self.assertEqual((fitted.x, fitted.y), (0, 0))

    def test_clamp_negative_origin(self):
        region = Region(-50, -80, 320, 320).clamp(1920, 1080)
        self.assertEqual((region.x, region.y), (0, 0))

    def test_validate_size_rejects_oversize(self):
        """§3.3：超出采集源要阻止并给出可用上限，不静默缩小。"""
        result = validate_size(2000, 1080, 1920, 1080)
        self.assertFalse(result.ok)
        self.assertIn("1920", result.message)

    def test_validate_size_floor(self):
        self.assertFalse(validate_size(8, 8, 1920, 1080).ok)
        self.assertTrue(validate_size(MIN_SIDE, MIN_SIDE, 1920, 1080).ok)

    def test_validate_size_without_source(self):
        self.assertFalse(validate_size(320, 320, 0, 0).ok)

    def test_parse_size_tolerates_fullwidth(self):
        """中文输入法下容易敲出全角数字，别让用户为此卡住。"""
        self.assertEqual(parse_size("３２０"), 320)
        self.assertEqual(parse_size(" 416 "), 416)
        self.assertIsNone(parse_size("abc"))
        self.assertIsNone(parse_size(""))

    def test_ratio_lock_uses_reference(self):
        """锁定比例必须按当前生效的宽高算，否则另一边原地不动（曾经的 bug）。"""
        # 当前 1280×720，用户把宽改成 640 → 高应为 360
        w, h = apply_ratio_lock(640, 720, "width", 1920, 1080, ref_width=1280, ref_height=720)
        self.assertEqual((w, h), (640, 360))
        # 用户把高改成 480 → 宽应为 853（480 × 16/9 四舍五入）
        w, h = apply_ratio_lock(1280, 480, "height", 1920, 1080, ref_width=1280, ref_height=720)
        self.assertEqual((w, h), (853, 480))

    def test_ratio_lock_square(self):
        w, h = apply_ratio_lock(640, 320, "width", 1920, 1080, ref_width=320, ref_height=320)
        self.assertEqual((w, h), (640, 640))

    def test_ratio_lock_clamps_to_source(self):
        """锁定后超出源时按能放下的一维等比缩回来。"""
        w, h = apply_ratio_lock(1920, 720, "width", 1920, 1080, ref_width=1280, ref_height=720)
        self.assertLessEqual(w, 1920)
        self.assertLessEqual(h, 1080)


# ======================================================================
# 命名规则
# ======================================================================
class TestNaming(unittest.TestCase):
    def test_first_session_sequence(self):
        session = next_session_id([], datetime(2026, 9, 13, 10, 30, 0))
        self.assertEqual(session.sequence, 1)
        self.assertIn("20260913", session.uid)

    def test_sequence_increments_within_day(self):
        first = next_session_id([], datetime(2026, 9, 13, 10, 0, 0))
        second = next_session_id([first.uid], datetime(2026, 9, 13, 11, 0, 0))
        self.assertEqual(second.sequence, 2)
        self.assertNotEqual(first.uid, second.uid)

    def test_filename_roundtrip(self):
        session = next_session_id([], datetime(2026, 9, 13, 10, 30, 0))
        moment = datetime(2026, 9, 13, 10, 30, 15, 123000)
        name = image_filename(session, moment, 7)
        self.assertTrue(is_echo_filename(name))
        parsed = parse_filename(name)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["session_uid"], session.uid)

    def test_foreign_filename_rejected(self):
        self.assertFalse(is_echo_filename("frame_0001.png"))
        self.assertIsNone(parse_filename("frame_0001.png"))


# ======================================================================
# 质量与去重
# ======================================================================
class TestQuality(unittest.TestCase):
    def test_identical_images_share_hash(self):
        a = make_image(seed=1)
        b = a.copy()
        self.assertEqual(pixel_hash(a), pixel_hash(b))
        self.assertEqual(measure(a).image_hash, measure(b).image_hash)

    def test_different_images_differ(self):
        self.assertNotEqual(pixel_hash(make_image(seed=1)), pixel_hash(make_image(seed=2)))

    def test_black_frame_flagged(self):
        dark = np.zeros((320, 320, 3), dtype=np.uint8)
        report = measure(dark)
        self.assertIn("black", report.flags)

    def test_report_has_all_hashes(self):
        report = measure(make_image(seed=3))
        self.assertTrue(report.image_hash)
        self.assertTrue(report.dhash)
        self.assertTrue(report.ahash)
        self.assertTrue(report.phash)


# ======================================================================
# YOLO 坐标换算
# ======================================================================
class TestLabelMath(unittest.TestCase):
    def test_convert_box_in_crop_center(self):
        """全帧 640×640，裁剪区 (200,200)-(440,440)，中心框应落在裁剪图正中。"""
        result = convert_full_frame_box(
            0, 300, 300, 340, 340,
            crop_x=200, crop_y=200, crop_w=240, crop_h=240,
            full_w=640, full_h=640,
        )
        self.assertIsNotNone(result.box)
        box = result.box
        self.assertAlmostEqual(box.x_center, 0.5, places=6)
        self.assertAlmostEqual(box.y_center, 0.5, places=6)

    def test_convert_box_outside_crop_dropped(self):
        """完全在裁剪区外的目标必须丢弃，而不是产生越界标注。"""
        result = convert_full_frame_box(
            0, 10, 10, 40, 40,
            crop_x=200, crop_y=200, crop_w=240, crop_h=240,
            full_w=640, full_h=640,
        )
        self.assertIsNone(result.box)
        self.assertTrue(result.dropped_reason)

    def test_convert_box_clipped_at_edge(self):
        """跨在裁剪边界上的目标要被截断，且仍在 0..1 内。"""
        result = convert_full_frame_box(
            0, 180, 300, 260, 340,
            crop_x=200, crop_y=200, crop_w=240, crop_h=240,
            full_w=640, full_h=640,
        )
        self.assertIsNotNone(result.box)
        box = result.box
        self.assertGreaterEqual(box.x_center - box.width / 2, -1e-9)
        self.assertLessEqual(box.x_center + box.width / 2, 1 + 1e-9)

    def test_label_roundtrip(self):
        text = "0 0.500000 0.500000 0.250000 0.250000\n1 0.100000 0.200000 0.300000 0.400000\n"
        parsed = parse_label_text(text, 2)
        self.assertTrue(parsed.ok)
        self.assertEqual(len(parsed.boxes), 2)
        rebuilt = parse_label_text("\n".join(b.to_line() for b in parsed.boxes), 2)
        self.assertEqual(len(rebuilt.boxes), 2)

    def test_out_of_range_rejected(self):
        parsed = parse_label_text("0 1.500000 0.500000 0.200000 0.200000\n", 1)
        self.assertFalse(parsed.ok)

    def test_unknown_class_rejected(self):
        parsed = parse_label_text("9 0.500000 0.500000 0.200000 0.200000\n", 2)
        self.assertFalse(parsed.ok)

    def test_empty_file_recognised(self):
        parsed = parse_label_text("", 2)
        self.assertTrue(parsed.ok)
        self.assertTrue(parsed.empty)


# ======================================================================
# 写盘
# ======================================================================
class TestWriter(TempCase):
    def test_png_roundtrip_size(self):
        payload = encode_png(make_image(320, 256, seed=5))
        path = self.tmp / "a.png"
        atomic_write_bytes(path, payload)
        self.assertTrue(path.exists())
        self.assertEqual(read_image_size(path), (320, 256))

    def test_png_survives_chinese_path(self):
        """保存目录带中文是常态，OpenCV 的 imwrite 在这里会静默失败。"""
        folder = self.tmp / "游戏截图数据集" / "session_01"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "截图_001.png"
        written = save_png(make_image(160, 120, seed=6), target)
        self.assertTrue(target.exists())
        self.assertEqual(written, target.stat().st_size)  # 返回的是写入字节数
        self.assertEqual(read_image_size(target), (160, 120))

    def test_atomic_write_leaves_no_part_file(self):
        path = self.tmp / "b.png"
        atomic_write_bytes(path, encode_png(make_image(64, 64, seed=7)))
        leftovers = list(self.tmp.glob("*.part"))
        self.assertEqual(leftovers, [])

    def test_writable_check_creates_missing_dir(self):
        """保存目录可以还不存在，选目录时就该顺手建出来。"""
        target = self.tmp / "还没建的目录"
        ok, message = check_directory_writable(target)
        self.assertTrue(ok, message)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.glob(".echo_write_test*")), [])

    def test_writable_check_creates_and_cleans_probe(self):
        ok, _ = check_directory_writable(self.tmp)
        self.assertTrue(ok)
        self.assertEqual(list(self.tmp.glob(".echo_write_test*")), [])

    def test_layout_paths(self):
        layout = ProjectLayout(self.tmp / "proj")
        self.assertEqual(layout.session_rel("s1", "a.png"), "images/s1/a.png")
        self.assertEqual(layout.absolute("images/s1/a.png"), layout.root / "images" / "s1" / "a.png")


# ======================================================================
# 数据库往返
# ======================================================================
class TestDatabase(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.layout = ProjectLayout(self.tmp / "proj").ensure()
        self.db = Database(self.layout.db_path)
        self.session = next_session_id([], datetime(2026, 9, 13, 10, 0, 0))

    def tearDown(self) -> None:
        self.db.close()
        super().tearDown()

    def _insert(self, index: int, **overrides) -> int:
        moment = datetime(2026, 9, 13, 10, 0, index)
        name = image_filename(self.session, moment, index)
        record = ImageRecord(
            rel_path=self.layout.session_rel(self.session.uid, name),
            session_uid=self.session.uid,
            captured_at=moment,
            output_w=320,
            output_h=320,
            image_hash=f"hash{index}",
            **overrides,
        )
        return self.db.insert_image(record)

    def test_insert_and_fetch(self):
        image_id = self._insert(1)
        row = self.db.get_image(image_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["session_uid"], self.session.uid)
        self.assertEqual(row["output_w"], 320)

    def test_fetch_by_path(self):
        image_id = self._insert(2)
        row = self.db.get_image(image_id)
        again = self.db.get_image_by_path(row["rel_path"])
        self.assertIsNotNone(again)
        self.assertEqual(again["id"], image_id)

    def test_status_transition_and_counts(self):
        ids = [self._insert(i) for i in (1, 2, 3)]
        self.assertEqual(self.db.count_images(), 3)
        self.assertEqual(self.db.set_review_status(ids[:2], ReviewStatus.LABELED), 2)
        counts = self.db.status_counts()
        self.assertEqual(counts.get(ReviewStatus.LABELED.value), 2)
        self.assertEqual(counts.get(ReviewStatus.PENDING.value), 1)

    def test_query_filters_by_status(self):
        keep = self._insert(1)
        drop = self._insert(2)
        self.db.set_review_status([drop], ReviewStatus.EXCLUDED)
        rows = self.db.query_images(statuses=[ReviewStatus.EXCLUDED.value])
        self.assertEqual([r["id"] for r in rows], [drop])
        rows = self.db.query_images(statuses=[ReviewStatus.PENDING.value])
        self.assertEqual([r["id"] for r in rows], [keep])

    def test_mark_duplicate_and_similar(self):
        first = self._insert(1)
        second = self._insert(2)
        self.db.mark_duplicate(second, first)
        row = self.db.get_image(second)
        self.assertEqual(row["exact_dup_of"], first)

        group = self.db.next_similar_group()
        self.db.mark_similar(second, group, first)
        row = self.db.get_image(second)
        self.assertEqual(row["similar_group"], group)
        self.assertEqual(row["similar_to"], first)

    def test_classes_roundtrip(self):
        self.db.set_classes(["fish", "crab"])
        self.assertEqual(self.db.get_classes(), ["fish", "crab"])

    def test_session_uids(self):
        self.db.upsert_session(
            self.session.uid,
            started_at=datetime(2026, 9, 13, 10, 0, 0),
            source_type="window",
            source_label="Test Game",
            source_id="identity",
            backend="wgc",
            region=(0, 0, 320, 320),
            output_size=(320, 320),
        )
        self.assertEqual(self.db.all_session_uids(), [self.session.uid])

    def test_integrity_ok(self):
        self._insert(1)
        self.assertEqual(self.db.integrity_check().lower(), "ok")

    def test_recover_orphans_registers_untracked_and_drops_temp(self):
        """断电后可能留下 .part 与未登记的 PNG，恢复流程要能收拾干净。"""
        # 一个半截的临时文件
        temp = self.layout.session_dir(self.session.uid) / "half.png.part"
        temp.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(temp, b"partial")
        # 一个写了盘但没进库的孤儿
        orphan = self.layout.session_dir(self.session.uid) / "session_20260913_001_100000000_000001.png"
        save_png(make_image(64, 64, seed=9), orphan)

        report = recover_orphans(self.layout.root, self.db)
        self.assertTrue(any("half.png.part" in p for p in report.temp_files_removed))
        self.assertEqual(len(report.orphans_registered), 1)
        self.assertEqual(self.db.count_images(), 1)

    def test_recover_reports_missing_file(self):
        image_id = self._insert(1)
        row = self.db.get_image(image_id)
        # 库里有记录但盘上没有文件
        report = recover_orphans(self.layout.root, self.db)
        self.assertTrue(report.missing_files or report.unreadable)


# ======================================================================
# 数据集导出 / 导入
# ======================================================================
class TestDataset(TempCase):
    CLASSES = ["fish", "crab"]

    def setUp(self) -> None:
        super().setUp()
        self.layout = ProjectLayout(self.tmp / "proj").ensure()
        self.db = Database(self.layout.db_path)
        self.session = next_session_id([], datetime(2026, 9, 13, 10, 0, 0))
        self.db.upsert_session(
            self.session.uid,
            started_at=datetime(2026, 9, 13, 10, 0, 0),
            source_type="window",
            source_label="Test Game",
            source_id="identity",
            backend="wgc",
            region=(0, 0, 320, 320),
            output_size=(320, 320),
        )
        self.db.set_classes(self.CLASSES)
        self._build(20)

    def tearDown(self) -> None:
        self.db.close()
        super().tearDown()

    def _build(self, count: int, session=None) -> None:
        session = session or self.session
        for index in range(1, count + 1):
            moment = datetime(2026, 9, 13, 10, 0, index)
            name = image_filename(session, moment, index)
            rel = self.layout.session_rel(session.uid, name)
            target = self.layout.absolute(rel)
            save_png(make_image(320, 320, seed=100 + index), target)

            label_rel = f"labels/{session.uid}/{Path(name).with_suffix('.txt').name}"
            write_label_file(self.layout.absolute(label_rel), [YoloBox(0, 0.5, 0.5, 0.2, 0.2)])

            self.db.insert_image(
                ImageRecord(
                    rel_path=rel,
                    session_uid=session.uid,
                    captured_at=moment,
                    output_w=320,
                    output_h=320,
                    image_hash=f"h{index}",
                    label_path=label_rel,
                    review_status=ReviewStatus.LABELED.value,
                )
            )

    def _plan(self, items, skipped):
        splits, purged, strategy = plan_splits(items, train=0.8, val=0.1, test=0.1, guard_frames=2)
        return ExportPlan(
            root=self.layout.root,
            out_dir=self.layout.exports_dir / "exp1",
            class_names=list(self.CLASSES),
            splits=splits,
            purged=purged,
            skipped=list(skipped),
            problems=[],
            strategy=strategy,
            split_seed=20260913,
        )

    def test_collect_items_reads_labels(self):
        items, skipped = collect_items(self.db, self.layout, self.CLASSES)
        self.assertEqual(len(items), 20)
        self.assertEqual(skipped, [])
        self.assertTrue(all(item.boxes for item in items))
        self.assertTrue(all(item.abs_path.exists() for item in items))

    def test_plan_splits_ratio_and_no_leakage(self):
        items, skipped = collect_items(self.db, self.layout, self.CLASSES)
        plan = self._plan(items, skipped)
        counts = plan.counts()
        self.assertEqual(sum(counts.values()) + len(plan.purged), 20)
        self.assertGreater(counts.get("train", 0), 0)
        # 单批次会走 contiguous 策略，且三个集合都要有样本——
        # 早先固定宽度的保护带会让 val 段交叉为空，整批退回 train。
        self.assertEqual(plan.strategy, "contiguous")
        for name in ("train", "val", "test"):
            self.assertGreater(counts.get(name, 0), 0, f"{name} 集合为空")
        self.assertFalse(validate_plan(plan), "划分结果不应有校验问题")

    def test_plan_splits_three_sessions_uses_session_strategy(self):
        """批次 ≥ 3 时整批分配，相邻帧天然同批，不需要保护带。"""
        existing = [self.session.uid]
        for index in (2, 3):
            session = next_session_id(existing, datetime(2026, 9, 13, 12, 0, index))
            existing.append(session.uid)
            self.db.upsert_session(
                session.uid,
                started_at=datetime(2026, 9, 13, 12, 0, index),
                source_type="window",
                source_label="Test Game",
                source_id="identity",
                backend="wgc",
                region=(0, 0, 320, 320),
                output_size=(320, 320),
            )
            self._build(10, session=session)

        items, _ = collect_items(self.db, self.layout, self.CLASSES)
        splits, purged, strategy = plan_splits(items)
        self.assertEqual(strategy, "session")
        self.assertEqual(purged, [])
        self.assertEqual(sum(len(v) for v in splits.values()), 40)
        for name in ("train", "val", "test"):
            self.assertTrue(splits[name], f"{name} 集合为空")
        # 同一批次不能横跨两个集合
        for session_uid in {item.session_uid for item in items}:
            homes = {
                name for name, group in splits.items()
                if any(item.session_uid == session_uid for item in group)
            }
            self.assertEqual(len(homes), 1, f"批次 {session_uid} 跨了 {homes}")

    def test_export_refuses_empty_dataset(self):
        for row in self.db.query_images():
            self.db.set_review_status([row["id"]], ReviewStatus.PENDING)
        items, _ = collect_items(self.db, self.layout, self.CLASSES)
        self.assertEqual(items, [])

        # UI 层会在这里把 validate_plan 的结果填进 problems
        plan = self._plan(items, [])
        plan.problems = validate_plan(plan)
        result = write_export(plan, self.db)
        self.assertFalse(result.ok)
        self.assertTrue(result.problems)

    def test_export_refuses_empty_even_without_validation(self):
        """调用方漏跑 validate_plan 时也不能静默导出空数据集。"""
        for row in self.db.query_images():
            self.db.set_review_status([row["id"]], ReviewStatus.PENDING)
        items, _ = collect_items(self.db, self.layout, self.CLASSES)
        plan = self._plan(items, [])
        self.assertEqual(plan.problems, [])          # 故意不填
        result = write_export(plan, self.db)
        self.assertFalse(result.ok)

    def test_write_export_produces_dataset_files(self):
        items, skipped = collect_items(self.db, self.layout, self.CLASSES)
        plan = self._plan(items, skipped)
        result = write_export(plan, self.db)
        self.assertTrue(result.ok, result.problems)

        out = result.out_dir
        self.assertTrue((out / "data.yaml").exists())
        self.assertTrue((out / "classes.json").exists())
        self.assertTrue((out / "split_manifest.json").exists())

        # 落盘结构是 images/<split>/ 与 labels/<split>/，data.yaml 里的路径要与之一致。
        # 保护带淘汰的帧按设计不导出，所以数量是总数减去 purged。
        expected = 20 - result.n_purged
        images = [p for p in out.rglob("*.png")]
        labels = [
            p for p in out.rglob("*.txt")
            if p.parent.parent.name == "labels"
        ]
        self.assertGreater(expected, 0)
        self.assertEqual(len(images), expected)
        self.assertEqual(len(labels), expected)

        yaml_text = (out / "data.yaml").read_text(encoding="utf-8")
        self.assertIn("train: images/train", yaml_text)
        self.assertIn("val: images/val", yaml_text)
        self.assertIn("fish", yaml_text)

        # 每个 split 下的 labels 必须与 images 同名，且三集合都不为空
        for split_name in ("train", "val", "test"):
            img_dir = out / "images" / split_name
            lbl_dir = out / "labels" / split_name
            self.assertTrue(img_dir.is_dir(), split_name)
            names = {p.stem for p in img_dir.glob("*.png")}
            self.assertTrue(names, f"{split_name} 集合为空")
            for stem in names:
                self.assertTrue((lbl_dir / f"{stem}.txt").exists(), stem)

        # 保护带淘汰的样本要记录在清单里，且三段加淘汰等于总数
        manifest = json.loads((out / "split_manifest.json").read_text(encoding="utf-8"))
        counted = sum(len(v) for v in manifest["splits"].values())
        self.assertEqual(counted + len(manifest["purged_guard_band"]), 20)

    def test_export_refuses_empty_dataset(self):
        for row in self.db.query_images():
            self.db.set_review_status([row["id"]], ReviewStatus.PENDING)
        items, _ = collect_items(self.db, self.layout, self.CLASSES)
        self.assertEqual(items, [])
        plan = self._plan(items, [])
        result = write_export(plan, self.db)
        self.assertFalse(result.ok)

    def test_import_ultralytics_dry_run(self):
        source = self.tmp / "external"
        (source / "images").mkdir(parents=True)
        (source / "labels").mkdir(parents=True)
        save_png(make_image(320, 320, seed=42), source / "images" / "sample.png")
        (source / "labels" / "sample.txt").write_text(
            "0 0.500000 0.500000 0.200000 0.200000\n", encoding="utf-8"
        )
        (source / "data.yaml").write_text(
            "path: .\ntrain: images\nval: images\nnc: 2\nnames: [fish, crab]\n",
            encoding="utf-8",
        )

        self.assertEqual(detect_format(source), "ultralytics")
        report = import_labels(source, self.db, self.layout, self.CLASSES, apply=False)
        self.assertEqual(report.fmt, "ultralytics")
        self.assertGreaterEqual(report.n_files, 1)
        self.assertFalse(report.applied)
        # dry-run 不能改动数据库
        self.assertEqual(self.db.count_images(), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
