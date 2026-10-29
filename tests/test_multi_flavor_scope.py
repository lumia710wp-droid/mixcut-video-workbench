"""Multi-flavor scopes across real facts/search/plan code, isolated Base and media."""

import copy
import pytest
from test_material_search import search_library, facts_library, legacy_library  # noqa: F401
from test_workbench import app, brief  # noqa: F401
from mixcut.core import Blocked, atomic_write, digest, file_hash, load
from mixcut.contracts import content_hash
from mixcut.fact_planning import planning_segments
from mixcut.material_search import (
    build_index,
    query_index,
    show_candidate,
    validate_director_plan,
    _settings,
)
from mixcut.product_scope import available_scopes, project_scope, scope_values
from mixcut.workbench import Workbench
from mixcut import script_stage, director_stage, director_execution

ALL = {"brand_id": "brand", "goods_id": "全部口味", "goods_scope": "brand_all", "goods_ids": []}
SELECT = {
    "brand_id": "brand",
    "goods_id": "多口味混剪",
    "goods_scope": "selected",
    "goods_ids": ["durian", "strawberry"],
}


@pytest.fixture
def multi_library(search_library):  # noqa: F811
    root = search_library
    clips = load(root / "catalog/clips.json")
    facts = load(root / "catalog/material_facts_v1.json")
    records = load(root / "catalog/product_identity_confirmations.json")
    item = records["records"][0]["items"][0]
    products = [
        {"goods_id": "strawberry", "start_sec": 0, "end_sec": 10},
        {"goods_id": "durian", "start_sec": 10, "end_sec": 20},
    ]
    item.update(goods_id="multi", products=products)
    clips[0]["goods_id"] = "multi"
    clips[0]["content_sha256"] = content_hash(clips[0])
    facts["materials"][0]["parent"]["content_sha256"] = clips[0]["content_sha256"]
    facts["materials"][0]["identity"].update(confirmed_goods="multi", confirmed_products=products)
    record = records["records"][0]
    record["plan_sha256"] = digest(
        {
            k: v
            for k, v in record.items()
            if k not in {"plan_sha256", "verified_at", "feishu_readback_count"}
        }
    )
    for material in facts["materials"]:
        material["product_confirmation"]["plan_sha256"] = record["plan_sha256"]
        material["content_sha256"] = digest(
            {k: v for k, v in material.items() if k != "content_sha256"}
        )
    atomic_write(root / "catalog/clips.json", clips)
    atomic_write(root / "catalog/material_facts_v1.json", facts)
    atomic_write(root / "catalog/product_identity_confirmations.json", records)
    build_index(root)
    return root


def search(scope, **changes):
    return {
        **scope,
        "query": "收口",
        "must": ["收口"],
        "min_duration_sec": 0.5,
        "limit": 12,
        "max_per_source": 4,
        **changes,
    }


def test_options_include_confirmed_multi_source_flavors_not_parent_labels(multi_library):
    assert available_scopes(multi_library) == [
        {"brand": "brand", "goods": ["durian", "mango", "strawberry"]}
    ]
    assert all(
        c["goods_id"] not in {"durian", "strawberry"}
        for c in load(multi_library / "catalog/clips.json")
    )


def test_multi_scope_search_labels_ranges_and_detail_without_crossing_products(multi_library):
    root = multi_library
    result = query_index(root, search(SELECT))
    assert {c["goods_id"] for c in result["candidates"]} == {"strawberry", "durian"}
    path = root / "reports/multi.json"
    atomic_write(path, result)
    for c in result["candidates"]:
        assert c["end_frame"] <= 300 if c["goods_id"] == "strawberry" else c["start_frame"] >= 300
        assert show_candidate(root, path, c["candidate_id"])["candidate"] == c
    all_result = query_index(root, search(ALL))
    assert all_result["eligible_sources"] == 12 and result["eligible_sources"] == 1
    with pytest.raises(Blocked, match="尚未绑定素材库范围"):
        query_index(root, search({**ALL, "brand_id": "other"}))


def test_all_flavors_does_not_include_unconfirmed_materials(multi_library):
    root = multi_library
    atomic_write(root / "catalog/product_identity_confirmations.json", {"records": []})
    assert not planning_segments(root, ALL)
    assert available_scopes(root) == []


def test_legacy_scope_results_and_segment_hashes_remain_unchanged(multi_library):
    root = multi_library
    old = {"brand_id": "brand", "goods_id": "mango"}
    before = planning_segments(root, old)
    planning_segments(root, ALL)
    query_index(root, search(SELECT))
    assert planning_segments(root, old) == before
    result = query_index(root, search(old))
    assert result["scope"] == old and result["eligible_sources"] == 11
    p = root / "reports/legacy.json"
    atomic_write(p, result)
    assert (
        show_candidate(root, p, result["candidates"][0]["candidate_id"])["candidate"]
        == result["candidates"][0]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"goods_scope": "selected", "goods_ids": []},
        {"goods_scope": "selected", "goods_ids": ["unknown"]},
        {"goods_scope": "selected", "goods_ids": ["mango", "mango"]},
        {"goods_scope": "brand_all", "goods_ids": ["mango"]},
        {"goods_scope": [], "goods_ids": []},
        {"brand_id": "other"},
    ],
)
def test_invalid_or_unavailable_scopes_are_rejected(multi_library, changes):
    with pytest.raises(Blocked):
        project_scope(multi_library, {**ALL, **changes})


@pytest.mark.parametrize("scope", [ALL, SELECT])
def test_create_persists_scope_and_retry_cannot_expand_it(multi_library, app, scope):  # noqa: F811
    root = multi_library
    original, remote = app
    atomic_write(root / "config/feishu_mapping.local.json", original.mapping)
    atomic_write(root / "config/executable_montage_v3.json", {"enabled": True})
    bench = Workbench(root, call=remote)
    body = brief(**scope)
    first = bench.create_project(body)
    assert bench.create_project(body)["reused"] and remote.creates == 1
    policy = load(root / f"config/script_project_policies/{first['group_id']}.json")
    assert scope_values(policy) == scope
    changed = SELECT if scope == ALL else ALL
    with pytest.raises(Blocked, match="同一提交标识"):
        bench.create_project({**body, **changed})
    assert remote.creates == 1 and not (root / "jobs/codex_director_v1").exists()


def test_plan_and_packet_bind_exact_multi_scope_and_mixed_product_windows(
    multi_library, monkeypatch
):
    root = multi_library
    result = query_index(root, search(SELECT))
    path = root / "reports/selected.json"
    atomic_write(path, result)
    script = {"task_id": "video", "content_sha256": "text-version", "narration": "original"}
    script_path = root / "reports/script.json"
    atomic_write(script_path, script)
    candidates = [
        next(c for c in result["candidates"] if c["goods_id"] == g) for g in SELECT["goods_ids"]
    ]
    shots = []
    for i, c in enumerate(candidates):
        shots.append(
            {
                "candidate_id": c["candidate_id"],
                "search_result_path": str(path),
                "need_id": f"N{i}",
                "source_in_frame": c["start_frame"],
                "source_out_frame": c["start_frame"] + 30,
                "timeline_start_frame": i * 30,
                "duration_frames": 30,
                "speed": 1,
                "source_audio_gain": 0,
                "transition_out": {"type": "hard_cut", "frames": 0},
                "rationale": "isolated mixed-flavor test",
                "selection_basis": {
                    "type": "indexed_facts",
                    "document_id": c["document_id"],
                    "facts_sha256": c["facts_sha256"],
                },
            }
        )
    plan = {
        "entity": "codex_director_plan",
        "version": 1,
        **SELECT,
        "script_path": str(script_path),
        "script_sha256": file_hash(script_path),
        "tasks": [{"task_id": "video", "total_frames": 60, "shots": shots}],
    }
    assert validate_director_plan(root, plan, check_media=False)["shots"] == 2
    monkeypatch.setattr("mixcut.executable.verify_media_available", lambda *a: None)
    monkeypatch.setattr(
        director_execution,
        "media_preflight",
        lambda *a: {"items": [{"recommended_transform": None, "letterbox_detected": False}] * 2},
    )
    atomic_write(root / "config/config.local.json", {})
    plan_path = root / "reports/plan.json"
    atomic_write(plan_path, plan)
    packet = director_stage.build_packet(root, plan_path, "video")
    assert scope_values(packet) == SELECT and packet["text_sha256"] == "text-version"
    assert {s["goods_id"] for s in packet["shots"]} == set(SELECT["goods_ids"])
    atomic_write(root / "config/script_project_policies/selected_group.json", SELECT)
    monkeypatch.setattr(director_stage, "current_text", lambda *a: {**script, "target_seconds": 2})
    director_stage.validate_packet(root, "selected_group", packet)
    mislabeled = copy.deepcopy(packet)
    mislabeled["shots"][0]["goods_id"] = "mango"
    with pytest.raises(Blocked, match="商品范围"):
        director_stage.validate_packet(root, "selected_group", mislabeled)
    changed = copy.deepcopy(plan)
    changed.update(ALL)
    with pytest.raises(Blocked, match="商品/品牌不符"):
        validate_director_plan(root, changed, check_media=False)


def test_script_model_receives_scope_without_catalog_dump(multi_library, monkeypatch):
    root = multi_library
    group = "multi_group"
    atomic_write(
        root / f"config/script_project_policies/{group}.json",
        {**SELECT, "workflow_mode": "script_first_v1"},
    )
    rows = [
        {
            "fields": {
                "task_id": "v",
                "target_duration_sec": 10,
                "request_group_id": group,
                "creative_keywords": "mixed flavors",
            }
        }
    ]
    requests = []

    def copy_fn(_root, request, _path):
        requests.append(request)
        return {
            "scripts": [
                {
                    "task_id": "v",
                    "title": "mix",
                    "narration": "果" * 50,
                    "headline": ["a", "b", "c", "d"],
                }
            ]
        }

    record = script_stage.generate(root, group, rows, copy_fn=copy_fn)
    assert scope_values(requests[0]["settings"]) == SELECT
    assert "catalog" not in requests[0] and "materials" not in requests[0]
    assert record["scripts"][0]["material_scope"] == SELECT
    assert record["scripts"][0]["review_status"] == "pending"
    wrong = {**SELECT, "goods_ids": ["mango", "strawberry"]}
    with pytest.raises(Blocked):
        _settings(root, {**wrong, "group_id": group})
