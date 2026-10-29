"""New task generation must use actual footage, independently of sales narration."""

import copy
import json

import pytest

from mixcut.contracts import content_hash
from mixcut.core import Blocked, atomic_write, load
from mixcut.deepseek import _payload, _preview, parse_response
from mixcut.material_index import index_materials
from mixcut.script_input import active_request, model_prompt, validate_outline


def inventory(root):
    atomic_write(
        root / "config/workflow.local.json",
        {
            "script_generation_policy": "approved_material_context_constrained",
            "visual_analysis_proposals": "analysis/proposals.json",
        },
    )
    clips, proposals, confirmations = [], [], []
    for i in range(6):
        clip = {
            "clip_id": f"clip-{i}",
            "revision": 3,
            "goods_id": "芒果" if i < 4 else "榴莲",
            "brand_id": "示例品牌",
            "rights_confirmed": True,
            "review_status": "approved",
            "tags": {"risk": []},
            "description": f"双手包入果肉，捏合整形，第{i}个角度。",
            "usable_start_sec": 2,
            "usable_end_sec": 9,
        }
        clip["content_sha256"] = content_hash(clip)
        clip["approval"] = {
            "actor_id": "human",
            "channel": "feishu",
            "approved_at": "2026-10-21",
            "approved_content_sha256": clip["content_sha256"],
        }
        clips.append(clip)
        proposals.append(
            {
                "clip_id": clip["clip_id"],
                "analysis_sha256": f"analysis-{i}",
                "proposal": {"description": clip["description"]},
                "candidate_windows": [],
            }
        )
        confirmations.append(
            {
                "clip_id": clip["clip_id"],
                "analysis_sha256": f"analysis-{i}",
                "confirmed_catalog_revision": 3,
                "confirmed_catalog_content_sha256": clip["content_sha256"],
            }
        )
    atomic_write(root / "catalog/clips.json", clips)
    atomic_write(root / "analysis/proposals.json", {"records": proposals})
    atomic_write(root / "catalog/material_confirmations.json", {"records": confirmations})
    return {
        "task_id": "VIDEO_REAL",
        "creative_keywords": "芒果糯米糍 糯叽叽 团购优惠",
        "target_duration_sec": 15,
    }


def outline_for(request):
    return {
        "entity": "script_outline",
        "task_id": request["task_id"],
        "request_sha256": request["request_sha256"],
        "title": "芒果糯米糍推荐",
        "narration_mode": request["narration_mode"],
        "total_frames": 450,
        "shots": [
            {
                "shot_id": s["slot_id"],
                "visual_intent": s["visual_intent"],
                "semantic_queries": s["semantic_queries"],
                "target_frames": s["target_frames"],
                "narration": "团购优惠别错过。",
                "caption": "点击团购",
            }
            for s in request["material_slots"]
        ],
    }


def test_new_task_cannot_fall_back_to_unconstrained_invention(tmp_path):
    fields = inventory(tmp_path)
    req = active_request(tmp_path, fields)
    assert req["input_policy"] == "approved_material_context_constrained"
    assert req["narration_mode"] == "sales_independent"
    assert len(req["material_slots"]) == 4
    assert {s["goods_id"] for s in req["material_slots"]} == {"芒果"}
    assert sum(s["target_frames"] for s in req["material_slots"]) == 450
    assert req == active_request(tmp_path, fields)
    prompt = model_prompt(req)
    assert "逐句或逐镜头语义对应" in prompt["copy_instruction"]
    assert "clip_id" not in str(prompt) and "usable_start_sec" not in str(prompt)
    assert (
        len(
            _payload(
                req, {"max_input_chars": 6000, "model": "deepseek-flash", "max_output_tokens": 4096}
            )["input"]
        )
        < 6000
    )
    out = outline_for(req)
    validate_outline(out, req)
    bad = copy.deepcopy(out)
    bad["shots"][0]["visual_intent"] = "顾客咬开糯米糍拉丝，展示团购优惠界面"
    with pytest.raises(Blocked, match="Material slot changed"):
        validate_outline(bad, req)
    text = _preview(out)
    assert "销售旁白（独立表达" in text and "画面安排（仅使用已有批准素材）" in text
    assert "画面需求：" not in text


@pytest.mark.parametrize("change", ["pending", "approval", "confirmation", "duration"])
def test_insufficient_approved_footage_blocks_instead_of_inventing(tmp_path, change):
    fields = inventory(tmp_path)
    path = tmp_path / "catalog/clips.json"
    clips = load(path)
    if change == "pending":
        clips[0]["review_status"] = "pending"
    elif change == "approval":
        clips[0]["description"] = "changed without approval"
    elif change == "confirmation":
        p = tmp_path / "catalog/material_confirmations.json"
        data = load(p)
        data["records"][0]["analysis_sha256"] = "stale"
        atomic_write(p, data)
    else:
        fields["target_duration_sec"] = 120
    atomic_write(path, clips)
    with pytest.raises(Blocked) as exc:
        active_request(tmp_path, fields)
    assert exc.value.code == "MATERIAL_INSUFFICIENT"


def test_material_based_project_and_variation_survive_prompt(tmp_path):
    fields = inventory(tmp_path) | {
        "request_group_id": "REQ_NEW",
        "script_ordinal": 2,
        "variation_strategy": "opening",
        "variation_angle": "换个开场",
    }
    atomic_write(
        tmp_path / "config/script_project_policies/REQ_NEW.json",
        {"narration_mode": "material_based"},
    )
    req = active_request(tmp_path, fields)
    assert req["narration_mode"] == "material_based"
    assert model_prompt(req)["variation"]["angle"] == "换个开场"
    first = active_request(tmp_path, fields | {"script_ordinal": 1})
    assert first["material_slots"][0]["clip_id"] != req["material_slots"][0]["clip_id"]


def test_sales_caption_does_not_require_matching_coupon_or_customer_footage(tmp_path):
    req = active_request(tmp_path, inventory(tmp_path))
    out = outline_for(req)
    # Provider supplies copy + protected anonymous slots. Local metadata is authoritative.
    raw = {k: out[k] for k in ["title", "shots", "total_frames"]}
    response = {
        "status": "completed",
        "model": "deepseek-flash",
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(raw)}],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    out, _ = parse_response(response, req, {"model": "deepseek-flash"})
    out.update(
        schema_version="1.0.0",
        revision=1,
        provider="deepseek",
        model="deepseek-flash",
        review_status="approved",
    )
    out["content_sha256"] = content_hash(out)
    out["approval"] = {
        "actor_id": "human",
        "channel": "feishu",
        "approved_at": "2026-10-21",
        "approved_content_sha256": out["content_sha256"],
    }
    atomic_write(tmp_path / f"requests/VIDEO_REAL/{req['request_sha256']}.json", req)
    atomic_write(tmp_path / "jobs/VIDEO_REAL/r1/script_outline.json", out)
    result = index_materials(tmp_path, "VIDEO_REAL")
    assert result["production_ready"] and result["material_gap_count"] == 0


def test_punctuation_does_not_consume_spoken_character_budget(tmp_path):
    request = active_request(tmp_path, inventory(tmp_path))
    out = outline_for(request)
    limit = request["material_slots"][0]["max_narration_chars"]
    out["shots"][0]["narration"] = "团" * limit + "，！"
    validate_outline(out, request)
    out["shots"][0]["narration"] += "多"
    with pytest.raises(Blocked, match="Narration too long"):
        validate_outline(out, request)
