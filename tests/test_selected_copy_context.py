"""Synthetic library scale checks are not real-media or paid-provider acceptance."""

import copy
import json
import pytest
from test_montage_v3 import (
    approve_segments,
    setup_policy,
    request_rows,
    fake_copy,
    fake_tts,
)
from test_montage_v3 import library  # noqa: F401 - pytest fixture
from mixcut.core import Blocked, atomic_write, load, digest
from mixcut.montage import allocate_batch, validate_allocation
from mixcut.executable import (
    build_copy_input,
    selected_copy_request,
    input_snapshot,
    generate_batch,
)


def synthetic_pool(count):
    return [
        {
            "source_id": f"source_{n:06d}",
            "segment_id": f"segment_{n:06d}",
            "source_sha256": "a" * 64,
            "content_sha256": "b" * 64,
            "source_in_frame": 0,
            "source_out_frame": 600,
            "role": "general",
            "description": f"画面{n:06d}：把果肉包入糯米皮。",
            "events": [
                {"frame": 30, "text": f"动作{n:06d}：放入果肉。"},
                {"frame": 590, "text": "未选区间的动作不应进入文案"},
            ],
        }
        for n in range(count)
    ]


def synthetic_request(count):
    segments = synthetic_pool(count)
    rows = request_rows()
    plans, _ = allocate_batch(
        segments, [{"task_id": r["fields"]["task_id"], "total_frames": 180} for r in rows]
    )
    return selected_copy_request(
        input_snapshot(rows, {"brand_id": "brand", "goods_id": "mango"}), plans, segments
    )


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_input_is_independent_of_unselected_library_size(count):
    request = synthetic_request(count)
    text = build_copy_input(request, 6000)
    assert text == build_copy_input(synthetic_request(100), 6000)
    assert len(request["available_visuals"]) == 12
    assert len(text) < 2000
    assert "未选区间" not in text and "source_" not in text and "segment_" not in text
    assert len(json.loads(text)["task_visuals"]) == 3


def test_saved_selection_retries_without_researching_the_growing_library(library, monkeypatch):  # noqa: F811
    import mixcut.executable as executable

    approve_segments(library)
    setup_policy(library)
    seen = []

    def unavailable(root, request, folder):
        seen.append(copy.deepcopy(request))
        raise Blocked("TEST_UNAVAILABLE", "fake provider unavailable", 3)

    with pytest.raises(Blocked):
        generate_batch(library, {}, "group", request_rows(), copy_fn=unavailable, tts_fn=fake_tts)
    path = library / "requests/executable_v3/group_v3_1/input.selected_v1.json"
    original = path.read_bytes()
    saved = load(path)
    assert len(saved["available_visuals"]) == 12
    from mixcut.segments import segment_hash, eligible_segments

    index = load(library / "catalog/segments_v3.json")
    extra = copy.deepcopy(index["segments"][0])
    extra.update(segment_id="new_segment_after_selection", description="新增但未被本批选择的画面")
    extra["content_sha256"] = segment_hash(extra)
    extra["approval"]["approved_content_sha256"] = extra["content_sha256"]
    index["segments"].append(extra)
    atomic_write(library / "catalog/segments_v3.json", index)
    assert len(eligible_segments(library, "brand", "mango")) == 13

    def forbidden(*args, **kwargs):
        raise AssertionError("Retry must reuse the valid persisted selection")

    monkeypatch.setattr(executable, "allocate_batch", forbidden)
    batch = generate_batch(library, {}, "group", request_rows(), copy_fn=fake_copy, tts_fn=fake_tts)
    assert path.read_bytes() == original
    assert [{"task_id": s["task_id"], "shots": s["shots"]} for s in batch["scripts"]] == saved[
        "allocation"
    ]


def test_old_undispatched_library_input_is_preserved_during_migration(library):  # noqa: F811
    segments = approve_segments(library)
    setup_policy(library)
    settings = load(library / "config/script_project_policies/group.json")
    old = {
        **input_snapshot(request_rows(), settings),
        "available_visuals": [
            {"segment_id": s["segment_id"], "description": s["description"], "events": s["events"]}
            for s in segments
        ],
    }
    folder = library / "requests/executable_v3/group_v3_1"
    atomic_write(folder / "input.json", old)
    original = (folder / "input.json").read_bytes()
    generate_batch(library, {}, "group", request_rows(), copy_fn=fake_copy, tts_fn=fake_tts)
    assert (folder / "input.json").read_bytes() == original
    selected = load(folder / "input.selected_v1.json")
    assert selected["visual_context_policy"] == "selected_shots_v1"
    assert len(build_copy_input(selected, 6000)) < 6000


def test_legacy_paid_response_is_recovered_without_redispatch(library, monkeypatch):  # noqa: F811
    import mixcut.deepseek as provider

    segments = approve_segments(library)
    setup_policy(library)
    settings = load(library / "config/script_project_policies/group.json")
    old = {
        **input_snapshot(request_rows(), settings),
        "available_visuals": [
            {"segment_id": s["segment_id"], "description": s["description"], "events": s["events"]}
            for s in segments
        ],
    }
    folder = library / "requests/executable_v3/group_v3_1"
    atomic_write(folder / "input.json", old)
    atomic_write(folder / "model_intent.json", {"input_sha256": digest(old)})
    result = fake_copy(library, old, folder)
    atomic_write(
        folder / "model_response.json",
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(result)}],
                }
            ],
        },
    )
    before = (folder / "input.json").read_bytes()
    monkeypatch.setattr(provider, "load_model_config", lambda *args: {"max_input_chars": 1000})

    def forbidden(*args):
        raise AssertionError("An existing paid response must never be resubmitted")

    monkeypatch.setattr(provider, "_read_key", forbidden)
    monkeypatch.setattr(provider, "urllib_transport", forbidden)
    batch = generate_batch(library, {}, "group", request_rows(), tts_fn=fake_tts)
    assert len(batch["scripts"]) == 3
    assert (folder / "input.json").read_bytes() == before
    assert not (folder / "input.selected_v1.json").exists()


def test_saved_plan_changes_and_new_reservations_cannot_be_silently_replaced():
    segments = synthetic_pool(100)
    requests = [{"task_id": f"task_{n}", "total_frames": 180} for n in range(3)]
    plans, _ = allocate_batch(segments, requests)
    assert validate_allocation(plans, segments, requests)["status"] == "passed"
    changed = copy.deepcopy(segments)
    changed[0]["content_sha256"] = digest("changed facts")
    with pytest.raises(Blocked) as error:
        validate_allocation(plans, changed, requests)
    assert error.value.code == "V3_SEGMENT_STALE"
    with pytest.raises(Blocked) as error:
        validate_allocation(plans, segments, requests, reserved=[plans[0]["shots"][0]])
    assert error.value.code == "V3_RESERVATION_OVERLAP"
