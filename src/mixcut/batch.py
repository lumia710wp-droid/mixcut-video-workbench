"""Single-machine serial scheduling with explicit unknown-result and resume states."""

from __future__ import annotations
import shutil
from pathlib import Path
from typing import Callable
from .core import Blocked, atomic_write, file_hash, load, now, safe_id, writer_lock


def run_serial(
    workspace: Path,
    batch_id: str,
    task_ids: list[str],
    execute: Callable[[str], dict],
    min_free_bytes: int = 512 * 1024 * 1024,
) -> dict:
    safe_id(batch_id)
    if len(task_ids) != len(set(task_ids)) or not 1 <= len(task_ids) <= 30:
        raise Blocked("BATCH_SCOPE", "Require 1-30 unique task IDs", 2)
    for task in task_ids:
        safe_id(task)
    directory = workspace / "batches" / batch_id
    report: dict = {"batch_id": batch_id, "tasks": [], "at": now()}
    with writer_lock(workspace):
        for task in task_ids:
            state_path = directory / "tasks" / f"{task}.json"
            if state_path.exists():
                old = load(state_path)
                if old["status"] == "technical_success":
                    output = Path(old["output_path"])
                    if not output.exists() or file_hash(output) != old["output_sha256"]:
                        raise Blocked("FILE_CHANGED", "Previously successful output has changed", 6)
                    report["tasks"].append(old)
                    continue
                # A failed/unknown run is never silently retried by running the batch again.
                report["tasks"].append(old)
                continue
            if shutil.disk_usage(workspace).free < min_free_bytes:
                report["paused_reason"] = "DISK_LOW"
                break
            intent = {
                "task_id": task,
                "status": "needs_reconcile",
                "render_status": "unknown",
                "qc_status": "not_checked",
                "human_review": "pending",
                "sync_status": "pending",
                "started_at": now(),
            }
            atomic_write(state_path, intent)
            try:
                result = execute(task)
                output = Path(result["output_path"])
                if (
                    result.get("qc_status") != "passed"
                    or result.get("render_status") != "succeeded"
                    or not output.is_file()
                    or file_hash(output) != result.get("output_sha256")
                ):
                    raise Blocked(
                        "QC_FAILED",
                        "Callback did not return a real hash-verified technical output",
                        4,
                    )
                state = {
                    **intent,
                    **result,
                    "status": "technical_success",
                    "human_review": "pending",
                }
            except Blocked as e:
                state = {
                    **intent,
                    "status": "needs_reconcile"
                    if e.exit_code == 5
                    else "blocked"
                    if e.exit_code in {3, 6}
                    else "failed",
                    "error_code": e.code,
                    "error": str(e),
                }
                atomic_write(state_path, state)
                report["tasks"].append(state)
                if e.code in {"AUTH_MISSING", "DISK_LOW", "ENGINE_UNAVAILABLE"}:
                    report["paused_reason"] = e.code
                    break
                continue
            except Exception:
                # Intent deliberately remains unknown; caller must reconcile before recovery.
                raise
            atomic_write(state_path, state)
            report["tasks"].append(state)
        report["technical_success"] = sum(
            s["status"] == "technical_success" for s in report["tasks"]
        )
        report["complete"] = len(report["tasks"]) == len(task_ids) and report[
            "technical_success"
        ] == len(task_ids)
        report["human_approved"] = 0
        atomic_write(directory / "report.json", report)
    return report
