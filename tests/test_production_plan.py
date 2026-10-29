import copy
import shutil
from pathlib import Path

from mixcut.contracts import content_hash, validate_plan
from mixcut.core import atomic_write, file_hash, load
from mixcut.production_plan import NORMALIZATION_PROFILE, compile_edit_plan, normalize_source


def _approve(record: dict) -> dict:
    value = copy.deepcopy(record)
    value["content_sha256"] = content_hash(value)
    value["approval"] = {
        "actor_id": "human",
        "channel": "local_manual",
        "approved_at": "2026-10-16T00:00:00+00:00",
        "approved_content_sha256": value["content_sha256"],
    }
    return value


def test_normalize_source_reuses_verified_cache(tmp_path):
    source_root = tmp_path / "source"
    output_root = tmp_path / "normalized"
    source_root.mkdir()
    output_root.mkdir()
    original = source_root / "clip.mov"
    original.write_bytes(b"original")
    source = {
        "source_id": "s_abc",
        "file_sha256": file_hash(original),
        "locations": [{"root_id": "source", "relative_path": "clip.mov"}],
    }
    target = output_root / "s_abc.mp4"
    target.write_bytes(b"normalized")
    receipt = {
        "source_id": "s_abc",
        "source_sha256": source["file_sha256"],
        "working_sha256": file_hash(target),
        "root_id": "normalized_cfr30_v1",
        "relative_path": target.name,
        "total_frames": 300,
        "profile": NORMALIZATION_PROFILE,
    }
    atomic_write(output_root / "s_abc.json", receipt)
    config = {
        "source_roots": [
            {"root_id": "source", "path": str(source_root)},
            {"root_id": "normalized_cfr30_v1", "path": str(output_root)},
        ]
    }
    assert normalize_source(tmp_path, config, source) == receipt
    original.unlink()
    assert normalize_source(tmp_path, config, source) == receipt
    target.write_bytes(b"tampered")
    import pytest
    from mixcut.core import Blocked

    with pytest.raises(Blocked, match="Preserving unverified"):
        normalize_source(tmp_path, config, source)


def test_compile_edit_plan_locks_approved_media_audio_and_frames(tmp_path, monkeypatch):
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    root = Path(__file__).parents[1]
    shutil.copy(root / "schemas/edit_plan.schema.json", schemas)
    shutil.copy(root / "schemas/product.schema.json", schemas)
    normalized = tmp_path / "media/normalized_cfr30_v1"
    normalized.mkdir(parents=True)
    config = {
        "source_roots": [
            {"root_id": "normalized_cfr30_v1", "path": str(normalized)},
            {"root_id": "mixcut_workspace", "path": str(tmp_path)},
        ],
        "output": {"fit_mode": "contain"},
    }
    product = _approve(
        {
            "schema_version": "1.0.0",
            "entity": "product",
            "is_fixture": False,
            "brand_id": "品牌",
            "goods_id": "STORE_MULTI_PRODUCT",
            "revision": 1,
            "name": "门店多品",
            "claims": [],
            "forbidden_phrases": ["价格"],
            "review_status": "approved",
            "content_sha256": "",
            "approval": None,
        }
    )
    atomic_write(tmp_path / "config/goods/store_multi_product_v1.json", product)
    atomic_write(tmp_path / "config/taxonomy.json", {"version": "taxonomy-1"})
    atomic_write(
        tmp_path / "catalog/feishu_snapshot.json",
        {
            "tables": {
                "tasks": [
                    {
                        "record_id": "rec1",
                        "fields": {"task_id": "TASK_001", "batch_id": "BATCH_1"},
                    }
                ]
            }
        },
    )
    request = {
        "schema_version": "1.0.0",
        "entity": "script_request",
        "task_id": "TASK_001",
        "creative_keywords": "门店",
        "seed_sentences": "手工制作",
        "target_duration_sec": 24,
        "target_duration_frames": 720,
        "input_policy": "approved_material_context_constrained",
        "material_context_sha256": "context-hash",
        "material_slots": [],
        "request_sha256": "request-hash",
    }
    shots = []
    clips = []
    sources = []
    index_shots = []
    for number in range(1, 7):
        shot_id = f"M{number:02d}"
        clip_id = f"clip-{number}"
        source_id = f"source-{number}"
        slot = {
            "slot_id": shot_id,
            "clip_id": clip_id,
            "clip_revision": 1,
            "approved_content_sha256": "",
            "analysis_sha256": f"analysis-{number}",
            "goods_id": "商品",
            "usable_start_sec": 2,
            "usable_end_sec": 7,
            "visual_intent": f"画面{number}",
            "semantic_queries": [f"动作{number}"],
            "target_frames": 120,
            "max_narration_chars": 10,
            "max_caption_chars": 10,
        }
        clip = _approve(
            {
                "clip_id": clip_id,
                "source_id": source_id,
                "revision": 1,
                "goods_id": "商品",
                "review_status": "approved",
                "rights_confirmed": True,
                "tags": {"risk": []},
                "content_sha256": "",
                "approval": None,
            }
        )
        slot["approved_content_sha256"] = clip["content_sha256"]
        request["material_slots"].append(slot)
        clips.append(clip)
        sources.append(
            {
                "source_id": source_id,
                "file_sha256": f"{number:064x}",
                "locations": [{"root_id": "unused", "relative_path": f"{number}.mov"}],
            }
        )
        shots.append(
            {
                "shot_id": shot_id,
                "narration": f"旁白{number}",
                "caption": f"字幕{number}",
                "visual_intent": slot["visual_intent"],
                "semantic_queries": slot["semantic_queries"],
                "target_frames": 120,
            }
        )
        index_shots.append(
            {
                "shot_id": shot_id,
                "caption": f"字幕{number}",
                "candidates": [
                    {
                        "clip_id": clip_id,
                        "start_sec": 2,
                        "end_sec": 7,
                        "production_eligible": True,
                    }
                ],
            }
        )
    outline = _approve(
        {
            "schema_version": "1.0.0",
            "entity": "script_outline",
            "task_id": "TASK_001",
            "request_sha256": "request-hash",
            "title": "测试",
            "shots": shots,
            "total_frames": 720,
            "revision": 1,
            "provider": "deepseek",
            "model": "deepseek-flash",
            "review_status": "approved",
            "content_sha256": "",
            "approval": None,
        }
    )
    atomic_write(tmp_path / "requests/TASK_001/request-hash.json", request)
    atomic_write(tmp_path / "jobs/TASK_001/r1/script_outline.json", outline)
    atomic_write(tmp_path / "catalog/clips.json", clips)
    atomic_write(tmp_path / "catalog/sources.json", sources)
    atomic_write(
        tmp_path / "jobs/TASK_001/r1/material_index_preview.json",
        {
            "production_ready": True,
            "script_sha256": outline["content_sha256"],
            "script_revision": 1,
            "index_id": "index-1",
            "shot_count": 6,
            "shots_with_candidates": 6,
            "eligible_shot_count": 6,
            "material_gap_count": 0,
            "shots": index_shots,
        },
    )
    voiceover = tmp_path / "audio/narration.wav"
    voiceover.parent.mkdir(parents=True)
    voiceover.write_bytes(b"audio")
    atomic_write(
        tmp_path / "jobs/TASK_001/r1/preproduction/audio_caption_manifest.json",
        {
            "status": "ready_for_edit_plan",
            "script_sha256": outline["content_sha256"],
            "voiceover": {"path": str(voiceover), "sha256": file_hash(voiceover), "gain": 1.0},
            "captions": [
                {
                    "shot_id": shot["shot_id"],
                    "start_frame": index * 120,
                    "end_frame": index * 120 + 60,
                    "text": shot["caption"],
                }
                for index, shot in enumerate(shots)
            ],
            "style": {"style_sha256": "1" * 64},
            "formal_edit_plan_created": False,
        },
    )

    def fake_normalize(_workspace, _config, source):
        path = normalized / f"{source['source_id']}.mp4"
        path.write_bytes(source["source_id"].encode())
        return {
            "working_sha256": file_hash(path),
            "root_id": "normalized_cfr30_v1",
            "relative_path": path.name,
            "total_frames": 900,
        }

    monkeypatch.setattr("mixcut.production_plan.normalize_source", fake_normalize)
    result = compile_edit_plan(tmp_path, config, "TASK_001")
    plan = load(Path(result["path"]))
    validate_plan(plan, tmp_path)
    assert result["status"] == "ready_for_openchatcut_import"
    assert len(plan["media"]) == len(plan["shots"]) == len(plan["captions"]) == 6
    assert [shot["timeline_start_frame"] for shot in plan["shots"]] == [
        0,
        120,
        240,
        360,
        480,
        600,
    ]
    assert all(shot["source_in_frame"] == 75 for shot in plan["shots"])
    assert plan["audio_tracks"][0]["sha256"] == file_hash(voiceover)
    manifest = load(tmp_path / "jobs/TASK_001/r1/preproduction/audio_caption_manifest.json")
    assert manifest["formal_edit_plan_created"] is True
    assert manifest["build_id"] == plan["build_id"]
