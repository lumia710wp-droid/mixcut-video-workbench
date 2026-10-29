from pathlib import Path
from types import SimpleNamespace

import pytest

from mixcut.batch_preflight import preflight_batch
from mixcut.contracts import content_hash
from mixcut.core import Blocked, atomic_write, file_hash


def setup_batch(tmp_path: Path, monkeypatch):
    source_root = tmp_path / "originals"
    source_root.mkdir()
    source = source_root / "sample.mov"
    source.write_bytes(b"fixture source")
    atomic_write(
        tmp_path / "catalog/sources.json",
        [
            {
                "source_id": "S1",
                "file_sha256": file_hash(source),
                "locations": [{"root_id": "authorized", "relative_path": source.name}],
            }
        ],
    )
    clip = {
        "clip_id": "C1",
        "source_id": "S1",
        "goods_id": "G1",
        "rights_confirmed": True,
        "usable_start_sec": 2,
        "usable_end_sec": 6,
        "review_status": "approved",
    }
    clip["content_sha256"] = content_hash(clip)
    clip["approval"] = {
        "actor_id": "test-reviewer",
        "approved_at": "test-only",
        "approved_content_sha256": clip["content_sha256"],
    }
    atomic_write(tmp_path / "catalog/clips.json", [clip])
    accepted = []
    for task_id in ("G3_1", "G3_2", "G3_3"):
        final = tmp_path / "jobs" / task_id / "final.mp4"
        final.parent.mkdir(parents=True)
        final.write_bytes(b"fixture output")
        accepted.append(
            {
                "task_id": task_id,
                "final_path": str(final),
                "sha256": file_hash(final),
                "human": "approved",
            }
        )
    atomic_write(tmp_path / "reports/g3/test.json", {"status": "passed", "tasks": accepted})
    monkeypatch.setattr(
        "mixcut.batch_preflight.shutil.disk_usage", lambda _: SimpleNamespace(free=10000)
    )
    monkeypatch.setattr(
        "mixcut.batch_preflight.inspect_task",
        lambda _, task: {"task_id": task, "stage": "ready_for_openchatcut_project"},
    )
    config = {"source_roots": [{"root_id": "authorized", "path": str(source_root)}]}
    manifest = {
        "batch_id": "B10",
        "is_fixture": False,
        "g3_acceptance_report": "reports/g3/test.json",
        "tasks": [{"task_id": f"T{i}"} for i in range(10)],
        "scratch_bytes_per_task": 100,
        "min_free_bytes_after_batch": 100,
    }
    return config, manifest, source


def test_preflight_is_local_and_does_not_execute_or_authorize(tmp_path, monkeypatch):
    config, manifest, _ = setup_batch(tmp_path, monkeypatch)
    result = preflight_batch(tmp_path, config, manifest)
    assert result["status"] == "ready"
    assert result["execution_authorized"] is False
    assert result["network_calls"] == result["model_calls"] == result["renders_started"] == 0
    assert result["materials"][0]["available_frames"] == 120
    assert not (tmp_path / "batches").exists()


@pytest.mark.parametrize("count", [3, 31])
def test_g4_cannot_use_three_outputs_or_overlarge_batch(tmp_path, monkeypatch, count):
    config, manifest, _ = setup_batch(tmp_path, monkeypatch)
    manifest["tasks"] = [{"task_id": f"T{i}"} for i in range(count)]
    with pytest.raises(Blocked, match="10-30"):
        preflight_batch(tmp_path, config, manifest)


def test_duplicate_tasks_cannot_fill_batch(tmp_path, monkeypatch):
    config, manifest, _ = setup_batch(tmp_path, monkeypatch)
    manifest["tasks"][-1] = manifest["tasks"][0]
    with pytest.raises(Blocked, match="Duplicate"):
        preflight_batch(tmp_path, config, manifest)


def test_missing_original_and_changed_accepted_g3_remain_blocked(tmp_path, monkeypatch):
    config, manifest, source = setup_batch(tmp_path, monkeypatch)
    source.unlink()
    (tmp_path / "jobs/G3_1/final.mp4").write_bytes(b"changed")
    result = preflight_batch(tmp_path, config, manifest)
    codes = {r["code"] for r in result["blockers"]}
    assert {"SOURCE_UNAVAILABLE", "NO_VERIFIED_MATERIALS", "G3_EVIDENCE_CHANGED"} <= codes
    assert result["source_checks"]["S1"]["status"] == "blocked"


def test_unapproved_scripts_and_low_disk_are_reported_without_model_calls(tmp_path, monkeypatch):
    config, manifest, _ = setup_batch(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "mixcut.batch_preflight.inspect_task",
        lambda _, task: {"task_id": task, "stage": "waiting_creative_input"},
    )
    monkeypatch.setattr(
        "mixcut.batch_preflight.shutil.disk_usage", lambda _: SimpleNamespace(free=100)
    )
    result = preflight_batch(tmp_path, config, manifest)
    assert result["budget"]["minimum_new_script_calls"] == 10
    assert result["budget"]["cost_estimate"] is None
    assert {"DISK_LOW", "TASK_NOT_READY", "MODEL_BATCH_BUDGET_REVIEW_REQUIRED"} <= {
        r["code"] for r in result["blockers"]
    }
    assert result["model_calls"] == 0


def test_report_path_cannot_escape_workspace(tmp_path, monkeypatch):
    config, manifest, _ = setup_batch(tmp_path, monkeypatch)
    manifest["g3_acceptance_report"] = "../outside.json"
    with pytest.raises(Blocked, match="within this workspace"):
        preflight_batch(tmp_path, config, manifest)
