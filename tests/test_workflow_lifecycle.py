from pathlib import Path

from mixcut.contracts import content_hash
from mixcut.core import atomic_write, file_hash, load
from mixcut.runner import _connected_project_ids, finalize
from mixcut.sync import sync_final_reviews
from mixcut.workflow import inspect_task


def approved_outline(task: str, revision: int = 1) -> dict:
    row = {
        "entity": "script_outline",
        "task_id": task,
        "revision": revision,
        "review_status": "approved",
        "is_fixture": False,
    }
    row["content_sha256"] = content_hash(row)
    row["approval"] = {
        "actor_id": "reviewer",
        "channel": "feishu",
        "approved_at": "2026-10-16T00:00:00+00:00",
        "approved_content_sha256": row["content_sha256"],
    }
    return row


def test_inspector_reports_material_index_as_next_safe_step(tmp_path: Path):
    task = "TASK_1"
    path = tmp_path / "jobs" / task / "r1" / "script_outline.json"
    atomic_write(path, approved_outline(task))
    result = inspect_task(tmp_path, task)
    assert result["stage"] == "ready_for_material_index"
    assert result["command"].endswith(f"material-index --task {task}")


def test_connected_project_parser_requires_the_visible_editor():
    result = {
        "structuredContent": {
            "connectedProjectIds": ["P1"],
            "editors": [
                {"projectId": "P1", "connected": True},
                {"projectId": "P2", "connected": False},
            ],
        }
    }

    assert _connected_project_ids(result) == {"P1"}


def _candidate_job(tmp_path: Path) -> tuple[Path, dict]:
    job = tmp_path / "jobs/TASK_1/r1/b_build"
    candidate = job / "candidate.mp4"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"real candidate bytes")
    output_hash = file_hash(candidate)
    plan = {
        "task_id": "TASK_1",
        "batch_id": "BATCH_1",
        "script_revision": 1,
        "script_sha256": "script-hash",
        "build_id": "b_build",
        "is_fixture": False,
        "shots": [{"clip_id": "CLIP_1"}, {"clip_id": "CLIP_2"}],
    }
    atomic_write(job / "edit_plan.json", plan)
    atomic_write(
        job / "qc.json",
        {"status": "passed", "output_sha256": output_hash, "human_review": "pending"},
    )
    atomic_write(
        job / "state.json",
        {
            "task_id": "TASK_1",
            "build_id": "b_build",
            "candidate_sha256": output_hash,
            "render_status": "succeeded",
            "qc_status": "passed",
            "human_review": "pending",
            "sync_status": "synced",
            "stage": "waiting_final_review",
            "final_path": None,
            "project_ref": "openchatcut://project/p1",
        },
    )
    return job, plan


def test_final_review_binds_exact_build_and_candidate(tmp_path: Path):
    job, _ = _candidate_job(tmp_path)
    rows = [
        {
            "record_id": "rec1",
            "fields": {
                "task_id": "TASK_1",
                "final_review_build_id": "b_build",
                "final_review_action": ["approve"],
                "final_review_actor": "Alice",
                "final_review_comment": "通过",
            },
        }
    ]
    result = sync_final_reviews(tmp_path, rows, "2026-10-16T01:00:00+00:00")
    review = load(job / "human_review.json")
    state = load(job / "state.json")
    assert result["approved"] == 1
    assert review["action"] == "approve" and review["actor"] == "Alice"
    assert review["output_sha256"] == file_hash(job / "candidate.mp4")
    assert state["stage"] == "ready_to_finalize"


def test_finalize_archives_and_commits_usage_once(tmp_path: Path):
    job, plan = _candidate_job(tmp_path)
    output_hash = file_hash(job / "candidate.mp4")
    snapshot_at = "2026-10-16T01:00:00+00:00"
    atomic_write(
        job / "human_review.json",
        {
            "task_id": "TASK_1",
            "build_id": "b_build",
            "output_sha256": output_hash,
            "status": "approved",
            "action": "approve",
            "actor": "Alice",
            "channel": "feishu",
            "snapshot_at": snapshot_at,
            "approved_at": snapshot_at,
        },
    )
    fields = {
        "build_id": "b_build",
        "final_review_build_id": "b_build",
        "final_review_action": ["approve"],
        "final_review_actor": "Alice",
        "script_revision": 1,
        "script_sha256": plan["script_sha256"],
    }
    final = finalize(
        tmp_path,
        job,
        "b_build",
        {"snapshot_at": snapshot_at, "fields": fields},
    )
    assert final.is_file() and file_hash(final) == output_hash
    assert load(job / "state.json")["stage"] == "delivered"
    events = list((tmp_path / "ledger/events").glob("*.json"))
    assert len(events) == 2
    finalize(
        tmp_path,
        job,
        "b_build",
        {"snapshot_at": snapshot_at, "fields": fields},
    )
    assert len(list((tmp_path / "ledger/events").glob("*.json"))) == 2
