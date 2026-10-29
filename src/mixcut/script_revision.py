"""Versioned script edits, one-shot rewrites, and measured draft timing."""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Callable

from .audio import synthesize_local, voice_config_hash
from .contracts import content_hash
from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id
from .deepseek import (
    _payload,
    _read_key,
    _response_json,
    _preview,
    load_model_config,
    parse_response,
    urllib_transport,
)
from .script_input import active_request, validate_reviewable_outline
from .relocation import runtime_path, preserved_timing


def newest_outline(workspace: Path, task_id: str) -> tuple[Path, dict]:
    safe_id(task_id)
    candidates = []
    for path in (workspace / "jobs" / task_id).glob("r*/script_outline.json"):
        outline = load(path)
        if not isinstance(outline.get("revision"), int) or isinstance(outline["revision"], bool):
            raise Blocked("SCRIPT_REVISION_INVALID", str(path), 6)
        candidates.append((outline["revision"], path, outline))
    if not candidates:
        raise Blocked("SCRIPT_MISSING", "请先生成脚本", 3)
    _, path, outline = max(candidates, key=lambda item: item[0])
    request = load(workspace / "requests" / task_id / f"{outline['request_sha256']}.json")
    validate_reviewable_outline(outline, request)
    return path, outline


def editable_text(outline: dict) -> str:
    lines = ["标题：" + outline["title"]]
    for shot in outline["shots"]:
        lines += [
            "",
            f"【{shot['shot_id']}】",
            "旁白：" + shot["narration"],
            "字幕：" + shot["caption"],
            "画面需求：" + shot["visual_intent"],
            "素材检索词：" + json.dumps(shot["semantic_queries"], ensure_ascii=False),
        ]
    return "\n".join(lines)


def apply_edit(outline: dict, text: str) -> dict:
    """Human-readable per-shot text; never eval or execute user content."""
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise Blocked("SCRIPT_DRAFT_INVALID", "请按当前分镜格式填写脚本修改稿", 2)
    result = copy.deepcopy(outline)
    expected = {s["shot_id"] for s in result["shots"]}
    blocks: dict[str, dict] = {}
    current = None
    last_key = None
    title = None
    labels = {
        "旁白": "narration",
        "字幕": "caption",
        "画面需求": "visual_intent",
        "素材检索词": "semantic_queries",
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("标题：") or line.startswith("标题:"):
            if title is not None:
                raise Blocked("SCRIPT_DRAFT_INVALID", "标题重复", 2)
            title = line[3:].strip()
            continue
        match = re.fullmatch(r"【([^】]+)】", line)
        if match:
            current = match[1]
            if current not in expected or current in blocks:
                raise Blocked("SCRIPT_DRAFT_INVALID", "镜头编号不存在或重复", 2)
            blocks[current] = {}
            last_key = None
            continue
        match = re.match(r"^(旁白|字幕|画面需求|素材检索词)[:：](.*)$", line)
        if match and current:
            last_key = labels[match[1]]
            if last_key in blocks[current]:
                raise Blocked("SCRIPT_DRAFT_INVALID", "镜头字段重复", 2)
            blocks[current][last_key] = match[2].strip()
        elif current and last_key:
            blocks[current][last_key] += " " + line
        else:
            raise Blocked("SCRIPT_DRAFT_INVALID", "请保留标题、镜头编号和各字段标签", 2)
    if not title or set(blocks) != expected:
        raise Blocked("SCRIPT_DRAFT_INVALID", "标题或镜头缺失", 2)
    result["title"] = title
    for shot in result["shots"]:
        values = blocks[shot["shot_id"]]
        if set(values) != set(labels.values()) or not all(values.values()):
            raise Blocked("SCRIPT_DRAFT_INVALID", "每镜头须包含旁白、字幕、画面需求和检索词", 2)
        queries = values["semantic_queries"]
        if queries == "；".join(shot["semantic_queries"]):
            # Previously generated drafts used a separator also present inside
            # real descriptions. An untouched draft must preserve exact tokens.
            values["semantic_queries"] = list(shot["semantic_queries"])
        elif queries.startswith("["):
            try:
                parsed = json.loads(queries)
            except ValueError as exc:
                raise Blocked("SCRIPT_DRAFT_INVALID", "素材检索词格式无效", 2) from exc
            if not isinstance(parsed, list) or not all(
                isinstance(q, str) and q.strip() for q in parsed
            ):
                raise Blocked("SCRIPT_DRAFT_INVALID", "素材检索词须为文本列表", 2)
            values["semantic_queries"] = parsed
        else:
            values["semantic_queries"] = [
                x.strip() for x in re.split("[；;]", queries) if x.strip()
            ]
        if not values["semantic_queries"]:
            raise Blocked("SCRIPT_DRAFT_INVALID", "素材检索词不能为空", 2)
        shot.update(values)
    return result


def allocate_timing(outline: dict, measurements: list[dict], target_frames: int) -> dict:
    if len(measurements) != len(outline["shots"]):
        raise Blocked("AUDIO_SEGMENTS", "缺少实测配音", 4)
    speech = []
    for m in measurements:
        samples = m.get("samples")
        if (
            not isinstance(samples, int)
            or isinstance(samples, bool)
            or samples <= 0
            or m.get("sample_rate") != 48000
        ):
            raise Blocked("AUDIO_MEASUREMENT", "必须使用真实48kHz PCM采样数", 4)
        speech.append(math.ceil(samples / 1600))
    required = sum(speech)
    result = {
        "status": "passed" if required <= target_frames else "overflow",
        "target_frames": target_frames,
        "speech_frames": required,
        "speech_seconds": sum(m["samples"] for m in measurements) / 48000,
        "required_seconds": required / 30,
        "excess_frames": max(0, required - target_frames),
        "basis": "actual_48000Hz_samples_no_audio_speed_change",
        "segments": [],
    }
    if required > target_frames:
        return result
    extra = target_frames - required
    padding = [extra * n // required for n in speech]
    remainder = extra - sum(padding)
    ranking = sorted(range(len(speech)), key=lambda i: (-(extra * speech[i] % required), i))
    for i in ranking[:remainder]:
        padding[i] += 1
    cursor = 0
    for shot, frames, tail, m in zip(outline["shots"], speech, padding, measurements, strict=True):
        shot["target_frames"] = frames + tail
        result["segments"].append(
            {
                "shot_id": shot["shot_id"],
                "start_frame": cursor,
                "duration_frames": frames + tail,
                "speech_frames": frames,
                "speech_samples": m["samples"],
                "audio_path": m["path"],
                "audio_sha256": m["sha256"],
                "caption_start_frame": cursor,
                "caption_end_frame": cursor + frames,
            }
        )
        cursor += frames + tail
    assert cursor == target_frames
    result["tail_padding_frames"] = padding
    return result


def revision_request(
    workspace: Path, fields: dict, parent: dict, *, action: str, feedback=""
) -> dict:
    creative = active_request(workspace, fields)
    request = copy.deepcopy(creative)
    request.update(
        creative_request_sha256=creative["request_sha256"],
        revision_parent_sha256=parent["content_sha256"],
        revision_action=action,
        revision_feedback_sha256=digest(feedback or ""),
    )
    if action == "rewrite":
        if not isinstance(feedback, str) or not feedback.strip():
            raise Blocked("REWRITE_FEEDBACK_REQUIRED", "请填写脚本修改请求后再AI重写", 3)
        request["revision_context"] = {
            "feedback": feedback.strip(),
            "title": parent["title"],
            "shots": parent["shots"],
        }
    request.pop("request_sha256", None)
    request["request_sha256"] = digest(request)
    return request


def rewrite_once(workspace: Path, request: dict, *, transport=None, environ=None) -> dict:
    """One paid request only after an explicit rewrite button; immutable response ledger."""
    config = load_model_config(workspace)
    key = digest({"request": request["request_sha256"], "model": config["model"]})
    folder = workspace / "jobs" / safe_id(request["task_id"]) / "rewrite_calls" / key
    receipt_path = folder / "run.json"
    response_path = folder / "response.json"
    if receipt_path.exists():
        if response_path.exists():
            parsed, _ = parse_response(load(response_path), request, config)
            return parsed
        raise Blocked("MODEL_OUTCOME_UNKNOWN", "此重写请求已有调用记录，请先对账，不自动重试", 5)
    secret = _read_key(config, environ)
    payload = _payload(request, config)
    receipt = {
        "at": now(),
        "status": "dispatching",
        "network_attempted": True,
        "automatic_retries": 0,
        "request_sha256": request["request_sha256"],
        "model": config["model"],
    }
    atomic_write(receipt_path, receipt)
    try:
        raw = (transport or urllib_transport)(
            payload, secret, config["endpoint"], config["timeout_seconds"]
        )
        response = _response_json(raw)
        atomic_write(response_path, response)
        outline, usage = parse_response(response, request, config)
    except (Exception, KeyboardInterrupt) as exc:
        receipt.update(status="outcome_unknown_or_rejected", finished_at=now())
        atomic_write(receipt_path, receipt)
        if isinstance(exc, Blocked):
            raise
        raise Blocked("MODEL_OUTCOME_UNKNOWN", "重写调用结果未知，不自动重试", 5) from exc
    receipt.update(status="completed", finished_at=now(), usage=usage)
    atomic_write(receipt_path, receipt)
    return outline


def save_retimed(
    workspace: Path,
    config: dict,
    parent: dict,
    request: dict,
    edited: dict,
    *,
    measure: Callable | None = None,
    fresh_check: Callable | None = None,
    prepare_commit: Callable | None = None,
) -> tuple[Path, dict, dict]:
    """Draft audio is local review evidence, never a production approval."""
    task_id = safe_id(parent["task_id"])
    identity = digest(
        {
            "parent": parent["content_sha256"],
            "request": request["request_sha256"],
            "title": edited["title"],
            "shots": edited["shots"],
            "voice": voice_config_hash(config["audio"]),
        }
    )
    journal = workspace / "jobs" / task_id / "review_edits" / f"{identity}.json"
    if journal.exists() and load(journal).get("outline_path"):
        saved = load(journal)
        return (
            runtime_path(workspace, saved["outline_path"]),
            load(runtime_path(workspace, saved["outline_path"])),
            load(runtime_path(workspace, saved["timing_path"])),
        )
    atomic_write(
        journal,
        {"at": now(), "status": "measuring_draft_audio", "parent_sha256": parent["content_sha256"]},
    )
    if request.get("generation_policy_version") == "inventory-montage-1":
        slots = request["material_slots"]
        if [s["shot_id"] for s in edited["shots"]] != [s["slot_id"] for s in slots]:
            raise Blocked(
                "SCRIPT_POLICY_CHANGED", "旧脚本的画面未绑定真实素材，请先按意见AI重写", 3
            )
        for shot, slot in zip(edited["shots"], slots, strict=True):
            if (
                shot["visual_intent"] != slot["visual_intent"]
                or shot["semantic_queries"] != slot["semantic_queries"]
            ):
                raise Blocked(
                    "MATERIAL_SCRIPT_SCOPE", "画面安排由批准素材确定；请只修改旁白和字幕", 3
                )
        edited["narration_mode"] = request["narration_mode"]
    measurements = [
        (measure or (lambda text: synthesize_local(workspace, config, text)))(s["narration"])
        for s in edited["shots"]
    ]
    timing = allocate_timing(edited, measurements, request["target_duration_frames"])
    # Failed timing still stores the edited words as a new draft, never approved.
    if timing["status"] == "overflow":
        total = request["target_duration_frames"]
        for i, shot in enumerate(edited["shots"]):
            shot["target_frames"] = total // len(edited["shots"]) + (
                i < total % len(edited["shots"])
            )
    edited["total_frames"] = request["target_duration_frames"]
    if request.get("input_policy") == "approved_material_context_constrained":
        for slot, shot in zip(request["material_slots"], edited["shots"], strict=True):
            slot["target_frames"] = shot["target_frames"]
        request["material_context_sha256"] = digest(request["material_slots"])
        request.pop("request_sha256", None)
        request["request_sha256"] = digest(request)
    if fresh_check:
        fresh_check()
    _, latest = newest_outline(workspace, task_id)
    if latest["content_sha256"] != parent["content_sha256"]:
        raise Blocked("SCRIPT_INPUT_STALE", "处理期间脚本版本已变化", 6)
    versions = [
        int(p.name[1:]) for p in (workspace / "jobs" / task_id).glob("r*") if p.name[1:].isdigit()
    ]
    revision = max(versions, default=0) + 1
    target = workspace / "jobs" / task_id / f"r{revision}" / "script_outline.json"
    edited.update(
        schema_version="1.0.0",
        entity="script_outline",
        task_id=task_id,
        revision=revision,
        request_sha256=request["request_sha256"],
        review_status="pending",
        approval=None,
        created_at=now(),
        timing_check={
            "status": timing["status"],
            "voice_config_sha256": voice_config_hash(config["audio"]),
        },
        revision_origin={
            "parent_revision": parent["revision"],
            "parent_sha256": parent["content_sha256"],
            "action": request["revision_action"],
        },
        applied_change_request_sha256=request["revision_feedback_sha256"],
    )
    edited["content_sha256"] = content_hash(edited)
    atomic_write(workspace / "requests" / task_id / f"{request['request_sha256']}.json", request)
    validate_reviewable_outline(edited, request)
    timing.update(
        script_sha256=edited["content_sha256"],
        script_revision=revision,
        voice_config_sha256=voice_config_hash(config["audio"]),
        task_id=task_id,
        production_approved=False,
    )
    timing_path = target.parent / "draft_timing.json"
    assert not target.exists() and not timing_path.exists()
    if prepare_commit:
        prepare_commit(target, edited, timing)
    stage = journal.with_suffix(".stage")
    if stage.exists():
        raise Blocked("SCRIPT_COMMIT_RECONCILE", "保留上次未完成的版本提交，请先对账", 5)
    atomic_write(stage / "script_outline.json", edited)
    atomic_write(stage / "draft_timing.json", timing)
    stage.rename(target.parent)
    atomic_write(
        journal,
        {
            "at": now(),
            "status": "completed_pending_review",
            "outline_path": str(target),
            "timing_path": str(timing_path),
            "parent_sha256": parent["content_sha256"],
        },
    )
    return target, edited, timing


def measure_review_timing(workspace: Path, path: Path, outline: dict, *, measure=None) -> dict:
    """Measure the exact reviewed words without changing their revision or timing."""
    evidence = path.parent / "draft_timing.json"
    if evidence.exists():
        return timing_verified(workspace, path, outline)
    config = load(workspace / "config/config.local.json")
    measurements = [
        (measure or (lambda text: synthesize_local(workspace, config, text)))(s["narration"])
        for s in outline["shots"]
    ]
    segments = []
    cursor = 0
    for shot, m in zip(outline["shots"], measurements, strict=True):
        if (
            isinstance(m.get("samples"), bool)
            or not isinstance(m.get("samples"), int)
            or m["samples"] <= 0
            or m.get("sample_rate") != 48000
        ):
            raise Blocked("AUDIO_MEASUREMENT", "必须使用真实48kHz PCM采样数", 4)
        speech = math.ceil(m["samples"] / 1600)
        segments.append(
            {
                "shot_id": shot["shot_id"],
                "start_frame": cursor,
                "duration_frames": shot["target_frames"],
                "speech_frames": speech,
                "speech_samples": m["samples"],
                "audio_path": m["path"],
                "audio_sha256": m["sha256"],
                "caption_start_frame": cursor,
                "caption_end_frame": cursor + speech,
            }
        )
        cursor += shot["target_frames"]
    timing = {
        "status": "passed"
        if all(s["speech_frames"] <= s["duration_frames"] for s in segments)
        else "overflow",
        "task_id": outline["task_id"],
        "script_revision": outline["revision"],
        "script_sha256": outline["content_sha256"],
        "target_frames": outline["total_frames"],
        "speech_seconds": sum(m["samples"] for m in measurements) / 48000,
        "speech_frames": sum(s["speech_frames"] for s in segments),
        "voice_config_sha256": voice_config_hash(config["audio"]),
        "basis": "actual_48000Hz_samples_exact_reviewed_script_no_retiming",
        "segments": segments,
        "production_approved": False,
    }
    atomic_write(evidence, timing)
    return timing_verified(workspace, path, outline)


def timing_verified(workspace: Path, path: Path, outline: dict) -> dict:
    p = path.parent / "draft_timing.json"
    if not p.exists():
        raise Blocked("SCRIPT_TIMING_REQUIRED", "请先点击修改并重新编排，完成配音时长校验", 3)
    timing = load(p)
    if timing.get("script_sha256") != outline["content_sha256"] or timing.get("status") != "passed":
        raise Blocked("SCRIPT_TIMING_BLOCKED", "配音超时或时长证据不匹配，请修改文案或延长时长", 3)
    if timing.get("voice_config_sha256") != voice_config_hash(
        load(workspace / "config/config.local.json")["audio"]
    ) and not preserved_timing(workspace, p, outline):
        raise Blocked("SCRIPT_TIMING_STALE", "音色或语速已变化，请重新编排", 6)
    segments = timing.get("segments", [])
    if len(segments) != len(outline["shots"]):
        raise Blocked("SCRIPT_TIMING_STALE", "时长证据缺少镜头", 6)
    cursor = 0
    for shot, segment in zip(outline["shots"], segments, strict=True):
        if (
            segment.get("shot_id") != shot["shot_id"]
            or segment.get("start_frame") != cursor
            or segment.get("duration_frames") != shot["target_frames"]
            or not 0 < segment.get("speech_frames", 0) <= shot["target_frames"]
        ):
            raise Blocked("SCRIPT_TIMING_STALE", "镜头与配音时长证据不匹配", 6)
        if file_hash(runtime_path(workspace, segment["audio_path"])) != segment["audio_sha256"]:
            raise Blocked("AUDIO_CHANGED", "试听音频已变化", 6)
        cursor += shot["target_frames"]
    if cursor != outline["total_frames"]:
        raise Blocked("SCRIPT_TIMING_STALE", "配音总时长证据不匹配", 6)
    return timing


def script_patch(outline: dict, timing: dict) -> dict:
    ok = timing["status"] == "passed"
    message = (
        (
            "通过：实测配音%.2f秒，目标%.2f秒；Tingting原速"
            % (timing["speech_seconds"], outline["total_frames"] / 30)
        )
        if ok
        else (
            "超时：实测配音%.2f秒，目标%.2f秒；请压缩文案或延长时长后重新编排"
            % (timing["speech_seconds"], outline["total_frames"] / 30)
        )
    )
    return {
        "script_revision": outline["revision"],
        "script_sha256": outline["content_sha256"],
        "script_input_sha256": outline["request_sha256"],
        "script_preview": _preview(outline),
        "script_edit_draft": editable_text(outline),
        "tts_measured_sec": timing["speech_seconds"],
        "script_timing_status": message,
        "script_review_status": "pending",
        "script_generation_status": "retimed_pending_review" if ok else "blocked_timing",
        "stage": "waiting_script_review" if ok else "blocked_script_timing",
        "plan_preview": None,
        "material_index_preview": None,
        "error_summary": message,
    }
