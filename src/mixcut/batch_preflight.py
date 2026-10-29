"""Read-only production readiness; never generate, approve, import or render."""

from __future__ import annotations

import shutil
from pathlib import Path

from .contracts import inward_frames, require_approval
from .core import Blocked, file_hash, load, now, resolve_media, safe_id
from .workflow import inspect_task


def preflight_batch(workspace: Path, config: dict, manifest: dict) -> dict:
    if manifest.get("is_fixture") is not False:
        raise Blocked("FIXTURE_BLOCKED", "Batch must explicitly declare real production inputs", 2)
    batch_id = safe_id(manifest["batch_id"])
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not 10 <= len(tasks) <= 30:
        raise Blocked("BATCH_SCOPE", "G4 requires 10-30 distinct tasks", 2)
    ids = [safe_id(task["task_id"]) for task in tasks]
    if len(set(ids)) != len(ids):
        raise Blocked("BATCH_SCOPE", "Duplicate task IDs cannot count toward G4", 2)
    scratch = manifest.get("scratch_bytes_per_task")
    floor = manifest.get("min_free_bytes_after_batch")
    if any(type(value) is not int or value < 0 for value in (scratch, floor)) or scratch == 0:
        raise Blocked(
            "BATCH_DISK_POLICY", "Explicit positive scratch and nonnegative floor needed", 2
        )
    assert isinstance(scratch, int) and isinstance(floor, int)
    blockers: list[dict] = []
    gate_path = (workspace / manifest["g3_acceptance_report"]).resolve()
    if not gate_path.is_relative_to((workspace / "reports/g3").resolve()):
        raise Blocked("G3_EVIDENCE_PATH", "Use a G3 report within this workspace", 2)
    gate = load(gate_path) if gate_path.exists() else {}
    if gate.get("status") != "passed":
        blockers.append({"code": "G3_REQUIRED"})
    else:
        # A report alone cannot turn changed/missing G3 outputs into accepted evidence.
        accepted = gate.get("tasks", [])
        if len(accepted) != 3 or len({r.get("task_id") for r in accepted}) != 3:
            blockers.append({"code": "G3_EVIDENCE_INVALID"})
        for row in accepted:
            path = Path(row.get("final_path", "")).resolve()
            if (
                not path.is_relative_to((workspace / "jobs").resolve())
                or not path.is_file()
                or file_hash(path) != row.get("sha256")
                or row.get("human") != "approved"
            ):
                blockers.append({"code": "G3_EVIDENCE_CHANGED", "task_id": row.get("task_id")})
    free = shutil.disk_usage(workspace).free
    required = scratch * len(ids) + floor
    if free < required:
        blockers.append({"code": "DISK_LOW", "required_bytes": required, "free_bytes": free})
    sources_path = workspace / "catalog/sources.json"
    clips_path = workspace / "catalog/clips.json"
    sources = {s["source_id"]: s for s in load(sources_path)} if sources_path.exists() else {}
    clips = load(clips_path) if clips_path.exists() else []
    verified_sources: dict[str, dict] = {}
    materials = []
    for clip in clips:
        try:
            require_approval(clip)
            source = sources[clip["source_id"]]
            if source["source_id"] not in verified_sources:
                errors = []
                for location in source["locations"]:
                    try:
                        path = resolve_media(
                            config["source_roots"], location["root_id"], location["relative_path"]
                        )
                        if file_hash(path) != source["file_sha256"]:
                            raise Blocked("FILE_CHANGED", source["source_id"], 6)
                        verified_sources[source["source_id"]] = {"status": "verified"}
                        break
                    except (Blocked, OSError) as exc:
                        errors.append(getattr(exc, "code", "SOURCE_UNAVAILABLE"))
                else:
                    verified_sources[source["source_id"]] = {
                        "status": "blocked",
                        "errors": errors or ["SOURCE_LOCATION_MISSING"],
                    }
            if verified_sources[source["source_id"]]["status"] != "verified":
                raise Blocked("SOURCE_UNAVAILABLE", source["source_id"])
            a, b = inward_frames(clip["usable_start_sec"], clip["usable_end_sec"])
            materials.append(
                {
                    "clip_id": clip["clip_id"],
                    "goods_id": clip["goods_id"],
                    "available_frames": b - a,
                }
            )
        except (Blocked, KeyError) as exc:
            blockers.append(
                {
                    "code": getattr(exc, "code", "MATERIAL_METADATA_INVALID"),
                    "clip_id": clip.get("clip_id"),
                }
            )
    if not materials:
        blockers.append({"code": "NO_VERIFIED_MATERIALS"})
    stages = []
    need_model = 0
    for task_id in ids:
        try:
            state = inspect_task(workspace, task_id)
        except (Blocked, OSError, KeyError) as exc:
            state = {
                "task_id": task_id,
                "stage": "blocked",
                "error_code": getattr(exc, "code", "TASK_METADATA_INVALID"),
            }
        stages.append(state)
        if state["stage"] in {"waiting_creative_input", "ready_for_script_generation"}:
            need_model += 1
        if state["stage"] in {
            "waiting_creative_input",
            "ready_for_script_generation",
            "waiting_script_review",
            "revision_requested",
            "blocked",
        }:
            blockers.append({"code": "TASK_NOT_READY", "task_id": task_id, "stage": state["stage"]})
    if need_model:
        blockers.append(
            {"code": "MODEL_BATCH_BUDGET_REVIEW_REQUIRED", "minimum_new_script_calls": need_model}
        )
    return {
        "batch_id": batch_id,
        "at": now(),
        "status": "blocked" if blockers else "ready",
        "scope": "local_preflight_only",
        "network_calls": 0,
        "model_calls": 0,
        "renders_started": 0,
        "execution_authorized": False,
        "task_count": len(ids),
        "tasks": stages,
        "blockers": blockers,
        "disk": {
            "free_bytes": free,
            "required_bytes": required,
            "scratch_bytes_per_task": scratch,
            "min_free_bytes_after_batch": floor,
            "basis": "explicit conservative policy; not a render-size guarantee",
        },
        "materials": materials,
        "source_checks": verified_sources,
        "budget": {
            "minimum_new_script_calls": need_model,
            "automatic_retries": 0,
            "cost_estimate": None,
            "batch_allowance": "not_authorized_by_preflight",
        },
        "capacity_note": "Verified intervals are not proof of script matching or cross-task uniqueness.",
    }
