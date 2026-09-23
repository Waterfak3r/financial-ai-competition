"""把相对路径限制在指定根目录内。"""

from __future__ import annotations

import re
from pathlib import Path

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


class PathBoundaryError(ValueError):
    """路径或运行标识越界。"""


def resolve_inside(root: Path, relative: str) -> Path:
    """解析相对路径，并要求结果仍在 root 内。"""

    if not isinstance(relative, str) or relative.strip() == "" or relative != relative.strip():
        raise PathBoundaryError("路径必须是去掉首尾空白的相对路径。")
    if relative.startswith(("/", "\\")) or (len(relative) >= 2 and relative[1] == ":"):
        raise PathBoundaryError("只能使用数据根目录内的相对路径。")
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PathBoundaryError("路径不能包含 . 或 .. 。")
    root_resolved = root.resolve()
    resolved = (root_resolved / Path(*parts)).resolve()
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise PathBoundaryError("路径超出允许的数据目录。")
    return resolved


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None or ".." in run_id:
        raise PathBoundaryError("run_id 不合法。")
    return run_id


def run_directory(runs_root: Path, run_id: str) -> Path:
    checked = validate_run_id(run_id)
    root_resolved = runs_root.resolve()
    resolved = (root_resolved / checked).resolve()
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise PathBoundaryError("run_id 超出运行目录。")
    return resolved
