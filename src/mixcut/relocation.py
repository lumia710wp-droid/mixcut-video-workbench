"""Resolve migrated local files without rewriting immutable approval evidence."""

from pathlib import Path

from .core import Blocked, file_hash, load


def runtime_path(workspace: Path, raw: str) -> Path:
    path = Path(raw)
    mapping = workspace / "config/relocation.local.json"
    if not path.is_absolute() or not mapping.exists():
        return path
    previous = Path(load(mapping)["previous_workspace"])
    if not path.is_relative_to(previous):
        return path
    relocated = (workspace / path.relative_to(previous)).resolve()
    if not relocated.is_relative_to(workspace.resolve()):
        raise Blocked("RELOCATION_ESCAPE", "Migrated path escapes this workspace", 6)
    return relocated


def preserved_timing(workspace: Path, path: Path, outline: dict) -> bool:
    """Only a byte-identical, already approved migration snapshot can be reused."""
    mapping = workspace / "config/relocation.local.json"
    if not mapping.exists():
        return False
    approval = outline.get("approval") or {}
    if approval.get("approved_content_sha256") != outline.get("content_sha256"):
        return False
    key = str(path.resolve().relative_to(workspace.resolve()))
    pinned = load(mapping).get("preserved_timings", {}).get(key, {})
    return bool(
        pinned.get("script_sha256") == outline.get("content_sha256")
        and pinned.get("file_sha256") == file_hash(path)
    )
