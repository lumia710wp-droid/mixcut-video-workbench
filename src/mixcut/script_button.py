"""Feishu button queue -> one explicit model request -> reviewable script.

No public listener, scheduler, media operation or approval side effect.
"""

from __future__ import annotations

import json
import fcntl
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .core import Blocked, atomic_write, digest, load, now, safe_id
from .deepseek import generate_outline, reconcile_outline, load_model_config
from .feishu import cell_equal, invoke, program_patch, pull_rows, unwrap
from .script_input import active_request


def service_control(workspace: Path, action: str) -> dict:
    """Project-local process only; no launchd/global configuration changes."""
    ensure_worker_host(workspace, action)
    pid_path = workspace / "ledger/script_button_worker.json"
    stop_path = workspace / "ledger/script_button.stop"
    info = load(pid_path) if pid_path.exists() else {}
    pid = info.get("pid")
    running = False
    # The worker holds this project-local lock for its entire lifetime. Avoid
    # process-list permissions and PID reuse when detecting an existing worker.
    with (workspace / "ledger/script_button_worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        except BlockingIOError:
            running = True
    if action == "status":
        status_path = workspace / "reports/script_button_status.json"
        return {
            "running": running,
            "pid": pid if running else None,
            "last_tick": load(status_path) if status_path.exists() else None,
        }
    if action == "stop":
        atomic_write(stop_path, {"at": now()})
        return {"status": "stop_requested", "running": running}
    if running:
        return {"status": "already_running", "pid": pid}
    load_model_config(workspace)  # Local validation, no provider request.
    stop_path.unlink(missing_ok=True)
    log_path = workspace / "reports/script_button_worker.log"
    env = {**os.environ, "PYTHONPATH": str(workspace / "src")}
    with log_path.open("ab") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mixcut",
                "--workspace",
                str(workspace),
                "--json",
                "script-button",
                "watch",
            ],
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    info = {"at": now(), "pid": child.pid, "log": str(log_path)}
    atomic_write(pid_path, info)
    return {"status": "started", **info}


def ensure_worker_host(workspace: Path, action: str) -> None:
    role = workspace / "config/runtime_role.local.json"
    if action in {"start", "watch", "once"} and role.exists():
        if load(role).get("role") == "access_client":
            raise Blocked("WORKSPACE_RELOCATED", "此实例不是配置的后台写入方，请勿启动本机worker", 3)


def default_task_id(record_id: str) -> str:
    return "VIDEO_" + digest({"record_id": record_id})[:16].upper()


def process_once(
    workspace: Path, *, call: Callable | None = None, generator=None, requested_task_id=None
) -> dict:
    """Caller holds writer_lock. Reads only this project's task table.

    Each request requires the explicit queued value written by the button.
    Unknown writes are read back on later ticks, never blindly repeated.
    """
    mapping = load(workspace / "config/feishu_mapping.local.json")
    table = mapping["tables"]["tasks"]
    fields = table["field_ids"]
    if "generation_request" not in fields:
        raise Blocked("BUTTON_NOT_CONFIGURED", "Configure the Feishu button first", 3)
    remote = call or (lambda args: invoke(args, workspace))
    from .script_review_queue import process_reviews

    review_report = process_reviews(workspace, mapping, remote)
    common = ["--base-token", mapping["base_token"], "--table-id", table["table_id"]]
    rows = pull_rows(remote, mapping["base_token"], table["table_id"])
    by_id = {row["record_id"]: row for row in rows}
    journal = workspace / "ledger/script_button"
    journal.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "at": now(),
        "queued": 0,
        "handled": [],
        "pending_sync": [],
        "errors": [],
        "script_reviews": review_report,
    }

    def decoded(row):
        return {key: row["fields"].get(fid) for key, fid in fields.items()}

    def fresh_request(record_id, task_id):
        raw = unwrap(remote(["+record-get", *common, "--record-id", record_id, "--format", "json"]))
        ids = raw.get("record_id_list", [])
        columns = raw.get("field_id_list") or raw.get("fields", [])
        matrix = raw.get("data", [])
        if ids != [record_id] or len(matrix) != 1:
            raise Blocked("BUTTON_ROW_MISSING", "The requested task row is no longer readable", 6)
        f = decoded({"fields": dict(zip(columns, matrix[0], strict=True))})
        actual_id = f.get("task_id") or default_task_id(record_id)
        if actual_id != task_id:
            raise Blocked("SCRIPT_INPUT_STALE", "Task identity changed after clicking", 6)
        f["task_id"] = actual_id
        return active_request(workspace, f)

    def reconcile(path, receipt):
        row = by_id.get(receipt["record_id"])
        patch = receipt.get("patch", {})
        if (
            row
            and patch
            and all(cell_equal(row["fields"].get(fields[k]), v) for k, v in patch.items())
        ):
            receipt.update(status="synced", verified_at=now())
            atomic_write(path, receipt)
            for event_id in receipt.get("event_ids", []):
                event_path = workspace / "ledger/outbox" / f"{event_id}.json"
                event = load(event_path)
                event.update(status="synced", verified_at=now(), readback_evidence=str(path))
                atomic_write(event_path, event)
            return True
        return False

    pending_records = set()
    for path in journal.glob("*.json"):
        receipt = load(path)
        if receipt["status"] in {"write_unknown", "acknowledged"}:
            if not reconcile(path, receipt):
                pending_records.add(receipt["record_id"])
                report["pending_sync"].append(str(path))

    # A duplicate business key is a conflict, never a second generation target.
    task_ids = [decoded(r).get("task_id") for r in rows]
    for row in rows:
        f = decoded(row)
        if f.get("generation_request") != "queued" and not (
            requested_task_id is not None and f.get("task_id") == requested_task_id
        ):
            continue
        report["queued"] += 1
        rid = row["record_id"]
        if rid in pending_records:
            continue
        task_id = f.get("task_id") or default_task_id(rid)
        if task_ids.count(task_id) > 1:
            report["errors"].append({"record_id": rid, "code": "DUPLICATE_TASK_ID"})
            continue
        # Also protect an auto ID colliding with another manually entered ID.
        if not f.get("task_id") and task_id in task_ids:
            report["errors"].append({"record_id": rid, "code": "DUPLICATE_TASK_ID"})
            continue
        try:
            safe_id(task_id)
            request = fresh_request(rid, task_id)
        except Blocked as exc:
            # Do not turn malformed inputs into paid requests.
            report["errors"].append({"record_id": rid, "code": exc.code})
            path = journal / f"{digest({'record_id': rid, 'invalid_fields': f})}.json"
            patch = {
                "generation_request": "blocked",
                "script_generation_status": "waiting_input",
                "error_summary": f"{exc.code}: {exc}",
            }
            receipt = {"at": now(), "record_id": rid, "patch": patch, "status": "write_unknown"}
            atomic_write(path, receipt)
            ack = remote(
                [
                    "+record-batch-update",
                    *common,
                    "--json",
                    json.dumps(
                        {"update_records": {rid: {fields[k]: v for k, v in patch.items()}}},
                        ensure_ascii=False,
                    ),
                ]
            )
            receipt.update(ack=ack, status="acknowledged")
            atomic_write(path, receipt)
            continue
        key = digest({"record_id": rid, "request_sha256": request["request_sha256"]})
        path = journal / f"{key}.json"
        receipt = {
            "at": now(),
            "record_id": rid,
            "task_id": task_id,
            "request_sha256": request["request_sha256"],
            "status": "processing",
        }
        # generate_outline has a persisted dispatch ledger: crash recovery cannot
        # issue a second model call for the same task/input/model combination.
        atomic_write(path, receipt)
        patch = {"task_id": task_id}
        event_ids = []
        try:
            from .script_revision import newest_outline, editable_text

            previous_default = None
            if f.get("script_revision"):
                _, previous = newest_outline(workspace, task_id)
                previous_default = editable_text(previous)
            generate = generator or generate_outline
            config = load_model_config(workspace)
            run_id = (
                "ds_"
                + digest(
                    {
                        "task_id": task_id,
                        "request_sha256": request["request_sha256"],
                        "model": config["model"],
                    }
                )[:20]
            )
            run_path = workspace / "jobs" / task_id / "generation" / f"{run_id}.json"
            if (
                generator is None
                and run_path.exists()
                and load(run_path).get("status") == "output_rejected"
            ):
                # Saved provider text can be revalidated without any second charge.
                generate = reconcile_outline
            result = generate(
                workspace,
                config,
                task_id,
                request_loader=lambda: fresh_request(rid, task_id),
            )
            receipt["result"] = result
            if result["status"] != "already_generated" and "script_edit_draft" in fields:
                _, parent = newest_outline(workspace, task_id)
                draft = decoded(row).get("script_edit_draft")
                if not draft or draft == previous_default:
                    patch["script_edit_draft"] = editable_text(parent)
                patch["script_timing_status"] = (
                    "未校验：审核通过时自动校验本地配音时长；改稿可重新编排。"
                )
            for event_path in (workspace / "ledger/outbox").glob("*.json"):
                event = load(event_path)
                if (
                    event["business_key"] == task_id
                    and event["table"] == "tasks"
                    and event["status"] not in {"synced", "superseded"}
                    and event.get("fields", {}).get("script_input_sha256")
                    == request["request_sha256"]
                    and event.get("fields", {}).get("script_generation_status")
                    in {"generated_pending_review", "retimed_pending_review", "blocked_timing"}
                ):
                    patch.update(event["fields"])
                    event_ids.append(event["id"])
            if result["status"] != "already_generated" and not event_ids:
                raise Blocked("BUTTON_RESULT_MISSING", "Generated output has no sync event", 6)
            # Cached approved scripts retain their existing review state.
            patch["generation_request"] = "completed"
        except Blocked as exc:
            receipt["error"] = {"code": exc.code, "message": str(exc)}
            patch.update(
                generation_request="blocked",
                error_summary=f"{exc.code}: {exc}",
                script_generation_status="blocked_no_automatic_retry",
            )
            report["errors"].append({"record_id": rid, "code": exc.code})
        program_patch({k: v for k, v in patch.items() if k != "script_edit_draft"}, "tasks")
        receipt.update(patch=patch, event_ids=event_ids, status="write_unknown")
        atomic_write(path, receipt)  # Persist intent BEFORE remote mutation.
        payload = {"update_records": {rid: {fields[k]: v for k, v in patch.items()}}}
        ack = remote(
            ["+record-batch-update", *common, "--json", json.dumps(payload, ensure_ascii=False)]
        )
        receipt.update(ack=ack, status="acknowledged", acknowledged_at=now())
        atomic_write(path, receipt)
        if unwrap(ack).get("ignored_fields"):
            report["errors"].append({"record_id": rid, "code": "BUTTON_FIELDS_IGNORED"})
        report["handled"].append({"task_id": task_id, "receipt": str(path)})
    atomic_write(workspace / "reports/script_button_status.json", report)
    return report
