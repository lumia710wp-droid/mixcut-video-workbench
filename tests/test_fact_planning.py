"""Isolated factual-input workflow; fake providers do not prove real generation."""

import pytest
from test_montage_v3 import setup_policy, fake_copy, fake_tts, request_rows
from test_montage_v3 import library as legacy_library  # noqa: F401 - imported pytest fixture
from mixcut.core import atomic_write, load, digest, Blocked
from mixcut.contracts import content_hash
from mixcut.fact_planning import planning_segments
from mixcut.executable import generate_batch, preflight, validate_current, review_script


@pytest.fixture
def facts_library(legacy_library):  # noqa: F811 - pytest injects the imported fixture
    root = legacy_library
    sources = load(root / "catalog/sources.json")
    clips = load(root / "catalog/clips.json")
    items, materials = [], []
    for source, clip in zip(sources, clips):
        clip.update(
            brand_id=None,
            review_status="pending",
            approval=None,
            revision=1,
            source_type="store_shot",
            description="observed source",
            tags={"content": [], "action": [], "shot_size": [], "risk": []},
        )
        clip["content_sha256"] = content_hash(clip)
        products = [{"goods_id": "mango", "start_sec": 0, "end_sec": 20}]
        item = {
            "source_id": source["source_id"],
            "source_sha256": source["file_sha256"],
            "goods_id": "mango",
            "products": products,
            "previous_facts_sha256": "a" * 64,
        }
        items.append(item)
        materials.append(
            {
                "source": source,
                "parent": {"clip_id": clip["clip_id"], "content_sha256": clip["content_sha256"]},
                "summary": "test-only observed actions",
                "identity": {"confirmed_goods": "mango", "confirmed_products": products},
                "coverage": {"actually_viewed_sheet_ids": ["test_sheet"]},
                "evidence": {"sheets": [{"id": "test_sheet"}]},
                "phases": [
                    {"start_sec": 0, "end_sec": 2, "observation": "取皮", "quality": "清楚"},
                    {"start_sec": 2, "end_sec": 8, "observation": "放果肉包拢", "quality": "清楚"},
                    {
                        "start_sec": 8,
                        "end_sec": 20,
                        "observation": "收口整形展示",
                        "quality": "清楚",
                    },
                ],
                "events": [],
                "production_approved": False,
            }
        )
    plan = {
        "decision_channel": "codex_chat",
        "decision_actor": "human_user_in_current_chat",
        "items": items,
        "test_only": True,
    }
    plan["plan_sha256"] = digest(plan)
    plan.update(verified_at="fixture date", feishu_readback_count=12)
    for row in materials:
        row["product_confirmation"] = {
            "plan_sha256": plan["plan_sha256"],
            "previous_facts_sha256": "a" * 64,
        }
        row["content_sha256"] = digest(row)
    atomic_write(root / "catalog/clips.json", clips)
    atomic_write(root / "catalog/product_identity_confirmations.json", {"records": [plan]})
    atomic_write(root / "catalog/material_facts_v1.json", {"materials": materials})
    atomic_write(
        root / "catalog/material_workflow_policy.json",
        {
            "planning_input": "facts_v1",
            "brand_scopes": {
                "brand": {
                    "root_ids": ["originals"],
                    "sources": {s["source_id"]: s["file_sha256"] for s in sources},
                }
            },
        },
    )
    setup_policy(root)
    return root


def settings(root):
    return load(root / "config/script_project_policies/group.json")


def test_facts_plan_without_any_individual_cut_or_material_approval(facts_library):
    root = facts_library
    before_clips = load(root / "catalog/clips.json")
    before_segments = load(root / "catalog/segments_v3.json")
    candidates = planning_segments(root, settings(root))
    assert len({s["source_id"] for s in candidates}) == 12
    assert all(s["approval"] is None and s["review_status"] == "planning_input" for s in candidates)
    result = preflight(root, settings(root), request_rows())
    assert result["diversity"]["cross_video_overlap_frames"] == 0
    assert all(s["unique_source_duration_ratio"] >= 0.7 for s in result["diversity"]["scripts"])
    assert load(root / "catalog/clips.json") == before_clips
    assert load(root / "catalog/segments_v3.json") == before_segments


def test_identity_unknown_or_other_product_is_not_inferred(facts_library):
    root = facts_library
    assert not planning_segments(root, {**settings(root), "goods_id": "strawberry"})
    atomic_write(root / "catalog/product_identity_confirmations.json", {"records": []})
    assert not planning_segments(root, settings(root))
    with pytest.raises(Blocked) as error:
        preflight(root, settings(root), request_rows())
    assert error.value.code == "V3_NO_CONFIRMED_FACTS"


def test_live_feishu_content_changes_and_rejections_block_without_individual_approval(
    facts_library,
):
    from mixcut.workbench import Workbench

    root = facts_library
    clips = load(root / "catalog/clips.json")
    atomic_write(
        root / "config/taxonomy.json",
        {
            "axes": {axis: [] for axis in ["content", "action", "shot_size", "risk"]},
            "blocking_risks": [],
        },
    )
    live = [
        {
            "fields": {
                **c,
                "content_tags": [],
                "action_tags": [],
                "shot_size_tags": [],
                "risk_tags": [],
            }
        }
        for c in clips
    ]
    # No network client and no approval call: exercise the production gate directly.
    bench = object.__new__(Workbench)
    bench.root = root
    bench.rows = lambda *args: live
    bench.v3_material_gate(settings(root))
    live[0]["fields"]["goods_id"] = "changed remotely"
    with pytest.raises(Blocked) as error:
        bench.v3_material_gate(settings(root))
    assert error.value.code == "V3_MATERIAL_LIVE_STALE"
    live[0]["fields"]["goods_id"] = clips[0]["goods_id"]
    live[0]["fields"]["review_status"] = ["rejected"]
    with pytest.raises(Blocked) as error:
        bench.v3_material_gate(settings(root))
    assert error.value.code == "V3_MATERIAL_LIVE_STALE"


def test_scope_foreign_brand_changed_parent_and_source_are_excluded(facts_library):
    root = facts_library
    clips = load(root / "catalog/clips.json")
    clips[0]["brand_id"] = "other brand"
    clips[0]["content_sha256"] = content_hash(clips[0])
    atomic_write(root / "catalog/clips.json", clips)
    assert len({s["source_id"] for s in planning_segments(root, settings(root))}) == 11
    sources = load(root / "catalog/sources.json")
    sources[1]["file_sha256"] = "b" * 64
    atomic_write(root / "catalog/sources.json", sources)
    assert len({s["source_id"] for s in planning_segments(root, settings(root))}) == 10
    policy = load(root / "catalog/material_workflow_policy.json")
    policy["brand_scopes"] = {}
    atomic_write(root / "catalog/material_workflow_policy.json", policy)
    with pytest.raises(Blocked) as error:
        planning_segments(root, settings(root))
    assert error.value.code == "V3_LIBRARY_SCOPE"


def test_full_source_is_plannable_across_observation_stage_boundaries(facts_library):
    candidates = planning_segments(facts_library, settings(facts_library))
    assert any(s["source_in_frame"] == 0 and s["source_out_frame"] == 600 for s in candidates)
    assert any(s["source_in_frame"] == 60 and s["source_out_frame"] == 240 for s in candidates)


def test_known_duplicate_sources_do_not_count_as_independent(facts_library):
    root = facts_library
    policy = load(root / "catalog/material_workflow_policy.json")
    policy["duplicate_source_groups"] = [["s_0", "s_1"]]
    atomic_write(root / "catalog/material_workflow_policy.json", policy)
    candidates = planning_segments(root, settings(root))
    assert "s_0" in {s["source_id"] for s in candidates}
    assert "s_1" not in {s["source_id"] for s in candidates}


def test_complete_script_approval_is_required_and_facts_edits_invalidate_it(facts_library):
    root = facts_library
    before = load(root / "catalog/clips.json")
    batch = generate_batch(root, {}, "group", request_rows(), copy_fn=fake_copy, tts_fn=fake_tts)
    script = batch["scripts"][0]
    with pytest.raises(Blocked) as error:
        validate_current(root, script, approved=True)
    assert error.value.code == "V3_SCRIPT_UNAPPROVED"
    reviewed = review_script(
        root,
        "group",
        script["task_id"],
        script["content_sha256"],
        "approve",
        {"id": "ou_test_human"},
        request_rows(),
    )
    validate_current(root, reviewed["scripts"][0], approved=True)
    assert load(root / "catalog/clips.json") == before
    facts = load(root / "catalog/material_facts_v1.json")
    sid = script["shots"][0]["source_id"]
    row = next(x for x in facts["materials"] if x["source"]["source_id"] == sid)
    row["summary"] = "changed observation"
    row["content_sha256"] = digest({k: v for k, v in row.items() if k != "content_sha256"})
    atomic_write(root / "catalog/material_facts_v1.json", facts)
    with pytest.raises(Blocked) as error:
        validate_current(root, reviewed["scripts"][0], approved=True)
    assert error.value.code == "V3_SEGMENT_STALE"
    with pytest.raises(Blocked):
        generate_batch(root, {}, "group", request_rows(), copy_fn=fake_copy, tts_fn=fake_tts)


def test_missing_media_blocks_before_any_provider_call(facts_library):
    root = facts_library
    for p in (root / "originals").iterdir():
        p.unlink()

    def forbidden(*args):
        raise AssertionError("provider called with unavailable media")

    with pytest.raises(Blocked) as error:
        generate_batch(root, {}, "group", request_rows(), copy_fn=forbidden, tts_fn=forbidden)
    assert error.value.code == "V3_MEDIA_UNAVAILABLE"


def test_multi_product_identity_never_crosses_another_products_stage(facts_library):
    root = facts_library
    facts = load(root / "catalog/material_facts_v1.json")
    history = load(root / "catalog/product_identity_confirmations.json")
    clips = load(root / "catalog/clips.json")
    row = facts["materials"][0]
    products = [
        {"goods_id": "mango", "start_sec": 0, "end_sec": 2},
        {"goods_id": "berry", "start_sec": 2, "end_sec": 8},
        {"goods_id": "mango", "start_sec": 8, "end_sec": 20},
    ]
    clips[0]["goods_id"] = "multi"
    clips[0]["content_sha256"] = content_hash(clips[0])
    row["parent"]["content_sha256"] = clips[0]["content_sha256"]
    row["identity"]["confirmed_products"] = products
    record = history["records"][0]
    record["items"][0].update(goods_id="multi", products=products)
    record["plan_sha256"] = digest(
        {
            k: v
            for k, v in record.items()
            if k not in {"plan_sha256", "verified_at", "feishu_readback_count"}
        }
    )
    for f in facts["materials"]:
        f["product_confirmation"]["plan_sha256"] = record["plan_sha256"]
        f["content_sha256"] = digest({k: v for k, v in f.items() if k != "content_sha256"})
    atomic_write(root / "catalog/clips.json", clips)
    atomic_write(root / "catalog/material_facts_v1.json", facts)
    atomic_write(root / "catalog/product_identity_confirmations.json", history)
    windows = [s for s in planning_segments(root, settings(root)) if s["source_id"] == "s_0"]
    assert windows and all(
        s["source_out_frame"] <= 60 or s["source_in_frame"] >= 240 for s in windows
    )
