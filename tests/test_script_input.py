from pathlib import Path

import pytest

from mixcut.core import Blocked, load
from mixcut.script_input import (
    collect_requests,
    model_prompt,
    request_from_task_fields,
    validate_outline,
)


def fields(**changes):
    return {
        "task_id": "TASK_001",
        "creative_keywords": "门店手工制作，真实，节奏快",
        "seed_sentences": "一颗糯米糍是怎么包出来的？",
        "target_duration_sec": 24,
    } | changes


def test_request_uses_human_creative_input_only():
    request = request_from_task_fields(fields())
    prompt = model_prompt(request)
    assert request["target_duration_frames"] == 720
    assert set(prompt).isdisjoint({"clips", "materials", "source_path"})
    assert "素材" not in str(prompt.get("creative_keywords"))


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"creative_keywords": "", "seed_sentences": None}, "SCRIPT_INPUT_MISSING"),
        ({"creative_keywords": "  ", "seed_sentences": "  "}, "SCRIPT_INPUT_MISSING"),
        ({"target_duration_sec": 0}, "SCRIPT_DURATION"),
        ({"target_duration_sec": "24"}, "SCRIPT_INPUT_INVALID"),
    ],
)
def test_request_rejects_missing_or_invalid_input(changes, code):
    with pytest.raises(Blocked) as exc:
        request_from_task_fields(fields(**changes))
    assert exc.value.code == code


def outline_for(request):
    return {
        "entity": "script_outline",
        "task_id": request["task_id"],
        "request_sha256": request["request_sha256"],
        "title": "手工包制",
        "shots": [
            {
                "shot_id": "S01",
                "narration": "先提出问题。",
                "caption": "怎么包出来？",
                "visual_intent": "吸引注意的制作开场",
                "semantic_queries": ["原料处理近景", "手工制作动作"],
                "target_frames": 180,
            },
            {
                "shot_id": "S02",
                "narration": "再展示制作步骤。",
                "caption": "一步步完成",
                "visual_intent": "连续制作过程",
                "semantic_queries": ["包制", "收口", "装托"],
                "target_frames": 180,
            },
            {
                "shot_id": "S03",
                "narration": "接着说明关键动作。",
                "caption": "关键动作",
                "visual_intent": "突出手工动作细节",
                "semantic_queries": ["手工动作", "制作细节"],
                "target_frames": 180,
            },
            {
                "shot_id": "S04",
                "narration": "最后展示完成状态。",
                "caption": "完成出品",
                "visual_intent": "完整成品收尾",
                "semantic_queries": ["完成品", "门店出品"],
                "target_frames": 180,
            },
        ],
        "total_frames": 720,
    }


def test_outline_is_semantic_and_exact_duration():
    request = request_from_task_fields(fields())
    validate_outline(outline_for(request), request)


def test_outline_rejects_material_binding_and_wrong_duration():
    request = request_from_task_fields(fields())
    outline = outline_for(request)
    outline["shots"][0]["clip_id"] = "clip-1"
    with pytest.raises(Blocked) as exc:
        validate_outline(outline, request)
    assert exc.value.code == "SCRIPT_MATERIAL_LEAK"
    outline = outline_for(request)
    outline["shots"][0]["target_frames"] -= 1
    with pytest.raises(Blocked) as exc:
        validate_outline(outline, request)
    assert exc.value.code == "SCRIPT_TIMING_UNSATISFIABLE"


def test_material_constrained_outline_cannot_change_visual_slots():
    request = request_from_task_fields(fields())
    outline = outline_for(request)
    slots = []
    for shot in outline["shots"]:
        slots.append(
            {
                "slot_id": shot["shot_id"],
                "clip_id": "clip-" + shot["shot_id"],
                "goods_id": "测试商品",
                "description": "已批准画面",
                "visual_intent": shot["visual_intent"],
                "semantic_queries": shot["semantic_queries"],
                "target_frames": shot["target_frames"],
                "max_narration_chars": 20,
                "max_caption_chars": 20,
            }
        )
    request.update(
        input_policy="approved_material_context_constrained",
        material_slots=slots,
        material_context_sha256="context-hash",
    )
    request["request_sha256"] = "material-request"
    outline["request_sha256"] = request["request_sha256"]
    validate_outline(outline, request)
    prompt = model_prompt(request)
    assert prompt["material_slots"][0]["slot_id"] == "S01"
    assert "clip_id" not in str(prompt)
    changed = outline_for(request)
    changed["request_sha256"] = request["request_sha256"]
    changed["shots"][0]["visual_intent"] = "不存在的顾客交付画面"
    with pytest.raises(Blocked) as exc:
        validate_outline(changed, request)
    assert exc.value.code == "MATERIAL_SCRIPT_SCOPE"


def test_collect_requests_waits_or_persists(tmp_path: Path):
    waiting = [
        {
            "record_id": "r1",
            "fields": fields(task_id="TASK_WAIT", seed_sentences="", creative_keywords=""),
        }
    ]
    ready = [{"record_id": "r2", "fields": fields()}]
    report = collect_requests(tmp_path, waiting + ready)
    assert report["waiting"] == ["TASK_WAIT"]
    assert report["ready"][0]["task_id"] == "TASK_001"
    saved = load(Path(report["ready"][0]["path"]))
    assert saved["input_policy"] == "human_creative_input_only_no_material_context"
    events = [load(path) for path in (tmp_path / "ledger/outbox").glob("*.json")]
    by_task = {event["business_key"]: event for event in events}
    assert by_task["TASK_WAIT"]["fields"]["script_preview"].startswith("等待人工填写")
    assert by_task["TASK_001"]["fields"]["script_review_status"] == "pending"


@pytest.mark.parametrize(
    "changes",
    [
        {"creative_keywords": None},
        {"seed_sentences": ""},
        {"seed_sentences": None, "target_duration_sec": None},
    ],
)
def test_one_creative_input_and_default_duration(changes):
    request = request_from_task_fields(fields(**changes))
    assert request["creative_keywords"] or request["seed_sentences"]
    assert request["target_duration_frames"] == 720
