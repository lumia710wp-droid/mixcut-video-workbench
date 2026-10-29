from pathlib import Path
import pytest
from mixcut import director_archive as a
from mixcut.core import Blocked, atomic_write, file_hash


def test_archive_requires_review_and_records_source_idempotently(tmp_path, monkeypatch):
    p = tmp_path / "build/candidate.mp4"
    p.parent.mkdir()
    p.write_bytes(b"approved-video")
    h = file_hash(p)
    status = {"candidate": {"path": str(p), "sha256": h}, "human_review": "pending"}
    record = {
        "revision": 1,
        "packet_sha256": "p123",
        "packet": {"shots": [{"source_id": "s1"}, {"source_id": "s1"}, {"source_id": "s2"}]},
    }
    monkeypatch.setattr(a, "cli_status", lambda root, task: status)
    monkeypatch.setattr(a, "latest", lambda root, task: record)
    with pytest.raises(Blocked):
        a.archive_cli_candidate(tmp_path, "VIDEO_A")
    status["human_review"] = "approved_by_user_chat"
    atomic_write(
        p.parent / "human_review_chat.json", {"candidate_sha256": h, "approved_at": "test"}
    )
    result = a.archive_cli_candidate(tmp_path, "VIDEO_A")
    assert file_hash(Path(result["path"])) == h
    assert len(list((tmp_path / "ledger/director_source_usage").glob("*.json"))) == 2
    assert a.archive_cli_candidate(tmp_path, "VIDEO_A") == result
    Path(result["path"]).write_bytes(b"changed")
    with pytest.raises(Blocked):
        a.archive_cli_candidate(tmp_path, "VIDEO_A")


def test_chat_approval_binds_exact_candidate(tmp_path, monkeypatch):
    p = tmp_path / "build/candidate.mp4"
    p.parent.mkdir()
    p.write_bytes(b"candidate")
    sha = file_hash(p)
    monkeypatch.setattr(
        a, "cli_status", lambda root, task: {"candidate": {"path": str(p), "sha256": sha}}
    )
    with pytest.raises(Blocked):
        a.record_chat_approval(tmp_path, "VIDEO_A", "wrong-hash", "审核通过")
    with pytest.raises(Blocked):
        a.record_chat_approval(tmp_path, "VIDEO_A", sha, "")
    receipt = a.record_chat_approval(tmp_path, "VIDEO_A", sha, "审核通过")
    assert receipt["source"] == "user_chat"
    assert receipt["candidate_sha256"] == sha
    assert a.record_chat_approval(tmp_path, "VIDEO_A", sha, "审核通过") == receipt
