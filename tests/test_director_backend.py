import pytest

from mixcut.core import Blocked, atomic_write, file_hash
from mixcut import director_backend as b


def test_auto_selects_existing_run_without_switching(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "latest", lambda root, task: {"revision": 1})
    monkeypatch.setattr(b, "folder", lambda root, task: root / task)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/occ").touch()
    assert b.choose_backend(tmp_path, "VIDEO_A") == "cli"
    old = tmp_path / "VIDEO_A/runs/r0001/state.json"
    old.parent.mkdir(parents=True)
    old.write_text("{}")
    assert b.choose_backend(tmp_path, "VIDEO_A") == "mcp"
    new = tmp_path / "VIDEO_A/cli_runs/r0001/state.json"
    new.parent.mkdir(parents=True)
    new.write_text("{}")
    assert b.choose_backend(tmp_path, "VIDEO_A") == "cli"
    assert b.choose_backend(tmp_path, "VIDEO_A", "mcp") == "mcp"


def test_remote_only_task_is_not_mistaken_for_local_run(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "latest", lambda root, task: None)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/occ").touch()
    assert b.choose_backend(tmp_path, "VIDEO_A") == "cli"
    assert b.cli_status(tmp_path, "VIDEO_A")["status"] == "not_started"


def test_cli_status_binds_review_and_qc_to_actual_file(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "latest", lambda root, task: {"revision": 1})
    monkeypatch.setattr(b, "folder", lambda root, task: root / task)
    media = tmp_path / "video/candidate.mp4"
    media.parent.mkdir()
    media.write_bytes(b"candidate")
    sha = file_hash(media)
    atomic_write(media.parent / "qc.json", {"status": "passed", "output_sha256": sha})
    atomic_write(
        media.parent / "human_review_chat.json", {"status": "approved", "candidate_sha256": sha}
    )
    state = tmp_path / "VIDEO_A/cli_runs/r0001/state.json"
    state.parent.mkdir(parents=True)
    atomic_write(
        state,
        {
            "task_id": "VIDEO_A",
            "status": "awaiting_content_review",
            "blocked_code": "old",
            "candidate": {"path": str(media), "sha256": sha},
        },
    )
    status = b.cli_status(tmp_path, "VIDEO_A")
    assert status["human_review"] == "approved_by_user_chat"
    assert "blocked_code" not in status
    media.write_bytes(b"changed")
    with pytest.raises(Blocked):
        b.cli_status(tmp_path, "VIDEO_A")
