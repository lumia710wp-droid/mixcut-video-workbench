"""Bounded batch-wide allocation and hash-bound executable-script reservations."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from .core import Blocked, atomic_write, digest, load, now, safe_id


def needed_frames(duration: int, speed: float) -> int:
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration <= 0
        or isinstance(speed, bool)
        or not isinstance(speed, (int, float))
        or not math.isfinite(speed)
        or not 1 <= speed <= 1.25
    ):
        raise Blocked("V3_TIMING", "播放速度须为1—1.25，时长须为整数帧", 2)
    return int((Decimal(duration) * Decimal(str(speed))).to_integral_value(rounding=ROUND_HALF_UP))


def overlap(a: dict, b: dict) -> bool:
    return (
        a["source_id"] == b["source_id"]
        and a["source_in_frame"] < b["source_out_frame"]
        and b["source_in_frame"] < a["source_out_frame"]
    )


def diversity(scripts: list[dict], minimum: float = 0.7) -> dict:
    if not scripts or any(not s.get("shots") for s in scripts):
        raise Blocked("V3_SHOTS", "批次没有完整画面轨", 2)
    if not 0 <= minimum <= 1:
        raise Blocked("V3_POLICY", "独有素材比例无效", 2)
    if len({s["task_id"] for s in scripts}) != len(scripts):
        raise Blocked("V3_TASK_DUPLICATE", "同批任务编号重复", 2)
    all_shots = [(s["task_id"], shot) for s in scripts for shot in s["shots"]]
    for n, (task, shot) in enumerate(all_shots):
        for other_task, other in all_shots[:n]:
            if overlap(shot, other):
                raise Blocked("V3_SOURCE_OVERLAP", f"{task}与{other_task}使用重叠源区间", 3)
    openings = [s["shots"][0]["source_id"] for s in scripts]
    if len(set(openings)) != len(openings):
        raise Blocked("V3_OPENING_REPEATED", "同批视频开头必须来自不同原片", 3)
    combinations = [frozenset(s["source_id"] for s in script["shots"]) for script in scripts]
    if len(set(combinations)) != len(combinations):
        raise Blocked("V3_COMBINATION_REPEATED", "只更换镜头顺序不算新的素材组合", 3)
    rows = []
    for script in scripts:
        others = {shot["source_id"] for task, shot in all_shots if task != script["task_id"]}
        total = sum(s["duration_frames"] for s in script["shots"])
        unique = sum(s["duration_frames"] for s in script["shots"] if s["source_id"] not in others)
        ratio = unique / total
        rows.append(
            {
                "task_id": script["task_id"],
                "unique_source_duration_ratio": ratio,
                "source_count": len({s["source_id"] for s in script["shots"]}),
                "opening_source_id": script["shots"][0]["source_id"],
            }
        )
        if ratio + 1e-9 < minimum:
            raise Blocked(
                "V3_DIVERSITY_SHORTAGE", f"{script['task_id']}独有原片时长低于{minimum:.0%}", 3
            )
    return {
        "status": "passed",
        "minimum_unique_source_ratio": minimum,
        "cross_video_overlap_frames": 0,
        "scripts": rows,
    }


def allocate_batch(
    segments: list[dict],
    requests: list[dict],
    *,
    reserved=(),
    speed: float = 1.1,
    minimum_unique: float = 0.7,
    max_nodes: int = 40000,
) -> tuple[list[dict], dict]:
    """Search the entire batch, not repeated independent best-match lookups."""
    if not segments:
        raise Blocked("V3_NO_REVIEWED_SEGMENTS", "请先核对素材的分段内容、切点及商品归属", 3)
    if not requests or len(requests) > 30:
        raise Blocked("V3_BATCH_SIZE", "批次须包含1—30条视频", 2)
    for r in requests:
        safe_id(r["task_id"])
    if len({r["task_id"] for r in requests}) != len(requests):
        raise Blocked("V3_TASK_DUPLICATE", "任务编号重复", 2)
    # Alternating cadence is fixed before search; real narration defines exact total frames.
    slot_lists = []
    for request in requests:
        total = request["total_frames"]
        if isinstance(total, bool) or not isinstance(total, int) or not 180 <= total <= 3600:
            raise Blocked("V3_DURATION", "成片时长须为6—120秒", 2)
        count = max(4, math.ceil(total / 75))
        weights = [0.8 if i % 2 == 0 else 1.2 for i in range(count)]
        lengths = [round(total * w / sum(weights)) for w in weights]
        lengths[-1] += total - sum(lengths)
        slot_lists.append(lengths)
    nodes = 0
    chosen: list[dict] = []
    completed: list[dict] = []
    ordered = sorted(
        segments, key=lambda s: (s["source_id"], s["source_in_frame"], s["segment_id"])
    )
    unavailable = list(reserved)

    def search(task_index: int, slot_index: int, shots: list[dict]):
        nonlocal nodes
        if task_index == len(requests):
            try:
                report = diversity(completed, minimum_unique)
            except Blocked:
                return None
            return [dict(s, shots=list(s["shots"])) for s in completed], report
        lengths = slot_lists[task_index]
        if slot_index == len(lengths):
            completed.append({"task_id": requests[task_index]["task_id"], "shots": list(shots)})
            result = search(task_index + 1, 0, [])
            completed.pop()
            return result
        duration = lengths[slot_index]
        needed = needed_frames(duration, speed)
        occupied = unavailable + chosen
        prior_sources = {s["source_id"] for s in chosen}
        own_sources = {s["source_id"] for s in shots}
        other_sources = {s["source_id"] for p in completed for s in p["shots"]}
        reuse_own = len(own_sources) >= min(4, len(lengths))
        # Prefer new sources; different roles are useful but cannot override legal ranges.
        candidates = sorted(
            ordered,
            key=lambda s: (
                s["source_id"] in other_sources,
                (s["source_id"] not in own_sources)
                if reuse_own
                else (s["source_id"] in prior_sources),
                s.get("planning_penalty", 0),
                slot_index == 0 and s["role"] not in {"opening", "general"},
                s["segment_id"],
            ),
        )
        for segment in candidates:
            source = segment["source_id"]
            if slot_index == 0 and any(s["shots"][0]["source_id"] == source for s in completed):
                continue
            if shots and shots[-1]["source_id"] == source:
                continue
            start = segment["source_in_frame"]
            # Skip every active reservation, including not-yet-delivered scripts.
            for used in sorted(
                (w for w in occupied if w["source_id"] == source),
                key=lambda w: w["source_in_frame"],
            ):
                if start < used["source_out_frame"] and used["source_in_frame"] < start + needed:
                    start = used["source_out_frame"]
            if start + needed > segment["source_out_frame"]:
                continue
            nodes += 1
            if nodes > max_nodes:
                raise Blocked("V3_SEARCH_LIMIT", "批次规划达到搜索上限，尚不能证明素材不足", 3)
            observed = segment.get("observed_phases", [])
            description = (
                "；".join(
                    p["observation"]
                    for p in observed
                    if p["start_sec"] * 30 < start + needed and p["end_sec"] * 30 > start
                )
                or segment["description"]
            )
            shot = {
                "shot_id": f"M{slot_index + 1:02d}",
                "segment_id": segment["segment_id"],
                "segment_sha256": segment["content_sha256"],
                "source_id": source,
                "source_sha256": segment["source_sha256"],
                "source_in_frame": start,
                "source_out_frame": start + needed,
                "timeline_start_frame": sum(s["duration_frames"] for s in shots),
                "duration_frames": duration,
                "speed": speed,
                "source_audio_gain": 0,
                "description": description,
                "transition_out": {"type": "hard_cut", "frames": 0},
            }
            chosen.append(shot)
            shots.append(shot)
            result = search(task_index, slot_index + 1, shots)
            shots.pop()
            chosen.pop()
            if result:
                return result
        return None

    answer = search(0, 0, [])
    if answer is None:
        raise Blocked(
            "V3_MATERIAL_SHORTAGE",
            "现有素材无法满足整批时长、不同开头和独有素材比例；请补充对应商品素材或调整批次需求",
            3,
        )
    return answer


def validate_allocation(
    plans: list[dict],
    segments: list[dict],
    requests: list[dict],
    *,
    reserved=(),
    speed: float = 1.1,
    minimum_unique: float = 0.7,
) -> dict:
    """Revalidate a saved selection before reusing its paid copy/voice results."""
    targets = {r["task_id"]: r["total_frames"] for r in requests}
    if len(plans) != len(targets) or {p["task_id"] for p in plans} != set(targets):
        raise Blocked("V3_INPUT_CHANGED", "已选画面与当前任务不符，请对账", 6)
    by_id = {s["segment_id"]: s for s in segments}
    for plan in plans:
        cursor = 0
        for shot in plan["shots"]:
            segment = by_id.get(shot["segment_id"])
            if (
                not segment
                or segment["content_sha256"] != shot["segment_sha256"]
                or segment["source_id"] != shot["source_id"]
                or segment["source_sha256"] != shot["source_sha256"]
                or not segment["source_in_frame"]
                <= shot["source_in_frame"]
                < shot["source_out_frame"]
                <= segment["source_out_frame"]
            ):
                raise Blocked("V3_SEGMENT_STALE", "已选素材变化或不可用，请先对账", 6)
            if (
                shot["timeline_start_frame"] != cursor
                or shot["speed"] != speed
                or shot["source_audio_gain"] != 0
                or shot["transition_out"] != {"type": "hard_cut", "frames": 0}
                or shot["source_out_frame"] - shot["source_in_frame"]
                != needed_frames(shot["duration_frames"], speed)
            ):
                raise Blocked("V3_TIMELINE", "已选镜头时序或速度不符", 6)
            if any(overlap(shot, used) for used in reserved):
                raise Blocked("V3_RESERVATION_OVERLAP", "已选区间已被其他批次预占", 6)
            cursor += shot["duration_frames"]
        if cursor != targets[plan["task_id"]]:
            raise Blocked("V3_INPUT_CHANGED", "已选画面与目标时长不符", 6)
    return diversity(plans, minimum_unique)


def executable_hash(script: dict) -> str:
    return digest(
        {
            k: v
            for k, v in script.items()
            if k not in {"content_sha256", "approval", "review_status", "updated_at"}
        }
    )


def validate_executable(script: dict, segments: list[dict], *, approved: bool = False) -> None:
    if script.get("version") != 3 or script.get("content_sha256") != executable_hash(script):
        raise Blocked("V3_SCRIPT_HASH", "执行脚本版本或摘要不符", 6)
    if script.get("is_fixture"):
        raise Blocked("FIXTURE_BLOCKED", "测试执行脚本不能用于生产", 3)
    from jsonschema import Draft202012Validator

    schema = __import__("json").loads(
        (Path(__file__).parent / "executable_v3_schema.json").read_text()
    )
    if list(Draft202012Validator(schema).iter_errors(script)):
        raise Blocked("V3_SCRIPT_SCHEMA", "执行脚本格式不完整或参数无效", 2)
    by_id = {s["segment_id"]: s for s in segments}
    cursor = 0
    if not script.get("shots"):
        raise Blocked("V3_SHOTS", "执行脚本没有画面轨", 2)
    for shot in script["shots"]:
        segment = by_id.get(shot["segment_id"])
        if (
            not segment
            or segment["content_sha256"] != shot["segment_sha256"]
            or segment["source_sha256"] != shot["source_sha256"]
            or segment["source_id"] != shot["source_id"]
            or not segment["source_in_frame"]
            <= shot["source_in_frame"]
            < shot["source_out_frame"]
            <= segment["source_out_frame"]
        ):
            raise Blocked("V3_SEGMENT_STALE", "素材分段变化、越界或不存在", 6)
        if (
            shot["timeline_start_frame"] != cursor
            or shot["source_audio_gain"] != 0
            or shot["source_out_frame"] - shot["source_in_frame"]
            != needed_frames(shot["duration_frames"], shot["speed"])
        ):
            raise Blocked("V3_TIMELINE", "镜头起点、时长、速度或源帧不一致", 6)
        if shot.get("transition_out") != {"type": "hard_cut", "frames": 0}:
            raise Blocked("V3_TRANSITION_UNSUPPORTED", "当前执行器仅验证硬切，不能静默替换转场", 3)
        cursor += shot["duration_frames"]
    if cursor != script["total_frames"]:
        raise Blocked("V3_TIMELINE", "画面轨总时长不符", 6)
    voice = script["voiceover"]
    if not (
        voice["sha256"]
        and 0 <= voice["start_frame"]
        and voice["start_frame"] + voice["duration_frames"] <= cursor
    ):
        raise Blocked("V3_AUDIO", "配音范围或摘要无效", 6)
    from .doubao import clean_text

    if not script["captions"] or clean_text(
        "".join(c["text"] for c in script["captions"])
    ) != clean_text(script["narration"]):
        raise Blocked("V3_CAPTION_TEXT", "字幕与完整口播不一致", 6)
    previous = 0
    for cap in script["captions"]:
        if not cap["text"] or not previous <= cap["start_frame"] < cap["end_frame"] <= cursor:
            raise Blocked("V3_CAPTION", "字幕重叠或越界", 6)
        previous = cap["end_frame"]
    for i, shot in enumerate(script["shots"]):
        if any(overlap(shot, other) for other in script["shots"][:i]):
            raise Blocked("V3_SOURCE_OVERLAP", "单条视频重复源区间", 6)
    if approved:
        approval = script.get("approval") or {}
        if (
            script.get("review_status") != "approved"
            or approval.get("approved_content_sha256") != script["content_sha256"]
            or not approval.get("actor_id")
            or not approval.get("approved_at")
        ):
            raise Blocked("V3_SCRIPT_UNAPPROVED", "当前完整执行脚本尚未批准", 3)


def reservations(root: Path) -> list[dict]:
    records = [load(p) for p in (root / "ledger/montage_v3").glob("*.json")]
    return [r for r in records if r.get("state") in {"reserved", "approved", "consumed"}]


def reserve_batch(root: Path, batch: dict) -> None:
    """Caller holds writer_lock. One atomic file reserves the whole batch."""
    batch_id = safe_id(batch["batch_id"])
    path = root / "ledger/montage_v3" / f"{batch_id}.json"
    h = digest(batch["scripts"])
    if path.exists():
        old = load(path)
        if old["scripts_sha256"] == h and old["state"] in {"reserved", "approved", "consumed"}:
            return
        raise Blocked("V3_RESERVATION_CONFLICT", "该批已预占；取消/改版后才能重新规划", 6)
    shots = [s for script in batch["scripts"] for s in script["shots"]]
    for other in reservations(root):
        if any(overlap(a, b) for a in shots for b in other["shots"]):
            raise Blocked("V3_RESERVATION_OVERLAP", "源区间已被其他未释放脚本预占", 6)
    atomic_write(
        path,
        {
            "batch_id": batch_id,
            "state": "reserved",
            "scripts_sha256": h,
            "content_hashes": [s["content_sha256"] for s in batch["scripts"]],
            "shots": shots,
            "created_at": now(),
        },
    )


def release_batch(root: Path, batch_id: str, expected_sha256: str, actor: dict) -> dict:
    path = root / "ledger/montage_v3" / f"{safe_id(batch_id)}.json"
    row = load(path)
    if row["scripts_sha256"] != expected_sha256 or not actor.get("id"):
        raise Blocked("V3_RESERVATION_STALE", "预占版本变化或操作者缺失", 6)
    if row["state"] == "consumed":
        raise Blocked("V3_ALREADY_CONSUMED", "已交付记录不能当作未使用素材释放", 6)
    row.update(state="released", released_at=now(), released_by=actor["id"])
    atomic_write(path, row)
    return row
