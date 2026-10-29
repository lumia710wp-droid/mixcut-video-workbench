"""Timing recovery is tested with fake providers; no paid service is called."""

import json
import pytest
import mixcut.deepseek as provider
from test_montage_v3 import (
    setup_policy,
    approve_segments,
    request_rows,
    fake_copy,
    fake_tts,
)
from test_montage_v3 import library  # noqa: F401 - pytest fixture
from mixcut.core import Blocked, atomic_write, load, digest
from mixcut.executable import generate_batch, input_snapshot, policy
from mixcut.copy_drafts import save_copy_draft, draft_status, cached_voice
from mixcut.doubao import request_parameters


def test_audio_cache_key_is_unchanged_for_the_actual_failed_narration():
    text = "上海宝子有口福，现做糯米团，白皮折拢包住芒果块，果肉饱满，口感软糯，多种水果可选。"
    assert (
        digest(request_parameters(text))
        == "931989433aa2fd103fbb7230eda0555fb06ac47a698746e9f589771e5443cb9b"
    )


def test_failed_timing_can_be_edited_and_resumed_without_repeating_copy(library, monkeypatch):  # noqa: F811
    root = library
    setup_policy(root)
    approve_segments(root)
    calls = []
    monkeypatch.setattr(
        provider,
        "load_model_config",
        lambda *args: {
            "max_input_chars": 6000,
            "model": "fake",
            "max_output_tokens": 8192,
            "endpoint": "https://example.invalid",
            "timeout_seconds": 10,
        },
    )
    monkeypatch.setattr(provider, "_read_key", lambda *args: "test-only")

    def transport(payload, *args):
        calls.append(payload)
        request = json.loads(payload["input"])
        result = fake_copy(root, request, root)
        if request.get("voice_calibration"):
            for script in result["scripts"]:
                script["narration"] += "自然推荐" * (len(calls) - 1)
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

    def short_voice(*args):
        result = fake_tts(*args)
        result.update(duration_frames=90, duration_seconds=3)
        return result

    with pytest.raises(Blocked) as error:
        generate_batch(root, {}, "group", request_rows(), tts_fn=short_voice)
    assert error.value.code == "V3_VOICE_DURATION" and "3.000" in str(error.value)
    folder = root / "requests/executable_v3/group_v3_1"
    raw_response = (folder / "model_response.json").read_bytes()
    raw_input = (folder / "input.selected_v1.json").read_bytes()
    measured = load(folder / "timing_report.json")
    assert measured["tasks"]["task_0"]["status"] == "blocked"
    assert measured["tasks"]["task_1"]["status"] == "blocked"
    assert measured["correction_rounds"] == 2
    status = draft_status(root, "group", request_rows())
    edited = {r["fields"]["task_id"]: "芒果香甜一口" for r in request_rows()}
    report = save_copy_draft(
        root, "group", request_rows(), edited, status["copy_sha256"], {"type": "test"}
    )
    assert report["revision"] == 1 and len(calls) == 3
    assert (folder / "model_response.json").read_bytes() == raw_response
    assert (folder / "input.selected_v1.json").read_bytes() == raw_input
    batch = generate_batch(root, {}, "group", request_rows(), tts_fn=fake_tts)
    assert len(calls) == 3 and all(s["narration"] == "芒果香甜一口" for s in batch["scripts"])
    assert all(s["review_status"] == "pending" and s["approval"] is None for s in batch["scripts"])
    assert load(folder / "timing_report.json")["status"] == "passed"
    with pytest.raises(Blocked) as error:
        save_copy_draft(
            root, "group", request_rows(), edited, report["copy_sha256"], {"type": "test"}
        )
    assert error.value.code == "V3_COPY_DRAFT_LOCKED"


def test_draft_revisions_and_stale_writes_preserve_old_records(library):  # noqa: F811
    root = library
    setup_policy(root)
    rows = request_rows()
    folder = root / "requests/executable_v3/group_v3_1"
    original = fake_copy(root, input_snapshot(rows, policy(root, "group")), folder)
    atomic_write(
        folder / "model_response.json",
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(original)}],
                }
            ],
        },
    )
    edited = {r["fields"]["task_id"]: "芒果香甜一口" for r in rows}
    first = save_copy_draft(root, "group", rows, edited, digest(original), {"type": "test"})
    first_bytes = (folder / "copy_drafts/r0001.json").read_bytes()
    with pytest.raises(Blocked) as error:
        save_copy_draft(root, "group", rows, edited, digest(original), {"type": "test"})
    assert error.value.code == "V3_COPY_DRAFT_STALE"
    status = draft_status(root, "group", rows)
    assert all(
        s["voice_status"] == "not_generated" and s["measured_seconds"] is None
        for s in status["scripts"]
    )
    edited["task_0"] = "芒果清甜一口"
    second = save_copy_draft(root, "group", rows, edited, first["copy_sha256"], {"type": "test"})
    assert second["revision"] == 2
    assert (folder / "copy_drafts/r0001.json").read_bytes() == first_bytes


def test_unknown_voice_is_not_misreported_as_zero_seconds(library):  # noqa: F811
    root = library
    text = "未完成配音"
    folder = root / "audio/doubao_v3" / digest(request_parameters(text))
    atomic_write(folder / "receipt.json", {"status": "failed_or_unknown"})
    assert cached_voice(root, text) == {"status": "unknown_or_failed"}


def test_workbench_save_copy_is_read_only_for_providers_and_material_selection(library):  # noqa: F811
    from mixcut.workbench import Workbench, EXPECTED_KEYS

    root = library
    setup_policy(root)
    rows = request_rows()
    original = fake_copy(root, input_snapshot(rows, policy(root, "group")), root)
    atomic_write(
        root / "requests/executable_v3/group_v3_1/model_response.json",
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(original)}],
                }
            ],
        },
    )
    bench = object.__new__(Workbench)
    bench.root = root
    bench.v3_rows = lambda group: rows
    bench.task = lambda task_id: {**rows[0], "record_id": "rec_test"}
    bench.auth_user = lambda: {"id": "ou_test"}

    def forbidden(*args):
        raise AssertionError("Saving text must not select material or call a provider")

    bench.v3_material_gate = forbidden
    result = bench.action(
        "task_0",
        {
            "action": "save_copy",
            "expected": {k: rows[0]["fields"].get(k) for k in EXPECTED_KEYS},
            "batch_input_sha256": digest(input_snapshot(rows, policy(root, "group"))),
            "copy_sha256": digest(original),
            "texts": {r["fields"]["task_id"]: "芒果香甜一口" for r in rows},
        },
    )
    assert result["status"] == "saved" and result["revision"] == 1
