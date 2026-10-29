from __future__ import annotations

import math
import unicodedata

from pathlib import Path
from typing import Any

from .analyze import proposal_set_path
from .contracts import content_hash, require_approval, inward_frames
from .core import Blocked, atomic_write, digest, load, safe_id
from .feishu import enqueue, index_rows


def _plain_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Blocked("SCRIPT_INPUT_INVALID", "Creative text fields must be plain text", 2)
    return value.strip()


def request_from_task_fields(fields: dict) -> dict:
    """Build the model input from human task fields only.

    Material descriptions, tags, clip IDs and source paths are intentionally absent.
    """
    task_id = _plain_text(fields.get("task_id"))
    keywords = _plain_text(fields.get("creative_keywords"))
    sentences = _plain_text(fields.get("seed_sentences"))
    duration = fields.get("target_duration_sec")
    if duration in (None, ""):
        duration = 24
    if not keywords and not sentences:
        raise Blocked("SCRIPT_INPUT_MISSING", "创意关键词或参考句子至少填写一项")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise Blocked("SCRIPT_INPUT_INVALID", "目标时长必须是数字", 2)
    if not 6 <= float(duration) <= 120:
        raise Blocked("SCRIPT_DURATION", "目标时长必须在6到120秒之间", 2)
    duration_frames = round(float(duration) * 30)
    request = {
        "schema_version": "1.0.0",
        "entity": "script_request",
        "task_id": task_id,
        "creative_keywords": keywords,
        "seed_sentences": sentences,
        "target_duration_sec": duration_frames / 30,
        "target_duration_frames": duration_frames,
        "input_policy": "human_creative_input_only_no_material_context",
    }
    if fields.get("request_group_id"):
        ordinal = fields.get("script_ordinal")
        strategy = fields.get("variation_strategy")
        angle = _plain_text(fields.get("variation_angle"))
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, (int, float))
            or not math.isfinite(ordinal)
            or int(ordinal) != ordinal
            or not 1 <= ordinal <= 30
            or strategy not in {"distinct", "opening"}
            or not angle
            or len(angle) > 100
        ):
            raise Blocked("SCRIPT_VARIATION_INVALID", "需求分组的脚本序号或差异策略无效", 2)
        request["variation"] = {
            "request_group_id": _plain_text(fields["request_group_id"]),
            "ordinal": int(ordinal),
            "strategy": strategy,
            "angle": angle,
        }
    request["request_sha256"] = digest(request)
    return request


def material_constrained_request(workspace: Path, fields: dict) -> dict:
    """Bind a creative request to the current approved material inventory."""
    base = request_from_task_fields(fields)
    profile_path = workspace / "config/material_script_profiles.json"
    if not profile_path.is_file():
        raise Blocked("MATERIAL_SCRIPT_PROFILE_MISSING", str(profile_path), 3)
    profile = load(profile_path)
    order = profile.get("tasks", {}).get(base["task_id"])
    queries = profile.get("queries", {})
    if not isinstance(order, list) or not 4 <= len(order) <= 6 or len(order) != len(set(order)):
        raise Blocked("MATERIAL_SCRIPT_PROFILE_INVALID", "Task requires 4 to 6 unique clips", 2)

    clips = {clip["clip_id"]: clip for clip in load(workspace / "catalog/clips.json")}
    proposal_path = proposal_set_path(workspace)
    proposals = {record["clip_id"]: record for record in load(proposal_path).get("records", [])}
    confirmation_path = workspace / "catalog/material_confirmations.json"
    confirmations = {
        record["clip_id"]: record for record in load(confirmation_path).get("records", [])
    }
    base_frames, remainder = divmod(base["target_duration_frames"], len(order))
    slots = []
    for index, clip_id in enumerate(order, 1):
        clip = clips.get(clip_id)
        proposal = proposals.get(clip_id)
        confirmation = confirmations.get(clip_id)
        semantic_queries = queries.get(clip_id)
        if not clip or not proposal or not confirmation:
            raise Blocked("MATERIAL_CONTEXT_STALE", f"Missing approved evidence: {clip_id}", 6)
        if (
            clip.get("review_status") != "approved"
            or bool((clip.get("tags") or {}).get("risk"))
            or confirmation.get("analysis_sha256") != proposal.get("analysis_sha256")
            or confirmation.get("confirmed_catalog_revision") != clip.get("revision")
            or confirmation.get("confirmed_catalog_content_sha256") != clip.get("content_sha256")
        ):
            raise Blocked("MATERIAL_CONTEXT_STALE", f"Approval binding is stale: {clip_id}", 6)
        if (
            not isinstance(semantic_queries, list)
            or not semantic_queries
            or not all(isinstance(value, str) and value.strip() for value in semantic_queries)
        ):
            raise Blocked("MATERIAL_SCRIPT_PROFILE_INVALID", f"Missing queries: {clip_id}", 2)
        target_frames = base_frames + (1 if index <= remainder else 0)
        available_frames = round((clip["usable_end_sec"] - clip["usable_start_sec"]) * 30)
        if available_frames < target_frames:
            raise Blocked("MATERIAL_DURATION", f"Approved range is too short: {clip_id}", 3)
        observations = proposal.get("observations", {})
        visual_intent = f"{observations.get('title', '')}：{observations.get('role', '')}".strip(
            "："
        )
        if not visual_intent or len(visual_intent) > 200:
            raise Blocked("MATERIAL_CONTEXT_INVALID", f"Invalid visual brief: {clip_id}", 2)
        slots.append(
            {
                "slot_id": f"M{index:02d}",
                "clip_id": clip_id,
                "clip_revision": clip["revision"],
                "approved_content_sha256": clip["content_sha256"],
                "analysis_sha256": proposal["analysis_sha256"],
                "goods_id": clip["goods_id"],
                "description": clip["description"],
                "visual_intent": visual_intent,
                "semantic_queries": semantic_queries,
                "target_frames": target_frames,
                "usable_start_sec": clip["usable_start_sec"],
                "usable_end_sec": clip["usable_end_sec"],
                "max_narration_chars": 10,
                "max_caption_chars": 10,
            }
        )
    request = {
        **base,
        "input_policy": "approved_material_context_constrained",
        "material_slots": slots,
        "material_context_sha256": digest(slots),
    }
    request["request_sha256"] = digest(
        {key: value for key, value in request.items() if key != "request_sha256"}
    )
    return request


def narration_mode(workspace: Path, fields: dict) -> str:
    group = fields.get("request_group_id")
    path = (
        workspace / "config/script_project_policies" / f"{safe_id(group)}.json" if group else None
    )
    policy = load(path) if path and path.is_file() else {}
    mode = policy.get("narration_mode", "sales_independent")
    if mode not in {"sales_independent", "material_based"}:
        raise Blocked("SCRIPT_MODE_INVALID", "文案方式无效", 2)
    return mode


def inventory_request(workspace: Path, fields: dict) -> dict:
    """Select current approved footage for any new task; never invent missing shots."""
    base = request_from_task_fields(fields)
    clips_path = workspace / "catalog/clips.json"
    if not clips_path.is_file():
        raise Blocked("MATERIAL_CONTEXT_MISSING", "请先入库并审核素材，再生成脚本", 3)
    clips = load(clips_path)
    proposals = {p["clip_id"]: p for p in load(proposal_set_path(workspace))["records"]}
    confirmations_path = workspace / "catalog/material_confirmations.json"
    confirmations = (
        {c["clip_id"]: c for c in load(confirmations_path).get("records", [])}
        if confirmations_path.is_file()
        else {}
    )
    brief = base["creative_keywords"] + " " + base["seed_sentences"]
    # Restrict to the explicitly named product when known, including products
    # whose clips are still pending. Never substitute another flavour silently.
    goods = {c.get("goods_id") for c in clips if c.get("goods_id")}
    named_goods = {g for g in goods if g in brief}
    candidates = []
    for clip in clips:
        if named_goods and clip.get("goods_id") not in named_goods:
            continue
        proposal = proposals.get(clip["clip_id"], {})
        confirmation = confirmations.get(clip["clip_id"], {})
        try:
            require_approval(clip)
            start, end = inward_frames(clip["usable_start_sec"], clip["usable_end_sec"])
        except Blocked:
            continue
        if (
            bool((clip.get("tags") or {}).get("risk"))
            or not clip.get("goods_id")
            or not clip.get("brand_id")
            or not clip.get("description", "").strip()
            or confirmation.get("analysis_sha256") != proposal.get("analysis_sha256")
            or not proposal.get("analysis_sha256")
            or confirmation.get("confirmed_catalog_revision") != clip.get("revision")
            or confirmation.get("confirmed_catalog_content_sha256") != clip.get("content_sha256")
        ):
            continue
        candidates.append((clip, proposal, end - start))
    # A stable order and task variation produce repeatable but varied footage.
    candidates.sort(key=lambda c: c[0]["clip_id"])
    offset = (int(fields.get("script_ordinal") or 1) - 1) % max(1, len(candidates))
    candidates = candidates[offset:] + candidates[:offset]
    selected = []
    for count in range(min(6, len(candidates)), 3, -1):
        frames, remainder = divmod(base["target_duration_frames"], count)
        usable = [c for c in candidates if c[2] >= frames + bool(remainder)]
        if len(usable) >= count:
            selected = usable[:count]
            break
    if not selected:
        raise Blocked(
            "MATERIAL_INSUFFICIENT",
            "当前商品的已批准素材不足以组成4—6个镜头或覆盖目标时长；请审核更多素材或缩短时长，系统不会编造画面",
            3,
        )
    slots = []
    for i, (clip, proposal, _) in enumerate(selected):
        target = frames + (i < remainder)
        # These fields are local, immutable footage facts. Captions/narration
        # must never participate in material matching for independent sales copy.
        visual = clip["description"][:200]
        slots.append(
            {
                "slot_id": f"M{i + 1:02d}",
                "clip_id": clip["clip_id"],
                "clip_revision": clip["revision"],
                "approved_content_sha256": clip["content_sha256"],
                "analysis_sha256": proposal["analysis_sha256"],
                "goods_id": clip["goods_id"],
                "description": clip["description"],
                "visual_intent": visual,
                "semantic_queries": [visual[:80]],
                "target_frames": target,
                "usable_start_sec": clip["usable_start_sec"],
                "usable_end_sec": clip["usable_end_sec"],
                "max_narration_chars": max(4, min(100, int(target / 30 * 3))),
                "max_caption_chars": 20,
            }
        )
    request = {
        **base,
        "input_policy": "approved_material_context_constrained",
        "generation_policy_version": "inventory-montage-1",
        "narration_mode": narration_mode(workspace, fields),
        "material_slots": slots,
        "material_context_sha256": digest(slots),
    }
    request["request_sha256"] = digest({k: v for k, v in request.items() if k != "request_sha256"})
    return request


def active_request(workspace: Path, fields: dict) -> dict:
    from .executable import uses_v3

    if uses_v3(workspace, fields):
        raise Blocked("V3_BATCH_REQUIRED", "请在工作台生成整批执行脚本；逐条旧版生成已停用", 3)
    workflow_path = workspace / "config/workflow.local.json"
    if workflow_path.is_file() and load(workflow_path).get("script_generation_policy") == (
        "approved_material_context_constrained"
    ):
        profile_path = workspace / "config/material_script_profiles.json"
        profiles = load(profile_path).get("tasks", {}) if profile_path.is_file() else {}
        if fields.get("task_id") in profiles:
            return material_constrained_request(workspace, fields)
        return inventory_request(workspace, fields)
    return request_from_task_fields(fields)


def model_prompt(request: dict) -> dict:
    """Send anonymous approved visual briefs, never source paths or media IDs."""
    if request.get("input_policy") == "approved_material_context_constrained":
        slots = [
            {
                "slot_id": slot["slot_id"],
                "goods_id": slot["goods_id"],
                "visible_description": slot["description"],
                "visual_intent": slot["visual_intent"],
                "semantic_queries": slot["semantic_queries"],
                "target_frames": slot["target_frames"],
                "max_narration_chars": slot["max_narration_chars"],
                "max_caption_chars": slot["max_caption_chars"],
            }
            for slot in request["material_slots"]
        ]
        independent = request.get("narration_mode") == "sales_independent"
        return {
            "instruction": (
                "根据人工创意输入和已批准画面槽位写短视频脚本。必须逐槽位使用，"
                "不得增加画面、顾客、门店全景、成品断面或其他未提供内容。"
            ),
            "narration_mode": request.get("narration_mode", "material_based"),
            "copy_instruction": (
                "写一篇连贯的销售推荐旁白，再分配为短句和字幕。旁白与画面独立，"
                "不要求逐句或逐镜头语义对应，不要为销售话术新增任何画面需求。"
                "只使用人工输入支持的商品事实；糯叽叽不等于拉丝，团购优惠不等于指定价格或折扣。"
                "不编造试吃人物、顾客评价、优惠界面、赠品、库存、每天现做等未提供事实。"
                if independent
                else "围绕已批准素材确实可见的内容写旁白；不可把推测写成事实，画面只能使用已有槽位。"
            ),
            **({"variation": request["variation"]} if "variation" in request else {}),
            "task_id": request["task_id"],
            "creative_keywords": request["creative_keywords"],
            "seed_sentences": request["seed_sentences"],
            "target_duration_frames": request["target_duration_frames"],
            "material_slots": slots,
            "output_contract": {
                "shots": "数量、顺序和slot_id必须与material_slots完全一致",
                "protected": "visual_intent、semantic_queries、target_frames必须逐值复制",
                "editable": "只创作title、narration和caption",
                "forbidden": "价格、功效、配方、销量、产地、文件路径、源时间区间或不存在的画面",
            },
        }
    return {
        "instruction": (
            "根据人工输入拆分短视频脚本。先写叙事和镜头语义需求，不选择具体素材，"
            "不编造价格、配方、功效或无法由输入支持的商品事实。"
            "如有variation，按指定角度与序号创作不同表达，不得仅更换标题。"
        ),
        **({"variation": request["variation"]} if "variation" in request else {}),
        "task_id": request["task_id"],
        "creative_keywords": request["creative_keywords"],
        "seed_sentences": request["seed_sentences"],
        "target_duration_frames": request["target_duration_frames"],
        "output_contract": {
            "shots": "2至12个；每个含narration、caption、visual_intent、semantic_queries、target_frames",
            "timing": "所有target_frames之和必须等于目标时长",
            "forbidden": "clip_id/source_id/source_path/source time range/material file",
        },
    }


def _contains_material_binding(value: Any) -> bool:
    forbidden = {"clip_id", "source_id", "source_path", "source_start_sec", "source_end_sec"}
    if isinstance(value, dict):
        return bool(forbidden & set(value)) or any(
            _contains_material_binding(v) for v in value.values()
        )
    if isinstance(value, list):
        return any(_contains_material_binding(v) for v in value)
    return False


def validate_outline(outline: dict, request: dict) -> None:
    required = {
        "entity",
        "task_id",
        "request_sha256",
        "title",
        "shots",
        "total_frames",
    }
    if not required <= set(outline) or outline["entity"] != "script_outline":
        raise Blocked("MODEL_SCHEMA_INVALID", "Missing script outline fields", 2)
    if (
        outline["task_id"] != request["task_id"]
        or outline["request_sha256"] != request["request_sha256"]
    ):
        raise Blocked(
            "SCRIPT_INPUT_STALE", "Generated script is not bound to current creative input", 6
        )
    if _contains_material_binding(outline):
        raise Blocked("SCRIPT_MATERIAL_LEAK", "Script generation must precede material indexing", 6)
    shots = outline["shots"]
    if not isinstance(shots, list) or not 4 <= len(shots) <= 6:
        raise Blocked("MODEL_SCHEMA_INVALID", "Script requires 4 to 6 semantic shots", 2)
    total = 0
    for shot in shots:
        if set(shot) != {
            "shot_id",
            "narration",
            "caption",
            "visual_intent",
            "semantic_queries",
            "target_frames",
        }:
            raise Blocked("MODEL_SCHEMA_INVALID", "Unexpected shot fields", 2)
        if not all(
            isinstance(shot[k], str) and shot[k].strip()
            for k in ["shot_id", "narration", "caption", "visual_intent"]
        ):
            raise Blocked("MODEL_SCHEMA_INVALID", "Shot text cannot be empty", 2)
        if not isinstance(shot["semantic_queries"], list) or not shot["semantic_queries"]:
            raise Blocked("MODEL_SCHEMA_INVALID", "Semantic material queries are required", 2)
        if not all(isinstance(q, str) and q.strip() for q in shot["semantic_queries"]):
            raise Blocked("MODEL_SCHEMA_INVALID", "Invalid semantic material query", 2)
        frames = shot["target_frames"]
        if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
            raise Blocked(
                "MODEL_SCHEMA_INVALID", "Shot duration must be positive integer frames", 2
            )
        total += frames
    if total != request["target_duration_frames"] or outline["total_frames"] != total:
        raise Blocked(
            "SCRIPT_TIMING_UNSATISFIABLE", "Generated shots do not equal requested duration", 2
        )
    if request.get("input_policy") == "approved_material_context_constrained":
        slots = request.get("material_slots")
        if not isinstance(slots, list) or len(slots) != len(shots):
            raise Blocked("MATERIAL_SCRIPT_SCOPE", "Material slot count changed", 4)
        for shot, slot in zip(shots, slots, strict=True):
            protected = {
                "shot_id": "slot_id",
                "visual_intent": "visual_intent",
                "semantic_queries": "semantic_queries",
                "target_frames": "target_frames",
            }
            if any(shot[shot_key] != slot[slot_key] for shot_key, slot_key in protected.items()):
                raise Blocked(
                    "MATERIAL_SCRIPT_SCOPE", f"Material slot changed: {slot['slot_id']}", 4
                )
            narration_length = len("".join(shot["narration"].split()))
            if request.get("generation_policy_version") == "inventory-montage-1":
                # This is a speech-length hint, not the actual timing gate.
                narration_length = sum(
                    not c.isspace() and not unicodedata.category(c).startswith("P")
                    for c in shot["narration"]
                )
            caption_length = len("".join(shot["caption"].split()))
            if narration_length > slot["max_narration_chars"]:
                raise Blocked("MATERIAL_SCRIPT_LENGTH", f"Narration too long: {slot['slot_id']}", 4)
            if caption_length > slot["max_caption_chars"]:
                raise Blocked("MATERIAL_SCRIPT_LENGTH", f"Caption too long: {slot['slot_id']}", 4)
    if request.get("narration_mode") and outline.get("narration_mode") != request["narration_mode"]:
        raise Blocked("SCRIPT_MODE_INVALID", "脚本文案方式与当前需求不一致", 6)


def validate_reviewable_outline(outline: dict, request: dict) -> None:
    validate_outline(outline, request)
    required = {
        "schema_version",
        "revision",
        "provider",
        "model",
        "review_status",
        "content_sha256",
        "approval",
    }
    if not required <= set(outline) or outline["schema_version"] != "1.0.0":
        raise Blocked("MODEL_SCHEMA_INVALID", "Missing reviewable outline metadata", 2)
    if isinstance(outline["revision"], bool) or not isinstance(outline["revision"], int):
        raise Blocked("MODEL_SCHEMA_INVALID", "Outline revision must be a positive integer", 2)
    if outline["revision"] < 1 or outline["review_status"] not in {
        "pending",
        "approved",
        "rejected",
        "stale",
    }:
        raise Blocked("MODEL_SCHEMA_INVALID", "Invalid outline review state", 2)
    if outline["content_sha256"] != content_hash(outline):
        raise Blocked("SCRIPT_HASH", "Outline content hash does not match", 6)
    if outline["review_status"] == "approved":
        require_approval(outline)


def collect_requests(workspace: Path, task_rows: list[dict]) -> dict:
    """Persist complete creative inputs and expose deterministic generation readiness."""
    by_task = index_rows(task_rows, "task_id")
    ready = []
    waiting = []
    errors = []
    for task_id, row in sorted(by_task.items()):
        fields = row["fields"]
        try:
            request = active_request(workspace, fields)
        except Blocked as exc:
            if exc.code == "SCRIPT_INPUT_MISSING":
                waiting.append(task_id)
                enqueue(
                    workspace,
                    "tasks",
                    task_id,
                    {
                        "stage": "waiting_creative_input",
                        "script_generation_status": "waiting_input",
                        "script_input_sha256": None,
                        "script_model": None,
                        "script_preview": "等待人工填写创意关键词或参考句子，点击一键生成脚本；时长默认24秒。",
                        "plan_preview": None,
                        "material_index_preview": None,
                        "error_summary": "创意关键词或参考句子至少填写一项；时长可选，默认24秒。",
                    },
                    supersede_group="script_generation_state",
                )
                continue
            errors.append({"task_id": task_id, "code": exc.code, "message": str(exc)})
            enqueue(
                workspace,
                "tasks",
                task_id,
                {
                    "stage": "creative_input_invalid",
                    "script_generation_status": "input_invalid",
                    "error_summary": str(exc),
                },
                supersede_group="script_generation_state",
            )
            continue
        path = workspace / "requests" / task_id / f"{request['request_sha256']}.json"
        if not path.exists():
            atomic_write(path, request)
        # Readback must not enqueue an obsolete ready_for_model patch over an
        # existing original or manually revised script for the same creative input.
        existing = []
        for outline_path in (workspace / "jobs" / task_id).glob("r*/script_outline.json"):
            outline = load(outline_path)
            existing.append((outline.get("revision", 0), outline))
        if existing:
            latest = max(existing, key=lambda item: item[0])[1]
            saved_path = workspace / "requests" / task_id / f"{latest['request_sha256']}.json"
            if saved_path.is_file():
                saved_request = load(saved_path)
                creative_hash = saved_request.get(
                    "creative_request_sha256", saved_request["request_sha256"]
                )
                if creative_hash == request["request_sha256"]:
                    continue
        ready.append(
            {"task_id": task_id, "request_sha256": request["request_sha256"], "path": str(path)}
        )
        constrained = request["input_policy"] == "approved_material_context_constrained"
        enqueue(
            workspace,
            "tasks",
            task_id,
            {
                "script_input_sha256": request["request_sha256"],
                "script_generation_status": "ready_for_model",
                "script_model": None,
                "stage": "ready_for_script_generation",
                "script_preview": (
                    "需求和真实批准素材已锁定，等待生成文案；销售旁白可独立于画面表达。"
                    if constrained
                    else "人工创意输入已锁定，等待大模型拆分脚本；尚未索引任何素材。"
                ),
                "plan_preview": None,
                "material_index_preview": None,
                "script_review_status": "pending",
                "error_summary": (
                    "当前流程按现有批准素材约束脚本；等待DeepSeek逐槽位生成。"
                    if constrained
                    else "人工创意输入已锁定；等待大模型拆脚本。此阶段不读取素材库。"
                ),
            },
            supersede_group="script_generation_state",
        )
    return {"ready": ready, "waiting": waiting, "errors": errors}
