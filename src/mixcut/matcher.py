from __future__ import annotations
from datetime import datetime, timezone
from .contracts import require_approval, inward_frames, validate_tags
from .core import Blocked


def validate_script(script: dict, product: dict, taxonomy: dict, production: bool = True) -> None:
    require_approval(script, production)
    require_approval(product, production)
    if (script["brand_id"], script["goods_id"], script["product_revision"]) != (
        product["brand_id"],
        product["goods_id"],
        product["revision"],
    ):
        raise Blocked("PRODUCT_ISOLATION", "Script does not match approved product")
    if script["taxonomy_version"] != taxonomy["version"]:
        raise Blocked("TAXONOMY_VERSION", "Script vocabulary has changed")
    claims = {c["claim_id"]: c for c in product["claims"]}
    if len(claims) != len(product["claims"]):
        raise Blocked("DUPLICATE_CLAIM", "Duplicate fact id", 2)
    if not 4 <= len(script["shots"]) <= 6:
        raise Blocked("SHOT_COUNT", "Require 4-6 shots", 2)
    if len({s["shot_id"] for s in script["shots"]}) != len(script["shots"]):
        raise Blocked("DUPLICATE_SHOT", "Shot IDs must be unique", 2)
    total = 0
    for shot in script["shots"]:
        for key in ["required_tags", "preferred_tags", "forbidden_tags"]:
            validate_tags(shot[key], taxonomy)
        for axis in taxonomy["axes"]:
            if set(shot["required_tags"][axis]) & set(shot["forbidden_tags"][axis]):
                raise Blocked("TAG_CONFLICT", shot["shot_id"], 2)
        t = shot["timing"]
        if not t["min_frames"] <= t["target_frames"] <= t["max_frames"]:
            raise Blocked("SCRIPT_TIMING", shot["shot_id"], 2)
        total += t["target_frames"]
        for cid in shot["claim_ids"]:
            if cid not in claims:
                raise Blocked("MISSING_CLAIM", cid)
            expiry = claims[cid].get("valid_until")
            if expiry and datetime.fromisoformat(expiry.replace("Z", "+00:00")) < datetime.now(
                timezone.utc
            ):
                raise Blocked("FACT_EXPIRED", cid)
        if script["audio_mode"] == "music_only" and shot["narration"].strip():
            raise Blocked("AUDIO_MODE", "music_only may not silently drop narration")
        if script["audio_mode"] == "tts_uniform" and not shot["narration"].strip():
            raise Blocked("NARRATION_MISSING", shot["shot_id"])
    if not max(660, script["total_min_frames"]) <= total <= min(840, script["total_max_frames"]):
        raise Blocked("TIMING_UNSATISFIABLE", "Total outside approved range")


def match(
    script: dict,
    clips: list[dict],
    taxonomy: dict,
    sources: dict,
    load_counts: dict | None = None,
    production: bool = True,
    max_nodes: int = 10000,
) -> dict:
    require_approval(script, production)
    load_counts = load_counts or {}
    candidates = {}
    rejections = {}
    shots = script["shots"]
    for shot in shots:
        choices = []
        bad = []
        for clip in clips:
            try:
                require_approval(clip, production)
                if (clip["brand_id"], clip["goods_id"]) != (script["brand_id"], script["goods_id"]):
                    raise Blocked("PRODUCT_ISOLATION", "Different product")
                if clip["taxonomy_version"] != taxonomy["version"]:
                    raise Blocked("TAXONOMY_VERSION", "Different vocabulary")
                validate_tags(clip["tags"], taxonomy)
                if set(clip["tags"]["risk"]) & set(taxonomy["blocking_risks"]):
                    raise Blocked("BLOCKING_RISK", "Risk")
                for axis in taxonomy["axes"]:
                    if not set(shot["required_tags"][axis]) <= set(clip["tags"][axis]):
                        raise Blocked("REQUIRED_TAG", "Missing tag")
                    if set(shot["forbidden_tags"][axis]) & set(clip["tags"][axis]):
                        raise Blocked("FORBIDDEN_TAG", "Forbidden tag")
                source = sources.get(clip["source_id"])
                if source is None or clip["usable_end_sec"] > source["duration_sec"]:
                    raise Blocked("CLIP_RANGE", "Source range")
                a, b = inward_frames(clip["usable_start_sec"], clip["usable_end_sec"])
                n = shot["timing"]["target_frames"]
                if b - a < n:
                    raise Blocked("INSUFFICIENT_MATERIALS", "Short continuous interval")
                if shot["must_keep_action_complete"] and b - a != n:
                    raise Blocked(
                        "ACTION_INCOMPLETE", "Whole approved action interval must fit exactly"
                    )
                score = sum(
                    len(set(shot["preferred_tags"][k]) & set(clip["tags"][k]))
                    for k in taxonomy["axes"]
                )
                choices.append(
                    (
                        (-score, load_counts.get(clip["clip_id"], 0), clip["clip_id"]),
                        {
                            "shot_id": shot["shot_id"],
                            "clip_id": clip["clip_id"],
                            "clip_revision": clip["revision"],
                            "source_id": clip["source_id"],
                            "source_in_frame": a,
                            "source_out_frame": a + n,
                            "duration_frames": n,
                        },
                    )
                )
            except Blocked as e:
                bad.append({"clip_id": clip["clip_id"], "code": e.code})
        candidates[shot["shot_id"]] = [v for _, v in sorted(choices, key=lambda x: x[0])]
        rejections[shot["shot_id"]] = bad
    order = sorted(shots, key=lambda s: (len(candidates[s["shot_id"]]), s["shot_id"]))
    nodes = 0

    def search(i, chosen):
        nonlocal nodes
        if i == len(order):
            return chosen
        for c in candidates[order[i]["shot_id"]]:
            nodes += 1
            if nodes > max_nodes:
                raise Blocked(
                    "MATCH_SEARCH_LIMIT",
                    "Bounded search exhausted; not proof that no solution exists",
                )
            if any(
                c["clip_id"] == x["clip_id"]
                or (
                    c["source_id"] == x["source_id"]
                    and c["source_in_frame"] < x["source_out_frame"]
                    and x["source_in_frame"] < c["source_out_frame"]
                )
                for x in chosen
            ):
                continue
            result = search(i + 1, chosen + [c])
            if result is not None:
                return result
        return None

    selected = search(0, [])
    if selected is None:
        raise Blocked(
            "INSUFFICIENT_MATERIALS",
            "Current matcher found no legal assignment; "
            + str({s["shot_id"]: len(candidates[s["shot_id"]]) for s in shots}),
        )
    by_id = {x["shot_id"]: x for x in selected}
    cursor = 0
    result = []
    for shot in shots:
        x = by_id[shot["shot_id"]]
        x["timeline_start_frame"] = cursor
        cursor += x["duration_frames"]
        result.append(x)
    return {
        "assignments": result,
        "total_frames": cursor,
        "candidate_counts": {k: len(v) for k, v in candidates.items()},
        "rejections": rejections,
        "search_nodes": nodes,
    }
