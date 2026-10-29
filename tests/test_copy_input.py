"""Bounded copy context preserves facts, user requirements, and dispatch recovery."""

import copy
import json
import pytest
import mixcut.deepseek as provider
from mixcut.core import Blocked, atomic_write, digest, load
from mixcut.executable import build_copy_input, generate_copy


def long_request():
    return {
        "settings": {
            "brand_id": "品牌",
            "goods_id": "芒果",
            "narration_mode": "sales_independent",
            "product_facts": {"allergens": ["牛奶"], "unsupported_claims": ["无糖"]},
            "generation_task_ids": [f"task_{n}" for n in range(3)],
            "excluded_source_ids": ["source_" + "a" * 64],
            "speed": 1.1,
        },
        "tasks": [
            {
                "task_id": f"task_{n}",
                "target_duration_sec": 15,
                "creative_keywords": "自然口语，完整保留我的要求",
                "variation_angle": str(n),
            }
            for n in range(3)
        ],
        "visual_context_policy": "selected_shots_v1",
        "available_visuals": [
            {
                "task_id": f"task_{n % 3}",
                "segment_id": "fact_" + str(n).zfill(24),
                "description": f"画面{n}：手把黄色果肉放入白色糯米皮，再包拢。",
                "events": [{"frame": n * 30 + i, "text": "手放入果肉并包拢"} for i in range(6)],
            }
            for n in range(18)
        ],
    }


def test_copy_context_preserves_requirements_and_distinct_facts_without_edit_metadata():
    request = long_request()
    before = copy.deepcopy(request)
    text = build_copy_input(request, 6000)
    context = json.loads(text)
    assert len(text) <= 6000
    assert context["tasks"] == request["tasks"]
    assert context["settings"]["product_facts"] == request["settings"]["product_facts"]
    for task in request["tasks"]:
        tid = task["task_id"]
        assert context["task_visuals"][tid]["observations"] == [
            v["description"] for v in request["available_visuals"] if v["task_id"] == tid
        ]
        assert context["task_visuals"][tid]["events"] == ["手放入果肉并包拢"]
    assert not {"excluded_source_ids", "generation_task_ids", "speed"} & context["settings"].keys()
    assert request == before


def test_existing_blocked_input_can_dispatch_once_with_compact_context(tmp_path, monkeypatch):
    request = long_request()
    folder = tmp_path / "existing_failed_request"
    atomic_write(folder / "input.json", request)
    before = (folder / "input.json").read_bytes()
    config = {
        "max_input_chars": 6000,
        "model": "test-only",
        "max_output_tokens": 8192,
        "endpoint": "https://example.invalid",
        "timeout_seconds": 20,
    }
    monkeypatch.setattr(provider, "load_model_config", lambda *args: config)
    monkeypatch.setattr(provider, "_read_key", lambda *args: "test-only-key")
    result = {
        "scripts": [
            {
                "task_id": f"task_{n}",
                "title": "测试",
                "narration": "测试旁白",
                "headline": ["主题", "商品", "卖点", "口感"],
            }
            for n in range(3)
        ]
    }
    calls = []

    def transport(payload, *args):
        calls.append(payload)
        assert len(payload["input"]) <= config["max_input_chars"]
        assert json.loads(payload["input"])["tasks"] == request["tasks"]
        return json.dumps(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(result)}],
                    }
                ],
            }
        ).encode()

    monkeypatch.setattr(provider, "urllib_transport", transport)
    assert generate_copy(tmp_path, request, folder) == result
    assert len(calls) == 1
    assert (folder / "input.json").read_bytes() == before
    receipt = load(folder / "model_intent.json")
    assert receipt["input_sha256"] == digest(request)
    assert receipt["model_input_chars"] == len(calls[0]["input"])
    assert receipt["model_input_sha256"] == digest(load(folder / "model_input.json"))
    config["max_input_chars"] = 1000
    assert generate_copy(tmp_path, request, folder) == result
    assert len(calls) == 1  # A saved response does not need dispatch or a fresh input budget.
    changed = copy.deepcopy(request)
    changed["tasks"][0]["creative_keywords"] = "changed requirement"
    with pytest.raises(Blocked) as error:
        generate_copy(tmp_path, changed, folder)
    assert error.value.code == "V3_INPUT_CHANGED"
    assert len(calls) == 1


def test_oversize_creative_requirements_are_not_truncated_or_dispatched(tmp_path, monkeypatch):
    request = long_request()
    request["tasks"][0]["creative_keywords"] = "必须保留的需求" * 1000
    before = copy.deepcopy(request)
    monkeypatch.setattr(provider, "load_model_config", lambda *args: {"max_input_chars": 6000})

    def forbidden(*args):
        raise AssertionError("No credentials or paid provider call before the input gate")

    monkeypatch.setattr(provider, "_read_key", forbidden)
    monkeypatch.setattr(provider, "urllib_transport", forbidden)
    with pytest.raises(Blocked) as error:
        generate_copy(tmp_path, request, tmp_path / "oversize")
    assert error.value.code == "V3_INPUT_LIMIT"
    assert request == before
    assert not (tmp_path / "oversize/model_intent.json").exists()


def test_unknown_prior_dispatch_stays_blocked_after_context_compaction(tmp_path, monkeypatch):
    folder = tmp_path / "unknown"
    atomic_write(folder / "model_intent.json", {"status": "dispatching"})
    monkeypatch.setattr(provider, "load_model_config", lambda *args: {"max_input_chars": 1000})
    with pytest.raises(Blocked) as error:
        generate_copy(tmp_path, long_request(), folder)
    assert error.value.code == "V3_MODEL_UNKNOWN"


def test_full_candidate_library_is_rejected_before_paid_dispatch(tmp_path, monkeypatch):
    request = long_request()
    request.pop("visual_context_policy")
    monkeypatch.setattr(provider, "load_model_config", lambda *args: {"max_input_chars": 6000})

    def forbidden(*args):
        raise AssertionError("Unselected library must never reach the provider")

    monkeypatch.setattr(provider, "_read_key", forbidden)
    with pytest.raises(Blocked) as error:
        generate_copy(tmp_path, request, tmp_path / "library")
    assert error.value.code == "V3_VISUAL_SELECTION_REQUIRED"
    assert not (tmp_path / "library/model_intent.json").exists()
