import copy
from pathlib import Path

import pytest

from mixcut.contracts import content_hash, require_approval
from mixcut.core import Blocked, atomic_write, digest, file_hash, load
from mixcut.feishu import TASK_FIELDS
from mixcut.script_input import collect_requests, request_from_task_fields
from mixcut.script_revision import (
    allocate_timing,
    apply_edit,
    editable_text,
    newest_outline,
    revision_request,
    rewrite_once,
    save_retimed,
)
from mixcut.script_review_queue import process_reviews
from test_deepseek import model_config, outline_for, provider_response, task_fields
from test_script_button import Remote


def workspace(w: Path, action="retime"):
    config = {
        "audio": {
            "provider": "macos_say",
            "voice": "Tingting",
            "provider_version": "test",
            "words_per_minute": 180,
        }
    }
    atomic_write(w / "config/config.local.json", config)
    atomic_write(w / "config/deepseek.local.json", model_config())
    fields = task_fields()
    request = request_from_task_fields(fields)
    outline = outline_for(request)
    outline.update(
        schema_version="1.0.0",
        revision=1,
        provider="deepseek",
        model="deepseek-flash",
        review_status="pending",
        approval=None,
    )
    outline["content_sha256"] = content_hash(outline)
    atomic_write(w / "requests/TASK_001" / f"{request['request_sha256']}.json", request)
    atomic_write(w / "jobs/TASK_001/r1/script_outline.json", outline)
    fields.update(
        script_revision=1,
        script_sha256=outline["content_sha256"],
        script_input_sha256=request["request_sha256"],
        script_review_status="pending",
        script_edit_draft=editable_text(outline),
        script_change_request=None,
        script_action=action,
        script_action_actor=[{"id": "ou_test"}],
        script_action_revision=1,
        script_action_sha256=outline["content_sha256"],
        script_action_input_sha256=request["request_sha256"],
        script_action_draft=editable_text(outline),
        script_action_feedback=None,
        script_action_duration=24,
    )
    mapping = {
        "base_token": "test",
        "tables": {
            "tasks": {"table_id": "tbltest", "field_ids": {k: k for k, _, _, _ in TASK_FIELDS}}
        },
    }
    return config, request, outline, Remote(fields), mapping


def measured(w: Path, seconds=4):
    def call(text):
        p = w / "fake_audio" / f"{digest(text)}.wav"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"test-only-audio-measurement")
        return {
            "path": str(p),
            "sha256": file_hash(p),
            "samples": int(seconds * 48000),
            "sample_rate": 48000,
        }

    return call


def capture(remote, outline, action):
    remote.fields.update(
        script_action=action,
        script_action_revision=outline["revision"],
        script_action_sha256=outline["content_sha256"],
        script_action_input_sha256=outline["request_sha256"],
        script_action_draft=remote.fields["script_edit_draft"],
    )


def test_edit_roundtrip_and_validation(tmp_path):
    _, _, o, _, _ = workspace(tmp_path)
    assert apply_edit(o, editable_text(o)) == o
    changed = apply_edit(o, editable_text(o).replace("第1段旁白", "删减后的旁白"))
    assert changed["shots"][0]["narration"] == "删减后的旁白"
    assert o["shots"][0]["narration"] == "第1段旁白"
    for text in [
        "",
        editable_text(o).replace("【S01】", "【S99】"),
        editable_text(o).replace("旁白：第1段旁白", ""),
    ]:
        with pytest.raises(Blocked):
            apply_edit(o, text)


def test_measured_allocation_and_overflow(tmp_path):
    _, _, o, _, _ = workspace(tmp_path)
    times = [
        {"samples": n * 48000, "sample_rate": 48000, "path": "test", "sha256": "test"}
        for n in [2, 6, 3, 5]
    ]
    result = allocate_timing(o, times, 600)
    assert result["status"] == "passed"
    assert sum(s["target_frames"] for s in o["shots"]) == 600
    assert o["shots"][1]["target_frames"] > o["shots"][0]["target_frames"]
    assert all(
        x["caption_end_frame"] <= x["start_frame"] + x["duration_frames"]
        for x in result["segments"]
    )
    assert allocate_timing(o, times, 300)["status"] == "overflow"
    with pytest.raises(Blocked):
        allocate_timing(o, [{"samples": True, "sample_rate": 48000}] * 4, 600)


def test_retime_new_version_no_model_then_readback(tmp_path):
    _, _, original, remote, m = workspace(tmp_path)
    first = process_reviews(tmp_path, m, remote, measure=measured(tmp_path))
    assert first["handled"] and not first["errors"]
    path, new = newest_outline(tmp_path, "TASK_001")
    assert new["revision"] == 2 and new["review_status"] == "pending"
    assert load(tmp_path / "jobs/TASK_001/r1/script_outline.json") == original
    assert remote.pending["script_review_action"] == "none"
    assert not list((tmp_path / "jobs").glob("*/rewrite_calls/*/run.json"))
    process_reviews(tmp_path, m, remote, measure=lambda text: pytest.fail("repeated TTS"))
    assert remote.writes == 1
    remote.visible()
    process_reviews(tmp_path, m, remote)
    assert all(
        load(p)["status"] == "synced"
        for p in (tmp_path / "ledger/script_review_buttons").glob("*.json")
    )
    assert load(path.parent / "draft_timing.json")["script_sha256"] == new["content_sha256"]


@pytest.mark.parametrize(
    "change,code",
    [
        ({"script_action_actor": None}, "REVIEW_ACTOR_REQUIRED"),
        ({"script_action_sha256": "old"}, "REVIEW_VERSION_STALE"),
        ({"script_edit_draft": "changed-after-click"}, "SCRIPT_INPUT_STALE"),
        ({"target_duration_sec": 30}, "SCRIPT_INPUT_STALE"),
    ],
)
def test_stale_or_unauthenticated_cannot_edit(tmp_path, change, code):
    _, _, _, remote, m = workspace(tmp_path)
    remote.fields.update(change)
    r = process_reviews(tmp_path, m, remote, measure=lambda t: pytest.fail("unauthorized TTS"))
    assert r["errors"][0]["code"] == code
    assert not (tmp_path / "jobs/TASK_001/r2").exists()


def test_approve_requires_timing_and_exact_current_version(tmp_path):
    _, _, _, remote, m = workspace(tmp_path, "approve")
    assert process_reviews(tmp_path, m, remote)["errors"][0]["code"] == "SCRIPT_TIMING_REQUIRED"
    remote.visible()
    process_reviews(tmp_path, m, remote)
    remote.fields["script_action"] = "retime"
    process_reviews(tmp_path, m, remote, measure=measured(tmp_path))
    remote.visible()
    process_reviews(tmp_path, m, remote)
    _, o = newest_outline(tmp_path, "TASK_001")
    capture(remote, o, "approve")
    assert not process_reviews(tmp_path, m, remote)["errors"]
    _, approved = newest_outline(tmp_path, "TASK_001")
    require_approval(approved)
    assert approved["approval"]["actor_id"] == "ou_test"
    assert remote.pending["script_review_revision"] == 2


def test_overflow_keeps_edited_draft_but_blocks_approve(tmp_path):
    _, _, _, remote, m = workspace(tmp_path)
    process_reviews(tmp_path, m, remote, measure=measured(tmp_path, 8))
    _, o = newest_outline(tmp_path, "TASK_001")
    assert o["revision"] == 2 and o["timing_check"]["status"] == "overflow"
    assert "超时" in remote.pending["script_timing_status"]
    remote.visible()
    process_reviews(tmp_path, m, remote)
    capture(remote, o, "approve")
    assert process_reviews(tmp_path, m, remote)["errors"][0]["code"] == "SCRIPT_TIMING_BLOCKED"
    assert newest_outline(tmp_path, "TASK_001")[1]["approval"] is None


def test_reject_with_feedback_never_calls_tts_or_model(tmp_path):
    _, _, _, remote, m = workspace(tmp_path, "reject")
    remote.fields.update(script_change_request="不合适", script_action_feedback="不合适")
    r = process_reviews(
        tmp_path,
        m,
        remote,
        measure=lambda t: pytest.fail("TTS"),
        rewriter=lambda *a: pytest.fail("model"),
    )
    assert not r["errors"]
    assert newest_outline(tmp_path, "TASK_001")[1]["review_status"] == "rejected"


def test_rewrite_uses_feedback_parent_and_one_call(tmp_path):
    _, _, parent, remote, _ = workspace(tmp_path)
    q = revision_request(tmp_path, remote.fields, parent, action="rewrite", feedback="去掉拉丝")
    calls = []

    def transport(payload, *args):
        calls.append(payload)
        assert "去掉拉丝" in payload["input"] and parent["title"] in payload["input"]
        return provider_response(q)

    rewrite_once(tmp_path, q, transport=transport, environ={"TEST_DEEPSEEK_KEY": "test"})
    rewrite_once(tmp_path, q, transport=lambda *a: pytest.fail("second call"))
    assert len(calls) == 1


def test_unknown_rewrite_not_retried(tmp_path):
    _, _, parent, remote, _ = workspace(tmp_path)
    q = revision_request(tmp_path, remote.fields, parent, action="rewrite", feedback="改变表达")

    def timeout(*a):
        raise TimeoutError()

    with pytest.raises(Blocked):
        rewrite_once(tmp_path, q, transport=timeout, environ={"TEST_DEEPSEEK_KEY": "test"})
    with pytest.raises(Blocked):
        rewrite_once(tmp_path, q, transport=lambda *a: pytest.fail("retry"))


def test_readback_never_resets_revised_input_to_ready(tmp_path):
    c, _, parent, remote, _ = workspace(tmp_path)
    q = revision_request(tmp_path, remote.fields, parent, action="retime")
    save_retimed(tmp_path, c, parent, q, copy.deepcopy(parent), measure=measured(tmp_path))
    r = collect_requests(tmp_path, [{"record_id": "rec1", "fields": remote.fields}])
    assert r["ready"] == []
    assert not list((tmp_path / "ledger/outbox").glob("*.json"))


def test_crash_after_local_version_recovers_without_tts_or_model(tmp_path, monkeypatch):
    import mixcut.script_review_queue as queue

    _, _, _, remote, mapping = workspace(tmp_path)
    real_save = queue.save_retimed

    def interrupted(*args, **kwargs):
        real_save(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(queue, "save_retimed", interrupted)
    with pytest.raises(KeyboardInterrupt):
        process_reviews(tmp_path, mapping, remote, measure=measured(tmp_path))
    assert newest_outline(tmp_path, "TASK_001")[1]["revision"] == 2
    monkeypatch.setattr(queue, "save_retimed", real_save)
    r = process_reviews(
        tmp_path,
        mapping,
        remote,
        measure=lambda t: pytest.fail("Repeated TTS"),
        rewriter=lambda *a: pytest.fail("Repeated paid call"),
    )
    assert r["handled"][0]["recovered"] is True
    assert remote.pending["script_revision"] == 2
    assert remote.writes == 1


def test_timing_evidence_must_match_each_shot(tmp_path):
    from mixcut.script_revision import timing_verified

    c, _, parent, remote, _ = workspace(tmp_path)
    q = revision_request(tmp_path, remote.fields, parent, action="retime")
    path, outline, timing = save_retimed(
        tmp_path, c, parent, q, copy.deepcopy(parent), measure=measured(tmp_path)
    )
    assert timing_verified(tmp_path, path, outline)["status"] == "passed"
    timing["segments"][0]["duration_frames"] -= 1
    atomic_write(path.parent / "draft_timing.json", timing)
    with pytest.raises(Blocked) as error:
        timing_verified(tmp_path, path, outline)
    assert error.value.code == "SCRIPT_TIMING_STALE"


def test_twenty_second_outline_matches_explicit_duration(tmp_path):
    from mixcut.audio import lock_outline_narration_timing

    _, _, outline, _, _ = workspace(tmp_path)
    outline["total_frames"] = 600
    for shot in outline["shots"]:
        shot["target_frames"] = 150
    measurements = [measured(tmp_path)(shot["narration"]) for shot in outline["shots"]]
    assert lock_outline_narration_timing(outline, measurements)["total_frames"] == 600


def test_untouched_queries_with_semicolons_roundtrip_in_new_and_legacy_drafts(tmp_path):
    _, _, o, _, _ = workspace(tmp_path)
    o["shots"][0]["semantic_queries"] = ["捏合；整形", "纸托;粉料"]
    o["content_sha256"] = content_hash(o)
    assert content_hash(apply_edit(o, editable_text(o))) == o["content_sha256"]
    new = editable_text(o)
    import json

    legacy = new.replace(
        json.dumps(o["shots"][0]["semantic_queries"], ensure_ascii=False),
        "；".join(o["shots"][0]["semantic_queries"]),
    )
    assert content_hash(apply_edit(o, legacy)) == o["content_sha256"]


def test_inventory_approval_measures_without_changing_reviewed_script(tmp_path):
    _, _, original, remote, mapping = workspace(tmp_path, "approve")
    original["narration_mode"] = "sales_independent"
    original["content_sha256"] = content_hash(original)
    atomic_write(tmp_path / "jobs/TASK_001/r1/script_outline.json", original)
    remote.fields["script_sha256"] = original["content_sha256"]
    capture(remote, original, "approve")
    result = process_reviews(tmp_path, mapping, remote, measure=measured(tmp_path, 4))
    assert not result["errors"]
    _, approved = newest_outline(tmp_path, "TASK_001")
    assert approved["content_sha256"] == original["content_sha256"]
    require_approval(approved)
    assert remote.pending["script_review_revision"] == 1
    assert remote.pending["tts_measured_sec"] == 16


def test_new_click_after_failed_approval_can_retry_same_version(tmp_path):
    _, _, original, remote, mapping = workspace(tmp_path, "approve")
    assert process_reviews(tmp_path, mapping, remote)["errors"]
    remote.visible()
    process_reviews(tmp_path, mapping, remote)
    from mixcut.script_revision import measure_review_timing

    measure_review_timing(
        tmp_path,
        tmp_path / "jobs/TASK_001/r1/script_outline.json",
        original,
        measure=measured(tmp_path, 4),
    )
    capture(remote, original, "approve")
    assert not process_reviews(tmp_path, mapping, remote)["errors"]
    assert remote.pending["script_review_status"] == "approved"
    assert list((tmp_path / "ledger/script_review_buttons/history").glob("*.json"))
