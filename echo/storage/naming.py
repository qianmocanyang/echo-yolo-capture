"""文件命名规则。

方案 §5.2 的示例：``session_20260913_001_021530123_000042.png``

拆开是四段：

| 段 | 含义 | 示例 |
| --- | --- | --- |
| `session_20260913_001` | 批次标识：创建日期 + 当天第几次批次 | `session_20260913_001` |
| `021530123` | 截图时刻 HHMMSSmmm | 02:15:30.123 |
| `000042` | 批次内递增序号，6 位补零 | 第 42 张 |

设计取舍：
* 时间戳在前、序号在后，这样按文件名排序 ≈ 按采集时间排序，方便人工在资源管理器里浏览。
* 序号 6 位宽足够一个批次放 999999 张，不必加宽也不必中途换宽度（换了会破坏排序）。
* 批次目录与文件名都只含 ASCII，避免不同工具链对中文路径的编码差异；
  保存目录本身允许中文与空格（方案 §5.1）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

SESSION_PREFIX = "session"
INDEX_WIDTH = 6

# 用于识别"这看起来是本工具产生的图"，恢复扫描时使用。
NAME_RE = re.compile(
    r"^session_(?P<day>\d{8})_(?P<seq>\d{3,})_(?P<time>\d{9})_(?P<index>\d{6})\.png$"
)
TMP_SUFFIX = ".part"


@dataclass(frozen=True, slots=True)
class SessionId:
    """一个采集批次的标识，例如 `session_20260913_001`。"""

    uid: str
    day: str
    sequence: int

    @staticmethod
    def create(day: datetime, sequence: int) -> "SessionId":
        uid = f"{SESSION_PREFIX}_{day:%Y%m%d}_{sequence:03d}"
        return SessionId(uid=uid, day=f"{day:%Y%m%d}", sequence=sequence)

    @staticmethod
    def parse(uid: str) -> "SessionId | None":
        match = re.match(r"^session_(\d{8})_(\d{3,})$", uid or "")
        if not match:
            return None
        return SessionId(uid=uid, day=match.group(1), sequence=int(match.group(2)))

    @property
    def pretty(self) -> str:
        try:
            day = datetime.strptime(self.day, "%Y%m%d")
        except ValueError:
            return self.uid
        return f"{day:%Y-%m-%d} 第 {self.sequence:03d} 批"


def next_session_id(existing_uids: list[str], now: datetime) -> SessionId:
    """给定已有批次列表，生成当天的下一个批次号。"""
    day = f"{now:%Y%m%d}"
    today = [
        parsed.sequence
        for parsed in (SessionId.parse(uid) for uid in existing_uids)
        if parsed is not None and parsed.day == day
    ]
    return SessionId.create(now, (max(today) + 1) if today else 1)


def image_filename(session: SessionId, captured_at: datetime, index: int) -> str:
    stamp = f"{captured_at:%H%M%S}{captured_at.microsecond // 1000:03d}"
    return f"{session.uid}_{stamp}_{index:0{INDEX_WIDTH}d}.png"


def is_echo_filename(name: str) -> bool:
    return bool(NAME_RE.match(name))


def parse_filename(name: str) -> dict | None:
    """反向解析文件名。解析失败返回 None（不猜）。"""
    match = NAME_RE.match(name)
    if not match:
        return None
    raw = f"{match.group('day')}{match.group('time')}"
    try:
        captured = datetime.strptime(raw, "%Y%m%d%H%M%S%f")
    except ValueError:
        return None
    return {
        "session_uid": f"{SESSION_PREFIX}_{match.group('day')}_{match.group('seq')}",
        "captured_at": captured,
        "index": int(match.group("index")),
    }
