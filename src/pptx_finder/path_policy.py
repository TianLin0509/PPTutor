"""Shared path policy for scanner, live watcher, index, and snapshots.

Only generated output folders of recognisable software projects are excluded.
A generic user folder named ``dist`` or ``artifacts`` remains searchable.  A
user can also explicitly select a generated output folder as an index root; in
that case the same opt-in is honoured by every consumer of this policy.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Iterable

_PROJECT_MARKERS = (
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    ".git",
)
_PROJECT_OUTPUT_DIRS = frozenset({"dist", "build", "out", "target", "artifacts"})
_TRUE_VALUES = {"1", "true", "yes", "on"}
_MARKER_CACHE_TTL_SEC = 2.0

# root key -> (marker path which proved the root, last verification time)
_known_project_roots: dict[str, tuple[str, float]] = {}
_known_project_roots_lock = threading.Lock()


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _under(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    try:
        return os.path.commonpath([_norm(path), _norm(root)]) == _norm(root)
    except ValueError:
        return False


def _include_all_project_outputs() -> bool:
    # Keep the original variable as a compatibility alias for existing users.
    for name in ("PPTUTOR_INCLUDE_PROJECT_OUTPUTS", "PPTUTOR_INCLUDE_PROJECT_DIST"):
        if os.environ.get(name, "").strip().lower() in _TRUE_VALUES:
            return True
    return False


def _project_root_has_marker(root: Path) -> bool:
    key = _norm(root)
    now = time.monotonic()
    with _known_project_roots_lock:
        cached = _known_project_roots.get(key)
    if cached is not None:
        marker_path, checked_at = cached
        if now - checked_at < _MARKER_CACHE_TTL_SEC:
            return True
        # Positive cache entries are short-lived.  This preserves the hot-path
        # benefit for event bursts but notices marker deletion without restart.
        if Path(marker_path).exists():
            with _known_project_roots_lock:
                _known_project_roots[key] = (marker_path, now)
            return True
        with _known_project_roots_lock:
            _known_project_roots.pop(key, None)

    for marker in _PROJECT_MARKERS:
        marker_path = root / marker
        if marker_path.exists():
            with _known_project_roots_lock:
                _known_project_roots[key] = (str(marker_path), now)
            return True
    # Do not cache negatives: a marker created after startup must be noticed.
    return False


def _detected_project_output_root(path: str | os.PathLike[str]) -> Path | None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    # Prefer the nearest output directory for nested projects.
    for index in range(len(parts) - 1, 0, -1):
        if parts[index].casefold() not in _PROJECT_OUTPUT_DIRS:
            continue
        project_root = Path(*parts[:index])
        if _project_root_has_marker(project_root):
            return Path(*parts[:index + 1])
    return None


def explicit_project_output_roots(
    roots: Iterable[str | os.PathLike[str]] | None,
) -> tuple[str, ...]:
    """Return explicit roots which themselves sit inside project outputs."""
    selected: list[str] = []
    for root in roots or ():
        if _detected_project_output_root(root) is None:
            continue
        normal = _norm(root)
        if normal not in selected:
            selected.append(normal)
    return tuple(selected)


def project_output_root(
    path: str | os.PathLike[str],
    *,
    explicit_output_roots: Iterable[str | os.PathLike[str]] = (),
) -> Path | None:
    """Return the containing generated project-output root, unless opted in."""
    if _include_all_project_outputs():
        return None
    output_root = _detected_project_output_root(path)
    if output_root is None:
        return None
    if any(_under(path, root) for root in explicit_output_roots):
        return None
    return output_root


def is_project_output_path(
    path: str | os.PathLike[str],
    *,
    explicit_output_roots: Iterable[str | os.PathLike[str]] = (),
) -> bool:
    return project_output_root(
        path,
        explicit_output_roots=explicit_output_roots,
    ) is not None


# Backward-compatible names used by tests and any local integrations.  They now
# cover all recognised project output folders, not only a directory named dist.
def project_dist_root(path: str | os.PathLike[str]) -> Path | None:
    return project_output_root(path)


def is_project_dist_path(path: str | os.PathLike[str]) -> bool:
    return is_project_output_path(path)
