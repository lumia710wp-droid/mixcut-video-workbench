"""Synthetic metadata/evidence tests; no real visual review or production approval."""

import copy
import json
from pathlib import Path
import pytest
from test_fact_planning import facts_library, legacy_library  # noqa: F401 - pytest fixtures
from mixcut.core import Blocked, atomic_write, load, digest, file_hash
from mixcut.contracts import content_hash
from mixcut.material_search import (
    build_index,
    query_index,
    show_candidate,
    validate_director_plan,
    _supports,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("手从托盘拿起黄色果肉放到白皮上", True),
        ("手移向左侧托盘，黄色果肉放到白皮中央", True),
        ("没有把黄色果肉放到白皮上", False),
        ("黄色果肉未放到白皮上", False),
        ("白皮放到盘上，旁边有果肉", False),
    ],
)
def test_observed_action_word_order_is_supported_without_crossing_meaning(text, expected):
    assert _supports(text, "放果肉") is expected


@pytest.fixture
def search_library(facts_library):  # noqa: F811 - imported pytest fixture
    root = facts_library
    data = load(root / "catalog/material_facts_v1.json")
    for material in data["materials"]:
        sid = material["source"]["source_id"]
        frames = []
        for t in [1, 3, 5, 9, 11, 13, 18]:
            path = root / "analysis" / f"{sid}_{t}.jpg"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(f"not actual pixels: synthetic evidence {sid} {t}".encode())
            frames.append({"path": str(path), "time_sec": t, "sha256": file_hash(path)})
        material["evidence"]["frames"] = frames
        material["content_sha256"] = digest(
            {k: v for k, v in material.items() if k != "content_sha256"}
        )
    atomic_write(root / "catalog/material_facts_v1.json", data)
    build_index(root)
    return root


def query(**changes):
    return {
        "brand_id": "brand",
        "goods_id": "mango",
        "query": "收口",
        "must": ["收口"],
        "min_duration_sec": 1.5,
        "limit": 3,
        "max_per_source": 2,
        "max_output_chars": 6000,
        **changes,
    }


def save_result(root, q=None, name="result.json"):
    result = query_index(root, q or query())
    path = root / "reports" / name
    atomic_write(path, result)
    return path, result


def make_plan(root):
    path, result = save_result(root)
    candidate = result["candidates"][0]
    detail = show_candidate(root, path, candidate["candidate_id"])
    script = root / "reports/script.json"
    atomic_write(script, {"task_id": "video", "narration": "test-only script"})
    shot = {
        "candidate_id": candidate["candidate_id"],
        "search_result_path": str(path),
        "source_in_frame": 240,
        "source_out_frame": 420,
        "timeline_start_frame": 0,
        "duration_frames": 180,
        "speed": 1,
        "source_audio_gain": 0,
        "transition_out": {"type": "hard_cut", "frames": 0},
        "rationale": "test-only declared reason",
        "visual_review": {
            "reviewer": "codex",
            "note": "synthetic test declaration, not real viewing",
            "evidence": [detail["frames"][0]],
        },
    }
    return {
        "entity": "codex_director_plan",
        "version": 1,
        "script_path": str(script),
        "script_sha256": file_hash(script),
        "brand_id": "brand",
        "goods_id": "mango",
        "tasks": [{"task_id": "video", "total_frames": 180, "shots": [shot]}],
    }


def test_index_reuses_without_modifying_facts_and_scope_is_enforced(search_library):
    root = search_library
    before = {p.name: file_hash(p) for p in (root / "catalog").glob("*.json")}
    result = build_index(root)
    assert result["status"] == "reused" and result["documents"] == 36
    assert query_index(root, query(goods_id="other"))["status"] == "no_match"
    assert before == {p.name: file_hash(p) for p in (root / "catalog").glob("*.json")}


def test_required_action_is_phase_specific_not_source_wide(search_library):
    root = search_library
    result = query_index(root, query())
    assert result["candidates"] and all(c["phase_index"] == 2 for c in result["candidates"])
    assert all(not c["requires_visual_review"] for c in result["candidates"])
    filling = query_index(root, query(query="放入果肉", must=["放果肉"]))
    assert filling["candidates"] and all(c["phase_index"] == 1 for c in filling["candidates"])
    assert all("包拢" in c["observation"] for c in filling["candidates"])


def test_result_limits_pagination_and_cursor_binding(search_library):
    root = search_library
    q = query(limit=12, max_output_chars=2600)
    first = query_index(root, q)
    assert 0 < len(first["candidates"]) < 12
    assert len(json.dumps(first, ensure_ascii=False, indent=2)) <= 2600
    second = query_index(root, {**q, "cursor": first["next_cursor"]})
    assert not {c["candidate_id"] for c in first["candidates"]} & {
        c["candidate_id"] for c in second["candidates"]
    }
    with pytest.raises(Blocked) as error:
        query_index(root, {**q, "query": "包拢", "cursor": first["next_cursor"]})
    assert error.value.code == "SEARCH_CURSOR_STALE"


def test_reservations_are_removed_and_old_cursor_is_invalid(search_library):
    root = search_library
    q = query(limit=1)
    result = query_index(root, q)
    sid = result["candidates"][0]["source_id"]
    atomic_write(
        root / "ledger/montage_v3/test.json",
        {
            "state": "reserved",
            "shots": [{"source_id": sid, "source_in_frame": 0, "source_out_frame": 600}],
        },
    )
    after = query_index(root, q)
    assert all(c["source_id"] != sid for c in after["candidates"])
    with pytest.raises(Blocked) as error:
        query_index(root, {**q, "cursor": result["next_cursor"]})
    assert error.value.code == "SEARCH_CURSOR_STALE"


def test_changed_facts_require_rebuild_and_unchanged_documents_are_reused(search_library):
    root = search_library
    data = load(root / "catalog/material_facts_v1.json")
    row = data["materials"][0]
    row["phases"][0]["observation"] = "新版取皮动作"
    row["content_sha256"] = digest({k: v for k, v in row.items() if k != "content_sha256"})
    atomic_write(root / "catalog/material_facts_v1.json", data)
    with pytest.raises(Blocked) as error:
        query_index(root, query())
    assert error.value.code == "SEARCH_INDEX_STALE"
    result = build_index(root)
    assert result["reused_documents"] == 33 and result["new_documents"] == 3


def test_negated_action_is_not_a_positive_match(search_library):
    root = search_library
    data = load(root / "catalog/material_facts_v1.json")
    for row in data["materials"]:
        row["phases"][2]["observation"] = "未见切开，没有切面，也无法判断顾客身份"
        row["content_sha256"] = digest({k: v for k, v in row.items() if k != "content_sha256"})
    atomic_write(root / "catalog/material_facts_v1.json", data)
    build_index(root)
    assert query_index(root, query(query="切开", must=["切开"]))["status"] == "no_match"


def test_show_only_returns_local_verified_source_evidence(search_library):
    root = search_library
    path, result = save_result(root)
    detail = show_candidate(root, path, result["candidates"][0]["candidate_id"])
    assert 1 <= len(detail["frames"]) <= 3
    assert all(Path(f["path"]).is_relative_to(root) for f in detail["frames"])
    assert detail["precision"] == "sampled_observation_not_edit_cut"
    Path(detail["frames"][0]["path"]).write_bytes(b"changed")
    with pytest.raises(Blocked) as error:
        show_candidate(root, path, result["candidates"][0]["candidate_id"])
    assert error.value.code == "SEARCH_EVIDENCE_CHANGED"


def test_result_tampering_is_not_accepted(search_library):
    root = search_library
    path, result = save_result(root)
    result["candidates"][0]["source_id"] = "other"
    atomic_write(path, result)
    with pytest.raises(Blocked) as error:
        show_candidate(root, path, result["candidates"][0]["candidate_id"])
    assert error.value.code == "SEARCH_RESULT_HASH"
    result["content_sha256"] = digest({k: v for k, v in result.items() if k != "content_sha256"})
    atomic_write(path, result)
    with pytest.raises(Blocked) as error:
        show_candidate(root, path, result["candidates"][0]["candidate_id"])
    assert error.value.code == "SEARCH_CANDIDATE_BINDING"


def test_plan_validation_never_creates_approval_or_reservation(search_library):
    root = search_library
    plan = make_plan(root)
    clips = load(root / "catalog/clips.json")
    report = validate_director_plan(root, plan)
    assert report["status"] == "validated_draft_not_approved"
    assert not report["human_approval_created"] and not report["reservations_created"]
    assert report["visual_review"] == "agent_declared_not_machine_proof"
    assert load(root / "catalog/clips.json") == clips
    assert not list((root / "ledger/montage_v3").glob("*.json"))


def test_valid_ingest_facts_can_be_reused_without_repeat_visual_review(search_library):
    root = search_library
    plan = make_plan(root)
    shot = plan["tasks"][0]["shots"][0]
    result = load(Path(shot["search_result_path"]))
    candidate = result["candidates"][0]
    shot.pop("visual_review")
    shot["selection_basis"] = {
        "type": "indexed_facts",
        "document_id": candidate["document_id"],
        "facts_sha256": candidate["facts_sha256"],
    }
    report = validate_director_plan(root, plan)
    assert report["indexed_facts_shots"] == 1 and report["supplementally_reviewed_shots"] == 0
    assert report["visual_review"] == "ingest_facts_reused"
    assert not report["human_approval_created"]
    shot["selection_basis"]["facts_sha256"] = "0" * 64
    with pytest.raises(Blocked) as error:
        validate_director_plan(root, plan)
    assert error.value.code == "DIRECTOR_FACTS_STALE"


def test_plan_rejects_changed_script_wrong_evidence_and_overlap(search_library):
    root = search_library
    plan = make_plan(root)
    bad = copy.deepcopy(plan)
    bad["tasks"][0]["shots"][0]["visual_review"]["evidence"][0]["path"] = str(
        root / "analysis/s_11_9.jpg"
    )
    bad["tasks"][0]["shots"][0]["visual_review"]["evidence"][0]["sha256"] = file_hash(
        root / "analysis/s_11_9.jpg"
    )
    with pytest.raises(Blocked) as error:
        validate_director_plan(root, bad)
    assert error.value.code == "DIRECTOR_EVIDENCE_SOURCE"
    bad = copy.deepcopy(plan)
    second = copy.deepcopy(bad["tasks"][0]["shots"][0])
    second["timeline_start_frame"] = 180
    bad["tasks"][0]["shots"].append(second)
    bad["tasks"][0]["total_frames"] = 360
    with pytest.raises(Blocked) as error:
        validate_director_plan(root, bad)
    assert error.value.code == "DIRECTOR_OVERLAP"
    Path(plan["script_path"]).write_text("changed script")
    with pytest.raises(Blocked) as error:
        validate_director_plan(root, plan)
    assert error.value.code == "DIRECTOR_SCRIPT_STALE"


def test_thousand_source_metadata_library_returns_a_bounded_page(search_library):
    root = search_library
    template_source = load(root / "catalog/sources.json")[0]
    template_clip = load(root / "catalog/clips.json")[0]
    template_facts = load(root / "catalog/material_facts_v1.json")["materials"][0]
    history = load(root / "catalog/product_identity_confirmations.json")
    record = history["records"][0]
    template_item = record["items"][0]
    sources, clips, facts, items = [], [], [], []
    for n in range(1000):
        sid, cid = f"synthetic_{n:04d}", f"clip_{n:04d}"
        source = copy.deepcopy(template_source)
        source.update(source_id=sid, file_sha256=digest(sid))
        clip = copy.deepcopy(template_clip)
        clip.update(clip_id=cid, source_id=sid)
        clip["content_sha256"] = content_hash(clip)
        material = copy.deepcopy(template_facts)
        material["source"] = source
        material["parent"] = {"clip_id": cid, "content_sha256": clip["content_sha256"]}
        item = {**template_item, "source_id": sid, "source_sha256": source["file_sha256"]}
        sources.append(source)
        clips.append(clip)
        facts.append(material)
        items.append(item)
    record["items"] = items
    record["plan_sha256"] = digest(
        {
            k: v
            for k, v in record.items()
            if k not in {"plan_sha256", "verified_at", "feishu_readback_count"}
        }
    )
    for material in facts:
        material["product_confirmation"]["plan_sha256"] = record["plan_sha256"]
        material["content_sha256"] = digest(
            {k: v for k, v in material.items() if k != "content_sha256"}
        )
    atomic_write(root / "catalog/sources.json", sources)
    atomic_write(root / "catalog/clips.json", clips)
    atomic_write(root / "catalog/material_facts_v1.json", {"materials": facts})
    atomic_write(root / "catalog/product_identity_confirmations.json", history)
    policy = load(root / "catalog/material_workflow_policy.json")
    policy["brand_scopes"]["brand"]["sources"] = {s["source_id"]: s["file_sha256"] for s in sources}
    atomic_write(root / "catalog/material_workflow_policy.json", policy)
    built = build_index(root)
    assert built["documents"] == 3000
    result = query_index(root, query(limit=6, max_per_source=1, max_output_chars=6000))
    assert result["eligible_sources"] == 1000
    assert len(result["candidates"]) == 6
    assert len(json.dumps(result, ensure_ascii=False, indent=2)) <= 6000
    assert result["next_cursor"] and result["provider_calls"] == 0
