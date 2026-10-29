import subprocess
from pathlib import Path

import pytest
from mixcut.core import Blocked, atomic_write, file_hash
from mixcut import director_candidate_sync as sync


def setup_candidate(tmp_path, monkeypatch):
    video = tmp_path / 'candidate.mp4'
    video.write_bytes(b'media')
    sha = file_hash(video)
    monkeypatch.setattr(sync, 'cli_status', lambda root, task: {'candidate': {'path': str(video), 'sha256': sha}})
    monkeypatch.setattr(sync, 'latest', lambda root, task: {'revision': 1})
    return video, sha


def test_sync_stages_verified_once_and_reuses(tmp_path, monkeypatch):
    video, sha = setup_candidate(tmp_path, monkeypatch)
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        receipt = tmp_path / 'jobs/cli_candidate_sync/VIDEO_A/r0001/media_uploads/VIDEO_A.receipt.json'
        atomic_write(receipt, {'status': 'verified', 'sha256': sha})
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(sync.subprocess, 'run', fake_run)
    assert sync.sync_candidate(tmp_path, 'VIDEO_A')['status'] == 'verified'
    assert sync.sync_candidate(tmp_path, 'VIDEO_A')['status'] == 'verified'
    assert video.read_bytes() == b'media'
    assert len(calls) == 2  # helper handles remote idempotence


def test_uncertain_upload_never_claims_success(tmp_path, monkeypatch):
    setup_candidate(tmp_path, monkeypatch)
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(sync.subprocess, 'run', timeout)
    with pytest.raises(Blocked, match='先对账'):
        sync.sync_candidate(tmp_path, 'VIDEO_A')
    assert not (tmp_path / 'jobs/cli_candidate_sync/VIDEO_A/r0001/sync_receipt.json').exists()


def test_conflicting_staged_file_is_not_overwritten(tmp_path, monkeypatch):
    _, sha = setup_candidate(tmp_path, monkeypatch)
    p = tmp_path / 'jobs/cli_candidate_sync/VIDEO_A/r0001/media_uploads' / f'VIDEO_A_{sha[:16]}.mp4'
    p.parent.mkdir(parents=True)
    p.write_bytes(b'other')
    with pytest.raises(Blocked):
        sync.sync_candidate(tmp_path, 'VIDEO_A')
    assert p.read_bytes() == b'other'
