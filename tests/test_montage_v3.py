import copy
from pathlib import Path
import pytest
from mixcut.core import Blocked, atomic_write, digest, file_hash, load
from mixcut.contracts import content_hash
from mixcut.segments import eligible_segments, read_segments, review_segments, segment_hash
from mixcut.montage import (
    allocate_batch,
    diversity,
    needed_frames,
    reservations,
    executable_hash,
    validate_executable,
)
from mixcut.executable import generate_batch, review_script, validate_current
from mixcut.doubao import aligned_captions
from mixcut.montage_engine import verify_native, motion_props

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def library(tmp_path):
    sources, clips, segments = [], [], []
    originals = tmp_path / "originals"
    originals.mkdir()
    atomic_write(
        tmp_path / "config/config.local.json",
        {"source_roots": [{"root_id": "originals", "path": str(originals)}]},
    )
    for n in range(12):
        sid, cid = f"s_{n}", f"c_{n}"
        media = originals / f"{n}.mov"
        media.write_bytes(f"isolated unit test source {n}".encode())
        source_hash = file_hash(media)
        sources.append(
            {
                "source_id": sid,
                "file_sha256": source_hash,
                "locations": [{"root_id": "originals", "relative_path": f"{n}.mov"}],
                "duration_sec": 20,
            }
        )
        clip = {
            "clip_id": cid,
            "source_id": sid,
            "brand_id": "brand",
            "goods_id": "mango",
            "usable_start_sec": 0,
            "usable_end_sec": 20,
            "rights_confirmed": True,
            "review_status": "approved",
            "is_fixture": False,
        }
        clip["content_sha256"] = content_hash(clip)
        clip["approval"] = {
            "actor_id": "ou_human",
            "approved_at": "date",
            "approved_content_sha256": clip["content_sha256"],
        }
        clips.append(clip)
        s = {
            "segment_id": f"seg_{n}",
            "source_id": sid,
            "source_sha256": source_hash,
            "parent_clip_id": cid,
            "source_in_frame": 0,
            "source_out_frame": 600,
            "description": f"Visible action {n}",
            "events": [],
            "role": "general",
            "evidence": {"file": "review.jpg"},
            "precision": "sampled_suggestion",
            "review_status": "pending",
            "approval": None,
            "version": 1,
        }
        s["content_sha256"] = segment_hash(s)
        segments.append(s)
    atomic_write(tmp_path / "catalog/sources.json", sources)
    atomic_write(tmp_path / "catalog/clips.json", clips)
    atomic_write(tmp_path / "catalog/segments_v3.json", {"version": 3, "segments": segments})
    atomic_write(
        tmp_path / "config/executable_montage_v3.json",
        load(ROOT / "config/executable_montage_v3.json"),
    )
    return tmp_path


def approve_segments(root):
    for n, c in enumerate(load(root / "catalog/clips.json")):
        index = read_segments(root)
        s = index["segments"][n]
        review_segments(
            root,
            c["clip_id"],
            {
                "confirmed": True,
                "index_sha256": digest(index),
                "parent_sha256": c["content_sha256"],
                "segments": [
                    {"segment_id": s["segment_id"], "expected_sha256": s["content_sha256"]}
                ],
            },
            {"id": "ou_human"},
        )
    return eligible_segments(root, "brand", "mango")


def request_rows():
    return [
        {
            "fields": {
                "task_id": f"task_{n}",
                "creative_keywords": "mango",
                "target_duration_sec": 6,
                "script_ordinal": n,
                "request_group_id": "group",
            }
        }
        for n in range(3)
    ]


def setup_policy(root):
    atomic_write(
        root / "config/script_project_policies/group.json",
        {
            "generation_policy_version": 3,
            "group_id": "group",
            "brand_id": "brand",
            "goods_id": "mango",
            "minimum_unique": 0.7,
        },
    )


def fake_copy(root, request, folder):
    return {
        "scripts": [
            {
                "task_id": r["task_id"],
                "title": "自然推荐",
                "narration": "芒果软糯一口",
                "headline": ["想吃点甜的", "芒果糯米糍", "芒果果肉", "软糯一口"],
            }
            for r in request["tasks"]
        ]
    }


def fake_tts(root, config, text):
    path = root / "audio/fake.mp3"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"not a real video - isolated unit test")
    return {
        "path": str(path),
        "sha256": file_hash(path),
        "duration_frames": 150,
        "duration_seconds": 5,
        "speaker": "test",
        "speech_rate": 8,
        "sentences": [
            {
                "text": text,
                "words": [
                    {"word": w, "startTime": i * 0.8, "endTime": (i + 1) * 0.8}
                    for i, w in enumerate(text)
                ],
            }
        ],
    }


def batch(root):
    setup_policy(root)
    approve_segments(root)
    return generate_batch(root, {}, "group", request_rows(), copy_fn=fake_copy, tts_fn=fake_tts)


def test_suggestions_never_imply_approval_and_version_invalidation(library):
    assert not eligible_segments(library, "brand", "mango")
    assert len(approve_segments(library)) == 12
    assert not eligible_segments(library, "brand", "strawberry")
    clips = load(library / "catalog/clips.json")
    clips[0]["description"] = "changed"
    atomic_write(library / "catalog/clips.json", clips)
    assert len(eligible_segments(library, "brand", "mango")) == 11


def test_segment_concurrent_edit_and_parent_range(library):
    index = read_segments(library)
    s = index["segments"][0]
    c = load(library / "catalog/clips.json")[0]
    body = {
        "confirmed": True,
        "index_sha256": "stale",
        "parent_sha256": c["content_sha256"],
        "segments": [{"segment_id": s["segment_id"], "expected_sha256": s["content_sha256"]}],
    }
    with pytest.raises(Blocked) as e:
        review_segments(library, c["clip_id"], body, {"id": "ou_human"})
    assert e.value.code == "SEGMENT_STALE"
    body["index_sha256"] = digest(index)
    body["segments"][0]["source_out_frame"] = 601
    with pytest.raises(Blocked) as e:
        review_segments(library, c["clip_id"], body, {"id": "ou_human"})
    assert e.value.code == "SEGMENT_PARENT_RANGE"
    assert read_segments(library) == index


def test_batch_allocation_distinct_opening_combinations_no_overlap(library):
    segments = approve_segments(library)
    plans, report = allocate_batch(
        segments, [{"task_id": f"t{i}", "total_frames": 180} for i in range(3)]
    )
    assert report["status"] == "passed"
    assert all(r["unique_source_duration_ratio"] >= 0.7 for r in report["scripts"])
    assert len({p["shots"][0]["source_id"] for p in plans}) == 3
    assert all(
        s["source_out_frame"] - s["source_in_frame"] == needed_frames(s["duration_frames"], 1.1)
        for p in plans
        for s in p["shots"]
    )
    bad = copy.deepcopy(plans)
    bad[1]["shots"][0] = copy.deepcopy(bad[0]["shots"][0])
    with pytest.raises(Blocked) as e:
        diversity(bad)
    assert e.value.code == "V3_SOURCE_OVERLAP"
    with pytest.raises(Blocked) as e:
        allocate_batch(segments[:1], [{"task_id": "t", "total_frames": 180}])
    assert e.value.code == "V3_MATERIAL_SHORTAGE"


def test_existing_reservations_move_source_windows(library):
    segments = approve_segments(library)
    used = [
        {"source_id": s["source_id"], "source_in_frame": 0, "source_out_frame": 100}
        for s in segments
    ]
    plans, _ = allocate_batch(segments, [{"task_id": "t", "total_frames": 180}], reserved=used)
    assert all(s["source_in_frame"] >= 100 for s in plans[0]["shots"])


@pytest.mark.parametrize("speed", [True, 0, 1.3, float("nan")])
def test_speed_validation(speed):
    with pytest.raises(Blocked):
        needed_frames(30, speed)


def test_complete_generation_idempotence_review_release_and_revision(library):
    result = batch(library)
    assert len(reservations(library)) == 1

    def forbidden(*a):
        raise AssertionError("should not call providers twice")

    assert (
        generate_batch(library, {}, "group", request_rows(), copy_fn=forbidden, tts_fn=forbidden)
        == result
    )
    s = result["scripts"][0]
    with pytest.raises(Blocked) as e:
        validate_current(library, s, approved=True)
    assert e.value.code == "V3_SCRIPT_UNAPPROVED"
    reviewed = review_script(
        library,
        "group",
        s["task_id"],
        s["content_sha256"],
        "approve",
        {"id": "ou_human"},
        request_rows(),
    )
    validate_current(library, reviewed["scripts"][0], approved=True)
    review_script(
        library,
        "group",
        s["task_id"],
        s["content_sha256"],
        "reject",
        {"id": "ou_human"},
        request_rows(),
    )
    assert not reservations(library)
    with pytest.raises(Blocked) as e:
        validate_current(library, reviewed["scripts"][0], approved=True)
    assert e.value.code == "V3_SCRIPT_SUPERSEDED"
    newer = generate_batch(library, {}, "group", request_rows(), copy_fn=fake_copy, tts_fn=fake_tts)
    assert newer["version"] == 2
    assert all(x["approval"] is None for x in newer["scripts"])


def test_preflight_blocks_before_paid_call(library):
    setup_policy(library)

    def forbidden(*a):
        raise AssertionError("paid call before approval")

    with pytest.raises(Blocked) as e:
        generate_batch(library, {}, "group", request_rows(), copy_fn=forbidden, tts_fn=forbidden)
    assert e.value.code == "V3_NO_REVIEWED_SEGMENTS"


def test_source_tamper_false_transition_and_caption_text(library):
    result = batch(library)
    s = result["scripts"][0]
    current_segments = eligible_segments(library, "brand", "mango")
    for modify, code in [
        (lambda s: s["shots"][0].update(source_out_frame=700), "V3_SEGMENT_STALE"),
        (
            lambda s: s["shots"][0].update(transition_out={"type": "dissolve", "frames": 5}),
            "V3_SCRIPT_SCHEMA",
        ),
        (lambda s: s["captions"][0].update(text="other"), "V3_CAPTION_TEXT"),
    ]:
        bad = copy.deepcopy(s)
        modify(bad)
        bad["content_sha256"] = executable_hash(bad)
        with pytest.raises(Blocked) as e:
            validate_executable(bad, current_segments)
        assert e.value.code == code


def test_actual_word_timestamps_required_and_text_verified():
    with pytest.raises(Blocked):
        aligned_captions("芒果", [{"text": "芒果"}], 2, 90)
    with pytest.raises(Blocked):
        aligned_captions(
            "芒果",
            [{"text": "芒果", "words": [{"word": "草莓", "startTime": 0, "endTime": 1}]}],
            2,
            90,
        )


def test_native_readback_detects_speed_source_voice_and_graphic_changes(library):
    s = batch(library)["scripts"][0]
    bindings = {x["source_id"]: {"asset_id": x["source_id"]} for x in s["shots"]}
    bindings.update(
        voiceover={"asset_id": "voice"}, typography={"asset_id": s["typography"]["asset_id"]}
    )
    items = [
        {
            "id": f"i{n}",
            "kind": "video",
            "sourceAssetId": x["source_id"],
            "sourceStartFrame": x["source_in_frame"],
            "sourceDurationInFrames": x["source_out_frame"] - x["source_in_frame"],
            "durationInFrames": x["duration_frames"],
            "startFrame": x["timeline_start_frame"],
            "volume": 0,
        }
        for n, x in enumerate(s["shots"])
    ]
    items += [
        {
            "kind": "audio",
            "sourceAssetId": "voice",
            "sourceStartFrame": 0,
            "startFrame": 6,
            "durationInFrames": 150,
            "volume": 1,
        },
        {
            "kind": "motion-graphic",
            "sourceAssetId": s["typography"]["asset_id"],
            "startFrame": 0,
            "durationInFrames": 180,
            "props": motion_props(s),
        },
    ]
    actual = {
        "timeline": {"items": items, "transitions": []},
        "settings": {"width": 1080, "height": 1920, "fps": 30},
    }
    assert verify_native(s, actual, bindings)["status"] == "passed"
    actual["timeline"]["items"][0]["sourceDurationInFrames"] -= 1
    report = verify_native(s, actual, bindings)
    assert report["status"] == "failed" and "shot_1.sourceDurationInFrames" in report["differences"]


def test_prepared_package_recovers_without_repeating_provider_calls(library, monkeypatch):
    import mixcut.executable as ex

    setup_policy(library)
    approve_segments(library)
    original = ex.save_batch

    def interrupted(*args):
        raise OSError("interrupted after reservation")

    monkeypatch.setattr(ex, "save_batch", interrupted)
    with pytest.raises(OSError):
        generate_batch(library, {}, "group", request_rows(), copy_fn=fake_copy, tts_fn=fake_tts)
    assert len(reservations(library)) == 1
    monkeypatch.setattr(ex, "save_batch", original)

    def forbidden(*args):
        raise AssertionError("provider should not be repeated")

    restored = generate_batch(
        library, {}, "group", request_rows(), copy_fn=forbidden, tts_fn=forbidden
    )
    assert restored == ex.latest_batch(library, "group")


def test_changed_input_cannot_approve_but_can_release_reservations(library):
    result = batch(library)
    script = result["scripts"][0]
    changed = request_rows()
    changed[0]["fields"]["creative_keywords"] = "different"
    with pytest.raises(Blocked) as e:
        review_script(
            library,
            "group",
            script["task_id"],
            script["content_sha256"],
            "approve",
            {"id": "ou_human"},
            changed,
        )
    assert e.value.code == "V3_INPUT_CHANGED"
    review_script(
        library,
        "group",
        script["task_id"],
        script["content_sha256"],
        "reject",
        {"id": "ou_human"},
        changed,
    )
    assert not reservations(library)


def test_scoped_migration_keeps_approved_sample_on_legacy_path(library):
    from mixcut.executable import uses_v3

    setup_policy(library)
    path = library / "config/script_project_policies/group.json"
    p = load(path)
    p["generation_task_ids"] = ["task_0", "task_1"]
    atomic_write(path, p)
    assert uses_v3(library, request_rows()[0]["fields"])
    assert not uses_v3(library, request_rows()[2]["fields"])


def test_native_readback_uses_actual_active_sequence_dimensions(library):
    script = batch(library)["scripts"][0]
    bindings = {x["source_id"]: {"asset_id": x["source_id"]} for x in script["shots"]}
    bindings.update(
        voiceover={"asset_id": "voice"}, typography={"asset_id": script["typography"]["asset_id"]}
    )
    native = {
        "activeTimelineId": "seq",
        "timelines": [{"id": "seq", "width": 1080, "height": 1920, "fps": 30}],
        "timeline": {"items": []},
    }
    report = verify_native(script, native, bindings)
    assert not any(x.startswith("canvas.") for x in report["differences"])


def test_provider_bytes_are_parsed_once_and_saved_for_reconcile(library, monkeypatch):
    import json
    import mixcut.deepseek as provider
    from mixcut.executable import generate_copy

    calls = []
    monkeypatch.setattr(
        provider,
        "load_model_config",
        lambda *args: {
            "max_input_chars": 20000,
            "model": "deepseek-flash",
            "max_output_tokens": 8192,
            "endpoint": "https://api.deepseek.com/responses",
            "timeout_seconds": 20,
        },
    )
    monkeypatch.setattr(provider, "_read_key", lambda *args: "isolated-test-key")
    result = fake_copy(library, {"tasks": [{"task_id": "task_0"}]}, library)

    def transport(*args):
        calls.append(args[0])
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
    folder = library / "requests/test-copy"
    assert generate_copy(library, {"tasks": []}, folder) == result
    assert generate_copy(library, {"tasks": []}, folder) == result
    assert len(calls) == 1


def test_provider_unknown_dispatch_is_not_repeated(library, monkeypatch):
    import mixcut.deepseek as provider
    from mixcut.executable import generate_copy

    monkeypatch.setattr(provider, "load_model_config", lambda *args: {"max_input_chars": 20000})
    folder = library / "requests/unknown"
    atomic_write(folder / "model_intent.json", {"status": "dispatching"})
    with pytest.raises(Blocked) as e:
        generate_copy(library, {}, folder)
    assert e.value.code == "V3_MODEL_UNKNOWN"


def test_old_plan_cannot_bypass_new_full_script_approval(library):
    from mixcut.contracts import validate_plan

    setup_policy(library)
    p = library / "config/script_project_policies/group.json"
    settings = load(p)
    settings["generation_task_ids"] = ["task_0", "task_1"]
    atomic_write(p, settings)
    with pytest.raises(Blocked) as e:
        validate_plan({"task_id": "task_0"}, library, True)
    assert e.value.code == "V3_EXECUTOR_REQUIRED"


def test_offline_source_blocks_before_paid_requests(library):
    setup_policy(library)
    approve_segments(library)
    for p in (library / "originals").glob("*"):
        p.unlink()

    def forbidden(*args):
        raise AssertionError("No provider call with unavailable source footage")

    with pytest.raises(Blocked) as e:
        generate_batch(library, {}, "group", request_rows(), copy_fn=forbidden, tts_fn=forbidden)
    assert e.value.code == "V3_MEDIA_UNAVAILABLE"
