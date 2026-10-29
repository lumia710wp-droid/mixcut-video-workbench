"""Versioned, evidence-backed source segments. Suggestions never imply approval."""

from __future__ import annotations

import math
from pathlib import Path
from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id
from .contracts import inward_frames, require_approval

INDEX = "catalog/segments_v3.json"


def segment_hash(row: dict) -> str:
    return digest(
        {
            k: v
            for k, v in row.items()
            if k not in {"content_sha256", "approval", "review_status", "updated_at"}
        }
    )


def read_segments(root: Path) -> dict:
    path = root / INDEX
    return load(path) if path.exists() else {"version": 3, "segments": []}


def validate_segment(row: dict, sources: dict) -> None:
    required = {
        "segment_id",
        "source_id",
        "source_sha256",
        "parent_clip_id",
        "source_in_frame",
        "source_out_frame",
        "description",
        "role",
        "events",
        "evidence",
        "precision",
        "content_sha256",
    }
    if not required <= row.keys():
        raise Blocked("SEGMENT_SCHEMA", "镜头分段缺少必要字段", 2)
    safe_id(row["segment_id"])
    source = sources.get(row["source_id"])
    a, b = row["source_in_frame"], row["source_out_frame"]
    if (
        not source
        or source["file_sha256"] != row["source_sha256"]
        or any(isinstance(n, bool) or not isinstance(n, int) for n in (a, b))
        or not 0 <= a < b <= math.floor(source["duration_sec"] * 30)
    ):
        raise Blocked("SEGMENT_RANGE", "分段越界或源文件版本变化", 6)
    if row["role"] not in {"opening", "body", "ending", "general"}:
        raise Blocked("SEGMENT_ROLE", "镜头用途无效", 2)
    if row["precision"] not in {"sampled_suggestion", "cut_points_reviewed"}:
        raise Blocked("SEGMENT_PRECISION", "必须区分抽样建议与已核对切点", 2)
    if not isinstance(row["events"], list) or not isinstance(row["description"], str):
        raise Blocked("SEGMENT_SCHEMA", "事件和画面描述格式无效", 2)
    for event in row["events"]:
        if (
            not isinstance(event, dict)
            or not isinstance(event.get("text"), str)
            or isinstance(event.get("frame"), bool)
            or not isinstance(event.get("frame"), int)
            or not a <= event["frame"] < b
        ):
            raise Blocked("SEGMENT_EVENT", "动作点不在分段内", 2)
    if not row["description"] or not row["evidence"]:
        raise Blocked("SEGMENT_EVIDENCE", "分段需要画面描述与观看证据", 2)
    if row["content_sha256"] != segment_hash(row):
        raise Blocked("SEGMENT_HASH", "分段内容摘要变化", 6)


def import_suggestions(root: Path) -> dict:
    """Reuse recorded visual evidence. Do not invent per-second observations."""
    clips = {r["clip_id"]: r for r in load(root / "catalog/clips.json")}
    sources = {r["source_id"]: r for r in load(root / "catalog/sources.json")}
    index = load(root / "catalog/ai_analysis_index.json")
    current = read_segments(root)
    known = {r["segment_id"]: r for r in current["segments"]}
    added = 0
    for ref in index.get("records", []):
        path = Path(ref["proposal_path"])
        if not path.is_file():
            # Relocated workspaces retain historical absolute references.
            try:
                suffix = path.parts[path.parts.index("analysis") :]
                path = root.joinpath(*suffix)
            except ValueError:
                continue
        path = path.resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            continue
        record = load(path)
        clip = clips.get(record.get("clip_id"))
        if not clip or record.get("source_sha256") != sources[clip["source_id"]]["file_sha256"]:
            continue
        for window in record.get("candidate_windows", []):
            a, b = inward_frames(window["start_sec"], window["end_sec"])
            sid = "seg_" + digest([clip["source_id"], record["analysis_sha256"], a, b])[:24]
            if sid in known:
                continue
            raw_events = record.get("observations", {}).get("events", [])
            events = []
            if isinstance(raw_events, list):
                for event in raw_events:
                    if (
                        isinstance(event, list)
                        and len(event) >= 2
                        and isinstance(event[0], (int, float))
                        and isinstance(event[1], str)
                        and a <= round(event[0] * 30) < b
                    ):
                        events.append({"frame": round(event[0] * 30), "text": event[1]})
            row = {
                "segment_id": sid,
                "source_id": clip["source_id"],
                "source_sha256": sources[clip["source_id"]]["file_sha256"],
                "parent_clip_id": clip["clip_id"],
                "source_in_frame": a,
                "source_out_frame": b,
                "description": record.get("proposal", {}).get("description") or clip["description"],
                "role": "general",
                "events": events,
                "evidence": {
                    "analysis_sha256": record["analysis_sha256"],
                    "proposal_relative_path": str(path.relative_to(root)),
                    "window_evidence": window.get("evidence", ""),
                },
                "precision": "sampled_suggestion",
                "review_status": "pending",
                "approval": None,
                "version": 1,
            }
            row["content_sha256"] = segment_hash(row)
            validate_segment(row, sources)
            known[sid] = row
            added += 1
    current.update(segments=list(known.values()), updated_at=now())
    atomic_write(root / INDEX, current)
    return {
        "status": "suggestions_imported",
        "added": added,
        "total": len(known),
        "approved_automatically": 0,
    }


def review_segments(root: Path, clip_id: str, body: dict, actor: dict) -> dict:
    """Caller holds writer_lock; exact parent and index hashes guard concurrent edits."""
    safe_id(clip_id)
    if body.get("confirmed") is not True or not actor.get("id"):
        raise Blocked("SEGMENT_REVIEW_REQUIRED", "需由登录用户确认观看证据和切点", 4)
    clips = {r["clip_id"]: r for r in load(root / "catalog/clips.json")}
    clip = clips.get(clip_id)
    if not clip or body.get("parent_sha256") != clip["content_sha256"]:
        raise Blocked("MATERIAL_STALE", "父素材内容已变化，请刷新", 6)
    require_approval(clip)
    index = read_segments(root)
    if body.get("index_sha256") != digest(index):
        raise Blocked("SEGMENT_STALE", "分段列表已变化，请刷新核对", 6)
    sources = {r["source_id"]: r for r in load(root / "catalog/sources.json")}
    edits = body.get("segments")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
        raise Blocked("SEGMENT_SCHEMA", "请选择需要确认的分段", 2)
    by_id = {s["segment_id"]: s for s in index["segments"]}
    reviewed = []
    for edit in edits:
        sid = edit.get("segment_id")
        if sid not in by_id or by_id[sid]["parent_clip_id"] != clip_id:
            raise Blocked("SEGMENT_SCOPE", "分段不属于当前素材", 6)
        old = by_id[sid]
        if edit.get("expected_sha256") != old["content_sha256"]:
            raise Blocked("SEGMENT_STALE", "分段版本变化", 6)
        if old.get("duplicate_review_required") or old.get("superseded_suggestion"):
            raise Blocked(
                "SEGMENT_REVIEW_BLOCKED", "疑似重复或已被更细建议替代，请先核对并选择有效分段", 3
            )
        row = {
            **old,
            **{
                k: edit[k]
                for k in ("source_in_frame", "source_out_frame", "description", "role", "events")
                if k in edit
            },
        }
        lo, hi = inward_frames(clip["usable_start_sec"], clip["usable_end_sec"])
        if not lo <= row["source_in_frame"] < row["source_out_frame"] <= hi:
            raise Blocked("SEGMENT_PARENT_RANGE", "需先在素材审核中确认新的父素材可用区间", 6)
        row.update(
            precision="cut_points_reviewed",
            version=old["version"] + 1,
            parent_content_sha256=clip["content_sha256"],
            review_status="approved",
        )
        row["content_sha256"] = segment_hash(row)
        row["approval"] = {
            "actor_id": actor["id"],
            "approved_at": now(),
            "approved_content_sha256": row["content_sha256"],
            "channel": "workbench_explicit_segment_review",
        }
        validate_segment(row, sources)
        by_id[sid] = row
        reviewed.append(sid)
    index.update(segments=list(by_id.values()), updated_at=now())
    atomic_write(root / INDEX, index)
    return {"status": "approved", "segment_ids": reviewed}


def eligible_segments(root: Path, brand: str, goods: str) -> list[dict]:
    clips = {r["clip_id"]: r for r in load(root / "catalog/clips.json")}
    sources = {r["source_id"]: r for r in load(root / "catalog/sources.json")}
    eligible = []
    for s in read_segments(root)["segments"]:
        try:
            validate_segment(s, sources)
            clip = clips[s["parent_clip_id"]]
            require_approval(clip)
            approval = s.get("approval") or {}
            if (
                (clip["brand_id"], clip["goods_id"]) != (brand, goods)
                or s.get("duplicate_review_required")
                or s.get("superseded_suggestion")
                or s["precision"] != "cut_points_reviewed"
                or s["review_status"] != "approved"
                or approval.get("approved_content_sha256") != s["content_sha256"]
                or not approval.get("actor_id")
                or not approval.get("approved_at")
                or s.get("parent_content_sha256") != clip["content_sha256"]
            ):
                continue
            lo, hi = inward_frames(clip["usable_start_sec"], clip["usable_end_sec"])
            if lo <= s["source_in_frame"] < s["source_out_frame"] <= hi:
                eligible.append(s)
        except (Blocked, KeyError):
            continue
    return eligible


def source_file(root: Path, config: dict, source: dict) -> Path:
    """Original media stays read-only and inside its configured root."""
    roots = {r["root_id"]: Path(r["path"]).resolve() for r in config["source_roots"]}
    for loc in source["locations"]:
        base = roots.get(loc["root_id"])
        if base is None:
            continue
        path = (base / loc["relative_path"]).resolve()
        if path.is_relative_to(base) and path.is_file():
            if file_hash(path) != source["file_sha256"]:
                raise Blocked("SOURCE_CHANGED", source["source_id"], 6)
            return path
    raise Blocked("SOURCE_UNAVAILABLE", "原片未挂载或路径不可读", 3)


def add_proposals(root: Path, proposals: dict) -> dict:
    """Append AI proposals backed by existing, hash-verified local image evidence."""
    sources = {s["source_id"]: s for s in load(root / "catalog/sources.json")}
    clips = {c["clip_id"]: c for c in load(root / "catalog/clips.json")}
    index = read_segments(root)
    by_id = {s["segment_id"]: s for s in index["segments"]}
    added = 0
    for proposal in proposals["segments"]:
        clip = clips.get(proposal["parent_clip_id"])
        if not clip or proposal["source_sha256"] != sources[clip["source_id"]]["file_sha256"]:
            raise Blocked("SEGMENT_SOURCE_STALE", "AI分段来源变化", 6)
        evidence = proposal["evidence"]
        if not evidence.get("frames") or not evidence.get("limitations"):
            raise Blocked("SEGMENT_EVIDENCE", "分段需要采样帧、实际观看方式与局限", 2)
        for frame in evidence["frames"]:
            path = (root / frame["relative_path"]).resolve()
            if (
                not path.is_relative_to(root.resolve())
                or not path.is_file()
                or file_hash(path) != frame["sha256"]
            ):
                raise Blocked("SEGMENT_EVIDENCE_CHANGED", "分段采样证据缺失或变化", 6)
        sid = "seg_" + digest(proposal)[:24]
        if sid in by_id:
            continue
        row = {
            **proposal,
            "segment_id": sid,
            "source_id": clip["source_id"],
            "precision": "sampled_suggestion",
            "review_status": "pending",
            "approval": None,
            "version": 1,
        }
        row["content_sha256"] = segment_hash(row)
        validate_segment(row, sources)
        by_id[sid] = row
        added += 1
    index.update(segments=list(by_id.values()), updated_at=now())
    atomic_write(root / INDEX, index)
    return {"added": added, "total": len(by_id), "approved_automatically": 0}
