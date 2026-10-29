"""Director review and deterministic execution, with no real cloud side effects."""

import copy
import pytest
from mixcut import director_stage as stage, director_execution as execution, script_stage
from mixcut.core import Blocked, atomic_write, digest, load
from mixcut import doubao


def setup(root, monkeypatch):
    atomic_write(
        root / "config/script_project_policies/g.json",
        {"brand_id": "b", "goods_id": "m", "workflow_mode": "script_first_v1"},
    )
    atomic_write(
        root / "config/director_voice_policy.json",
        {
            "speech_rate": 8,
            "voices": [
                {"name": n, "speaker": s}
                for n, s in [("悦", "y"), ("梓", "z"), ("亲", "q"), ("阳", "a")]
            ],
        },
    )
    rows = [
        {
            "record_id": f"r{i}",
            "fields": {
                "task_id": f"t{i}",
                "target_duration_sec": 10,
                "creative_keywords": "mango",
                "request_group_id": "g",
                "script_ordinal": i,
            },
        }
        for i in range(3)
    ]
    cp = {
        "scripts": [
            {
                "task_id": f"t{i}",
                "title": "test",
                "narration": "果" * 50,
                "headline": ["a", "b", "c", "d"],
            }
            for i in range(3)
        ]
    }
    record = script_stage.store_copy(root, "g", rows, cp, provenance={"type": "test"})
    for s in record["scripts"]:
        record = script_stage.review(
            root, "g", rows, s["task_id"], s["content_sha256"], "approve", {"id": "test_actor"}
        )
    facts = [
        {"source": {"source_id": f"s{i}"}, "content_sha256": digest([i]), "phases": [{}]}
        for i in range(3)
    ]
    atomic_write(root / "catalog/material_facts_v1.json", {"materials": facts})
    monkeypatch.setattr(
        "mixcut.fact_planning.planning_segments",
        lambda *a: [
            {"source_id": f"s{i}", "source_in_frame": 0, "source_out_frame": 400} for i in range(3)
        ],
    )
    packets = []
    for i, s in enumerate(record["scripts"]):
        fh = facts[i]["content_sha256"]
        packets.append(
            {
                "version": 1,
                "entity": "director_review_packet",
                "task_id": s["task_id"],
                "brand_id": "b",
                "goods_id": "m",
                "text_sha256": s["content_sha256"],
                "total_frames": 300,
                "origin_plan_sha256": "evidence",
                "shots": [
                    {
                        "source_id": f"s{i}",
                        "source_name": f"original{i}.mp4",
                        "need_id": "N1",
                        "source_in_frame": 0,
                        "source_out_frame": 330,
                        "timeline_start_frame": 0,
                        "duration_frames": 300,
                        "speed": 1.1,
                        "source_audio_gain": 0,
                        "transition_out": {"type": "hard_cut", "frames": 0},
                        "rationale": "fruit",
                        "selection_basis": {
                            "type": "indexed_facts",
                            "document_id": "phase_" + digest([f"s{i}", fh, 0])[:24],
                            "facts_sha256": fh,
                        },
                    }
                ],
            }
        )
    return rows, record, packets


class Bench:
    def __init__(self, root):
        self.root = root
        self.data = {}
        self.unknown = False
        self.mapping = {"tables": {"tasks": {"field_ids": {k: k for k in stage.FIELDS}}}}

    def task(self, task):
        return {"record_id": task, "fields": self.data.setdefault(task, {})}

    def patch(self, rid, patch):
        self.data.setdefault(rid, {}).update(patch)
        if self.unknown:
            raise TimeoutError("unknown write")

    def verify_patch(self, task, patch):
        assert all(self.data[task][k] == v for k, v in patch.items())


def approve(root, packet, bench):
    record = stage.submit(root, "g", packet)
    stage.publish(bench, packet["task_id"])
    return stage.review(
        bench, packet["task_id"], record["packet_sha256"], "approve", {"id": "human_test"}
    )


def test_review_gate_publication_and_real_identity_required(tmp_path, monkeypatch):
    rows, text, packets = setup(tmp_path, monkeypatch)
    b = Bench(tmp_path)
    monkeypatch.setattr(doubao, "synthesize", lambda *a, **kw: pytest.fail("Must not synthesize"))
    record = stage.submit(tmp_path, "g", packets[0])
    assert record["review_status"] == "pending"
    with pytest.raises(Blocked, match="审核"):
        stage.gate(tmp_path, "t0")
    with pytest.raises(Blocked):
        stage.review(b, "t0", record["packet_sha256"], "approve", {"id": "x"})
    stage.publish(b, "t0")
    assert "original0.mp4" in b.data["t0"]["director_preview"]
    assert "语速+8" in b.data["t0"]["director_preview"]
    with pytest.raises(Blocked):
        stage.review(b, "t0", record["packet_sha256"], "approve", {})
    stage.review(b, "t0", record["packet_sha256"], "approve", {"id": "human_test"})
    assert stage.gate(tmp_path, "t0")["approval"]["actor_id"] == "human_test"
    assert stage.submit(tmp_path, "g", packets[0])["revision"] == 1


def test_unknown_publish_reconciles_and_approval_cannot_escape_unknown_sync(tmp_path, monkeypatch):
    _, _, p = setup(tmp_path, monkeypatch)
    b = Bench(tmp_path)
    r = stage.submit(tmp_path, "g", p[0])
    b.unknown = True
    with pytest.raises(TimeoutError):
        stage.publish(b, "t0")
    b.unknown = False
    stage.publish(b, "t0")  # reads matching fields; no repeat patch
    b.unknown = True
    with pytest.raises(TimeoutError):
        stage.review(b, "t0", r["packet_sha256"], "approve", {"id": "human"})
    with pytest.raises(Blocked):
        stage.gate(tmp_path, "t0")
    b.unknown = False
    stage.publish(b, "t0")
    assert stage.gate(tmp_path, "t0")["sync_status"] == "synced"


def test_single_edit_preserves_other_approval_and_only_invalidates_affected_plan(
    tmp_path, monkeypatch
):
    rows, text, p = setup(tmp_path, monkeypatch)
    b = Bench(tmp_path)
    approve(tmp_path, p[0], b)
    approve(tmp_path, p[1], b)
    changed = script_stage.edit(tmp_path, "g", rows, {"t0": "果" * 51}, text["content_sha256"])
    assert changed["scripts"][1:] == text["scripts"][1:]
    with pytest.raises(Blocked):
        stage.gate(tmp_path, "t0")
    assert stage.gate(tmp_path, "t1")["review_status"] == "approved"
    script_stage.review(
        tmp_path,
        "g",
        rows,
        "t0",
        changed["scripts"][0]["content_sha256"],
        "reject",
        {"id": "human"},
    )
    calls = []

    def generate(root, request, path):
        calls.append(request)
        assert [t["task_id"] for t in request["tasks"]] == ["t0"]
        return {
            "scripts": [
                {
                    "task_id": "t0",
                    "title": "new",
                    "narration": "果" * 52,
                    "headline": ["a", "b", "c", "d"],
                }
            ]
        }

    regenerated = script_stage.generate(tmp_path, "g", rows, copy_fn=generate)
    assert len(calls) == 1 and regenerated["scripts"][1:] == text["scripts"][1:]


def test_changed_facts_or_batch_overlap_block_even_after_approval(tmp_path, monkeypatch):
    _, _, p = setup(tmp_path, monkeypatch)
    b = Bench(tmp_path)
    approve(tmp_path, p[0], b)
    bad = copy.deepcopy(p[1])
    bad["shots"] = p[0]["shots"]
    with pytest.raises(Blocked):
        stage.submit(tmp_path, "g", bad)
    facts = load(tmp_path / "catalog/material_facts_v1.json")
    facts["materials"][0]["content_sha256"] = "changed"
    atomic_write(tmp_path / "catalog/material_facts_v1.json", facts)
    with pytest.raises(Blocked):
        stage.gate(tmp_path, "t0")


def test_unresolved_step_never_repeated_completed_step_reused(tmp_path, monkeypatch):
    _, _, p = setup(tmp_path, monkeypatch)
    b = Bench(tmp_path)
    approve(tmp_path, p[0], b)
    assert not stage.claim_step(tmp_path, "t0", "project", {"name": "A"})["reused"]
    with pytest.raises(Blocked):
        stage.claim_step(tmp_path, "t0", "project", {"name": "A"})
    with pytest.raises(Blocked):
        stage.complete_step(tmp_path, "t0", "project", {"name": "B"}, {"id": "x"})
    stage.complete_step(tmp_path, "t0", "project", {"name": "A"}, {"id": "x"})
    assert stage.claim_step(tmp_path, "t0", "project", {"name": "A"})["reused"]
    with pytest.raises(Blocked):
        stage.complete_step(tmp_path, "t0", "project", {"name": "A"}, {"id": "y"})


def test_rotation_persistent_and_voice_is_part_of_tts_cache_key(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    voices = [execution.assign_voice(tmp_path, f"t{i}") for i in range(5)]
    assert [v["speaker"] for v in voices] == ["y", "z", "q", "a", "y"]
    assert execution.assign_voice(tmp_path, "t0") == voices[0]
    assert load(tmp_path / "ledger/director_voice_assignments.json")["next_index"] == 5
    a = doubao.request_parameters("text", speaker="y")
    b = doubao.request_parameters("text", speaker="z")
    assert digest(a) != digest(b) and a["audio_params"]["speech_rate"] == 8


@pytest.mark.parametrize(
    "seconds,status", [(8.499, "short"), (8.5, "passed"), (9.5, "passed"), (9.501, "long")]
)
def test_timing_bounds(seconds, status):
    assert execution.timing(seconds, 10)["status"] == status


def test_cumulative_sentence_text_does_not_duplicate_captions():
    receipt = {
        "duration_seconds": 2,
        "sentences": [
            {
                "text": "果香果香果香",
                "words": [
                    {"word": "果", "startTime": 0, "endTime": 0.3},
                    {"word": "香", "startTime": 0.4, "endTime": 0.8},
                ],
            }
        ],
    }
    assert execution.captions("果香", receipt, 300) == [
        {"text": "果香", "start_frame": 6, "end_frame": 300}
    ]
    with pytest.raises(Blocked):
        execution.captions("果香香", receipt, 300)


def test_missing_media_is_checked_without_model(tmp_path, monkeypatch):
    _, _, p = setup(tmp_path, monkeypatch)
    with pytest.raises(Blocked, match="工作副本"):
        execution.media_preflight(tmp_path, p[0], {})


def test_speaker_estimate_uses_existing_audio_without_generating_new_voice(tmp_path, monkeypatch):
    rows, record, _ = setup(tmp_path, monkeypatch)
    policy = load(tmp_path / "config/director_voice_policy.json")
    policy["voices"][0]["calibration"] = {"characters": 42, "duration_seconds": 9.312}
    atomic_write(tmp_path / "config/director_voice_policy.json", policy)
    execution.assign_voice(tmp_path, "new")
    budget = execution.estimated_budget(tmp_path, "new", 10)
    assert budget["minimum_characters"] == 39 and budget["maximum_characters"] == 42
    assert budget["suggested_characters"] == 41
    monkeypatch.setattr(
        doubao, "synthesize", lambda *a, **kw: pytest.fail("Estimate cannot generate audio")
    )
    row = [
        {**rows[0], "fields": {**rows[0]["fields"], "task_id": "new", "request_group_id": "other"}}
    ]
    requests = []

    def provider(root, request, path):
        requests.append(request)
        return {
            "scripts": [
                {
                    "task_id": "new",
                    "title": "test",
                    "narration": "果" * 41,
                    "headline": ["a", "b", "c", "d"],
                }
            ]
        }

    script_stage.generate(tmp_path, "other", row, copy_fn=provider)
    assert requests[0]["script_length"][0]["maximum_characters"] == 42


def test_editor_compiler_rejects_volatile_uploads_and_separates_speed_from_timing(
    tmp_path, monkeypatch
):
    from mixcut import director_editor as editor

    _, _, p = setup(tmp_path, monkeypatch)
    b = Bench(tmp_path)
    record = approve(tmp_path, p[0], b)
    atomic_write(
        stage.folder(tmp_path, "t0") / "audio_r0001.json",
        {
            "director_sha256": record["packet_sha256"],
            "status": "ready_for_editing",
            "audio": {"path": "/provider/speech.mp3", "sha256": "audio", "duration_frames": 270},
            "captions": [],
        },
    )
    monkeypatch.setattr(
        editor,
        "media_preflight",
        lambda *args: {
            "status": "passed",
            "items": [{"source_id": "s0", "working_sha256": "video"}],
        },
    )
    native = {
        "timeline": {"items": [], "width": 1080, "height": 1920, "fps": 30},
        "mediaPool": {
            "assets": [
                {"name": "s0.mp4", "id": "source_pool", "src": "/media/uploads/source.mp4"},
                {"name": "speech.mp3", "id": "voice_pool", "src": "/media/uploads/voice.mp3"},
                {"name": "t0_director_typography", "id": "graphic_pool"},
            ]
        },
    }
    result = editor.compile_adds(tmp_path, "t0", "session", native, {}, verify_uploads=False)
    assert result[-1]["arguments"]["adds"][0]["sourceStartFrame"] == 0
    native["mediaPool"]["assets"][0]["src"] = "blob:temporary"
    with pytest.raises(Blocked):
        editor.compile_adds(tmp_path, "t0", "session", native, {}, verify_uploads=False)
    adds = {
        "results": [
            {"ok": True, "kind": kind, "placed": {"itemId": iid}}
            for kind, iid in [
                ("video", "shot_item"),
                ("audio", "voice_item"),
                ("motion-graphic", "overlay_item"),
            ]
        ]
    }
    updates = editor.compile_updates(tmp_path, "t0", "session", adds)
    assert updates[0]["arguments"]["updates"][0]["speed"] == 1.1
    assert "durationInFrames" not in updates[0]["arguments"]["updates"][0]
    assert updates[1]["arguments"]["updates"][0]["durationInFrames"] == 300
    assert "speed" not in updates[1]["arguments"]["updates"][0]
