"""Read-only facts -> script-planning windows; full-script approval authorizes cuts."""

from __future__ import annotations
import math
from pathlib import Path
from .core import Blocked, digest, load
from .contracts import content_hash, inward_frames, require_approval
from .segments import eligible_segments, segment_hash
from .product_scope import includes, validate_scope


def workflow_policy(root: Path) -> dict:
    path = root / "catalog/material_workflow_policy.json"
    return load(path) if path.is_file() else {}


def uses_facts(root: Path, settings: dict) -> bool:
    return (
        settings.get("material_planning_source", workflow_policy(root).get("planning_input"))
        == "facts_v1"
    )


def planning_segments(root: Path, settings: dict) -> list[dict]:
    validate_scope(settings)
    if not uses_facts(root, settings):
        if "goods_scope" not in settings:
            return eligible_segments(root, settings["brand_id"], settings["goods_id"])
        goods = {
            c["goods_id"]
            for c in load(root / "catalog/clips.json")
            if c.get("brand_id") == settings["brand_id"]
            and c.get("goods_id")
            and includes(settings, c["goods_id"])
        }
        return [
            {**s, "goods_id": g}
            for g in sorted(goods)
            for s in eligible_segments(root, settings["brand_id"], g)
        ]
    path = root / "catalog/material_facts_v1.json"
    if not path.is_file():
        raise Blocked("V3_FACTS_MISSING", "素材事实索引缺失，请先完成素材标记", 3)
    policy = workflow_policy(root)
    scope = policy.get("brand_scopes", {}).get(settings["brand_id"], {})
    if not scope.get("sources") or not scope.get("root_ids"):
        raise Blocked("V3_LIBRARY_SCOPE", "当前项目尚未绑定素材库范围，需要修复项目配置", 3)
    sources = {x["source_id"]: x for x in load(root / "catalog/sources.json")}
    clips = {x["clip_id"]: x for x in load(root / "catalog/clips.json")}
    taxonomy_path = root / "config/taxonomy.json"
    blocking_risks = set(
        load(taxonomy_path)["blocking_risks"]
        if taxonomy_path.is_file()
        else ["uncertain_product", "old_price"]
    )
    history_path = root / "catalog/product_identity_confirmations.json"
    confirmations = load(history_path).get("records", []) if history_path.is_file() else []
    decisions = {}
    for record in confirmations:
        payload = {
            k: v
            for k, v in record.items()
            if k not in {"plan_sha256", "verified_at", "feishu_readback_count"}
        }
        if (
            record.get("decision_channel") == "codex_chat"
            and record.get("decision_actor") == "human_user_in_current_chat"
            and record.get("verified_at")
            and digest(payload) == record.get("plan_sha256")
        ):
            for item in record.get("items", []):
                decisions[(record["plan_sha256"], item["source_id"])] = item
    candidates = []
    for facts in load(path).get("materials", []):
        sid = facts.get("source", {}).get("source_id")
        source = sources.get(sid)
        clip = clips.get(facts.get("parent", {}).get("clip_id"))
        if (
            not source
            or not clip
            or clip.get("source_id") != sid
            or sid in settings.get("excluded_source_ids", [])
        ):
            continue
        if source["file_sha256"] != scope["sources"].get(sid):
            continue
        if not any(loc["root_id"] in scope["root_ids"] for loc in source["locations"]):
            continue
        if clip.get("brand_id") not in (None, "", settings["brand_id"]):
            continue
        if clip.get("review_status") == "rejected" or clip.get("is_fixture"):
            continue
        if set(clip.get("tags", {}).get("risk", [])) & blocking_risks:
            continue
        if (
            facts.get("content_sha256")
            != digest({k: v for k, v in facts.items() if k != "content_sha256"})
            or facts["source"]["file_sha256"] != source["file_sha256"]
            or clip.get("content_sha256") != content_hash(clip)
            or facts["parent"].get("content_sha256") != clip["content_sha256"]
        ):
            continue
        sheets = facts.get("evidence", {}).get("sheets", [])
        viewed = facts.get("coverage", {}).get("actually_viewed_sheet_ids", [])
        if not sheets or set(viewed) != {s["id"] for s in sheets}:
            continue
        phases = facts.get("phases", [])
        cursor = 0.0
        for p in phases:
            if (
                not all(
                    isinstance(p.get(k), (int, float))
                    and not isinstance(p[k], bool)
                    and math.isfinite(p[k])
                    for k in ["start_sec", "end_sec"]
                )
                or abs(p["start_sec"] - cursor) > 1e-6
                or not 0 <= p["start_sec"] < p["end_sec"] <= source["duration_sec"] + 1e-6
            ):
                break
            cursor = p["end_sec"]
        else:
            if not phases or abs(cursor - source["duration_sec"]) > 1e-6:
                continue
            confirmation = facts.get("product_confirmation", {})
            decision = decisions.get((confirmation.get("plan_sha256"), sid))
            if decision:
                if (
                    confirmation.get("previous_facts_sha256") != decision["previous_facts_sha256"]
                    or clip.get("goods_id") != decision["goods_id"]
                    or facts["identity"].get("confirmed_products") != decision["products"]
                    or decision["source_sha256"] != source["file_sha256"]
                ):
                    continue
                products = decision["products"]
            else:
                try:
                    require_approval(clip)
                except Blocked:
                    continue
                if facts["identity"].get("confirmed_goods") != clip.get("goods_id"):
                    continue
                products = [
                    {
                        "goods_id": clip["goods_id"],
                        "start_sec": 0,
                        "end_sec": source["duration_sec"],
                    }
                ]
            for product in products:
                if not includes(settings, product["goods_id"]):
                    continue
                lo, hi = product["start_sec"], product["end_sec"]
                if not 0 <= lo < hi <= source["duration_sec"]:
                    continue
                # These are planning choices derived now, never written into the factual index.
                windows = {
                    (max(lo, p["start_sec"]), min(hi, p["end_sec"]))
                    for p in phases
                    if p["start_sec"] < hi and p["end_sec"] > lo
                }
                windows.add((lo, hi))  # Scripts may cross adjacent content stages of this product.
                for a, b in sorted(windows):
                    start, end = inward_frames(a, b)
                    if end - start < 15:
                        continue
                    relevant = [p for p in phases if p["start_sec"] < b and p["end_sec"] > a]
                    whole = len(relevant) > 1
                    description = facts["summary"] if whole else relevant[0]["observation"]
                    quality = " ".join(p["quality"] for p in relevant)
                    events = [
                        {"frame": round(e["time_sec"] * 30), "text": e["text"]}
                        for e in facts.get("events", [])
                        if start <= round(e["time_sec"] * 30) < end
                    ]
                    row = {
                        "segment_id": "fact_"
                        + digest(
                            [
                                sid,
                                facts["content_sha256"],
                                product["goods_id"]
                                if "goods_scope" in settings
                                else settings["goods_id"],
                                digest(scope),
                                start,
                                end,
                            ]
                        )[:24],
                        "source_id": sid,
                        "source_sha256": source["file_sha256"],
                        "parent_clip_id": clip["clip_id"],
                        "parent_content_sha256": clip["content_sha256"],
                        "source_in_frame": start,
                        "source_out_frame": end,
                        "description": description,
                        "role": "general",
                        "events": events,
                        "precision": "script_planning_observation",
                        "review_status": "planning_input",
                        "approval": None,
                        "evidence": {
                            "facts_sha256": facts["content_sha256"],
                            "scope_sha256": digest(scope),
                            "identity_plan_sha256": confirmation.get("plan_sha256"),
                            "source": "material_facts_v1",
                        },
                        "observed_phases": relevant,
                        "planning_penalty": (3 if whole else 0)
                        + sum(word in quality for word in ["强反光", "大面积", "不清楚", "失焦"])
                        + (
                            4
                            if not whole
                            and any(word in description for word in ["空台面", "无人", "离开画面"])
                            else 0
                        ),
                    }
                    if "goods_scope" in settings:
                        row["goods_id"] = product["goods_id"]
                    row["content_sha256"] = segment_hash(row)
                    candidates.append(row)
    # Known suspected same-content pairs choose one source, never count both as unique footage.
    suppressed = set()
    available = {s["source_id"] for s in candidates}
    for group in policy.get("duplicate_source_groups", []):
        members = sorted(available.intersection(group))
        suppressed.update(members[1:])
    return [s for s in candidates if s["source_id"] not in suppressed]
