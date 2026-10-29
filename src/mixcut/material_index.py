from __future__ import annotations

from pathlib import Path
from typing import Any

from .analyze import proposal_set_path
from .contracts import require_approval
from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id
from .feishu import enqueue
from .script_input import validate_reviewable_outline


CONCEPTS: dict[str, tuple[str, ...]] = {
    "hands": ("双手", "手工", "手部", "工作人员", "制作动作"),
    "preparation": ("备料", "准备", "原料", "食材", "切", "切削", "slicing"),
    "fruit": ("水果", "果肉", "芒果", "清爽"),
    "filling": (
        "入馅",
        "馅料",
        "取馅",
        "取料",
        "放入",
        "放进",
        "包入",
        "位于包皮",
        "包皮中",
        "filling",
        "taking_filling",
    ),
    "wrapper": ("外皮", "包皮", "展开", "stretching_wrapper"),
    "closing": ("收口", "收边", "包拢", "捏合", "包制", "closing"),
    "kneading": ("揉", "揉制", "揉面"),
    "shaping": ("整形", "塑形", "shaping"),
    "liner": ("纸托", "装托", "托盘", "placing_in_liner"),
    "packing": ("包装", "纸袋", "出餐", "packing"),
    "finished": ("成品", "完成", "点心", "出品", "满足", "食欲"),
    "store": ("门店", "现场", "日常", "操作台", "环境"),
    "display": ("多品展示", "展示柜", "展示区", "柜台", "陈列"),
    "customer": ("顾客", "接过", "手提", "离店"),
    "bite_open": ("咬开", "切开", "断面", "内馅特写"),
    "closeup": ("近景", "特写", "细节", "closeup"),
    "medium": ("中景", "medium"),
}

HARD_CONCEPTS = {
    "fruit",
    "filling",
    "wrapper",
    "closing",
    "kneading",
    "liner",
    "packing",
    "display",
    "customer",
    "bite_open",
}

POLICY_VERSION = "semantic-index-5"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    return ""


def _concepts(value: Any) -> set[str]:
    text = _text(value)
    return {
        concept
        for concept, aliases in CONCEPTS.items()
        if any(alias.lower() in text for alias in aliases)
    }


def _newest_outline(workspace: Path, task_id: str) -> tuple[Path, dict, dict]:
    safe_id(task_id)
    found: list[tuple[int, Path, dict]] = []
    for path in (workspace / "jobs" / task_id).glob("r*/script_outline.json"):
        outline = load(path)
        revision = outline.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool):
            found.append((revision, path, outline))
    if not found:
        raise Blocked("SCRIPT_MISSING", "No generated script outline exists for this task", 3)
    _, path, outline = max(found, key=lambda item: item[0])
    request_path = workspace / "requests" / task_id / f"{outline['request_sha256']}.json"
    if not request_path.exists():
        raise Blocked("SCRIPT_INPUT_MISSING", "Approved script request is unavailable", 4)
    request = load(request_path)
    validate_reviewable_outline(outline, request)
    require_approval(outline)
    return path, outline, request


def _candidate_text(record: dict) -> dict:
    return {
        "description": record.get("proposal", {}).get("description"),
        "semantic_tags": record.get("semantic_tags"),
        "observations": {
            key: record.get("observations", {}).get(key)
            for key in ("title", "description", "events", "shot")
        },
    }


def _window_concepts(record: dict, window: dict) -> set[str]:
    base = _concepts(_candidate_text(record))
    events = record.get("observations", {}).get("events")
    start = window["start_sec"]
    end = window["end_sec"]
    window_events = []
    if isinstance(events, list):
        for event in events:
            if (
                isinstance(event, list)
                and len(event) >= 2
                and isinstance(event[0], (int, float))
                and start <= event[0] <= end
            ):
                window_events.append(event[1])
    window_hard = _concepts({"note": window.get("note"), "events": window_events})
    if not window_events and not window.get("note"):
        window_hard = base
    return (base - HARD_CONCEPTS) | (window_hard & HARD_CONCEPTS)


def _score(shot: dict, record: dict, window: dict) -> tuple[int, list[str]]:
    requested = _concepts(
        {
            "visual_intent": shot["visual_intent"],
            "semantic_queries": shot["semantic_queries"],
            "caption": shot["caption"],
        }
    )
    available = _window_concepts(record, window)
    required = requested & HARD_CONCEPTS
    if not required <= available:
        return 0, []
    matched = sorted(requested & available)
    return len(matched), matched


def material_index_preview_text(result: dict) -> str:
    lines = [
        f"候选索引 {result['index_id']}｜脚本r{result['script_revision']}",
    ]
    for shot in result["shots"]:
        if not shot["candidates"]:
            lines.append(f"{shot['shot_id']}：缺料｜{shot['caption']}")
            continue
        best = shot["candidates"][0]
        eligibility = "已批准可用" if best["production_eligible"] else "仍有门禁"
        lines.append(
            f"{shot['shot_id']}：{best['clip_id']} "
            f"{best['start_sec']:.1f}-{best['end_sec']:.1f}s｜{eligibility}"
        )
    lines.append(
        f"覆盖 {result['shots_with_candidates']}/{result['shot_count']}；"
        f"可生产候选镜头 {result['eligible_shot_count']} 个，缺料 {result['material_gap_count']} 个。"
    )
    return "\n".join(lines)


def index_materials(
    workspace: Path,
    task_id: str,
    proposals_path: Path | None = None,
    *,
    candidate_limit: int = 3,
) -> dict:
    """Rank visually analyzed clips after script approval without making production selections."""
    if candidate_limit < 1:
        raise Blocked("INDEX_LIMIT", "candidate_limit must be positive", 2)
    _, outline, request = _newest_outline(workspace, task_id)
    constrained_slots = {
        slot["slot_id"]: slot
        for slot in request.get("material_slots", [])
        if isinstance(slot, dict) and isinstance(slot.get("slot_id"), str)
    }
    proposal_file = proposal_set_path(workspace, proposals_path)
    proposals = load(proposal_file)
    records = proposals.get("records")
    if not isinstance(records, list) or not records:
        raise Blocked("MATERIAL_ANALYSIS_INVALID", "Visual proposal set has no records", 2)

    catalog_path = workspace / "catalog/clips.json"
    catalog = {clip["clip_id"]: clip for clip in load(catalog_path)}
    adoption_path = workspace / "catalog/ai_proposal_adoptions.json"
    adoptions = {}
    if adoption_path.exists():
        adoptions = {record["clip_id"]: record for record in load(adoption_path).get("records", [])}
    confirmation_path = workspace / "catalog/material_confirmations.json"
    confirmations = {}
    if confirmation_path.exists():
        confirmations = {
            record["clip_id"]: record for record in load(confirmation_path).get("records", [])
        }
    fresh: list[dict] = []
    stale: list[str] = []
    for record in records:
        clip_id = record.get("clip_id")
        clip = catalog.get(clip_id)
        adoption = adoptions.get(clip_id, {})
        confirmation = confirmations.get(clip_id, {})
        original_binding = record.get("catalog_revision_at_analysis") == (clip or {}).get(
            "revision"
        ) and record.get("catalog_content_sha256_at_analysis") == (clip or {}).get("content_sha256")
        adopted_binding = (
            adoption.get("analysis_sha256") == record.get("analysis_sha256")
            and adoption.get("adopted_catalog_revision") == (clip or {}).get("revision")
            and adoption.get("adopted_catalog_content_sha256") == (clip or {}).get("content_sha256")
        )
        confirmed_binding = (
            confirmation.get("analysis_sha256") == record.get("analysis_sha256")
            and confirmation.get("confirmed_catalog_revision") == (clip or {}).get("revision")
            and confirmation.get("confirmed_catalog_content_sha256")
            == (clip or {}).get("content_sha256")
        )
        if not clip or not (original_binding or adopted_binding or confirmed_binding):
            if isinstance(clip_id, str):
                stale.append(clip_id)
            continue
        windows = record.get("candidate_windows")
        if not isinstance(windows, list):
            windows = []
        valid_windows = [
            window
            for window in windows
            if isinstance(window, dict)
            and isinstance(window.get("start_sec"), (int, float))
            and isinstance(window.get("end_sec"), (int, float))
            and window["end_sec"] > window["start_sec"]
        ]
        if not valid_windows and not constrained_slots:
            continue
        fresh.append(
            {
                **record,
                "candidate_windows": valid_windows,
                "_confirmation_current": confirmed_binding,
            }
        )

    shots = []
    for shot in outline["shots"]:
        ranked = []
        bound_slot = constrained_slots.get(shot["shot_id"])
        if bound_slot:
            record = next(
                (item for item in fresh if item["clip_id"] == bound_slot.get("clip_id")), None
            )
            clip = catalog.get(bound_slot.get("clip_id"))
            if not record or not clip:
                raise Blocked(
                    "MATERIAL_CONTEXT_STALE",
                    f"Approved material binding is unavailable: {shot['shot_id']}",
                    6,
                )
            binding_current = (
                bound_slot.get("clip_revision") == clip.get("revision")
                and bound_slot.get("approved_content_sha256") == clip.get("content_sha256")
                and bound_slot.get("analysis_sha256") == record.get("analysis_sha256")
                and bound_slot.get("goods_id") == clip.get("goods_id")
                and bound_slot.get("usable_start_sec") == clip.get("usable_start_sec")
                and bound_slot.get("usable_end_sec") == clip.get("usable_end_sec")
            )
            if not binding_current:
                raise Blocked(
                    "MATERIAL_CONTEXT_STALE",
                    f"Approved material binding changed: {shot['shot_id']}",
                    6,
                )
            start_sec = bound_slot["usable_start_sec"]
            end_sec = bound_slot["usable_end_sec"]
            enough_frames = round((end_sec - start_sec) * 30) >= shot["target_frames"]
            if not enough_frames:
                raise Blocked(
                    "MATERIAL_DURATION",
                    f"Approved material range is too short: {shot['shot_id']}",
                    3,
                )
            confirmed = bool(record.get("_confirmation_current"))
            blockers = [
                reason
                for reason, blocked in (
                    ("material_not_approved", clip.get("review_status") != "approved"),
                    ("blocking_risk", bool((clip.get("tags") or {}).get("risk"))),
                    ("confirmation_receipt_missing", not confirmed),
                    (
                        "product_uncertain",
                        bool(record.get("proposal", {}).get("product_uncertain")) and not confirmed,
                    ),
                )
                if blocked
            ]
            score, matched = _score(
                shot,
                record,
                {
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "note": "approved material context binding",
                },
            )
            ranked.append(
                {
                    "clip_id": record["clip_id"],
                    "analysis_sha256": record["analysis_sha256"],
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "window_status": "approved_catalog_usable_range",
                    "score": score,
                    "matched_concepts": matched,
                    "duration_sufficient": True,
                    "material_review_status": clip.get("review_status"),
                    "product_uncertain": bool(record.get("proposal", {}).get("product_uncertain"))
                    and not confirmed,
                    "blocking_reasons": blockers,
                    "production_eligible": not blockers,
                    "material_binding": "approved_material_context",
                }
            )
        else:
            for record in fresh:
                clip = catalog[record["clip_id"]]
                choices = []
                for window in record["candidate_windows"]:
                    score, matched = _score(shot, record, window)
                    enough_frames = (
                        round((window["end_sec"] - window["start_sec"]) * 30)
                        >= shot["target_frames"]
                    )
                    if score == 0 or not enough_frames:
                        continue
                    confirmed = bool(record.get("_confirmation_current"))
                    blockers = [
                        reason
                        for reason, blocked in (
                            ("material_not_approved", clip.get("review_status") != "approved"),
                            ("blocking_risk", bool((clip.get("tags") or {}).get("risk"))),
                            ("confirmation_receipt_missing", not confirmed),
                            (
                                "product_uncertain",
                                bool(record.get("proposal", {}).get("product_uncertain"))
                                and not confirmed,
                            ),
                        )
                        if blocked
                    ]
                    choices.append(
                        {
                            "clip_id": record["clip_id"],
                            "analysis_sha256": record["analysis_sha256"],
                            "start_sec": window["start_sec"],
                            "end_sec": window["end_sec"],
                            "window_status": window.get("status"),
                            "score": score,
                            "matched_concepts": matched,
                            "duration_sufficient": True,
                            "material_review_status": clip.get("review_status"),
                            "product_uncertain": bool(
                                record.get("proposal", {}).get("product_uncertain")
                            )
                            and not confirmed,
                            "blocking_reasons": blockers,
                            "production_eligible": not blockers,
                            "material_binding": "semantic_candidate",
                        }
                    )
                if choices:
                    choices.sort(key=lambda item: (-item["score"], item["start_sec"]))
                    ranked.append(choices[0])
        ranked.sort(
            key=lambda item: (
                -item["score"],
                item["clip_id"],
            )
        )
        shots.append(
            {
                "shot_id": shot["shot_id"],
                "caption": shot["caption"],
                "target_frames": shot["target_frames"],
                "status": "candidates_found" if ranked else "material_gap",
                "candidates": ranked[:candidate_limit],
            }
        )

    identity = {
        "task_id": task_id,
        "script_revision": outline["revision"],
        "script_sha256": outline["content_sha256"],
        "proposal_file_sha256": file_hash(proposal_file),
        "catalog_file_sha256": file_hash(catalog_path),
        "adoption_receipt_sha256": file_hash(adoption_path) if adoption_path.exists() else None,
        "confirmation_receipt_sha256": (
            file_hash(confirmation_path) if confirmation_path.exists() else None
        ),
        "ranking_policy_version": POLICY_VERSION,
        "candidate_limit": candidate_limit,
        "material_context_sha256": request.get("material_context_sha256"),
    }
    material_gap_count = sum(not shot["candidates"] for shot in shots)
    eligible_shot_count = sum(
        any(candidate["production_eligible"] for candidate in shot["candidates"]) for shot in shots
    )
    production_ready = material_gap_count == 0 and eligible_shot_count == len(shots)
    status = (
        "production_candidates_ready"
        if production_ready
        else "approved_materials_with_gaps"
        if eligible_shot_count
        else "advisory_pending_material_review"
    )
    result = {
        "schema_version": "1.0.0",
        "entity": "material_index_preview",
        "index_id": "mi_" + digest(identity)[:16],
        **identity,
        "generated_at": now(),
        "mode": (
            "deterministic_bound_ranking_over_approved_material_context"
            if constrained_slots
            else "deterministic_semantic_ranking_over_codex_visual_analysis"
        ),
        "status": status,
        "production_ready": production_ready,
        "shot_count": len(shots),
        "shots_with_candidates": sum(bool(shot["candidates"]) for shot in shots),
        "material_gap_count": material_gap_count,
        "eligible_shot_count": eligible_shot_count,
        "stale_analysis_clip_ids": sorted(stale),
        "shots": shots,
        "guardrails": [
            "候选索引不等于素材批准或剪辑计划。",
            "只有当前批准内容及其确认回执匹配时，候选才可用于生产。",
            "存在缺料时不生成正式剪辑计划。",
        ],
    }
    target = (
        workspace / "jobs" / task_id / f"r{outline['revision']}" / "material_index_preview.json"
    )
    atomic_write(target, result)
    preview = material_index_preview_text(result)
    enqueue(
        workspace,
        "tasks",
        task_id,
        {
            "material_index_preview": preview,
            "stage": (
                "ready_for_edit_plan"
                if result["production_ready"]
                else "waiting_material_gap"
                if result["eligible_shot_count"]
                else "waiting_material_review"
            ),
            "plan_preview": None,
            "error_summary": (
                f"候选索引已生成：{result['shots_with_candidates']}/{result['shot_count']}个镜头有候选，"
                f"{result['material_gap_count']}个缺料；"
                f"{result['eligible_shot_count']}个镜头已有批准素材候选。"
            ),
        },
        supersede_group="material_index",
    )
    return {
        "status": result["status"],
        "task_id": task_id,
        "index_id": result["index_id"],
        "path": str(target),
        "shot_count": result["shot_count"],
        "shots_with_candidates": result["shots_with_candidates"],
        "material_gap_count": result["material_gap_count"],
        "eligible_shot_count": result["eligible_shot_count"],
        "stale_analysis_clip_ids": result["stale_analysis_clip_ids"],
        "production_ready": result["production_ready"],
    }
