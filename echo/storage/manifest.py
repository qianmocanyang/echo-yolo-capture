"""可移植的图片元数据清单（JSONL）。

方案 §5.2 把 `metadata/manifest.jsonl` 定位为「可移植的图片元数据记录」。
它的价值在于：即使 `dataset.db` 损坏、或者整份数据被搬到别的机器上，
这份纯文本清单仍然能被脚本直接读取。所以：

* 一行一条 JSON，追加写，不做全文件重写，崩溃最多丢最后一行。
* 字段名与数据库列名保持一致，导入导出时不需要做映射。
* 单独的文件锁，避免写线程与恢复扫描同时追加导致行内交错。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..logging_setup import get_logger

log = get_logger("storage.manifest")


class ManifestWriter:
    """追加写的 JSONL 清单。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                # 清单写失败不应该让图片保存失败：图片本身已经落盘了，索引在数据库里。
                # 这里只记日志，由上层把"清单可能不完整"的位置记进状态栏。
                log.warning("写入 manifest 失败（图片本身已保存）：%s", exc)

    def append_record(self, record: Any, **extra: Any) -> None:
        try:
            payload = asdict(record)
        except TypeError:  # pragma: no cover
            payload = dict(record)
        for key, value in payload.items():
            if isinstance(value, datetime):
                payload[key] = value.isoformat(timespec="milliseconds")
        payload.update(extra)
        payload.setdefault("manifest_written_at", datetime.now().isoformat(timespec="milliseconds"))
        self.append(payload)


def read_manifest(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读取清单，跳过坏行（不要因为一行坏了就整份读不出来）。"""
    path = Path(path)
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                log.debug("manifest 第 %d 行不是合法 JSON，已跳过", lineno)


def count_manifest(path: Path) -> int:
    return sum(1 for _ in read_manifest(path))
