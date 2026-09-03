"""Attachment path compatibility and containment helpers."""

from __future__ import annotations

import stat
from pathlib import Path

from ..core.errors import StorageError


def ensure_private_directory(path: Path) -> None:
    """Create a private directory without chmodding an unrelated existing parent."""
    try:
        existed = path.exists()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir():
            raise StorageError(f"不是目录: {path}")
        if not existed:
            path.chmod(0o700)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise StorageError(f"目录权限必须为 0700: {path}")
    except OSError as exc:
        raise StorageError(f"无法准备私有目录 {path}: {exc}") from exc


def attachment_path(value: str, root: Path, *, require_file: bool = False) -> Path | None:
    """Resolve absolute and legacy relative rows without escaping the attachment root."""
    root = root.resolve()
    path = Path(value)
    candidates = (
        [path.resolve()] if path.is_absolute() else [(root / path).resolve(), path.resolve()]
    )
    contained = [candidate for candidate in candidates if candidate.is_relative_to(root)]
    existing = next((candidate for candidate in contained if candidate.is_file()), None)
    if existing is not None:
        return existing
    if require_file or not contained:
        return None
    return contained[0]
