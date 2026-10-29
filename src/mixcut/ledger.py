from __future__ import annotations
from pathlib import Path
from .core import Blocked, atomic_write, digest, load, file_hash, now


def event_key(task: str, revision: int, clip: str) -> str:
    import hashlib

    return hashlib.sha256(f"{task}:{revision}:{clip}".encode()).hexdigest()


def reserve(
    workspace: Path, batch: str, task: str, revision: int, plan_sha: str, clips: list[str]
) -> None:
    for clip in clips:
        key = event_key(task, revision, clip)
        p = workspace / "ledger/reservations" / f"{digest([batch, key])}.json"
        if p.exists():
            old = load(p)
            if old["plan_sha256"] != plan_sha and old["state"] == "reserved":
                raise Blocked(
                    "RESERVATION_CONFLICT", "Release old plan explicitly before rematching", 6
                )
        atomic_write(
            p,
            {
                "batch_id": batch,
                "task_id": task,
                "script_revision": revision,
                "clip_id": clip,
                "event_key": key,
                "plan_sha256": plan_sha,
                "state": "reserved",
            },
        )


def commit_usage(
    workspace: Path,
    task: str,
    revision: int,
    build: str,
    clips: list[str],
    candidate: Path,
    qc: dict,
) -> None:
    if (
        not candidate.is_file()
        or qc.get("status") != "passed"
        or qc.get("output_sha256") != file_hash(candidate)
    ):
        raise Blocked("QC_REQUIRED", "Only verified real output can create usage events")
    for clip in set(clips):
        key = event_key(task, revision, clip)
        p = workspace / "ledger/events" / f"{key}.json"
        old = (
            load(p)
            if p.exists()
            else {
                "event_key": key,
                "task_id": task,
                "script_revision": revision,
                "clip_id": clip,
                "builds": {},
                "first_at": now(),
            }
        )
        old["builds"][build] = qc["output_sha256"]
        atomic_write(p, old)
    # Event-first commit is recoverable: load counts exclude matching reservations.
    for p in (workspace / "ledger/reservations").glob("*.json"):
        r = load(p)
        if (workspace / "ledger/events" / f"{r['event_key']}.json").exists():
            r["state"] = "consumed"
            atomic_write(p, r)


def counts(workspace: Path, include_reserved: bool = False) -> dict:
    events = {p.stem: load(p) for p in (workspace / "ledger/events").glob("*.json")}
    result: dict[str, int] = {}
    for e in events.values():
        result[e["clip_id"]] = result.get(e["clip_id"], 0) + 1
    if include_reserved:
        seen = set(events)
        for p in (workspace / "ledger/reservations").glob("*.json"):
            r = load(p)
            if r["state"] == "reserved" and r["event_key"] not in seen:
                seen.add(r["event_key"])
                result[r["clip_id"]] = result.get(r["clip_id"], 0) + 1
    return result
