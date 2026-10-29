from __future__ import annotations
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any
from jsonschema import Draft202012Validator, FormatChecker
from .core import Blocked, digest, load, safe_id

EXCLUDED = {"content_sha256", "approval", "review_status", "updated_at", "created_at", "sync_info"}


def content_hash(record: dict) -> str:
    return digest({k: v for k, v in record.items() if k not in EXCLUDED})


def plan_hash(plan: dict) -> str:
    return digest(
        {
            k: v
            for k, v in plan.items()
            if k not in {"plan_sha256", "build_id", "created_at", "attempts"}
        }
    )


def validate_record(record: dict, workspace: Path, production: bool = True) -> None:
    if production and record.get("is_fixture"):
        raise Blocked("FIXTURE_BLOCKED", "Synthetic objects are not production inputs", 2)
    entity = record.get("entity")
    if entity not in {
        "source",
        "clip",
        "product",
        "batch",
        "script",
        "edit_plan",
        "job_status",
        "taxonomy",
    }:
        raise Blocked("SCHEMA", "Unknown entity", 2)
    errors = list(
        Draft202012Validator(
            load(workspace / f"schemas/{entity}.schema.json"), format_checker=FormatChecker()
        ).iter_errors(record)
    )
    if errors:
        raise Blocked("SCHEMA", errors[0].message, 2)
    digest(
        record
    )  # Reject non-finite values even when JSON Schema accepts a numeric Python object.


def require_approval(record: dict, production: bool = True) -> None:
    if production and record.get("is_fixture"):
        raise Blocked("FIXTURE_BLOCKED", "Fixture approval is not human approval")
    approval = record.get("approval") or {}
    h = content_hash(record)
    if (
        record.get("review_status") != "approved"
        or not approval.get("actor_id")
        or not approval.get("approved_at")
    ):
        raise Blocked("UNAPPROVED", "A recorded human decision is required")
    if record.get("content_sha256") != h or approval.get("approved_content_sha256") != h:
        raise Blocked("APPROVAL_STALE", "Approval is bound to an older content hash", 6)
    if production and approval.get("channel") == "fixture":
        raise Blocked("FIXTURE_BLOCKED", "Fixture actor is not a real reviewer")


def inward_frames(start: Any, end: Any) -> tuple[int, int]:
    a, b = Decimal(str(start)), Decimal(str(end))
    if not a.is_finite() or not b.is_finite() or a < 0 or b <= a:
        raise Blocked("CLIP_RANGE", "Invalid source interval", 2)
    return int((a * 30).to_integral_value(rounding=ROUND_CEILING)), int(
        (b * 30).to_integral_value(rounding=ROUND_FLOOR)
    )


def validate_tags(tags: dict, taxonomy: dict) -> None:
    if set(tags) != set(taxonomy["axes"]):
        raise Blocked("TAG_OUT_OF_VOCAB", "All four tag axes are required", 2)
    for axis, values in tags.items():
        allowed = {x["id"] for x in taxonomy["axes"][axis]}
        if not isinstance(values, list) or any(v not in allowed for v in values):
            raise Blocked("TAG_OUT_OF_VOCAB", axis, 2)


def validate_plan(plan: dict, workspace: Path, production: bool = True) -> None:
    if production:
        for policy_path in (workspace / "config/script_project_policies").glob("*.json"):
            current_policy = load(policy_path)
            if current_policy.get("generation_policy_version") == 3 and plan.get(
                "task_id"
            ) in current_policy.get("generation_task_ids", []):
                raise Blocked(
                    "V3_EXECUTOR_REQUIRED", "任务已切换完整执行脚本，不能执行旧版素材计划", 3
                )
    validate_record(plan, workspace, production)
    if plan["plan_sha256"] != plan_hash(plan) or plan["build_id"] != "b_" + plan_hash(plan)[:16]:
        raise Blocked("PLAN_HASH", "Plan or build hash mismatch", 6)
    media = {m["media_id"]: m for m in plan["media"]}
    if len(media) != len(plan["media"]):
        raise Blocked("DUPLICATE_ID", "Duplicate media id", 2)
    cursor = 0
    clips: set[str] = set()
    ranges: dict[str, list[tuple[int, int]]] = {}
    shots = {}
    for shot in plan["shots"]:
        m = media.get(shot["media_id"])
        if m is None:
            raise Blocked("MEDIA_MISSING", shot["media_id"], 2)
        a, b = shot["source_in_frame"], shot["source_out_frame"]
        if (
            shot["timeline_start_frame"] != cursor
            or b - a != shot["duration_frames"]
            or a < 0
            or b > m["total_frames"]
        ):
            raise Blocked("TIMING_UNSATISFIABLE", "Gap, overlap, or invalid source interval", 2)
        if shot["clip_id"] in clips or shot["shot_id"] in shots:
            raise Blocked("DUPLICATE_CLIP", "Clip/shot repeated in one video", 2)
        if any(a < y and x < b for x, y in ranges.get(m["source_id"], [])):
            raise Blocked("SOURCE_OVERLAP", m["source_id"], 2)
        if shot["speed"] != 1 or shot["source_audio_gain"] != 0:
            raise Blocked("P0_UNSUPPORTED", "Speed must be 1 and source audio muted", 2)
        ranges.setdefault(m["source_id"], []).append((a, b))
        clips.add(shot["clip_id"])
        shots[shot["shot_id"]] = (cursor, cursor + shot["duration_frames"])
        cursor += shot["duration_frames"]
    if cursor != plan["total_frames"]:
        raise Blocked("TOTAL_FRAMES", "Unexpected total video frames", 2)
    if not 660 <= cursor <= 840:
        _validate_requested_duration(plan, workspace, cursor)
    for cap in plan["captions"]:
        a, b = shots.get(cap["shot_id"], (-1, -1))
        if not a <= cap["start_frame"] < cap["end_frame"] <= b:
            raise Blocked("CAPTION_RANGE", cap["shot_id"], 2)


def _validate_requested_duration(plan: dict, workspace: Path, frames: int) -> None:
    """Outside the legacy range, require this exact approved creative duration.

    No global widening for legacy/fixture plans: the saved approved outline and
    canonical request must bind this task, revision, hash and total frame count.
    """
    from .script_input import validate_reviewable_outline

    if not 180 <= frames <= 3600:
        raise Blocked("TOTAL_FRAMES", "Creative duration must be 6 to 120 seconds", 2)
    task_id = safe_id(plan["task_id"])
    outline_path = (
        workspace / "jobs" / task_id / f"r{plan['script_revision']}" / "script_outline.json"
    )
    if not outline_path.is_file():
        raise Blocked("TOTAL_FRAMES", "Nonlegacy duration requires an approved script outline", 3)
    outline = load(outline_path)
    request_id = safe_id(outline.get("request_sha256", ""))
    request_path = workspace / "requests" / task_id / f"{request_id}.json"
    if not request_path.is_file():
        raise Blocked(
            "SCRIPT_INPUT_MISSING", "Approved creative duration request is unavailable", 3
        )
    request = load(request_path)
    if request_id != digest({k: v for k, v in request.items() if k != "request_sha256"}):
        raise Blocked("SCRIPT_INPUT_STALE", "Creative duration request hash changed", 6)
    validate_reviewable_outline(outline, request)
    require_approval(outline)
    if (
        outline["task_id"] != task_id
        or outline["revision"] != plan["script_revision"]
        or outline["content_sha256"] != plan["script_sha256"]
        or outline["total_frames"] != frames
        or request["target_duration_frames"] != frames
    ):
        raise Blocked("SCRIPT_INPUT_STALE", "Plan duration differs from its approved script", 6)
