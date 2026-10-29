from __future__ import annotations
from pathlib import Path
from .core import Blocked, atomic_write, file_hash, load, now
from .feishu import invoke, pull_rows, index_rows, enqueue
from .review import review_clip, review_script
from .contracts import validate_record
from .script_input import collect_requests, validate_reviewable_outline
from .material_review import effective_fields, reconcile_material_reviews


def sync_pull(workspace: Path, mapping: dict) -> dict:
    def call(argv):
        return invoke(argv, workspace)

    reconcile_material_reviews(workspace, mapping, call)
    snapshots = {}
    for name, table in mapping["tables"].items():
        rows = pull_rows(call, mapping["base_token"], table["table_id"])
        reverse = {v: k for k, v in table["field_ids"].items()}
        snapshots[name] = [
            {
                "record_id": r["record_id"],
                "fields": {reverse[k]: v for k, v in r["fields"].items() if k in reverse},
            }
            for r in rows
        ]
        index_rows(snapshots[name], "clip_id" if name == "clips" else "task_id")
    snapshot_at = now()
    atomic_write(
        workspace / "catalog/feishu_snapshot.json",
        {"at": snapshot_at, "tables": snapshots},
    )
    path = workspace / "catalog/clips.json"
    clips = load(path) if path.exists() else []
    sources = {s["source_id"]: s for s in load(workspace / "catalog/sources.json")}
    tax = load(workspace / "config/taxonomy.json")
    by_key = index_rows(snapshots["clips"], "clip_id")
    errors = []
    updated = []
    for c in clips:
        if c["clip_id"] not in by_key:
            c["review_status"] = "stale"
            c["approval"] = None
            updated.append(c)
            continue
        f = by_key[c["clip_id"]]["fields"]
        for key in ("review_status",):
            if isinstance(f.get(key), list):
                f[key] = f[key][0] if len(f[key]) == 1 else None
        try:
            # Sync imports edits and invalidates stale approvals. Only the
            # authenticated workbench review action can create a new approval.
            r = review_clip(
                c,
                effective_fields(f),
                {"action": "none"},
                tax,
                sources[c["source_id"]]["duration_sec"],
            )
        except Blocked as e:
            # Fail closed while preserving evidence of prior version.
            r = dict(c)
            r["review_status"] = "stale"
            r["approval"] = None
            errors.append({"clip_id": c["clip_id"], "code": e.code, "message": str(e)})
        updated.append(r)
        enqueue(
            workspace,
            "clips",
            r["clip_id"],
            {
                "content_sha256": r["content_sha256"],
                "review_status": r["review_status"],
                "approved_content_sha256": (r.get("approval") or {}).get("approved_content_sha256"),
            },
        )
    atomic_write(path, updated)
    script_report = sync_script_reviews(workspace, snapshots["tasks"])
    request_report = collect_requests(workspace, snapshots["tasks"])
    final_review_report = sync_final_reviews(workspace, snapshots["tasks"], snapshot_at)
    report = {
        "at": now(),
        "clip_rows": len(snapshots["clips"]),
        "task_rows": len(snapshots["tasks"]),
        "approved_clips": sum(c["review_status"] == "approved" for c in updated),
        "scripts_checked": script_report["checked"],
        "approved_scripts": script_report["approved"],
        "script_requests_ready": len(request_report["ready"]),
        "script_requests_waiting": len(request_report["waiting"]),
        "final_reviews_checked": final_review_report["checked"],
        "final_reviews_approved": final_review_report["approved"],
        "errors": (
            errors
            + script_report["errors"]
            + request_report["errors"]
            + final_review_report["errors"]
        ),
    }
    atomic_write(workspace / "reports/sync_pull.json", report)
    return report


def _scalar(value):
    if isinstance(value, list):
        return value[0] if len(value) == 1 else None
    return value


def sync_final_reviews(workspace: Path, rows: list[dict], snapshot_at: str) -> dict:
    """Bind Feishu's explicit final decision to the exact local build and bytes."""
    by_task = index_rows(rows, "task_id")
    checked = approved = 0
    errors = []
    for state_path in sorted((workspace / "jobs").glob("*/r*/b_*/state.json")):
        build_dir = state_path.parent
        state = load(state_path)
        task_id = state.get("task_id")
        if not isinstance(task_id, str) or task_id not in by_task:
            continue
        candidate = build_dir / "candidate.mp4"
        qc_path = build_dir / "qc.json"
        plan_path = build_dir / "edit_plan.json"
        if not candidate.is_file() or not qc_path.exists() or not plan_path.exists():
            continue
        checked += 1
        fields = by_task[task_id]["fields"]
        plan = load(plan_path)
        qc = load(qc_path)
        actual_hash = file_hash(candidate)
        final_path = build_dir / "final.mp4"
        if (
            state.get("stage") == "delivered"
            and state.get("final_path") == str(final_path)
            and final_path.is_file()
            and file_hash(final_path) == actual_hash
        ):
            continue
        action = _scalar(fields.get("final_review_action")) or "none"
        actor = fields.get("final_review_actor")
        reviewed_build = fields.get("final_review_build_id")
        comment = fields.get("final_review_comment")
        review_path = build_dir / "human_review.json"
        prior = load(review_path) if review_path.exists() else {}
        exact_candidate = (
            plan.get("build_id") == state.get("build_id")
            and state.get("candidate_sha256") == actual_hash
            and qc.get("status") == "passed"
            and qc.get("output_sha256") == actual_hash
        )
        decision_matches = reviewed_build == plan.get("build_id")
        if action in {"approve", "reject"} and (not isinstance(actor, str) or not actor.strip()):
            errors.append(
                {
                    "task_id": task_id,
                    "code": "REVIEW_ACTOR_REQUIRED",
                    "message": "成片审核人不能为空",
                }
            )
            action = "none"
        if action == "approve" and decision_matches and exact_candidate:
            review = {
                "task_id": task_id,
                "build_id": plan["build_id"],
                "output_sha256": actual_hash,
                "status": "approved",
                "action": "approve",
                "actor": actor.strip(),
                "channel": "feishu",
                "comment": comment,
                "snapshot_at": snapshot_at,
                "approved_at": prior.get("approved_at") or now(),
            }
            approved += 1
        elif action == "reject" and decision_matches and exact_candidate:
            review = {
                "task_id": task_id,
                "build_id": plan["build_id"],
                "output_sha256": actual_hash,
                "status": "rejected",
                "action": "reject",
                "actor": actor.strip(),
                "channel": "feishu",
                "comment": comment,
                "snapshot_at": snapshot_at,
                "reviewed_at": now(),
            }
        else:
            review = {
                "task_id": task_id,
                "build_id": plan["build_id"],
                "output_sha256": actual_hash,
                "status": "stale" if prior.get("action") == "approve" else "pending",
                "action": "none",
                "actor": None,
                "channel": "feishu",
                "comment": comment,
                "snapshot_at": snapshot_at,
                "note": (
                    "Final review must specify this exact build, approve/reject, and an actor."
                ),
            }
        semantic_keys = {
            "task_id",
            "build_id",
            "output_sha256",
            "status",
            "action",
            "actor",
            "channel",
            "comment",
        }
        semantic_changed = any(review.get(key) != prior.get(key) for key in semantic_keys)
        if review != prior:
            atomic_write(review_path, review)
        next_stage = (
            "ready_to_finalize"
            if review["status"] == "approved"
            else "revision_requested"
            if review["status"] == "rejected"
            else "waiting_final_review"
        )
        state_changed = (
            state.get("human_review") != review["status"] or state.get("stage") != next_stage
        )
        state["human_review"] = review["status"]
        state["stage"] = next_stage
        if not semantic_changed and not state_changed:
            continue
        state["sync_status"] = "pending"
        state["updated_at"] = now()
        atomic_write(state_path, state)
        enqueue(
            workspace,
            "tasks",
            task_id,
            {
                "stage": state["stage"],
                "build_id": plan["build_id"],
                "render_status": state["render_status"],
                "qc_status": state["qc_status"],
                "human_review": review["status"],
                "sync_status": "pending",
                "project_ref": state.get("project_ref"),
                "final_path": state.get("final_path"),
                "error_summary": (
                    comment
                    if review["status"] == "rejected" and comment
                    else "成片技术检查已通过；等待人工审核当前build。"
                    if review["status"] in {"pending", "stale"}
                    else "人工审核已绑定当前build和候选文件；可以归档final。"
                ),
                "updated_at": now(),
            },
            supersede_group="production_state",
            supersede_groups={"material_index", "edit_plan", "production_state"},
        )
    return {"checked": checked, "approved": approved, "errors": errors}


def sync_script_reviews(workspace: Path, rows: list[dict]) -> dict:
    """Synchronize newest local script per task; no writes to human fields."""
    by_task = index_rows(rows, "task_id")
    newest: dict[str, tuple[Path, dict]] = {}
    errors = []
    invalid_tasks = set()
    paths = list((workspace / "jobs").glob("*/r*/script.json"))
    paths.extend((workspace / "jobs").glob("*/r*/script_outline.json"))
    for path in sorted(paths):
        script = load(path)
        # Explicit fixtures belong to G1/offline tests, never real table decisions.
        if script.get("is_fixture"):
            continue
        try:
            if script.get("entity") == "script_outline":
                request = load(
                    workspace / "requests" / script["task_id"] / f"{script['request_sha256']}.json"
                )
                validate_reviewable_outline(script, request)
            else:
                validate_record(script, workspace)
            task = script["task_id"]
            if path.parent.name != f"r{script['revision']}" or path.parent.parent.name != task:
                raise Blocked("SCRIPT_LOCATION", "Script identity does not match its directory")
        except Blocked as e:
            errors.append({"path": str(path), "code": e.code, "message": str(e)})
            invalid_tasks.add(path.parent.parent.name)
            continue
        if task not in newest or newest[task][1]["revision"] < script["revision"]:
            newest[task] = (path, script)
    approved = 0
    for task, (path, script) in newest.items():
        fields = {} if task in invalid_tasks else by_task.get(task, {}).get("fields", {})
        try:
            result = review_script(script, fields)
        except Blocked as e:
            result = dict(script, review_status="stale", approval=None)
            errors.append({"task_id": task, "code": e.code, "message": str(e)})
        if result != script:
            atomic_write(path, result)
        approved += result["review_status"] == "approved"
        if task in by_task:
            # Bind the status patch to the exact script revision and hash.  A later
            # revision can legitimately return to the same status (for example
            # r1 approved -> r2 pending -> r2 approved); status alone would reuse
            # the already-synced r1 outbox identity and leave Feishu stale.
            enqueue(
                workspace,
                "tasks",
                task,
                {
                    "script_revision": result["revision"],
                    "script_sha256": result["content_sha256"],
                    "script_review_status": result["review_status"],
                },
            )
    return {"checked": len(newest), "approved": approved, "errors": errors}
