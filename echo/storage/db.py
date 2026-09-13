"""项目索引数据库（SQLite）。

方案 §5.2 把 `metadata/dataset.db` 定位为「项目索引、审核状态及批次信息」。
这里的实现原则：

* **数据库不是唯一真相**。PNG 原图落在磁盘上，数据库只做索引。数据库丢失或未提交
  都可以通过 :func:`recover_orphans` 从文件名重建，绝不能反过来因为数据库问题丢图。
* 写盘顺序严格是「临时文件 → 重命名 → 提交数据库」（方案 §5.2）。
  所以数据库里存在但文件不存在 = 异常；文件存在但数据库没有 = 待恢复，可以修。
* WAL 模式：写线程在写，UI 线程在读，互不阻塞。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..logging_setup import get_logger

log = get_logger("storage.db")

SCHEMA_VERSION = 1


class ReviewStatus(str, Enum):
    """方案 §9.2 的审核状态。注意「待审核」≠「无目标」。"""

    PENDING = "pending"                  # 待审核
    TO_LABEL = "to_label"                # 待标注
    LABELED = "labeled"                  # 已标注
    VERIFIED_EMPTY = "verified_empty"    # 已确认无目标（可生成空标签，作为背景样本）
    EXCLUDED = "excluded"                # 已排除（不进数据集）

    @property
    def label(self) -> str:
        return {
            ReviewStatus.PENDING: "待审核",
            ReviewStatus.TO_LABEL: "待标注",
            ReviewStatus.LABELED: "已标注",
            ReviewStatus.VERIFIED_EMPTY: "已确认无目标",
            ReviewStatus.EXCLUDED: "已排除",
        }[self]

    @property
    def exportable(self) -> bool:
        """只有已标注与已确认无目标可以进导出。未标注不可冒充背景样本。"""
        return self in {ReviewStatus.LABELED, ReviewStatus.VERIFIED_EMPTY}

    @staticmethod
    def parse(value: str | None) -> "ReviewStatus":
        try:
            return ReviewStatus(value)
        except (ValueError, TypeError):
            return ReviewStatus.PENDING


class SaveResult(str, Enum):
    SAVED = "saved"            # 成功落盘
    WRITE_FAILED = "failed"    # 写盘失败
    DEFERRED = "deferred"      # 已接受但尚未落盘（入队）


class TriggerKind(str, Enum):
    MANUAL = "manual"
    TIMER = "timer"
    BURST = "burst"


_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_uid  TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    source_type  TEXT NOT NULL DEFAULT 'monitor',
    source_label TEXT NOT NULL DEFAULT '',
    source_id    TEXT NOT NULL DEFAULT '',
    backend      TEXT NOT NULL DEFAULT '',
    region_x     INTEGER NOT NULL DEFAULT 0,
    region_y     INTEGER NOT NULL DEFAULT 0,
    region_w     INTEGER NOT NULL DEFAULT 0,
    region_h     INTEGER NOT NULL DEFAULT 0,
    output_w     INTEGER NOT NULL DEFAULT 0,
    output_h     INTEGER NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uid   TEXT NOT NULL,
    rel_path      TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    trigger_kind  TEXT NOT NULL DEFAULT 'manual',
    backend       TEXT NOT NULL DEFAULT '',
    source_type   TEXT NOT NULL DEFAULT 'monitor',
    source_label  TEXT NOT NULL DEFAULT '',
    source_w      INTEGER NOT NULL DEFAULT 0,
    source_h      INTEGER NOT NULL DEFAULT 0,
    region_x      INTEGER NOT NULL DEFAULT 0,
    region_y      INTEGER NOT NULL DEFAULT 0,
    region_w      INTEGER NOT NULL DEFAULT 0,
    region_h      INTEGER NOT NULL DEFAULT 0,
    output_w      INTEGER NOT NULL DEFAULT 0,
    output_h      INTEGER NOT NULL DEFAULT 0,
    image_hash    TEXT NOT NULL DEFAULT '',
    dhash         TEXT NOT NULL DEFAULT '',
    ahash         TEXT NOT NULL DEFAULT '',
    phash         TEXT NOT NULL DEFAULT '',
    exact_dup_of  INTEGER,
    similar_group INTEGER,
    similar_to    INTEGER,
    quality_flags TEXT NOT NULL DEFAULT '',
    mean_luma     REAL,
    sharpness     REAL,
    scene_label   TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'pending',
    label_path    TEXT NOT NULL DEFAULT '',
    save_result   TEXT NOT NULL DEFAULT 'saved',
    is_burst      INTEGER NOT NULL DEFAULT 0,
    notes         TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_images_session ON images(session_uid);
CREATE INDEX IF NOT EXISTS idx_images_status  ON images(review_status);
CREATE INDEX IF NOT EXISTS idx_images_hash    ON images(image_hash);
CREATE INDEX IF NOT EXISTS idx_images_dhash   ON images(dhash);
CREATE INDEX IF NOT EXISTS idx_images_created ON images(created_at);

CREATE TABLE IF NOT EXISTS label_imports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at  TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    fmt          TEXT NOT NULL DEFAULT 'yolo',
    n_files      INTEGER NOT NULL DEFAULT 0,
    n_boxes      INTEGER NOT NULL DEFAULT 0,
    n_matched    INTEGER NOT NULL DEFAULT 0,
    n_unmatched  INTEGER NOT NULL DEFAULT 0,
    n_rejected   INTEGER NOT NULL DEFAULT 0,
    problems     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS exports (
    export_uid  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    out_dir     TEXT NOT NULL,
    class_map   TEXT NOT NULL DEFAULT '',
    split_seed  INTEGER NOT NULL DEFAULT 0,
    n_train     INTEGER NOT NULL DEFAULT 0,
    n_val       INTEGER NOT NULL DEFAULT 0,
    n_test      INTEGER NOT NULL DEFAULT 0,
    n_empty     INTEGER NOT NULL DEFAULT 0,
    report      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS classes (
    class_id INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE
);
"""


@dataclass(slots=True)
class ImageRecord:
    """一张图的完整索引记录。字段对应方案 §9.2。"""

    rel_path: str
    session_uid: str
    captured_at: datetime
    trigger_kind: str = TriggerKind.MANUAL.value
    backend: str = ""
    source_type: str = "monitor"
    source_label: str = ""
    source_w: int = 0
    source_h: int = 0
    region_x: int = 0
    region_y: int = 0
    region_w: int = 0
    region_h: int = 0
    output_w: int = 0
    output_h: int = 0
    image_hash: str = ""
    dhash: str = ""
    ahash: str = ""
    phash: str = ""
    exact_dup_of: int | None = None
    similar_group: int | None = None
    similar_to: int | None = None
    quality_flags: str = ""
    mean_luma: float | None = None
    sharpness: float | None = None
    scene_label: str = ""
    review_status: str = ReviewStatus.PENDING.value
    label_path: str = ""
    save_result: str = SaveResult.SAVED.value
    is_burst: bool = False
    notes: str = ""
    id: int | None = None
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def flags(self) -> list[str]:
        return [f for f in (self.quality_flags or "").split(",") if f]

    @property
    def status(self) -> ReviewStatus:
        return ReviewStatus.parse(self.review_status)


_COLUMNS = (
    "session_uid", "rel_path", "created_at", "captured_at", "trigger_kind", "backend",
    "source_type", "source_label", "source_w", "source_h",
    "region_x", "region_y", "region_w", "region_h", "output_w", "output_h",
    "image_hash", "dhash", "ahash", "phash",
    "exact_dup_of", "similar_group", "similar_to",
    "quality_flags", "mean_luma", "sharpness",
    "scene_label", "review_status", "label_path", "save_result", "is_burst", "notes",
)


class Database:
    """线程安全的 SQLite 包装。

    单连接 + 可重入锁。写入量级是每秒几张图，单连接远够用，
    换来的是绝对不必担心多连接下的 WAL 争用与 `database is locked`。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=15.0
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_DDL)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # ---- 生命周期 -------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 批次 -----------------------------------------------------------
    def existing_session_uids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT session_uid FROM sessions").fetchall()
        return [r["session_uid"] for r in rows]

    def all_session_uids(self) -> list[str]:
        """既有批次表里的，也有图片表里出现过的（防止会话表缺失导致重号）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_uid FROM sessions "
                "UNION SELECT session_uid FROM images"
            ).fetchall()
        return sorted({r["session_uid"] for r in rows})

    def upsert_session(
        self,
        session_uid: str,
        *,
        started_at: datetime,
        source_type: str,
        source_label: str,
        source_id: str,
        backend: str,
        region: tuple[int, int, int, int],
        output_size: tuple[int, int],
        note: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions(
                    session_uid, started_at, source_type, source_label, source_id,
                    backend, region_x, region_y, region_w, region_h, output_w, output_h, note
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_uid) DO UPDATE SET
                    source_label=excluded.source_label,
                    source_id=excluded.source_id,
                    backend=excluded.backend,
                    region_x=excluded.region_x, region_y=excluded.region_y,
                    region_w=excluded.region_w, region_h=excluded.region_h,
                    output_w=excluded.output_w, output_h=excluded.output_h
                """,
                (
                    session_uid, started_at.isoformat(timespec="seconds"), source_type,
                    source_label, source_id, backend,
                    region[0], region[1], region[2], region[3],
                    output_size[0], output_size[1], note,
                ),
            )
            self._conn.commit()

    def close_session(self, session_uid: str, ended_at: datetime | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_at=? WHERE session_uid=? AND ended_at IS NULL",
                ((ended_at or datetime.now()).isoformat(timespec="seconds"), session_uid),
            )
            self._conn.commit()

    def sessions(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM images i WHERE i.session_uid = s.session_uid) AS n_images
                FROM sessions s
                ORDER BY s.started_at DESC
                """
            ).fetchall()

    def session_summaries(self) -> list[sqlite3.Row]:
        """包含只有图片、没有会话行的批次（恢复出来的批次）。"""
        with self._lock:
            return self._conn.execute(
                """
                SELECT COALESCE(s.session_uid, d.session_uid) AS session_uid,
                       s.started_at,
                       COALESCE(s.source_label, '') AS source_label,
                       COALESCE(s.backend, '') AS backend,
                       COUNT(i.id) AS n_images,
                       SUM(CASE WHEN i.review_status='labeled' THEN 1 ELSE 0 END) AS n_labeled,
                       SUM(CASE WHEN i.review_status='excluded' THEN 1 ELSE 0 END) AS n_excluded
                FROM (SELECT DISTINCT session_uid FROM images) d
                LEFT JOIN sessions s ON s.session_uid = d.session_uid
                LEFT JOIN images i ON i.session_uid = d.session_uid
                GROUP BY COALESCE(s.session_uid, d.session_uid)
                ORDER BY COALESCE(s.started_at, '') DESC, session_uid DESC
                """
            ).fetchall()

    # ---- 图片 -----------------------------------------------------------
    def insert_image(self, record: ImageRecord) -> int:
        """提交一条图片记录。文件已经落盘才应该调用这里（方案 §5.2 的顺序）。"""
        values = (
            record.session_uid, record.rel_path,
            record.created_at.isoformat(timespec="milliseconds"),
            record.captured_at.isoformat(timespec="milliseconds"),
            record.trigger_kind, record.backend, record.source_type, record.source_label,
            record.source_w, record.source_h,
            record.region_x, record.region_y, record.region_w, record.region_h,
            record.output_w, record.output_h,
            record.image_hash, record.dhash, record.ahash, record.phash,
            record.exact_dup_of, record.similar_group, record.similar_to,
            record.quality_flags, record.mean_luma, record.sharpness,
            record.scene_label, record.review_status, record.label_path,
            record.save_result, 1 if record.is_burst else 0, record.notes,
        )
        placeholders = ",".join("?" for _ in _COLUMNS)
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT INTO images({','.join(_COLUMNS)}) VALUES({placeholders})", values
            )
            self._conn.commit()
            record.id = int(cursor.lastrowid)
            return record.id

    def rel_path_exists(self, rel_path: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM images WHERE rel_path=? LIMIT 1", (rel_path,)
            ).fetchone()
        return row is not None

    def find_by_hash(self, image_hash: str, session_uid: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM images WHERE image_hash=?"
        params: list[Any] = [image_hash]
        if session_uid:
            sql += " AND session_uid=?"
            params.append(session_uid)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def recent_images(self, limit: int = 8, session_uid: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM images"
        params: list[Any] = []
        if session_uid:
            sql += " WHERE session_uid=?"
            params.append(session_uid)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_images(
        self,
        *,
        session_uid: str | None = None,
        statuses: Sequence[str] | None = None,
        flags: Sequence[str] | None = None,
        only_duplicates: bool = False,
        only_similar: bool = False,
        search: str = "",
        order: str = "id DESC",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_uid:
            clauses.append("session_uid=?")
            params.append(session_uid)
        if statuses:
            clauses.append(f"review_status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if only_duplicates:
            clauses.append("exact_dup_of IS NOT NULL")
        if only_similar:
            clauses.append("similar_group IS NOT NULL")
        if search:
            clauses.append("(rel_path LIKE ? OR scene_label LIKE ? OR notes LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        if flags:
            for flag in flags:
                clauses.append("(',' || quality_flags || ',') LIKE ?")
                params.append(f"%,{flag},%")

        sql = "SELECT * FROM images"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # order 只允许白名单内的排序表达式，避免拼接注入
        allowed_orders = {
            "id DESC", "id ASC", "captured_at DESC", "captured_at ASC",
            "rel_path ASC", "rel_path DESC",
        }
        sql += " ORDER BY " + (order if order in allowed_orders else "id DESC")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def get_image(self, image_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM images WHERE id=?", (int(image_id),)
            ).fetchone()

    def get_image_by_path(self, rel_path: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM images WHERE rel_path=?", (rel_path,)
            ).fetchone()

    def set_review_status(self, image_ids: Iterable[int], status: ReviewStatus) -> int:
        ids = [int(i) for i in image_ids]
        if not ids:
            return 0
        with self._lock:
            cursor = self._conn.executemany(
                "UPDATE images SET review_status=? WHERE id=?",
                [(status.value, i) for i in ids],
            )
            self._conn.commit()
        return cursor.rowcount if cursor.rowcount >= 0 else len(ids)

    def set_label_path(self, image_id: int, label_path: str,
                       status: ReviewStatus | None = None) -> None:
        with self._lock:
            if status is None:
                self._conn.execute(
                    "UPDATE images SET label_path=? WHERE id=?", (label_path, int(image_id))
                )
            else:
                self._conn.execute(
                    "UPDATE images SET label_path=?, review_status=? WHERE id=?",
                    (label_path, status.value, int(image_id)),
                )
            self._conn.commit()

    def mark_duplicate(self, image_id: int, duplicate_of: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE images SET exact_dup_of=? WHERE id=?", (int(duplicate_of), int(image_id))
            )
            self._conn.commit()

    def mark_similar(self, image_id: int, group: int, similar_to: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE images SET similar_group=?, similar_to=? WHERE id=?",
                (int(group), int(similar_to), int(image_id)),
            )
            self._conn.commit()

    def next_similar_group(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(similar_group), 0) + 1 AS n FROM images"
            ).fetchone()
        return int(row["n"] or 1)

    def delete_image(self, image_id: int) -> sqlite3.Row | None:
        """只删索引，不删文件。文件删除由上层显式决定（且首版只允许手动删单张）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM images WHERE id=?", (int(image_id),)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM images WHERE id=?", (int(image_id),))
            self._conn.commit()
        return row

    def delete_images(self, image_ids: Iterable[int]) -> list[sqlite3.Row]:
        """批量删索引，不删文件。返回被删的行（含 rel_path，供上层处理文件）。"""
        rows: list[sqlite3.Row] = []
        with self._lock:
            for image_id in dict.fromkeys(int(i) for i in image_ids):
                row = self._conn.execute(
                    "SELECT * FROM images WHERE id=?", (image_id,)
                ).fetchone()
                if row is None:
                    continue
                self._conn.execute("DELETE FROM images WHERE id=?", (image_id,))
                rows.append(row)
            self._conn.commit()
        return rows

    def count_images(self, session_uid: str | None = None) -> int:
        with self._lock:
            if session_uid:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM images WHERE session_uid=?", (session_uid,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM images").fetchone()
        return int(row["n"])

    def status_counts(self, session_uid: str | None = None) -> dict[str, int]:
        sql = "SELECT review_status, COUNT(*) AS n FROM images"
        params: list[Any] = []
        if session_uid:
            sql += " WHERE session_uid=?"
            params.append(session_uid)
        sql += " GROUP BY review_status"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        counts = {status.value: 0 for status in ReviewStatus}
        for row in rows:
            counts[row["review_status"]] = int(row["n"])
        return counts

    def images_by_session(self, session_uid: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM images WHERE session_uid=? ORDER BY id ASC", (session_uid,)
            ).fetchall()

    # ---- 类别 -----------------------------------------------------------
    def set_classes(self, names: Sequence[str]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM classes")
            self._conn.executemany(
                "INSERT INTO classes(class_id, name) VALUES(?,?)",
                [(index, name) for index, name in enumerate(names)],
            )
            self._conn.commit()

    def get_classes(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM classes ORDER BY class_id ASC"
            ).fetchall()
        return [r["name"] for r in rows]

    # ---- 导入 / 导出记录 ------------------------------------------------
    def record_import(self, **kwargs: Any) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO label_imports(
                    imported_at, source_path, fmt, n_files, n_boxes,
                    n_matched, n_unmatched, n_rejected, problems
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    kwargs.get("imported_at", datetime.now().isoformat(timespec="seconds")),
                    kwargs.get("source_path", ""), kwargs.get("fmt", "yolo"),
                    int(kwargs.get("n_files", 0)), int(kwargs.get("n_boxes", 0)),
                    int(kwargs.get("n_matched", 0)), int(kwargs.get("n_unmatched", 0)),
                    int(kwargs.get("n_rejected", 0)), kwargs.get("problems", ""),
                ),
            )
            self._conn.commit()

    def record_export(self, **kwargs: Any) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO exports(
                    export_uid, created_at, out_dir, class_map, split_seed,
                    n_train, n_val, n_test, n_empty, report
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    kwargs["export_uid"],
                    kwargs.get("created_at", datetime.now().isoformat(timespec="seconds")),
                    kwargs.get("out_dir", ""), kwargs.get("class_map", ""),
                    int(kwargs.get("split_seed", 0)),
                    int(kwargs.get("n_train", 0)), int(kwargs.get("n_val", 0)),
                    int(kwargs.get("n_test", 0)), int(kwargs.get("n_empty", 0)),
                    kwargs.get("report", ""),
                ),
            )
            self._conn.commit()

    def list_exports(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM exports ORDER BY created_at DESC"
            ).fetchall()

    # ---- 维护 -----------------------------------------------------------
    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.commit()

    def integrity_check(self) -> str:
        with self._lock:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"


# --------------------------------------------------------------------------
# 崩溃恢复
# --------------------------------------------------------------------------

@dataclass(slots=True)
class RecoveryReport:
    temp_files_removed: list[str] = field(default_factory=list)
    orphans_registered: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (
            self.temp_files_removed or self.orphans_registered
            or self.missing_files or self.unreadable
        )

    def summary(self) -> str:
        parts = []
        if self.temp_files_removed:
            parts.append(f"清理残留临时文件 {len(self.temp_files_removed)} 个")
        if self.orphans_registered:
            parts.append(f"补登记未被索引的原图 {len(self.orphans_registered)} 张")
        if self.missing_files:
            parts.append(f"索引中 {len(self.missing_files)} 张图文件已丢失")
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} 个文件无法读取")
        return "；".join(parts) if parts else "无需要恢复的内容"


def recover_orphans(root: Path, db: Database, *, delete_temp: bool = False) -> RecoveryReport:
    """扫描保存目录，处理崩溃残留与未登记原图（方案 §5.2）。

    * `.part` 临时文件：说明写盘过程中断了。默认**保留**并在报告中列出，
      由用户决定是否删除；只有 ``delete_temp=True`` 才真正删除。
      这是刻意的：临时文件里可能正好是唯一一份那帧画面。
    * 文件名符合命名规则但数据库里没有记录的 PNG：补登记，标为待审核。
    * 数据库里有、磁盘上没有的记录：列出来，不自动删记录（可能只是目录没同步）。
    """
    from .naming import TMP_SUFFIX, is_echo_filename, parse_filename

    report = RecoveryReport()
    images_root = root
    if not images_root.exists():
        return report

    known = {
        row["rel_path"]
        for row in db.query_images()
    }

    for path in sorted(images_root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover
            continue

        if name.endswith(TMP_SUFFIX) or name.endswith(".tmp"):
            report.temp_files_removed.append(rel)
            if delete_temp:
                try:
                    path.unlink()
                except OSError as exc:
                    log.warning("删除临时文件失败 %s：%s", path, exc)
            continue

        if not is_echo_filename(name):
            continue
        if rel in known:
            continue

        parsed = parse_filename(name)
        if parsed is None:
            report.unreadable.append(rel)
            continue
        try:
            record = ImageRecord(
                rel_path=rel,
                session_uid=parsed["session_uid"],
                captured_at=parsed["captured_at"],
                trigger_kind=TriggerKind.TIMER.value,
                notes="崩溃恢复：由文件名重建索引",
            )
            db.insert_image(record)
            report.orphans_registered.append(rel)
        except sqlite3.Error as exc:  # pragma: no cover
            log.warning("补登记 %s 失败：%s", rel, exc)
            report.unreadable.append(rel)

    for rel in sorted(known):
        if not (root / rel).exists():
            report.missing_files.append(rel)

    return report
