"""V3 batch workflow. Human approval binds the complete, timed edit package."""

from __future__ import annotations

import json
from pathlib import Path
from jsonschema import Draft202012Validator
from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id
from .doubao import aligned_captions, synthesize
from .montage import (
    allocate_batch,
    executable_hash,
    release_batch,
    reservations,
    reserve_batch,
    validate_executable,
    validate_allocation,
)
from .fact_planning import planning_segments, uses_facts

COPY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scripts"],
    "properties": {
        "scripts": {
            "type": "array",
            "minItems": 1,
            "maxItems": 30,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id", "title", "narration", "headline"],
                "properties": {
                    k: {"type": "string", "minLength": 1, "maxLength": n}
                    for k, n in [("task_id", 80), ("title", 80), ("narration", 1000)]
                }
                | {
                    "headline": {
                        "type": "array",
                        "minItems": 4,
                        "maxItems": 4,
                        "items": {"type": "string", "minLength": 1, "maxLength": 10},
                    }
                },
            },
        }
    },
}


def policy(root: Path, group: str) -> dict:
    path = root / "config/script_project_policies" / f"{safe_id(group)}.json"
    return load(path) if path.exists() else {}


def uses_v3(root: Path, fields: dict) -> bool:
    settings = policy(root, fields.get("request_group_id") or "none")
    return settings.get("generation_policy_version") == 3 and (
        not settings.get("generation_task_ids")
        or fields.get("task_id") in settings["generation_task_ids"]
    )


def latest_batch(root: Path, group: str) -> dict | None:
    folder = root / "jobs/executable_v3" / safe_id(group)
    pointer = folder / "current.json"
    if not pointer.is_file():
        return None
    return load(folder / (safe_id(load(pointer)["batch_id"]) + ".json"))


def save_batch(root: Path, batch: dict) -> None:
    folder = root / "jobs/executable_v3" / safe_id(batch["group_id"])
    atomic_write(folder / (safe_id(batch["batch_id"]) + ".json"), batch)
    atomic_write(folder / "current.json", {"batch_id": batch["batch_id"]})


def input_snapshot(rows: list[dict], settings: dict) -> dict:
    # Human approvals and output fields do not alter the creative input.
    keys = [
        "task_id",
        "creative_keywords",
        "seed_sentences",
        "target_duration_sec",
        "variation_angle",
        "script_ordinal",
        "script_change_request",
    ]
    return {
        "settings": settings,
        "tasks": [
            {k: r["fields"].get(k) for k in keys}
            for r in sorted(rows, key=lambda r: r["fields"]["task_id"])
        ],
    }


def verify_media_available(root: Path, segments: list[dict], source_ids: set[str]) -> None:
    from .segments import source_file
    from .production_plan import NORMALIZATION_PROFILE

    config = load(root / "config/config.local.json")
    sources = {s["source_id"]: s for s in load(root / "catalog/sources.json")}
    cache_root = next(
        (
            Path(r["path"]).resolve()
            for r in config["source_roots"]
            if r["root_id"] == "normalized_cfr30_v1"
        ),
        None,
    )
    for source_id in source_ids:
        try:
            source_file(root, config, sources[source_id])
            continue
        except Blocked as error:
            if error.code != "SOURCE_UNAVAILABLE":
                raise
        if cache_root and cache_root.is_relative_to(root.resolve()):
            target = cache_root / f"{safe_id(source_id)}.mp4"
            receipt_path = target.with_suffix(".json")
            if target.is_file() and receipt_path.is_file():
                receipt = load(receipt_path)
                if (
                    receipt.get("source_sha256") == sources[source_id]["file_sha256"]
                    and receipt.get("profile") == NORMALIZATION_PROFILE
                    and receipt.get("working_sha256") == file_hash(target)
                ):
                    continue
        raise Blocked(
            "V3_MEDIA_UNAVAILABLE",
            f"{source_id}原片未挂载且缺少已验证制作副本；先准备素材，不发起文案或配音请求",
            3,
        )


def preflight(
    root: Path,
    settings: dict,
    rows: list[dict],
    *,
    check_media: bool = True,
    allocation: list[dict] | None = None,
) -> dict:
    segments = [
        s
        for s in planning_segments(root, settings)
        if s["source_id"] not in settings.get("excluded_source_ids", [])
    ]
    requests = [
        {
            "task_id": r["fields"]["task_id"],
            "total_frames": round(r["fields"]["target_duration_sec"] * 30),
        }
        for r in rows
    ]
    if not segments and uses_facts(root, settings):
        raise Blocked(
            "V3_NO_CONFIRMED_FACTS",
            "当前商品没有可规划的已确认事实，请检查商品确认或素材索引版本",
            3,
        )
    reserved = [s for r in reservations(root) for s in r["shots"]]
    if allocation is None:
        allocated, report = allocate_batch(
            segments,
            requests,
            reserved=reserved,
            speed=settings.get("speed", 1.1),
            minimum_unique=settings.get("minimum_unique", 0.7),
        )
    else:
        allocated = allocation
        report = validate_allocation(
            allocated,
            segments,
            requests,
            reserved=reserved,
            speed=settings.get("speed", 1.1),
            minimum_unique=settings.get("minimum_unique", 0.7),
        )
    if check_media:
        verify_media_available(
            root, segments, {s["source_id"] for p in allocated for s in p["shots"]}
        )
    return {"segments": segments, "allocation": allocated, "diversity": report}


def selected_copy_request(snapshot: dict, allocation: list[dict], segments: list[dict]) -> dict:
    """Only selected shots become copy context; the rest of the library stays local."""
    by_id = {s["segment_id"]: s for s in segments}
    visuals = []
    for plan in allocation:
        for shot in plan["shots"]:
            segment = by_id[shot["segment_id"]]
            visuals.append(
                {
                    "task_id": plan["task_id"],
                    "segment_id": shot["segment_id"],
                    "description": shot["description"],
                    "events": [
                        e
                        for e in segment.get("events", [])
                        if shot["source_in_frame"] <= e["frame"] < shot["source_out_frame"]
                    ],
                }
            )
    return {
        **snapshot,
        "visual_context_policy": "selected_shots_v1",
        "allocation": allocation,
        "available_visuals": visuals,
    }


def build_copy_input(request: dict, max_input_chars: int) -> str:
    """Project facts for copywriting; retain the complete edit input separately."""
    if request.get("script_stage") == "text_only_v1":
        text = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        if len(text) > max_input_chars:
            raise Blocked(
                "V3_INPUT_LIMIT", f"脚本需求超出输入预算（{len(text)}/{max_input_chars}字）", 2
            )
        return text
    execution_keys = {
        "excluded_source_ids",
        "generation_policy_version",
        "generation_task_ids",
        "group_id",
        "minimum_unique",
        "reason",
        "retained_approved_sample_task_id",
        "speed",
        "material_planning_source",
    }
    context = {
        k: v
        for k, v in request.items()
        if k not in {"settings", "available_visuals", "allocation", "visual_context_policy"}
    }
    context["settings"] = {
        k: v for k, v in request.get("settings", {}).items() if k not in execution_keys
    }
    visuals = request.get("available_visuals", [])
    task_ids = {t["task_id"] for t in request.get("tasks", [])}
    if visuals and (
        request.get("visual_context_policy") != "selected_shots_v1"
        or any(v.get("task_id") not in task_ids for v in visuals)
    ):
        raise Blocked("V3_VISUAL_SELECTION_REQUIRED", "文案输入须先按本批任务完成选材", 3)
    context["task_visuals"] = {
        task: {
            "observations": list(
                dict.fromkeys(v["description"] for v in visuals if v["task_id"] == task)
            ),
            "events": list(
                dict.fromkeys(
                    e["text"] for v in visuals if v["task_id"] == task for e in v.get("events", [])
                )
            ),
        }
        for task in sorted(task_ids)
    }
    from .doubao import spoken_char_hint

    calibration = {f["task_id"]: f for f in request.get("voice_calibration", [])}
    context["narration_timing"] = [
        {
            "task_id": t["task_id"],
            "target_seconds": t["target_duration_sec"],
            "spoken_char_hint": calibration.get(t["task_id"], {}).get(
                "spoken_char_hint", spoken_char_hint(t["target_duration_sec"])
            ),
            "count_excludes_punctuation": True,
        }
        for t in request.get("tasks", [])
        if isinstance(t.get("target_duration_sec"), (int, float))
    ]
    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if len(text) > max_input_chars:
        raise Blocked(
            "V3_INPUT_LIMIT",
            f"本批需求与已选镜头事实超出输入预算（{len(text)}/{max_input_chars}字），请调整本批需求；未自动增加调用",
            2,
        )
    return text


def copy_instructions(request: dict) -> str:
    if request.get("script_stage") == "text_only_v1":
        return (
            "当前只生成中国食品短视频的文字脚本，后续由Codex编导检索选材并剪辑。"
            "根据人工需求和明确商品归属写自然销售文案，不写包制工序解说来凑篇幅。"
            "按每条target_duration_sec和script_length控制不含标点字数，使文字适合目标时长；"
            "这里只是字数估时，不是假装已生成或测量音频。"
            "只输出指定JSON，narration是正文；headline依次为主题、商品、卖点、口感。"
            "每条只输出task_id、title、narration、headline；不要重复输出target_duration_sec等输入字段。"
            "不要捏造价格优惠、试吃经历、销量、原料配方或未提供事实；不引用不存在的画面。"
            "有text_length_correction时按该项字数范围修改previous_copy中的正文，保留其他要求。"
        )
    return (
        (
            "当前任务是旁白时长校准。必须修改narration而不是只换标题。"
            "逐条按voice_calibration.spoken_char_hint重写，"
            "统计不含标点字数，控制在该建议值前后1字。"
            "expand必须扩写、shorten必须压缩；不得原样返回原文。"
            "先完整构思自然的销售口播，再核对字数；不重复称呼或填充空话。"
            if request.get("voice_calibration")
            else ""
        )
        + "写中国食品短视频的自然口语销售推荐。整批给不同角度，使用提供的真实商品事实。"
        "像朋友说话，去掉广告套话、反问模板、虚构体验、保证、价格和优惠。"
        "不要捏造拉丝、每天现做等无证据事实。销售推荐无需逐句描述画面；素材讲解只讲提供画面。"
        "只输出指定JSON。headline依次为主题、商品、卖点、口感，短句不挤占画面。"
        "画面事实按task_visuals中的task_id分别提供，不得把其他任务画面写成当前任务画面。"
        "按narration_timing给出的不含标点字数建议安排口播，不要只写一句短开场。"
        "片头0.2秒片尾0.3秒，口播应接近目标时长，实际配音实测后再审核。"
        "若有voice_calibration，这是已完成配音后的文案校准：优先按该项实测语速、"
        "desired_voice_seconds和spoken_char_hint扩写或压缩当前旁白。"
        "保留销售目的和真实信息，新增内容须自然且不重复，不用空泛填充凑字数。"
    )


def generate_copy(root: Path, request: dict, receipt_folder: Path) -> dict:
    from .deepseek import load_model_config, _read_key, urllib_transport, _response_json

    cfg = load_model_config(root)
    intent, response_file = (
        receipt_folder / "model_intent.json",
        receipt_folder / "model_response.json",
    )
    if response_file.exists():
        if intent.exists() and load(intent).get("input_sha256") != digest(request):
            raise Blocked("V3_INPUT_CHANGED", "已有文案响应绑定其他请求，请先对账", 6)
        response = load(response_file)
    else:
        if intent.exists():
            raise Blocked("V3_MODEL_UNKNOWN", "该批模型请求结果未知，先对账，不自动重发", 5)
        text = build_copy_input(request, cfg["max_input_chars"])
        key = _read_key(cfg)
        atomic_write(receipt_folder / "model_input.json", json.loads(text))
        atomic_write(
            intent,
            {
                "status": "dispatching",
                "at": now(),
                "input_sha256": digest(request),
                "model_input_sha256": digest(json.loads(text)),
                "model_input_chars": len(text),
            },
        )
        raw = urllib_transport(
            {
                "model": cfg["model"],
                "input": text,
                "instructions": copy_instructions(request),
                "reasoning": {"effort": "none"},
                "stream": False,
                "max_output_tokens": cfg["max_output_tokens"],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "mixcut_batch_v3",
                        "schema": COPY_SCHEMA,
                    }
                },
            },
            key,
            cfg["endpoint"],
            cfg["timeout_seconds"],
        )
        response = _response_json(raw)
        atomic_write(response_file, response)
    if response.get("status") != "completed":
        raise Blocked("V3_MODEL_INCOMPLETE", "整批文案响应不完整，已保留回执", 5)
    chunks = [
        c["text"]
        for message in response.get("output", [])
        if message.get("type") == "message"
        for c in message.get("content", [])
        if c.get("type") == "output_text"
    ]
    try:
        result = json.loads("".join(chunks))
    except (ValueError, TypeError) as exc:
        raise Blocked("V3_COPY_FORMAT", "文案JSON无效，已保留响应，不重复调用", 4) from exc
    if request.get("script_stage") == "text_only_v1":
        from .script_stage import normalize_model_copy

        normalized = normalize_model_copy(request, result)
        if normalized != result:
            atomic_write(
                receipt_folder / "normalized_copy.json",
                {
                    "transform": "matching_text_target_echo_v1",
                    "response_sha256": digest(response),
                    "input_sha256": digest(request),
                    "copy": normalized,
                },
            )
        result = normalized
    if list(Draft202012Validator(COPY_SCHEMA).iter_errors(result)):
        raise Blocked("V3_COPY_SCHEMA", "文案结构不符，已保留响应，不重复调用", 4)
    return result


def generate_batch(
    root: Path,
    config: dict,
    group: str,
    rows: list[dict],
    *,
    copy_fn=None,
    tts_fn=None,
    correction_fn=None,
) -> dict:
    settings = policy(root, group)
    if settings.get("generation_policy_version") != 3:
        raise Blocked("V3_POLICY_REQUIRED", "项目未启用执行脚本流程", 3)
    if settings.get("workflow_mode") == "script_first_v1":
        raise Blocked(
            "SCRIPT_STAGE_ONLY", "脚本阶段只生成文字；配音在Codex剪辑阶段按已确认脚本生成", 3
        )
    snapshot = input_snapshot(rows, settings)
    prior = latest_batch(root, group)
    if prior and prior["state"] != "released":
        if prior["input_sha256"] == digest(snapshot):
            for script in prior["scripts"]:
                validate_current(root, script)
            return prior
        raise Blocked("V3_INPUT_CHANGED", "先退回并释放旧批次，再重新生成；旧批准不会沿用", 6)
    version = (prior["version"] + 1) if prior else 1
    batch_id = safe_id(f"{group}_v3_{version}")
    folder = root / "requests/executable_v3" / batch_id
    prepared_path = folder / "prepared_batch.json"
    if prepared_path.exists():
        prepared = load(prepared_path)
        if prepared["input_sha256"] != digest(snapshot):
            raise Blocked("V3_INPUT_CHANGED", "已有完整执行包，先恢复对账，不能覆盖", 6)
        current_segments = planning_segments(root, settings)
        for item in prepared["scripts"]:
            validate_executable(item, current_segments)
            voice_path = Path(item["voiceover"]["path"]).resolve()
            if (
                not voice_path.is_relative_to(root.resolve())
                or not voice_path.is_file()
                or file_hash(voice_path) != item["voiceover"]["sha256"]
            ):
                raise Blocked("V3_AUDIO_CHANGED", "恢复执行包时配音缺失或变化", 6)
        reserve_batch(root, prepared)
        save_batch(root, prepared)
        return prepared
    selected_file = folder / "input.selected_v1.json"
    saved_selection = load(selected_file) if selected_file.exists() else None
    if saved_selection and (
        {k: saved_selection[k] for k in ("settings", "tasks")} != snapshot
        or saved_selection.get("visual_context_policy") != "selected_shots_v1"
    ):
        raise Blocked("V3_INPUT_CHANGED", "已选画面绑定其他需求，请先对账", 6)
    initial = preflight(
        root,
        settings,
        rows,
        allocation=saved_selection["allocation"] if saved_selection else None,
    )  # No paid calls before material and saved-selection gates.
    folder.mkdir(parents=True, exist_ok=True)
    legacy_file = folder / "input.json"
    legacy_dispatched = (
        legacy_file.exists()
        and not selected_file.exists()
        and ((folder / "model_intent.json").exists() or (folder / "model_response.json").exists())
    )
    if legacy_dispatched:
        # Recover an already paid legacy response; never submit its full-library context anew.
        request = {
            **snapshot,
            "available_visuals": [
                {
                    "segment_id": s["segment_id"],
                    "description": s["description"],
                    "events": s["events"],
                }
                for s in initial["segments"]
            ],
        }
        input_file = legacy_file
    else:
        request = selected_copy_request(snapshot, initial["allocation"], initial["segments"])
        input_file = selected_file
    if input_file.exists() and load(input_file) != request:
        raise Blocked("V3_INPUT_CHANGED", "该版本已有生成回执且素材变化，请先对账", 6)
    if copy_fn is None and not any(
        (folder / n).exists() for n in ("model_intent.json", "model_response.json")
    ):
        from .deepseek import load_model_config

        build_copy_input(request, load_model_config(root)["max_input_chars"])
    if not input_file.exists():
        atomic_write(input_file, request)
    copy = (copy_fn or generate_copy)(root, request, folder)
    if list(Draft202012Validator(COPY_SCHEMA).iter_errors(copy)):
        raise Blocked("V3_COPY_SCHEMA", "文案格式不符", 4)
    from .copy_drafts import effective_copy
    from .voice_timing import calibrate_voice

    copy, _copy_revision = effective_copy(root, group, rows, copy, include_calibration=False)
    by_task = {s["task_id"]: s for s in copy["scripts"]}
    if len(by_task) != len(copy["scripts"]) or set(by_task) != {
        r["fields"]["task_id"] for r in rows
    }:
        raise Blocked("V3_TASK_MISMATCH", "模型遗漏、重复或虚构任务", 4)
    # For legacy completed copy, use only the compact, frozen selected-shot facts for corrections.
    correction_request = selected_copy_request(snapshot, initial["allocation"], initial["segments"])
    copy, audio = calibrate_voice(
        root,
        config,
        folder,
        correction_request,
        copy,
        rows,
        tts_fn or synthesize,
        correction_fn or generate_copy,
    )
    by_task = {s["task_id"]: s for s in copy["scripts"]}
    # Narration is measured against the fixed target. Keep the exact shots shown to copywriting.
    plans, report = initial["allocation"], initial["diversity"]
    template = load(root / "config/executable_montage_v3.json")["typography"]
    scripts = []
    for plan in plans:
        task = plan["task_id"]
        voice, draft = audio[task], by_task[task]
        total = sum(s["duration_frames"] for s in plan["shots"])
        script = {
            **plan,
            "version": 3,
            "revision": version,
            "batch_id": batch_id,
            "group_id": group,
            "input_sha256": digest(snapshot),
            "title": draft["title"],
            "narration": draft["narration"],
            "headline": draft["headline"],
            "typography": template,
            "total_frames": total,
            "fps": 30,
            "canvas": {"width": 1080, "height": 1920},
            "fit": "contain_black",
            "review_status": "pending",
            "approval": None,
            "voiceover": {
                k: voice[k] for k in ["path", "sha256", "duration_frames", "speaker", "speech_rate"]
            }
            | {"start_frame": 6, "gain": 1},
            "captions": aligned_captions(
                draft["narration"], voice["sentences"], voice["duration_seconds"], total
            ),
        }
        script["content_sha256"] = executable_hash(script)
        validate_executable(script, initial["segments"])
        scripts.append(script)
    batch = {
        "version": version,
        "group_id": group,
        "batch_id": batch_id,
        "input_sha256": digest(snapshot),
        "state": "reserved",
        "created_at": now(),
        "scripts": scripts,
        "diversity": report,
    }
    # Recoverable single writer: immutable package first, reservation second, pointer last.
    atomic_write(folder / "prepared_batch.json", batch)
    reserve_batch(root, batch)
    save_batch(root, batch)
    return batch


def validate_current(root: Path, script: dict, *, approved=False) -> None:
    settings = policy(root, script["group_id"])
    if settings.get("workflow_mode") == "script_first_v1":
        raise Blocked("SCRIPT_STAGE_ONLY", "旧音画执行包已退出当前脚本流程", 3)
    batch = latest_batch(root, script["group_id"])
    if (
        not batch
        or batch["batch_id"] != script["batch_id"]
        or batch["state"] == "released"
        or script not in batch["scripts"]
    ):
        raise Blocked("V3_SCRIPT_SUPERSEDED", "脚本已释放或被改版", 6)
    validate_executable(
        script,
        planning_segments(root, settings),
        approved=approved,
    )
    # Reservation binds content hashes, independent of later approval metadata.
    record = root / "ledger/montage_v3" / f"{safe_id(script['batch_id'])}.json"
    if not record.exists() or load(record)["state"] == "released":
        raise Blocked("V3_RESERVATION_MISSING", "当前脚本无有效预占", 6)
    if load(record).get("content_hashes") != [s["content_sha256"] for s in batch["scripts"]]:
        raise Blocked("V3_RESERVATION_STALE", "预占与脚本版本不一致", 6)
    path = Path(script["voiceover"]["path"]).resolve()
    if (
        not path.is_relative_to(root.resolve())
        or not path.is_file()
        or file_hash(path) != script["voiceover"]["sha256"]
    ):
        raise Blocked("V3_AUDIO_CHANGED", "配音文件不存在、越界或内容已变化", 6)


def review_script(
    root: Path,
    group: str,
    task: str,
    expected_hash: str,
    action: str,
    actor: dict,
    rows: list[dict],
) -> dict:
    batch = latest_batch(root, group)
    if not batch or not actor.get("id") or action not in {"approve", "reject"}:
        raise Blocked("V3_REVIEW_REQUIRED", "当前执行脚本不存在或审核无效", 3)
    script = next((s for s in batch["scripts"] if s["task_id"] == task), None)
    if not script or script["content_sha256"] != expected_hash:
        raise Blocked("V3_REVIEW_STALE", "执行脚本版本变化，请刷新核对", 6)
    if action == "approve" and batch["input_sha256"] != digest(
        input_snapshot(rows, policy(root, group))
    ):
        raise Blocked("V3_INPUT_CHANGED", "飞书需求已变更，不能沿用本次批准", 6)
    if action == "reject":
        if batch["state"] == "released":
            return batch
        record = load(root / "ledger/montage_v3" / f"{safe_id(batch['batch_id'])}.json")
        release_batch(root, batch["batch_id"], record["scripts_sha256"], actor)
        batch["state"] = "released"
        for item in batch["scripts"]:
            item.update(review_status="rejected", approval=None)
    else:
        validate_current(root, script)
        if script["review_status"] == "approved":
            return batch
        script.update(
            review_status="approved",
            approval={
                "actor_id": actor["id"],
                "approved_at": now(),
                "approved_content_sha256": expected_hash,
            },
        )
        if all(s["review_status"] == "approved" for s in batch["scripts"]):
            batch["state"] = "approved"
    save_batch(root, batch)
    return batch
