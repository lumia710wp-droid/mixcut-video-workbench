from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import require_approval
from .core import Blocked, file_hash, load, safe_id
from .engine import local_runtime_status


def openchatcut_runtime_status() -> dict:
    return local_runtime_status()


def scalar(value: Any) -> Any:
    """Normalize Feishu single-select cells without accepting ambiguous values."""
    if isinstance(value, list):
        return value[0] if len(value) == 1 else None
    return value


def current_build(workspace: Path, task_id: str, build_id: str | None = None) -> tuple[Path, dict]:
    """Resolve one formal build without falling back to a recent OpenChatCut project."""
    safe_id(task_id)
    if build_id is not None:
        safe_id(build_id)
    candidates: list[tuple[Path, dict]] = []
    for path in sorted((workspace / "jobs" / task_id).glob("r*/b_*/edit_plan.json")):
        plan = load(path)
        if plan.get("task_id") != task_id or path.parent.name != plan.get("build_id"):
            raise Blocked("BUILD_IDENTITY", f"Plan identity does not match {path}", 6)
        if build_id is None or plan["build_id"] == build_id:
            candidates.append((path.parent, plan))
    if build_id is None:
        workflow_path = workspace / "config/workflow.local.json"
        configured = (
            load(workflow_path).get("current_builds", {}).get(task_id)
            if workflow_path.exists()
            else None
        )
        if configured:
            configured_candidates = [item for item in candidates if item[1]["build_id"] == configured]
            if len(configured_candidates) == 1:
                return configured_candidates[0]
    if len(candidates) != 1:
        raise Blocked(
            "BUILD_SELECTION_REQUIRED",
            f"Expected one formal build for {task_id}; found {len(candidates)}",
            2,
        )
    return candidates[0]


def task_fields(workspace: Path, task_id: str) -> tuple[str, dict]:
    snapshot_path = workspace / "catalog/feishu_snapshot.json"
    if not snapshot_path.exists():
        raise Blocked("SYNC_SNAPSHOT_MISSING", "Run mixcut sync pull first", 3)
    snapshot = load(snapshot_path)
    rows = snapshot.get("tables", {}).get("tasks", [])
    matches = [row for row in rows if row.get("fields", {}).get("task_id") == task_id]
    if len(matches) != 1:
        raise Blocked("TASK_NOT_FOUND", f"Expected one Feishu task row for {task_id}", 2)
    return snapshot["at"], matches[0]["fields"]


def _latest_outline(workspace: Path, task_id: str) -> tuple[Path, dict] | None:
    found = []
    for path in (workspace / "jobs" / task_id).glob("r*/script_outline.json"):
        outline = load(path)
        if isinstance(outline.get("revision"), int):
            found.append((outline["revision"], path, outline))
    if not found:
        return None
    _, path, outline = max(found, key=lambda item: item[0])
    return path, outline


def _project_id(build_dir: Path) -> str | None:
    path = build_dir / "openchatcut/project.json"
    if not path.exists():
        return None
    value = load(path).get("project_id")
    return value if isinstance(value, str) and value else None


def _first_nested(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for nested in value.values():
            found = _first_nested(nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _first_nested(nested, key)
            if found is not None:
                return found
    return None


def inspect_task(workspace: Path, task_id: str) -> dict:
    """Derive the next safe action from artifacts instead of trusting one stage string."""
    safe_id(task_id)
    base = workspace / "jobs" / task_id
    if not base.is_dir():
        requests = list((workspace / "requests" / task_id).glob("*.json"))
        if requests:
            return {
                "task_id": task_id,
                "stage": "ready_for_script_generation",
                "next": "generate_script",
                "command": f"./mixcut --json script generate --task {task_id}",
            }
        return {
            "task_id": task_id,
            "stage": "waiting_creative_input",
            "next": "sync_pull",
            "command": "./mixcut --json sync pull",
        }
    latest = _latest_outline(workspace, task_id)
    if latest is None:
        return {
            "task_id": task_id,
            "stage": "ready_for_script_generation",
            "next": "generate_script",
            "command": f"./mixcut --json script generate --task {task_id}",
        }
    outline_path, outline = latest
    try:
        require_approval(outline)
    except Blocked:
        return {
            "task_id": task_id,
            "script_revision": outline["revision"],
            "stage": "waiting_script_review",
            "next": "human_script_review_in_feishu",
            "command": "./mixcut --json sync pull",
        }
    revision_dir = outline_path.parent
    material_path = revision_dir / "material_index_preview.json"
    if not material_path.exists() or not load(material_path).get("production_ready"):
        return {
            "task_id": task_id,
            "script_revision": outline["revision"],
            "stage": "ready_for_material_index",
            "next": "index_materials",
            "command": f"./mixcut --json material-index --task {task_id}",
        }
    audio_path = revision_dir / "preproduction/audio_caption_manifest.json"
    if not audio_path.exists() or load(audio_path).get("status") != "ready_for_edit_plan":
        return {
            "task_id": task_id,
            "script_revision": outline["revision"],
            "stage": "ready_for_audio",
            "next": "prepare_audio",
            "command": f"./mixcut --json audio prepare --script {outline_path}",
        }
    try:
        build_dir, plan = current_build(workspace, task_id)
    except Blocked as exc:
        if exc.code != "BUILD_SELECTION_REQUIRED" or list(revision_dir.glob("b_*/edit_plan.json")):
            raise
        return {
            "task_id": task_id,
            "script_revision": outline["revision"],
            "stage": "ready_for_edit_plan",
            "next": "compile_edit_plan",
            "command": f"./mixcut --json edit-plan --task {task_id}",
        }
    result = {
        "task_id": task_id,
        "script_revision": plan["script_revision"],
        "build_id": plan["build_id"],
        "build_dir": str(build_dir),
        "project_id": _project_id(build_dir),
    }
    final_path = build_dir / "final.mp4"
    state_path = build_dir / "state.json"
    if final_path.is_file() and state_path.exists():
        state = load(state_path)
        if state.get("stage") == "delivered" and state.get("final_path") == str(final_path):
            return {
                **result,
                "stage": "delivered",
                "next": "none",
                "final_path": str(final_path),
                "output_sha256": file_hash(final_path),
            }
    candidate = build_dir / "candidate.mp4"
    qc_path = build_dir / "qc.json"
    review_path = build_dir / "human_review.json"
    if candidate.is_file() and qc_path.exists() and load(qc_path).get("status") == "passed":
        review = load(review_path) if review_path.exists() else {}
        if review.get("action") == "approve":
            return {
                **result,
                "stage": "ready_to_finalize",
                "next": "finalize",
                "command": f"./mixcut --json finalize --task {task_id} --build {plan['build_id']}",
                "candidate_path": str(candidate),
            }
        return {
            **result,
            "stage": "waiting_final_review",
            "next": "human_final_review_in_feishu",
            "command": "./mixcut --json sync pull",
            "candidate_path": str(candidate),
        }
    finalize_dir = build_dir / "openchatcut/finalize"
    inspection_path = finalize_dir / "render_inspection.json"
    if inspection_path.exists():
        return {
            **result,
            "stage": "ready_for_candidate_registration",
            "next": "register_exported_mp4",
            "command": (
                f"./mixcut --json candidate register --task {task_id} "
                f"--build {plan['build_id']} --file /absolute/path/exported.mp4"
            ),
        }
    render_status_path = finalize_dir / "render_status.json"
    if render_status_path.exists():
        render_status = _first_nested(load(render_status_path), "status")
        if render_status == "completed":
            return {
                **result,
                "stage": "rendered_pending_inspection",
                "next": "inspect_openchatcut_render",
                "command": f"./mixcut --json run --task {task_id} --phase inspect",
            }
        return {
            **result,
            "stage": "rendering",
            "next": "track_openchatcut_render",
            "command": f"./mixcut --json run --task {task_id} --phase track",
        }
    if (finalize_dir / "render_job.json").exists():
        return {
            **result,
            "stage": "rendering",
            "next": "track_openchatcut_render",
            "command": f"./mixcut --json run --task {task_id} --phase track",
        }
    readback = build_dir / "openchatcut/finalize/final_readback_validation.json"
    if readback.exists() and load(readback).get("status") == "passed":
        return {
            **result,
            "stage": "ready_for_export",
            "next": "submit_openchatcut_render",
            "command": f"./mixcut --json run --task {task_id} --phase submit",
        }
    session_path = build_dir / "openchatcut/current_session.json"
    if session_path.exists() and load(session_path).get("status") == "applied":
        return {
            **result,
            "stage": "ready_for_readback",
            "next": "verify_applied_timeline",
            "command": f"./mixcut --json run --task {task_id} --phase readback",
        }
    if session_path.exists() and load(session_path).get("status") == "awaiting_review":
        return {
            **result,
            "stage": "waiting_openchatcut_apply",
            "next": "human_apply_proposal_in_openchatcut",
            "command": f"./mixcut --json run --task {task_id} --phase readback",
        }
    if result["project_id"]:
        return {
            **result,
            "stage": "ready_for_openchatcut_import",
            "next": "openchatcut_stage",
            "command": f"./mixcut --json run --task {task_id} --phase stage",
        }
    return {
        **result,
        "stage": "ready_for_openchatcut_project",
        "next": "create_openchatcut_project",
        "command": f"./mixcut --json run --task {task_id} --phase create",
    }


def workflow_status(
    workspace: Path, task_id: str | None = None, batch_id: str | None = None
) -> dict:
    if task_id and batch_id:
        raise Blocked("WORKFLOW_SCOPE", "Choose --task or --batch, not both", 2)
    if task_id:
        tasks = [task_id]
    else:
        task_names = {
            path.name
            for path in (workspace / "jobs").iterdir()
            if path.is_dir() and not path.name.startswith("G1_")
        }
        snapshot_path = workspace / "catalog/feishu_snapshot.json"
        if snapshot_path.exists():
            snapshot = load(snapshot_path)
            rows = snapshot.get("tables", {}).get("tasks", [])
            if batch_id:
                task_names = {
                    row.get("fields", {}).get("task_id")
                    for row in rows
                    if row.get("fields", {}).get("task_id")
                    and row.get("fields", {}).get("batch_id") == batch_id
                }
            else:
                task_names.update(
                    row.get("fields", {}).get("task_id")
                    for row in rows
                    if row.get("fields", {}).get("task_id")
                )
        elif batch_id:
            raise Blocked("SYNC_SNAPSHOT_MISSING", "Run mixcut sync pull first", 3)
        tasks = sorted(task_names)
    rows = [inspect_task(workspace, task) for task in tasks]
    runtime = openchatcut_runtime_status()
    doctor_path = workspace / "reports/doctor.json"
    connected_projects: set[str] = set()
    if doctor_path.exists():
        doctor = load(doctor_path)
        if doctor.get("mcp_live_probe_ok") is True:
            connected_projects = {
                value
                for value in doctor.get("mcp_connected_project_ids", [])
                if isinstance(value, str)
            }
            runtime["last_mcp_probe_at"] = doctor.get("checked_at")
            runtime["connected_project_ids"] = sorted(connected_projects)
    for row in rows:
        if row.get("project_id"):
            row["openchatcut_editor_connected"] = row["project_id"] in connected_projects
    return {
        "status": "ok",
        "batch_id": batch_id,
        "openchatcut_runtime": runtime,
        "tasks": rows,
        "counts": {
            stage: sum(row["stage"] == stage for row in rows)
            for stage in sorted({row["stage"] for row in rows})
        },
    }
