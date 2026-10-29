from pathlib import Path

import pytest

from mixcut.core import Blocked, atomic_write, file_hash, load
from mixcut.relocation import runtime_path
from mixcut.script_revision import revision_request, save_retimed, timing_verified
from mixcut.script_button import ensure_worker_host
from test_script_revision import workspace, measured


def test_migration_paths_preserve_boundary(tmp_path):
    atomic_write(tmp_path / "config/relocation.local.json", {"previous_workspace": "/old/app"})
    assert runtime_path(tmp_path, "/old/app/audio/a.wav") == tmp_path / "audio/a.wav"
    assert runtime_path(tmp_path, "/old/app-other/a.wav") == Path("/old/app-other/a.wav")
    with pytest.raises(Blocked, match="escapes"):
        runtime_path(tmp_path, "/old/app/../../escape.wav")
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(Blocked, match="escapes"):
        runtime_path(tmp_path, "/old/app/link/escape.wav")


def test_access_client_cannot_start_second_worker(tmp_path):
    atomic_write(tmp_path / "config/runtime_role.local.json", {"role": "access_client"})
    for action in ("start", "watch", "once"):
        with pytest.raises(Blocked) as error:
            ensure_worker_host(tmp_path, action)
        assert error.value.code == "WORKSPACE_RELOCATED"
    ensure_worker_host(tmp_path, "stop")
    ensure_worker_host(tmp_path, "status")


def test_preserved_timing_requires_exact_approved_bytes_and_audio(tmp_path):
    config, _, parent, remote, _ = workspace(tmp_path)
    request = revision_request(tmp_path, remote.fields, parent, action="retime")
    path, outline, timing = save_retimed(
        tmp_path, config, parent, request, parent.copy(), measure=measured(tmp_path)
    )
    timing_path = path.parent / "draft_timing.json"
    atomic_write(tmp_path / "config/relocation.local.json", {
        "previous_workspace": "/old/app",
        "preserved_timings": {str(timing_path.relative_to(tmp_path)): {
            "file_sha256": file_hash(timing_path), "script_sha256": outline["content_sha256"]
        }}
    })
    config["audio"]["provider_version"] = "new-macos"
    atomic_write(tmp_path / "config/config.local.json", config)
    with pytest.raises(Blocked) as error:
        timing_verified(tmp_path, path, outline)
    assert error.value.code == "SCRIPT_TIMING_STALE"
    outline["approval"] = {"approved_content_sha256": outline["content_sha256"]}
    assert timing_verified(tmp_path, path, outline) == timing
    audio_path = Path(timing["segments"][0]["audio_path"])
    original = audio_path.read_bytes()
    audio_path.write_bytes(b"changed")
    with pytest.raises(Blocked) as error:
        timing_verified(tmp_path, path, outline)
    assert error.value.code == "AUDIO_CHANGED"
    audio_path.write_bytes(original)
    changed = load(timing_path)
    changed["speech_frames"] = -1
    atomic_write(timing_path, changed)
    with pytest.raises(Blocked) as error:
        timing_verified(tmp_path, path, outline)
    assert error.value.code == "SCRIPT_TIMING_STALE"
