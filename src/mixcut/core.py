from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class Blocked(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int = 3):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: Path) -> Any:
    def invalid(value: str) -> None:
        raise Blocked("NONFINITE", f"Invalid JSON constant: {value}", 2)

    return json.loads(path.read_text(), parse_constant=invalid)


def atomic_write(path: Path, value: Any) -> None:
    data = canonical(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextlib.contextmanager
def writer_lock(workspace: Path, *, wait_seconds: float = 0) -> Iterator[None]:
    path = workspace / "ledger/writer.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as e:
                if time.monotonic() >= deadline:
                    raise Blocked(
                        "RUNNER_BUSY", "正在处理另一项操作，本次尚未执行，请稍后再点"
                    ) from e
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        try:
            f.seek(0)
            f.truncate()
            f.write(json.dumps({"pid": os.getpid(), "at": now()}))
            f.flush()
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def resolve_media(roots: list[dict], root_id: str, relative: str) -> Path:
    matches = [Path(r["path"]).resolve() for r in roots if r["root_id"] == root_id]
    if len(matches) != 1:
        raise Blocked("ROOT_UNKNOWN", "Expected one explicitly authorized root")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts or "\\" in relative:
        raise Blocked("INPUT_OUTSIDE_ROOT", relative, 2)
    path = (matches[0] / raw).resolve(strict=True)
    if not path.is_relative_to(matches[0]) or not path.is_file():
        raise Blocked("INPUT_OUTSIDE_ROOT", relative, 2)
    return path


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            result.append(json.loads(line))
        except ValueError as e:
            # Preserve the original; never silently truncate a damaged ledger.
            raise Blocked("CORRUPT_JSONL", f"{path}:{i}; original preserved", 4) from e
    return result


def safe_id(value: str) -> str:
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise Blocked("UNSAFE_ID", "Business IDs must be safe single path components", 2)
    return value
