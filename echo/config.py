"""配置模型与持久化。

方案 §9.1 给出的 JSON 结构就是这里的 schema。设计要点：

* 配置只在主线程读写；跨线程到达采集线程的关键参数（选区、间隔、采集源）
  通过 pipeline 的控制命令在安全边界生效（方案 §8.3），不走共享内存。
* `config_version` 用于迁移。新增字段一律给默认值，保证旧配置能直接升级。
* 写盘同样是原子写：临时文件 → 同目录重命名，避免断电留下半个 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import CONFIG_VERSION
from .logging_setup import get_logger
from .paths import config_path
from .region import RegionMode

log = get_logger("config")


# --------------------------------------------------------------------------
# 分区 dataclass
# --------------------------------------------------------------------------

@dataclass
class CaptureConfig:
    source_type: str = "monitor"          # monitor | window
    # 采集源的稳定标识。句柄不可跨重启复用（方案 §9.1），所以存的是"身份串"，
    # 启动时用它重新发现句柄；matched=False 时要求用户重新确认。
    source_identity: str = ""
    monitor_device: str = ""
    backend: str = "dxgi"                 # dxgi | wgc
    region_mode: str = RegionMode.CENTER.value
    width: int = 320
    height: int = 320
    offset_x: int = 0
    offset_y: int = 0
    ratio_locked: bool = False
    interval_ms: int = 500
    burst_fps: int = 8
    burst_count: int = 20
    allow_game_unfocused: bool = True
    preview_show_source: bool = False


@dataclass
class HotkeyConfig:
    capture_once: str = "F8"
    toggle_auto_capture: str = "F9"
    toggle_window: str = "Ctrl+Alt+E"


@dataclass
class StorageConfig:
    save_directory: str = ""
    format: str = "png"
    create_session_directory: bool = True


@dataclass
class QualityConfig:
    exact_dedup_auto_capture: bool = True
    similarity_action: str = "mark"       # off | mark
    similarity_threshold: int = 6         # dHash 汉明距离阈值
    manual_capture_bypass_dedup: bool = True
    flag_dark: bool = True
    flag_blur: bool = True


@dataclass
class UIConfig:
    theme: str = "dark"
    close_to_tray: bool = True
    success_notification: bool = False
    sound_feedback: bool = False
    tray_notify_tip_shown: bool = False


@dataclass
class DatasetConfig:
    class_names: list[str] = field(default_factory=list)
    split_train: float = 0.8
    split_val: float = 0.1
    split_test: float = 0.1
    split_seed: int = 20260913


@dataclass
class AppConfig:
    config_version: int = CONFIG_VERSION
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    # 非用户可编辑，记录上次使用的批次，便于重启后接着往里写
    last_session_uid: str = ""

    # ---- 便捷访问 -------------------------------------------------------
    @property
    def save_directory(self) -> Path | None:
        raw = (self.storage.save_directory or "").strip()
        if not raw:
            return None
        return Path(raw)

    def region(self) -> tuple[int, int, int, int]:
        c = self.capture
        return (c.offset_x, c.offset_y, c.width, c.height)

    def set_region(self, x: int, y: int, width: int, height: int) -> None:
        self.capture.offset_x = int(x)
        self.capture.offset_y = int(y)
        self.capture.width = int(width)
        self.capture.height = int(height)


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------

_SECTIONS = {
    "capture": CaptureConfig,
    "hotkeys": HotkeyConfig,
    "storage": StorageConfig,
    "quality": QualityConfig,
    "ui": UIConfig,
    "dataset": DatasetConfig,
}


def _coerce_scalar(value: Any, default: Any) -> Any:
    """尽最大努力把读到的东西变成默认值同类型的值。

    配置是用户可手改的文本文件，坏值不能让应用起不来，也不能让它悄悄带错。
    """
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(value, (int, float)):
            return bool(value)
        return default
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return value if isinstance(value, str) else ("" if value is None else str(value))
    if isinstance(default, list):
        return list(value) if isinstance(value, (list, tuple)) else default
    return value if value is not None else default


def config_from_dict(data: dict[str, Any]) -> AppConfig:
    """从字典构造配置，未知字段忽略、类型错误回退默认值。"""
    cfg = AppConfig()
    if not isinstance(data, dict):
        return cfg

    if isinstance(data.get("config_version"), (int, float, str)):
        try:
            cfg.config_version = int(data["config_version"])
        except (TypeError, ValueError):
            cfg.config_version = CONFIG_VERSION
    if isinstance(data.get("last_session_uid"), str):
        cfg.last_session_uid = data["last_session_uid"]

    for section_name, cls in _SECTIONS.items():
        raw = data.get(section_name)
        if not isinstance(raw, dict):
            continue
        target = getattr(cfg, section_name)
        valid = {f.name: f for f in fields(cls)}
        for key, value in raw.items():
            spec = valid.get(key)
            if spec is None:
                continue  # 未知字段：忽略，不报错，方便回滚版本
            default = getattr(target, key)
            setattr(target, key, _coerce_scalar(value, default))

    _sanitize(cfg)
    return cfg


def _sanitize(cfg: AppConfig) -> None:
    """把明显越界的值拉回可用范围。"""
    c = cfg.capture
    if c.source_type not in {"monitor", "window"}:
        c.source_type = "monitor"
    if c.backend not in {"dxgi", "wgc"}:
        c.backend = "dxgi"
    if c.region_mode not in {m.value for m in RegionMode}:
        c.region_mode = RegionMode.CENTER.value
    c.width = max(16, min(int(c.width), 16384))
    c.height = max(16, min(int(c.height), 16384))
    c.offset_x = max(0, int(c.offset_x))
    c.offset_y = max(0, int(c.offset_y))
    c.interval_ms = max(33, min(int(c.interval_ms), 60_000))
    c.burst_fps = max(1, min(int(c.burst_fps), 60))
    c.burst_count = max(1, min(int(c.burst_count), 2000))

    if cfg.storage.format != "png":
        cfg.storage.format = "png"  # 首版只写 PNG，避免无损性被破坏

    q = cfg.quality
    if q.similarity_action not in {"off", "mark"}:
        q.similarity_action = "mark"
    q.similarity_threshold = max(0, min(int(q.similarity_threshold), 32))

    d = cfg.dataset
    total = (d.split_train or 0) + (d.split_val or 0) + (d.split_test or 0)
    if total <= 0:
        d.split_train, d.split_val, d.split_test = 0.8, 0.1, 0.1
    else:
        d.split_train = d.split_train / total
        d.split_val = d.split_val / total
        d.split_test = d.split_test / total
    d.class_names = [str(n).strip() for n in d.class_names if str(n).strip()]


def config_to_dict(cfg: AppConfig) -> dict[str, Any]:
    data = asdict(cfg)
    data["config_version"] = CONFIG_VERSION
    return data


# --------------------------------------------------------------------------
# 存取
# --------------------------------------------------------------------------

def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """临时文件 → 同目录重命名。方案 §5.2 对图片用的是同一套思路。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_config(path: Path | None = None) -> tuple[AppConfig, list[str]]:
    """读取配置。返回 (配置, 迁移或修复说明)。"""
    target = path or config_path()
    notes: list[str] = []
    if not target.exists():
        return AppConfig(), notes

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("配置文件无法解析（%s），已回退默认配置", exc)
        notes.append(f"配置文件损坏，已使用默认配置：{exc}")
        return AppConfig(), notes

    version = raw.get("config_version", 0) if isinstance(raw, dict) else 0
    cfg = config_from_dict(raw if isinstance(raw, dict) else {})

    if version != CONFIG_VERSION:
        notes.append(f"配置已从 v{version} 迁移到 v{CONFIG_VERSION}")

    if cfg.hotkeys.capture_once.strip().upper() in {"F9", "CTRL+ALT+E"}:
        pass  # 录制阶段才校验冲突，这里不做静默改写
    return cfg, notes


def save_config(cfg: AppConfig, path: Path | None = None) -> None:
    target = path or config_path()
    atomic_write_json(target, config_to_dict(cfg))
