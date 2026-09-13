"""存储层：目录布局、原子写盘、SQLite 索引、可移植清单。"""

from .db import (
    Database,
    ImageRecord,
    RecoveryReport,
    ReviewStatus,
    SaveResult,
    TriggerKind,
    recover_orphans,
)
from .manifest import ManifestWriter, count_manifest, read_manifest
from .naming import SessionId, image_filename, is_echo_filename, next_session_id, parse_filename
from .writer import (
    MIN_FREE_BYTES,
    PNG_COMPRESSION,
    ProjectLayout,
    SaveError,
    atomic_write_bytes,
    check_directory_writable,
    check_space,
    encode_png,
    free_space_bytes,
    read_image,
    read_image_size,
    save_png,
)

__all__ = [
    "Database", "ImageRecord", "RecoveryReport", "ReviewStatus", "SaveResult",
    "TriggerKind", "recover_orphans",
    "ManifestWriter", "count_manifest", "read_manifest",
    "SessionId", "image_filename", "is_echo_filename", "next_session_id", "parse_filename",
    "ProjectLayout", "SaveError", "atomic_write_bytes", "check_directory_writable",
    "check_space", "encode_png", "free_space_bytes", "read_image", "read_image_size",
    "save_png", "MIN_FREE_BYTES", "PNG_COMPRESSION",
]
