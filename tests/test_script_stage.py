"""The script button never allocates material or synthesizes audio."""

import json
import pytest
from mixcut.core import Blocked, atomic_write, digest, load
from mixcut import script_stage, executable, doubao, voice_timing, deepseek
from mixcut.workbench import Workbench, EXPECTED_KEYS


def setup(root):
    atomic_write(
        root / "config/script_project_policies/group.json",
        {
            "group_id": "group",
            "generation_policy_version": 3,
            "workflow_mode": "script_first_v1",
            "brand_id": "brand",
            "goods_id": "mango",
            "narration_mode": "sales_independent",
            "store_shots_source_hashes": ["do-not-send-the-library"] * 1000,
        },
    )
    rows = [
        {
            "record_id": f"rec{n}",
            "fields": {
                "task_id": f"task_{n}",
                "target_duration_sec": 10,
                "creative_keywords": "真实水果现做",
                "request_group_id": "group",
                "script_ordinal": n,
            },
        }
        for n in range(1, 4)
    ]
    copy = {
        "scripts": [
            {
                "task_id": r["fields"]["task_id"],
                "title": "Test only",
                "narration": "果" * 50,
                "headline": ["甲", "乙", "丙", "丁"],
            }
            for r in rows
        ]
    }
    return rows, copy


def prohibit_media(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Script stage must never call media selection, TTS, or voice calibration")

    monkeypatch.setattr(executable, "preflight", forbidden)
    monkeypatch.setattr(doubao, "synthesize", forbidden)
    monkeypatch.setattr(executable, "synthesize", forbidden)
    monkeypatch.setattr(voice_timing, "calibrate_voice", forbidden)


def test_generate_and_reuse_are_text_only_without_a_material_library(tmp_path, monkeypatch):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    calls = []

    def provider(root, request, target):
        calls.append(request)
        assert request["script_stage"] == "text_only_v1"
        assert "store_shots_source_hashes" not in request["settings"]
        assert not any(
            k in request for k in ["available_visuals", "allocation", "voice_calibration"]
        )
        assert len(executable.build_copy_input(request, 6000)) < 2000
        assert "字数估时" in executable.copy_instructions(request)
        return copy

    record = script_stage.generate(tmp_path, "group", rows, copy_fn=provider)
    assert len(calls) == 1
    assert script_stage.generate(tmp_path, "group", rows, copy_fn=provider) == record
    for s in record["scripts"]:
        assert s["target_seconds"] == 10 and s["estimated_seconds"] == 10
        assert s["approval"] is None and s["review_status"] == "pending"
        assert not any(k in s for k in ["voiceover", "measured_seconds", "captions", "shots"])
    assert not (tmp_path / "audio").exists() and not (tmp_path / "ledger/montage_v3").exists()


def test_text_length_correction_has_no_tts(tmp_path, monkeypatch):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    calls = []

    def provider(root, request, target):
        calls.append(request)
        return (
            copy
            if len(calls) == 2
            else {"scripts": [{**s, "narration": "果" * 30} for s in copy["scripts"]]}
        )

    record = script_stage.generate(tmp_path, "group", rows, copy_fn=provider)
    assert len(calls) == 2 and calls[1]["text_length_correction"][0]["characters"] == 30
    assert record["scripts"][0]["characters"] == 50


def test_unknown_script_model_outcome_is_not_resent(tmp_path, monkeypatch):
    rows, _ = setup(tmp_path)
    prohibit_media(monkeypatch)
    monkeypatch.setattr(
        deepseek,
        "load_model_config",
        lambda *args: {
            "max_input_chars": 6000,
            "model": "fake",
            "max_output_tokens": 4096,
            "endpoint": "https://example.invalid",
            "timeout_seconds": 1,
        },
    )
    monkeypatch.setattr(deepseek, "_read_key", lambda *args: "test-only")
    calls = []

    def transport(*args):
        calls.append(1)
        raise TimeoutError("unknown")

    monkeypatch.setattr(deepseek, "urllib_transport", transport)
    with pytest.raises(TimeoutError):
        script_stage.generate(tmp_path, "group", rows)
    with pytest.raises(Blocked) as error:
        script_stage.generate(tmp_path, "group", rows)
    assert error.value.code == "V3_MODEL_UNKNOWN" and len(calls) == 1


def test_text_model_length_failure_reuses_both_completed_responses(tmp_path, monkeypatch):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    short = {"scripts": [{**s, "narration": "果" * 30} for s in copy["scripts"]]}
    monkeypatch.setattr(
        deepseek,
        "load_model_config",
        lambda *args: {
            "max_input_chars": 6000,
            "model": "fake",
            "max_output_tokens": 4096,
            "endpoint": "https://example.invalid",
            "timeout_seconds": 1,
        },
    )
    monkeypatch.setattr(deepseek, "_read_key", lambda *args: "test-only")
    calls = []

    def transport(*args):
        calls.append(1)
        return json.dumps(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(short)}],
                    }
                ],
            }
        ).encode()

    monkeypatch.setattr(deepseek, "urllib_transport", transport)
    record = script_stage.generate(tmp_path, "group", rows)
    assert script_stage.generate(tmp_path, "group", rows) == record
    assert all(s["length_check"]["status"] == "needs_revision" for s in record["scripts"])
    first = record["scripts"][0]
    with pytest.raises(Blocked) as error:
        script_stage.review(
            tmp_path,
            "group",
            rows,
            first["task_id"],
            first["content_sha256"],
            "approve",
            {"id": "fixture_actor"},
        )
    assert error.value.code == "SCRIPT_TEXT_LENGTH"
    assert len(calls) == 2 and not (tmp_path / "audio").exists()


def test_script_approval_handoff_and_edit_invalidate_approval_without_audio(tmp_path, monkeypatch):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    record = script_stage.store_copy(tmp_path, "group", rows, copy, provenance={"type": "test"})
    for s in record["scripts"]:
        record = script_stage.review(
            tmp_path, "group", rows, s["task_id"], s["content_sha256"], "approve", {"id": "ou_test"}
        )
    assert record["state"] == "approved"
    handoff = load(tmp_path / "jobs/script_drafts_v1/group/handoff_r0001.json")
    assert handoff["voice_generation_stage"] == "editing"
    assert handoff["next_stage"] == "codex_director_material_search"
    approved = (tmp_path / "jobs/script_drafts_v1/group/r0001.json").read_bytes()
    texts = {s["task_id"]: "果" * 51 for s in record["scripts"]}
    changed = script_stage.edit(tmp_path, "group", rows, texts, record["content_sha256"])
    assert changed["revision"] == 2 and all(s["approval"] is None for s in changed["scripts"])
    assert approved == (tmp_path / "jobs/script_drafts_v1/group/r0001.json").read_bytes()
    assert not (tmp_path / "audio").exists()


def test_old_executable_generator_is_blocked_after_workflow_migration(tmp_path, monkeypatch):
    rows, _ = setup(tmp_path)
    prohibit_media(monkeypatch)
    with pytest.raises(Blocked) as error:
        executable.generate_batch(tmp_path, {}, "group", rows)
    assert error.value.code == "SCRIPT_STAGE_ONLY"


def test_workbench_generate_save_and_approve_skip_material_gate_and_tts(tmp_path, monkeypatch):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    monkeypatch.setattr(executable, "generate_copy", lambda *args: copy)
    bench = object.__new__(Workbench)
    bench.root = tmp_path
    bench.v3_rows = lambda group: rows
    bench.task = lambda task: next(r for r in rows if r["fields"]["task_id"] == task)
    bench.auth_user = lambda: {"id": "ou_test"}
    bench.v3_material_gate = lambda *args: pytest.fail("No material gate during script generation")
    patches = []
    bench.patch = lambda rid, patch: patches.append((rid, patch))
    bench.verify_patch = lambda *args: None
    task = rows[0]["fields"]["task_id"]
    base = {
        "expected": {k: rows[0]["fields"].get(k) for k in EXPECTED_KEYS},
        "batch_input_sha256": digest(script_stage.snapshot(tmp_path, "group", rows)),
    }
    result = bench.action(task, {**base, "action": "generate", "confirm_model_call": True})
    assert result["stage"] == "text_script" and result["audio_generated"] is False
    record = script_stage.latest(tmp_path, "group")
    bench.action(
        task, {**base, "action": "approve", "script_sha256": record["scripts"][0]["content_sha256"]}
    )
    assert len(patches) == 6 and all("剪辑阶段" in patch["plan_preview"] for _, patch in patches)


def test_withdraw_then_regenerate_recovers_completed_copy_with_matching_target_echo(
    tmp_path, monkeypatch
):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    first = script_stage.store_copy(tmp_path, "group", rows, copy, provenance={"type": "test"})
    rejected = script_stage.review(
        tmp_path,
        "group",
        rows,
        first["scripts"][0]["task_id"],
        first["scripts"][0]["content_sha256"],
        "reject",
        {"id": "ou_test"},
    )
    assert rejected["state"] == "changes"
    assert rejected["scripts"][0]["review_status"] == "changes"
    assert all(s["review_status"] == "pending" for s in rejected["scripts"][1:])
    monkeypatch.setattr(deepseek, "load_model_config", lambda *args: {"max_input_chars": 6000})
    monkeypatch.setattr(
        deepseek, "urllib_transport", lambda *args: pytest.fail("Must reuse completed response")
    )
    raw = {
        "scripts": [
            {**s, "narration": "果" * 51, "target_duration_sec": 10} for s in copy["scripts"]
        ]
    }
    original_bytes = []

    def cached_provider(root, request, target):
        response = {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(raw)}]}
            ],
        }
        atomic_write(target / "model_response.json", response)
        atomic_write(target / "model_intent.json", {"input_sha256": digest(request)})
        before = (target / "model_response.json").read_bytes()
        response["output"][0]["content"][0]["text"] = json.dumps(
            {
                "scripts": [
                    x
                    for x in raw["scripts"]
                    if x["task_id"] in {t["task_id"] for t in request["tasks"]}
                ]
            }
        )
        atomic_write(target / "model_response.json", response)
        before = (target / "model_response.json").read_bytes()
        result = executable.generate_copy(root, request, target)
        assert (target / "model_response.json").read_bytes() == before
        original_bytes.append(before)
        normalized = load(target / "normalized_copy.json")
        assert normalized["response_sha256"] == digest(response)
        assert all("target_duration_sec" not in s for s in result["scripts"])
        return result

    second = script_stage.generate(tmp_path, "group", rows, copy_fn=cached_provider)
    assert second["revision"] == 2 and second["state"] == "pending"
    assert second["scripts"][0]["characters"] == 51
    assert all(s["target_seconds"] == 10 and s["characters"] == 50 for s in second["scripts"][1:])
    assert script_stage.generate(tmp_path, "group", rows, copy_fn=cached_provider) == second
    assert len(original_bytes) == 1
    assert load(tmp_path / "jobs/script_drafts_v1/group/r0001.json")["state"] == "changes"


@pytest.mark.parametrize("echo", [11, "10", True])
def test_target_echo_cannot_change_the_human_duration(tmp_path, monkeypatch, echo):
    rows, copy = setup(tmp_path)
    request = {"script_stage": "text_only_v1", "tasks": [r["fields"] for r in rows]}
    response = {"scripts": [{**s, "target_duration_sec": echo} for s in copy["scripts"]]}
    with pytest.raises(Blocked) as error:
        script_stage.normalize_model_copy(request, response)
    assert error.value.code == "SCRIPT_TARGET_MISMATCH"


def test_target_echo_normalization_does_not_accept_other_extra_fields(tmp_path, monkeypatch):
    rows, copy = setup(tmp_path)
    request = {"script_stage": "text_only_v1", "tasks": [r["fields"] for r in rows]}
    response = {
        "scripts": [
            {**s, "target_duration_sec": 10, "made_up": "not allowed"} for s in copy["scripts"]
        ]
    }
    normalized = script_stage.normalize_model_copy(request, response)
    with pytest.raises(Blocked) as error:
        script_stage.store_copy(tmp_path, "group", rows, normalized, provenance={"type": "test"})
    assert error.value.code == "SCRIPT_COPY_SCHEMA"


def test_one_short_draft_keeps_other_passes_and_allows_independent_review(tmp_path, monkeypatch):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    calls = []

    def provider(root, request, target):
        calls.append(request)
        return {
            "scripts": [
                {**s, "narration": "果" * (30 if i == 0 else 50)}
                if len(calls) == 1
                else {**s, "narration": "果" * (35 if i == 0 else 30)}
                for i, s in enumerate(copy["scripts"])
            ]
        }

    record = script_stage.generate(tmp_path, "group", rows, copy_fn=provider)
    assert len(calls) == 2 and len(calls[1]["text_length_correction"]) == 1
    bad, good, _ = record["scripts"]
    assert bad["characters"] == 35 and bad["length_check"]["status"] == "needs_revision"
    assert all(s["characters"] == 50 and "length_check" not in s for s in record["scripts"][1:])
    record = script_stage.review(
        tmp_path,
        "group",
        rows,
        good["task_id"],
        good["content_sha256"],
        "approve",
        {"id": "fixture_actor"},
    )
    approved = record["scripts"][1]
    with pytest.raises(Blocked) as error:
        script_stage.review(
            tmp_path,
            "group",
            rows,
            bad["task_id"],
            bad["content_sha256"],
            "approve",
            {"id": "fixture_actor"},
        )
    assert error.value.code == "SCRIPT_TEXT_LENGTH"
    repaired = script_stage.edit(
        tmp_path, "group", rows, {bad["task_id"]: "果" * 50}, record["content_sha256"]
    )
    assert "length_check" not in repaired["scripts"][0]
    assert repaired["scripts"][0]["review_status"] == "pending"
    assert repaired["scripts"][1] == approved and len(calls) == 2


def test_manual_invalid_edit_keeps_original_draft_and_approval_gate(tmp_path, monkeypatch):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    invalid = {"scripts": [{**s, "narration": "果" * 30} for s in copy["scripts"]]}
    with pytest.raises(Blocked) as error:
        script_stage.store_copy(
            tmp_path, "group", rows, invalid, provenance={"type": "manual_edit"}
        )
    assert (
        error.value.code == "SCRIPT_TEXT_LENGTH" and script_stage.latest(tmp_path, "group") is None
    )
    draft = script_stage.store_copy(
        tmp_path,
        "group",
        rows,
        invalid,
        provenance={"type": "script_ai"},
        allow_length_warning=True,
    )
    with pytest.raises(Blocked):
        script_stage.edit(tmp_path, "group", rows, {"task_1": "果" * 31}, draft["content_sha256"])
    assert script_stage.latest(tmp_path, "group") == draft
    assert all(s["approval"] is None for s in draft["scripts"])


@pytest.mark.parametrize(
    "count,expected",
    [
        (38, "estimate_warning"),
        (37, "estimate_warning"),
        (36, "needs_revision"),
        (43, "estimate_warning"),
        (45, "needs_revision"),
    ],
)
def test_estimated_length_tolerance_does_not_make_minor_word_differences_an_approval_bar(
    count, expected
):
    script = {
        "narration": "果" * count,
        "length_check": {
            "minimum_characters": 39,
            "maximum_characters": 42,
            "assumed_characters_per_second": 4.5,
            "status": "needs_revision",
        },
        "review_status": "pending",
        "title_style": {"present": True},
    }
    assert script_stage.length_review(script)["status"] == expected
    assert bool(script_stage.approval_disabled_reason(script)) == (expected == "needs_revision")


def test_old_pending_one_word_warning_can_be_reviewed_without_mutating_the_draft(
    tmp_path, monkeypatch
):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    copy["scripts"][0]["narration"] = "果" * 41
    # Reproduce the prior version: persist its old needs_revision marker and valid hash.
    record = script_stage.store_copy(
        tmp_path, "group", rows, copy, provenance={"type": "test"}, allow_length_warning=True
    )
    s = record["scripts"][0]
    assert s["length_check"]["status"] == "estimate_warning"
    s["length_check"]["status"] = "needs_revision"
    s["content_sha256"] = script_stage.script_hash(s)
    record["content_sha256"] = digest([x["content_sha256"] for x in record["scripts"]])
    script_stage._save(tmp_path, "group", record)
    original_hash = s["content_sha256"]
    original_text = s["narration"]
    reviewed = script_stage.review(
        tmp_path, "group", rows, s["task_id"], original_hash, "approve", {"id": "fixture_actor"}
    )
    assert reviewed["scripts"][0]["review_status"] == "approved"
    assert (
        reviewed["scripts"][0]["content_sha256"] == original_hash
        and reviewed["scripts"][0]["narration"] == original_text
    )
    assert reviewed["scripts"][1:] == record["scripts"][1:]


def test_manual_small_estimated_difference_saves_with_warning_but_large_difference_rejects(
    tmp_path, monkeypatch
):
    rows, copy = setup(tmp_path)
    prohibit_media(monkeypatch)
    first = script_stage.store_copy(tmp_path, "group", rows, copy, provenance={"type": "test"})
    changed = script_stage.edit(
        tmp_path, "group", rows, {"task_1": "果" * 40}, first["content_sha256"]
    )
    assert changed["scripts"][0]["length_check"]["status"] == "estimate_warning"
    with pytest.raises(Blocked):
        script_stage.edit(tmp_path, "group", rows, {"task_1": "果" * 30}, changed["content_sha256"])
    assert script_stage.latest(tmp_path, "group") == changed
