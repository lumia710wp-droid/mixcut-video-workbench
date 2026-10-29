import copy
from pathlib import Path

import pytest

from mixcut.contracts import content_hash
from mixcut.core import Blocked, atomic_write, load
from mixcut.material_index import index_materials


def approved_outline(task_id: str = "TASK_001") -> dict:
    outline = {
        "schema_version": "1.0.0",
        "entity": "script_outline",
        "task_id": task_id,
        "request_sha256": "request-hash",
        "title": "测试脚本",
        "shots": [
            {
                "shot_id": "S1",
                "narration": "把水果放入外皮。",
                "caption": "水果入馅",
                "visual_intent": "果肉放入外皮的手工近景",
                "semantic_queries": ["水果入馅", "双手包制"],
                "target_frames": 120,
            },
            {
                "shot_id": "S2",
                "narration": "顾客接过成品。",
                "caption": "交给顾客",
                "visual_intent": "顾客接过包装离店",
                "semantic_queries": ["顾客手提包装"],
                "target_frames": 120,
            },
            {
                "shot_id": "S3",
                "narration": "放进纸托。",
                "caption": "装托",
                "visual_intent": "成品装入纸托",
                "semantic_queries": ["装纸托"],
                "target_frames": 120,
            },
            {
                "shot_id": "S4",
                "narration": "门店日常。",
                "caption": "门店日常",
                "visual_intent": "门店操作台日常",
                "semantic_queries": ["门店现场"],
                "target_frames": 120,
            },
        ],
        "total_frames": 480,
        "revision": 1,
        "provider": "deepseek",
        "model": "deepseek-flash",
        "review_status": "approved",
        "approval": None,
        "created_at": "2026-10-15T00:00:00+00:00",
    }
    outline["content_sha256"] = content_hash(outline)
    outline["approval"] = {
        "actor_id": "human",
        "channel": "feishu",
        "approved_at": "2026-10-15T00:00:01+00:00",
        "approved_content_sha256": outline["content_sha256"],
    }
    return outline


def setup_workspace(tmp_path: Path) -> None:
    outline = approved_outline()
    request = {
        "schema_version": "1.0.0",
        "entity": "script_request",
        "task_id": "TASK_001",
        "creative_keywords": "测试",
        "seed_sentences": "测试",
        "target_duration_sec": 16,
        "target_duration_frames": 480,
        "input_policy": "human_creative_input_only_no_material_context",
        "request_sha256": "request-hash",
    }
    atomic_write(tmp_path / "requests/TASK_001/request-hash.json", request)
    atomic_write(tmp_path / "jobs/TASK_001/r1/script_outline.json", outline)
    clip = {
        "clip_id": "clip-fruit",
        "revision": 1,
        "content_sha256": "clip-hash",
        "goods_id": "芒果",
        "usable_start_sec": 4,
        "usable_end_sec": 11,
        "review_status": "pending",
        "rights_confirmed": False,
    }
    atomic_write(tmp_path / "catalog/clips.json", [clip])
    proposals = {
        "records": [
            {
                "clip_id": "clip-fruit",
                "catalog_revision_at_analysis": 1,
                "catalog_content_sha256_at_analysis": "clip-hash",
                "analysis_sha256": "analysis-hash",
                "proposal": {"description": "双手把黄色果肉放进白色包皮并装入纸托"},
                "semantic_tags": {
                    "group": "handmade_process",
                    "actions": ["filling", "closing", "placing_in_liner"],
                },
                "observations": {
                    "title": "水果包制",
                    "description": "门店操作台上的制作过程",
                    "shot": "closeup",
                },
                "candidate_windows": [{"start_sec": 4, "end_sec": 11}],
            }
        ]
    }
    atomic_write(tmp_path / "proposals.json", proposals)


def test_advisory_index_requires_material_review_and_reports_gaps(tmp_path):
    setup_workspace(tmp_path)
    result = index_materials(tmp_path, "TASK_001", tmp_path / "proposals.json")
    assert result["production_ready"] is False
    assert result["shots_with_candidates"] == 3
    assert result["material_gap_count"] == 1
    saved = load(Path(result["path"]))
    assert saved["shots"][0]["candidates"][0]["clip_id"] == "clip-fruit"
    assert saved["shots"][0]["candidates"][0]["production_eligible"] is False
    assert "material_not_approved" in saved["shots"][0]["candidates"][0]["blocking_reasons"]
    assert saved["shots"][1]["status"] == "material_gap"
    events = [load(path) for path in (tmp_path / "ledger/outbox").glob("*.json")]
    assert events[0]["fields"]["stage"] == "waiting_material_review"


def test_index_rejects_unapproved_script(tmp_path):
    setup_workspace(tmp_path)
    path = tmp_path / "jobs/TASK_001/r1/script_outline.json"
    outline = load(path)
    outline.update(review_status="pending", approval=None)
    outline["content_sha256"] = content_hash(outline)
    atomic_write(path, outline)
    with pytest.raises(Blocked) as exc:
        index_materials(tmp_path, "TASK_001", tmp_path / "proposals.json")
    assert exc.value.code == "UNAPPROVED"


def test_stale_visual_analysis_is_excluded(tmp_path):
    setup_workspace(tmp_path)
    path = tmp_path / "catalog/clips.json"
    clips = load(path)
    clips[0] = copy.deepcopy(clips[0])
    clips[0]["revision"] = 2
    atomic_write(path, clips)
    result = index_materials(tmp_path, "TASK_001", tmp_path / "proposals.json")
    assert result["shots_with_candidates"] == 0
    assert result["stale_analysis_clip_ids"] == ["clip-fruit"]


def test_current_confirmation_makes_candidates_eligible_but_keeps_gaps(tmp_path):
    setup_workspace(tmp_path)
    clips = load(tmp_path / "catalog/clips.json")
    clips[0].update(review_status="approved", rights_confirmed=True, tags={"risk": []})
    atomic_write(tmp_path / "catalog/clips.json", clips)
    atomic_write(
        tmp_path / "catalog/material_confirmations.json",
        {
            "records": [
                {
                    "clip_id": "clip-fruit",
                    "analysis_sha256": "analysis-hash",
                    "confirmed_catalog_revision": 1,
                    "confirmed_catalog_content_sha256": "clip-hash",
                }
            ]
        },
    )
    result = index_materials(tmp_path, "TASK_001", tmp_path / "proposals.json")
    assert result["status"] == "approved_materials_with_gaps"
    assert result["eligible_shot_count"] == 3
    assert result["production_ready"] is False
    saved = load(Path(result["path"]))
    assert saved["shots"][0]["candidates"][0]["production_eligible"] is True
    assert saved["shots"][0]["candidates"][0]["blocking_reasons"] == []
    events = [load(path) for path in (tmp_path / "ledger/outbox").glob("*.json")]
    assert events[0]["fields"]["stage"] == "waiting_material_gap"


def test_material_constrained_index_preserves_bound_clip(tmp_path):
    setup_workspace(tmp_path)
    clips = load(tmp_path / "catalog/clips.json")
    clips[0].update(review_status="approved", rights_confirmed=True, tags={"risk": []})
    atomic_write(tmp_path / "catalog/clips.json", clips)
    atomic_write(
        tmp_path / "catalog/material_confirmations.json",
        {
            "records": [
                {
                    "clip_id": "clip-fruit",
                    "analysis_sha256": "analysis-hash",
                    "confirmed_catalog_revision": 1,
                    "confirmed_catalog_content_sha256": "clip-hash",
                }
            ]
        },
    )
    request_path = tmp_path / "requests/TASK_001/request-hash.json"
    request = load(request_path)
    outline = load(tmp_path / "jobs/TASK_001/r1/script_outline.json")
    request.update(
        input_policy="approved_material_context_constrained",
        material_context_sha256="bound-context",
        material_slots=[
            {
                "slot_id": shot["shot_id"],
                "clip_id": "clip-fruit",
                "clip_revision": 1,
                "approved_content_sha256": "clip-hash",
                "analysis_sha256": "analysis-hash",
                "goods_id": "芒果",
                "usable_start_sec": 4,
                "usable_end_sec": 11,
                "visual_intent": shot["visual_intent"],
                "semantic_queries": shot["semantic_queries"],
                "target_frames": shot["target_frames"],
                "max_narration_chars": 100,
                "max_caption_chars": 100,
            }
            for shot in outline["shots"]
        ],
    )
    atomic_write(request_path, request)
    result = index_materials(tmp_path, "TASK_001", tmp_path / "proposals.json")
    saved = load(Path(result["path"]))
    assert saved["mode"] == "deterministic_bound_ranking_over_approved_material_context"
    assert saved["material_context_sha256"] == "bound-context"
    assert result["production_ready"] is True
    assert result["shots_with_candidates"] == 4
    assert result["material_gap_count"] == 0
    assert saved["shots"][0]["candidates"][0]["clip_id"] == "clip-fruit"
    assert saved["shots"][0]["candidates"][0]["material_binding"] == ("approved_material_context")
    assert saved["shots"][1]["candidates"][0]["score"] == 0
    assert saved["shots"][1]["candidates"][0]["start_sec"] == 4
    assert saved["shots"][1]["candidates"][0]["end_sec"] == 11


def test_material_constrained_index_rejects_changed_binding(tmp_path):
    test_material_constrained_index_preserves_bound_clip(tmp_path)
    clips = load(tmp_path / "catalog/clips.json")
    clips[0]["revision"] = 2
    atomic_write(tmp_path / "catalog/clips.json", clips)
    with pytest.raises(Blocked) as exc:
        index_materials(tmp_path, "TASK_001", tmp_path / "proposals.json")
    assert exc.value.code == "MATERIAL_CONTEXT_STALE"
