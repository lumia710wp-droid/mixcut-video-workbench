"""Deterministic director handoff, Feishu review gate, and resumable execution.

No model call or editor action is made by submission/review. Callers hold writer_lock.
"""

from __future__ import annotations

from pathlib import Path
from .core import Blocked, atomic_write, digest, load, now, safe_id
from . import script_stage
from .product_scope import scope_values, describe

FIELDS = {
    "director_preview": ("编导剪辑脚本", "text"),
    "director_revision": ("编导版本", "number"),
    "director_sha256": ("编导摘要", "text"),
    "director_review_status": ("编导审核结果", "text"),
}


def folder(root, task):
    return root / "jobs/codex_director_v1" / safe_id(task)


def latest(root, task):
    p = folder(root, task) / "current.json"
    if not p.is_file():
        return None
    record = load(p.parent / f"r{load(p)['revision']:04d}.json")
    if digest(record["packet"]) != record["packet_sha256"]:
        raise Blocked("DIRECTOR_CHANGED", "编导脚本摘要不符，请对账", 6)
    return record


def current_text(root, group, task):
    record = script_stage.latest(root, group)
    script = next((s for s in (record or {}).get("scripts", []) if s["task_id"] == task), None)
    if not script or script["review_status"] != "approved":
        raise Blocked("DIRECTOR_TEXT_REVIEW", "先审核通过此条文字稿，再提交编导脚本", 3)
    if (root / "config/director_typography_v1.json").is_file():
        from .title_style import validate

        validate(script)
    return script


def build_packet(root, plan_path, task):
    from .material_search import validate_director_plan

    plan_path = plan_path.resolve()
    if not plan_path.is_relative_to(root.resolve()):
        raise Blocked("DIRECTOR_PATH", "计划须位于工作区", 2)
    plan = load(plan_path)
    validation = validate_director_plan(root, plan)
    selected = next((t for t in plan["tasks"] if t["task_id"] == task), None)
    if not selected:
        raise Blocked("DIRECTOR_TASK", "计划不包含此任务", 2)
    raw = load(Path(plan["script_path"]))
    scripts = raw if isinstance(raw, list) else raw.get("scripts", [raw])
    script = next((s for s in scripts if s.get("task_id") == task), None)
    if not script or not script.get("content_sha256"):
        raise Blocked("DIRECTOR_TEXT_BINDING", "须绑定有版本摘要的文字稿", 2)
    shots = []
    for shot in selected["shots"]:
        result = load(Path(shot["search_result_path"]))
        candidate = next(
            c for c in result["candidates"] if c["candidate_id"] == shot["candidate_id"]
        )
        shots.append(
            {
                k: shot[k]
                for k in (
                    "need_id",
                    "source_in_frame",
                    "source_out_frame",
                    "timeline_start_frame",
                    "duration_frames",
                    "speed",
                    "source_audio_gain",
                    "transition_out",
                    "rationale",
                    "selection_basis",
                )
            }
        )
        source = next(
            s
            for s in load(root / "catalog/sources.json")
            if s["source_id"] == candidate["source_id"]
        )
        name = next(
            (x["relative_path"] for x in source.get("locations", [])), candidate["source_id"]
        )
        shots[-1].update(source_id=candidate["source_id"], source_name=name)
        if "goods_scope" in plan:
            shots[-1]["goods_id"] = candidate["goods_id"]
    from .director_execution import media_preflight

    preflight = media_preflight(root, {"shots": shots}, load(root / "config/config.local.json"))
    for shot, media in zip(shots, preflight["items"]):
        if media["recommended_transform"]:
            shot["transform"] = media["recommended_transform"]
    if any(x["letterbox_detected"] and not x["recommended_transform"] for x in preflight["items"]):
        raise Blocked("DIRECTOR_CROP_REQUIRED", "画幅需人工处理，请先确定裁切再提交编导", 3)
    return {
        "version": 1,
        "entity": "director_review_packet",
        "task_id": task,
        **scope_values(plan),
        "text_sha256": script["content_sha256"],
        "total_frames": selected["total_frames"],
        "shots": shots,
        "origin_plan_sha256": validation["plan_sha256"],
    }


def validate_packet(root, group, packet):
    from .material_search import _settings
    from .fact_planning import planning_segments
    from .montage import needed_frames, overlap, diversity

    task = safe_id(packet["task_id"])
    script = current_text(root, group, task)
    if packet.get("entity") != "director_review_packet" or packet.get("version") != 1:
        raise Blocked("DIRECTOR_PACKET", "编导交接格式不符", 2)
    if packet["text_sha256"] != script["content_sha256"]:
        raise Blocked("DIRECTOR_TEXT_STALE", "文字稿已变化，请重新编导审核", 6)
    if packet["total_frames"] != round(script["target_seconds"] * 30):
        raise Blocked("DIRECTOR_TIMING", "编导时长与人工目标不同", 2)
    settings = _settings(root, {**scope_values(packet), "group_id": group})
    segments = planning_segments(root, settings)
    facts = {
        f["source"]["source_id"]: f
        for f in load(root / "catalog/material_facts_v1.json")["materials"]
    }
    cursor = 0
    seen: list[dict] = []
    for shot in packet["shots"]:
        sid = shot["source_id"]
        a, b, d = (shot[k] for k in ("source_in_frame", "source_out_frame", "duration_frames"))
        if any(
            isinstance(v, bool) or not isinstance(v, int)
            for v in (a, b, d, shot["timeline_start_frame"])
        ):
            raise Blocked("DIRECTOR_TIMING", "镜头区间须为整数帧", 2)
        if (
            d <= 0
            or a < 0
            or isinstance(shot["speed"], bool)
            or not 1 <= shot["speed"] <= 1.25
            or b - a != needed_frames(d, shot["speed"])
        ):
            raise Blocked("DIRECTOR_TIMING", "源区间、速度或时长不符", 2)
        transform = shot.get("transform")
        if transform is not None and (
            set(transform) != {"scale"} or not 1 <= transform["scale"] <= 1.4
        ):
            raise Blocked("DIRECTOR_TRANSFORM", "只支持审核中展示的居中裁切缩放", 2)
        if not isinstance(shot.get("rationale"), str) or not shot["rationale"].strip():
            raise Blocked("DIRECTOR_REASON", "缺少选材理由", 2)
        if shot["timeline_start_frame"] != cursor or shot["source_audio_gain"] != 0:
            raise Blocked("DIRECTOR_TIMING", "时间线须连续且原声静音", 2)
        basis = shot["selection_basis"]
        if sid not in facts or basis["facts_sha256"] != facts[sid]["content_sha256"]:
            raise Blocked("DIRECTOR_FACT_STALE", "入库事实已变化，请重新检索", 6)
        if basis.get("type") != "indexed_facts" or basis.get("document_id") not in {
            "phase_" + digest([sid, facts[sid]["content_sha256"], n])[:24]
            for n in range(len(facts[sid]["phases"]))
        }:
            raise Blocked("DIRECTOR_FACT_BINDING", "镜头须引用当前入库事实阶段", 6)
        if not any(
            s["source_id"] == sid
            and s["source_in_frame"] <= a < b <= s["source_out_frame"]
            and ("goods_scope" not in settings or shot.get("goods_id") == s.get("goods_id"))
            for s in segments
        ):
            raise Blocked("DIRECTOR_SCOPE", "镜头超出已确认商品范围", 6)
        if shot["transition_out"] != {"type": "hard_cut", "frames": 0} or any(
            overlap(shot, s) for s in seen
        ):
            raise Blocked("DIRECTOR_OVERLAP", "镜头重复或转场规则不符", 2)
        seen.append(shot)
        cursor += d
    if not seen or cursor != packet["total_frames"]:
        raise Blocked("DIRECTOR_TIMING", "镜头总长不符", 2)
    batch = [{"task_id": task, "shots": packet["shots"]}]
    for p in (root / "jobs/codex_director_v1").glob("*/current.json"):
        other = latest(root, p.parent.name)
        if (
            other["task_id"] != task
            and other["group_id"] == group
            and other["review_status"] != "rejected"
        ):
            old_script = next(
                (
                    s
                    for s in script_stage.latest(root, group)["scripts"]
                    if s["task_id"] == other["task_id"]
                ),
                None,
            )
            if old_script and old_script["content_sha256"] == other["packet"]["text_sha256"]:
                for x in other["packet"]["shots"]:
                    if any(overlap(x, s) for s in seen):
                        raise Blocked("DIRECTOR_BATCH_OVERLAP", "与本批其他任务源区间重复", 6)
                batch.append({"task_id": other["task_id"], "shots": other["packet"]["shots"]})
    return diversity(batch, settings.get("minimum_unique", 0.7))


def preview(record, script):
    lines = [
        f"编导剪辑脚本 r{record['revision']} · {record['review_status']}",
        f"{script['title']}｜{record['packet']['total_frames'] / 30:g}秒",
        f"口播：{script['narration']}",
    ]
    if "goods_scope" in record["packet"]:
        lines.append("素材范围：" + describe(record["packet"]))
    for i, s in enumerate(record["packet"]["shots"], 1):
        lines.append(
            f"{i}. 成片{s['timeline_start_frame'] / 30:g}–{(s['timeline_start_frame'] + s['duration_frames']) / 30:g}秒｜"
            + (f"{s['goods_id']}｜" if s.get("goods_id") else "")
            + f"{s.get('source_name', s['source_id'])}｜源{s['source_in_frame'] / 30:g}–{s['source_out_frame'] / 30:g}秒｜"
            f"{s['speed']:g}倍、硬切、原声静音｜{s['rationale']}"
            + (f"｜居中放大{s['transform']['scale']:g}裁黑边" if s.get("transform") else "")
        )
    lines += [
        "审核通过后才生成配音及剪辑；配音实测不符时不自动改文案。",
        f"配音：{record['packet']['voice']['name']}，语速+8；口播目标占成片85%—95%。",
        f"摘要：{record['packet_sha256']}",
    ]
    return "\n".join(lines)


def save(root, record):
    f = folder(root, record["task_id"])
    atomic_write(f / f"r{record['revision']:04d}.json", record)
    atomic_write(f / "current.json", {"revision": record["revision"]})


def submit(root, group, packet):
    from .director_execution import assign_voice

    packet = {**packet, "voice": assign_voice(root, packet["task_id"])}
    validation = validate_packet(root, group, packet)
    task = packet["task_id"]
    prior = latest(root, task)
    if prior and prior["packet_sha256"] == digest(packet):
        return prior
    record = {
        "task_id": task,
        "group_id": group,
        "revision": (prior or {}).get("revision", 0) + 1,
        "packet": packet,
        "packet_sha256": digest(packet),
        "review_status": "pending",
        "approval": None,
        "sync_status": "pending",
        "created_at": now(),
        "diversity": validation,
    }
    save(root, record)
    return record


def publish(bench, task):
    record = latest(bench.root, task)
    if not record:
        raise Blocked("DIRECTOR_MISSING", "没有待回填的编导脚本", 3)
    script = current_text(bench.root, record["group_id"], task)
    if script["content_sha256"] != record["packet"]["text_sha256"]:
        raise Blocked("DIRECTOR_TEXT_STALE", "文案已变化，旧编导脚本不能回填", 6)
    patch = {
        "director_preview": preview(record, script),
        "director_revision": record["revision"],
        "director_sha256": record["packet_sha256"],
        "director_review_status": record["review_status"],
    }
    if not set(patch) <= set(bench.mapping["tables"]["tasks"]["field_ids"]):
        raise Blocked("DIRECTOR_SCHEMA", "先补齐编导审核字段", 3)
    row = bench.task(task)
    # Reconcile an acknowledged/unknown previous write before considering another write.
    if any(row["fields"].get(k) != v for k, v in patch.items()):
        receipt = (
            folder(bench.root, task)
            / f"publish_r{record['revision']:04d}_{record['review_status']}.json"
        )
        if receipt.exists():
            raise Blocked("DIRECTOR_SYNC_UNKNOWN", "回填结果尚未读回，禁止重复写入；请刷新对账", 6)
        atomic_write(
            receipt, {"status": "write_unknown", "patch_sha256": digest(patch), "at": now()}
        )
        bench.patch(row["record_id"], patch)
    bench.verify_patch(task, patch)
    record["sync_status"] = "synced"
    save(bench.root, record)
    return record


def review(bench, task, expected_hash, action, actor):
    record = latest(bench.root, task)
    if (
        not record
        or record["packet_sha256"] != expected_hash
        or action not in ("approve", "reject")
    ):
        raise Blocked("DIRECTOR_REVIEW_STALE", "编导版本已变化，请刷新", 6)
    if not actor.get("id"):
        raise Blocked("DIRECTOR_ACTOR", "缺少真实审核身份", 3)
    row = bench.task(task)["fields"]
    if (
        row.get("director_sha256") != expected_hash
        or row.get("director_revision") != record["revision"]
    ):
        raise Blocked("DIRECTOR_SYNC_REQUIRED", "先回填并读回当前编导版本再审核", 6)
    validate_packet(bench.root, record["group_id"], record["packet"])
    record["review_status"] = "approved" if action == "approve" else "rejected"
    record["sync_status"] = "pending"
    record["approval"] = (
        {"actor_id": actor["id"], "at": now(), "packet_sha256": expected_hash}
        if action == "approve"
        else None
    )
    save(bench.root, record)
    return publish(bench, task)


def gate(root, task):
    record = latest(root, task)
    if (
        not record
        or record["review_status"] != "approved"
        or record["sync_status"] != "synced"
        or not record["approval"]
    ):
        raise Blocked("DIRECTOR_REVIEW_REQUIRED", "须在飞书审核通过当前编导脚本，再配音剪辑", 3)
    if record["approval"]["packet_sha256"] != record["packet_sha256"]:
        raise Blocked("DIRECTOR_REVIEW_STALE", "编导审核版本不符", 6)
    validate_packet(root, record["group_id"], record["packet"])
    return record


def claim_step(root, task, name, parameters):
    record = gate(root, task)
    path = folder(root, task) / f"steps/r{record['revision']:04d}_{safe_id(name)}.json"
    key = digest({"packet": record["packet_sha256"], "parameters": parameters})
    if path.exists():
        prior = load(path)
        if prior["key"] != key:
            raise Blocked("DIRECTOR_STEP_CHANGED", "步骤参数已变化，须新版本", 6)
        if prior["status"] != "completed":
            raise Blocked(
                "DIRECTOR_STEP_UNKNOWN", "步骤结果未知，须核对已有工程/导出回执，禁止重复执行", 6
            )
        return {**prior, "reused": True}
    intent = {"key": key, "status": "unknown", "at": now(), "parameters": parameters}
    atomic_write(path, intent)
    return {**intent, "reused": False}


def complete_step(root, task, name, parameters, result):
    record = gate(root, task)
    path = folder(root, task) / f"steps/r{record['revision']:04d}_{safe_id(name)}.json"
    prior = load(path)
    if prior["key"] != digest({"packet": record["packet_sha256"], "parameters": parameters}):
        raise Blocked("DIRECTOR_STEP_CHANGED", "步骤绑定变化", 6)
    if prior["status"] == "completed" and prior["result"] != result:
        raise Blocked("DIRECTOR_STEP_CONFLICT", "已完成步骤不能覆盖", 6)
    prior.update(status="completed", result=result, completed_at=now())
    atomic_write(path, prior)
    return prior
