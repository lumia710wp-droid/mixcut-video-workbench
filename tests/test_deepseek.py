import json
from pathlib import Path

import pytest

from mixcut.contracts import content_hash
from mixcut.core import Blocked, atomic_write, digest, load
from mixcut.deepseek import (
    generate_outline,
    generate_tts_revision,
    parse_response,
    readiness,
    reconcile_outline,
    verify_connection,
)
from mixcut.review import review_script
from mixcut.script_input import request_from_task_fields, validate_reviewable_outline
from mixcut.sync import sync_script_reviews


def task_fields(**changes):
    return {
        "task_id": "TASK_001",
        "creative_keywords": "门店手工制作，真实，节奏快",
        "seed_sentences": "一颗糯米糍是怎么包出来的？",
        "target_duration_sec": 24,
        "material_index_preview": "must never reach the model",
    } | changes


def write_snapshot(workspace: Path, fields: dict | None = None) -> None:
    atomic_write(
        workspace / "catalog/feishu_snapshot.json",
        {
            "at": "2026-10-14T00:00:00+00:00",
            "tables": {
                "clips": [],
                "tasks": [{"record_id": "row-1", "fields": fields or task_fields()}],
            },
        },
    )


def model_config() -> dict:
    return {
        "provider": "deepseek",
        "endpoint": "https://api.deepseek.com/responses",
        "model": "deepseek-flash",
        "api_key_env": "TEST_DEEPSEEK_KEY",
        "api_key_file": None,
        "timeout_seconds": 10,
        "max_output_tokens": 4096,
        "max_input_chars": 6000,
        "max_remote_calls_per_command": 1,
        "automatic_retries": 0,
        "rate_card_path": None,
    }


def outline_for(request: dict) -> dict:
    shots = []
    for index in range(1, 5):
        shots.append(
            {
                "shot_id": f"S{index:02d}",
                "narration": f"第{index}段旁白",
                "caption": f"第{index}段字幕",
                "visual_intent": f"第{index}段画面语义",
                "semantic_queries": [f"第{index}段动作", "门店制作"],
                "target_frames": 180,
            }
        )
    return {
        "entity": "script_outline",
        "task_id": request["task_id"],
        "request_sha256": request["request_sha256"],
        "title": "手工制作过程",
        "shots": shots,
        "total_frames": 720,
    }


def provider_response(request: dict, *, content: str | None = None) -> bytes:
    generated = outline_for(request)
    for key in ("entity", "task_id", "request_sha256"):
        generated.pop(key)
    text = content if content is not None else json.dumps(generated, ensure_ascii=False)
    return json.dumps(
        {
            "id": "resp-test",
            "object": "response",
            "status": "completed",
            "model": "deepseek-flash",
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
        },
        ensure_ascii=False,
    ).encode()


def test_readiness_finds_private_provider_but_waits_for_human_input(tmp_path: Path):
    write_snapshot(tmp_path, task_fields(creative_keywords="", seed_sentences=""))
    report = readiness(
        tmp_path,
        model_config(),
        "TASK_001",
        environ={"TEST_DEEPSEEK_KEY": "secret"},
    )
    assert report["status"] == "blocked"
    assert "script_input_missing" in report["missing"]
    assert report["network_attempted"] is False


def test_auth_check_reports_availability_without_balance_or_secret(tmp_path: Path):
    raw = json.dumps(
        {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "CNY",
                    "total_balance": "123.45",
                    "granted_balance": "0.00",
                    "topped_up_balance": "123.45",
                }
            ],
        }
    ).encode()
    report = verify_connection(
        tmp_path,
        model_config(),
        transport=lambda key, timeout: raw,
        environ={"TEST_DEEPSEEK_KEY": "secret-balance-key"},
    )
    rendered = json.dumps(report)
    assert report["status"] == "connected" and report["api_calls_available"] is True
    assert report["balance_amounts_exposed"] is False
    assert "123.45" not in rendered and "secret-balance-key" not in rendered
    assert load(tmp_path / "reports/deepseek_auth_check.json") == report


def test_generate_one_reviewable_outline_without_material_context(tmp_path: Path):
    write_snapshot(tmp_path)
    request = request_from_task_fields(task_fields())
    calls = []

    def transport(payload, key, endpoint, timeout):
        calls.append(payload)
        assert key == "very-private-test-key"
        assert endpoint == "https://api.deepseek.com/responses"
        assert timeout == 10
        model_input = json.loads(payload["input"])
        assert "material_index_preview" not in model_input
        assert set(model_input).isdisjoint({"clips", "materials", "source_path"})
        return provider_response(request)

    result = generate_outline(
        tmp_path,
        model_config(),
        "TASK_001",
        transport=transport,
        environ={"TEST_DEEPSEEK_KEY": "very-private-test-key"},
    )
    assert result["status"] == "waiting_script_review"
    assert result["network_attempted"] is True
    assert len(calls) == 1
    assert "very-private-test-key" not in json.dumps(result)
    outline = load(Path(result["outline_path"]))
    validate_reviewable_outline(outline, request)
    assert outline["review_status"] == "pending" and outline["approval"] is None
    assert not any("clip_id" in shot for shot in outline["shots"])
    again = generate_outline(
        tmp_path,
        model_config(),
        "TASK_001",
        transport=lambda *_: pytest.fail("must not call provider twice"),
        environ={"TEST_DEEPSEEK_KEY": "very-private-test-key"},
    )
    assert again["status"] == "already_generated"
    assert again["network_attempted"] is False


def test_review_binds_outline_to_input_hash(tmp_path: Path):
    write_snapshot(tmp_path)
    request = request_from_task_fields(task_fields())
    result = generate_outline(
        tmp_path,
        model_config(),
        "TASK_001",
        transport=lambda *_: provider_response(request),
        environ={"TEST_DEEPSEEK_KEY": "secret"},
    )
    outline = load(Path(result["outline_path"]))
    fields = {
        "task_id": "TASK_001",
        "script_revision": 1,
        "script_review_revision": 1,
        "script_sha256": outline["content_sha256"],
        "script_input_sha256": request["request_sha256"],
        "script_review_action": "approve",
        "script_review_actor": "human-reviewer",
    }
    assert review_script(outline, fields)["review_status"] == "approved"
    stale = review_script(outline, fields | {"script_input_sha256": "0" * 64})
    assert stale["review_status"] == "pending" and stale["approval"] is None
    report = sync_script_reviews(
        tmp_path,
        [{"record_id": "row-1", "fields": fields}],
    )
    assert report == {"checked": 1, "approved": 1, "errors": []}
    assert load(Path(result["outline_path"]))["review_status"] == "approved"


def test_transport_failure_is_unknown_and_never_retried(tmp_path: Path):
    write_snapshot(tmp_path)
    count = 0

    def failed(*_):
        nonlocal count
        count += 1
        raise TimeoutError("ambiguous transport failure")

    with pytest.raises(Blocked) as exc:
        generate_outline(
            tmp_path,
            model_config(),
            "TASK_001",
            transport=failed,
            environ={"TEST_DEEPSEEK_KEY": "secret"},
        )
    assert exc.value.code == "MODEL_OUTCOME_UNKNOWN" and count == 1
    state = load(next((tmp_path / "jobs/TASK_001/generation").glob("*.json")))
    assert state["status"] == "outcome_unknown" and state["retry_allowed"] is False
    with pytest.raises(Blocked) as retry:
        generate_outline(
            tmp_path,
            model_config(),
            "TASK_001",
            transport=failed,
            environ={"TEST_DEEPSEEK_KEY": "secret"},
        )
    assert retry.value.code == "MODEL_RETRY_REQUIRES_INPUT_CHANGE" and count == 1


def test_rejects_empty_and_program_binds_identity(tmp_path: Path):
    write_snapshot(tmp_path)
    request = request_from_task_fields(task_fields())
    response = json.loads(provider_response(request))
    response["output"][0]["content"] = []
    with pytest.raises(Blocked) as empty:
        parse_response(response, request, model_config())
    assert empty.value.code == "MODEL_RESPONSE_EMPTY"
    supplied_binding = outline_for(request) | {"request_sha256": "0" * 64}
    parsed, _ = parse_response(
        json.loads(provider_response(request, content=json.dumps(supplied_binding))),
        request,
        model_config(),
    )
    assert parsed["task_id"] == request["task_id"]
    assert parsed["request_sha256"] == request["request_sha256"]
    fenced = json.dumps(supplied_binding, ensure_ascii=False)
    parsed_fenced, _ = parse_response(
        json.loads(provider_response(request, content=f"```json\n{fenced}\n```")),
        request,
        model_config(),
    )
    assert parsed_fenced["title"] == supplied_binding["title"]


def test_reconcile_saved_response_without_second_network_call(tmp_path: Path):
    write_snapshot(tmp_path)
    request = request_from_task_fields(task_fields())
    config = model_config()
    run_id = "ds_" + digest(
        {
            "task_id": request["task_id"],
            "request_sha256": request["request_sha256"],
            "model": config["model"],
        }
    )[:20]
    directory = tmp_path / "jobs/TASK_001/generation"
    response_path = directory / f"{run_id}.provider-response.json"
    atomic_write(response_path, json.loads(provider_response(request)))
    atomic_write(
        directory / f"{run_id}.json",
        {
            "run_id": run_id,
            "status": "output_rejected",
            "cost_status": "unpriced_without_verified_rate_card",
            "network_attempted": True,
        },
    )
    result = reconcile_outline(tmp_path, config, "TASK_001")
    assert result["status"] == "waiting_script_review"
    assert result["network_attempted"] is False
    assert result["reconciled_from_saved_response"] is True


def test_input_change_before_dispatch_blocks_without_network(tmp_path: Path):
    first = request_from_task_fields(task_fields())
    changed = request_from_task_fields(task_fields(seed_sentences="已经改变"))
    sequence = iter([first, changed])
    with pytest.raises(Blocked) as exc:
        generate_outline(
            tmp_path,
            model_config(),
            "TASK_001",
            transport=lambda *_: pytest.fail("stale input must not be sent"),
            environ={"TEST_DEEPSEEK_KEY": "secret"},
            request_loader=lambda: next(sequence),
        )
    assert exc.value.code == "SCRIPT_INPUT_STALE"


def test_tts_revision_changes_only_measured_overflow_narration(tmp_path: Path):
    write_snapshot(tmp_path)
    request = request_from_task_fields(task_fields())
    generated = generate_outline(
        tmp_path,
        model_config(),
        "TASK_001",
        transport=lambda *_: provider_response(request),
        environ={"TEST_DEEPSEEK_KEY": "secret"},
    )
    source_path = Path(generated["outline_path"])
    source = load(source_path)
    source["review_status"] = "approved"
    source["approval"] = {
        "actor_id": "human-reviewer",
        "channel": "feishu",
        "approved_at": "2026-10-15T00:00:00+00:00",
        "approved_content_sha256": source["content_sha256"],
    }
    assert source["content_sha256"] == content_hash(source)
    atomic_write(source_path, source)
    fit_shots = []
    for index, shot in enumerate(source["shots"]):
        speech = 200 if index == 0 else 100
        fit_shots.append(
            {
                "shot_id": shot["shot_id"],
                "target_frames": shot["target_frames"],
                "speech_frames": speech,
                "overflow_frames": max(0, speech - shot["target_frames"]),
            }
        )
    atomic_write(
        tmp_path / "reports/tts/narration_fit_20261015.json",
        {
            "tasks": [
                {
                    "task_id": "TASK_001",
                    "script_sha256": source["content_sha256"],
                    "shots": fit_shots,
                }
            ]
        },
    )
    revised = outline_for(request)
    revised["shots"][0]["narration"] = "第一段"
    for key in ("entity", "task_id", "request_sha256"):
        revised.pop(key)
    result = generate_tts_revision(
        tmp_path,
        model_config(),
        "TASK_001",
        transport=lambda *_: provider_response(
            request, content=json.dumps(revised, ensure_ascii=False)
        ),
        environ={"TEST_DEEPSEEK_KEY": "secret"},
    )
    assert result["status"] == "waiting_script_review" and result["revision"] == 2
    output = load(Path(result["outline_path"]))
    assert output["shots"][0]["narration"] == "第一段"
    assert output["shots"][1:] == source["shots"][1:]
    assert output["review_status"] == "pending" and output["approval"] is None
