import copy
import shutil
from pathlib import Path

import pytest

from mixcut.contracts import content_hash, plan_hash, validate_plan
from mixcut.core import Blocked, atomic_write, digest, load

ROOT = Path(__file__).resolve().parents[1]


def setup_plan(tmp_path):
    (tmp_path / "schemas").mkdir()
    shutil.copy(ROOT / "schemas/edit_plan.schema.json", tmp_path / "schemas")
    plan = load(ROOT / "examples/edit_plan.fixture.json")
    plan.update(is_fixture=False, total_frames=900)
    shots = []
    for i, shot in enumerate(plan["shots"]):
        shot.update(
            timeline_start_frame=i * 180,
            duration_frames=180,
            source_out_frame=shot["source_in_frame"] + 180,
        )
        shots.append(
            {
                "shot_id": shot["shot_id"],
                "narration": "原文",
                "caption": "字幕",
                "visual_intent": "门店制作",
                "semantic_queries": ["门店"],
                "target_frames": 180,
            }
        )
    for i, cap in enumerate(plan["captions"]):
        cap.update(start_frame=i * 180, end_frame=i * 180 + 120)
    request = {
        "schema_version": "1.0.0",
        "entity": "script_request",
        "task_id": plan["task_id"],
        "creative_keywords": "测试",
        "seed_sentences": "",
        "target_duration_sec": 30,
        "target_duration_frames": 900,
        "input_policy": "human_creative_input_only_no_material_context",
    }
    request["request_sha256"] = digest(request)
    outline = {
        "schema_version": "1.0.0",
        "entity": "script_outline",
        "task_id": plan["task_id"],
        "revision": plan["script_revision"],
        "request_sha256": request["request_sha256"],
        "title": "30秒",
        "shots": shots,
        "total_frames": 900,
        "provider": "manual",
        "model": "local",
        "review_status": "approved",
        "approval": None,
    }
    outline["content_sha256"] = content_hash(outline)
    outline["approval"] = {
        "actor_id": "human",
        "approved_at": "2026-10-19T00:00:00Z",
        "channel": "local_manual",
        "approved_content_sha256": outline["content_sha256"],
    }
    plan["script_sha256"] = outline["content_sha256"]
    plan["plan_sha256"] = plan_hash(plan)
    plan["build_id"] = "b_" + plan["plan_sha256"][:16]
    op = tmp_path / "jobs" / plan["task_id"] / "r1/script_outline.json"
    rp = tmp_path / "requests" / plan["task_id"] / f"{request['request_sha256']}.json"
    atomic_write(op, outline)
    atomic_write(rp, request)
    return plan, op, rp


def test_exact_approved_30_second_plan(tmp_path):
    plan, _, _ = setup_plan(tmp_path)
    validate_plan(plan, tmp_path)


@pytest.mark.parametrize(
    "change", ["pending", "request_changed", "script_hash", "duration", "missing"]
)
def test_nonlegacy_duration_requires_exact_approval_and_request(tmp_path, change):
    plan, op, rp = setup_plan(tmp_path)
    outline = load(op)
    if change == "pending":
        outline.update(review_status="pending", approval=None)
        atomic_write(op, outline)
    elif change == "request_changed":
        request = load(rp)
        request["target_duration_frames"] = 720
        atomic_write(rp, request)
    elif change == "script_hash":
        plan["script_sha256"] = "0" * 64
    elif change == "duration":
        plan["shots"][-1]["duration_frames"] -= 1
        plan["shots"][-1]["source_out_frame"] -= 1
        plan["total_frames"] -= 1
    else:
        op.rename(op.with_suffix(".preserved"))
    plan["plan_sha256"] = plan_hash(plan)
    plan["build_id"] = "b_" + plan["plan_sha256"][:16]
    with pytest.raises(Blocked):
        validate_plan(plan, tmp_path)


def test_legacy_fixture_plan_still_valid_without_outline(tmp_path):
    (tmp_path / "schemas").mkdir()
    shutil.copy(ROOT / "schemas/edit_plan.schema.json", tmp_path / "schemas")
    plan = copy.deepcopy(load(ROOT / "examples/edit_plan.fixture.json"))
    validate_plan(plan, tmp_path, production=False)
