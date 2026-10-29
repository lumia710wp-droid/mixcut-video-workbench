import os
import plistlib
from pathlib import Path

from mixcut.core import atomic_write, file_hash, load
from mixcut.doctor import CAPABILITIES, doctor
from mixcut.engine import local_runtime_status


def _executable(path: Path, name: str) -> str:
    path.write_text(f"#!/bin/sh\necho '{name} version test'\n")
    path.chmod(0o755)
    return str(path)


def _config(tmp_path: Path, bundle: Path) -> dict:
    return {
        "openchatcut": {"app_path": str(bundle)},
        "paths": {
            "ffmpeg": _executable(tmp_path / "ffmpeg", "ffmpeg"),
            "ffprobe": _executable(tmp_path / "ffprobe", "ffprobe"),
        },
        "cloud": {"cloud_analysis_enabled": False, "max_remote_calls": 0},
        "feishu": {"enabled": True},
        "source_roots": [{"root_id": "media", "path": str(tmp_path / "media")}],
    }


def test_doctor_supports_packaged_asar_and_preserves_live_g1_capabilities(tmp_path: Path):
    bundle = tmp_path / "OpenChatCut.app"
    contents = bundle / "Contents"
    resources = contents / "Resources"
    schema = resources / "dist/agent/openchatcut-tool-schemas.json"
    resources.mkdir(parents=True)
    schema.parent.mkdir(parents=True)
    (resources / "app.asar").write_bytes(b"packaged-app")
    (contents / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "0.2.16", "CFBundleVersion": "16"})
    )
    atomic_write(schema, {"version": 1, "edit": [{"name": "edit"}], "ask": []})
    (tmp_path / "media").mkdir()
    atomic_write(
        tmp_path / "reports/g0/settings_summary.json",
        {"dataDir": "/projects", "mediaDir": "/media", "import_roots": ""},
    )
    atomic_write(tmp_path / "reports/g0/tools.json", {"tools": [{"name": "status"}]})
    (tmp_path / "reports/g0/openchatcut_status.json").write_text("{}")
    (tmp_path / "reports/g1").mkdir(parents=True)
    (tmp_path / "reports/g1/G1_ACCEPTANCE.md").write_text("accepted")
    accepted = (
        tmp_path
        / "jobs/g1/accepted_test.mp4"
    )
    accepted.parent.mkdir(parents=True)
    accepted.write_bytes(b"video")

    result = doctor(tmp_path, _config(tmp_path, bundle))

    assert result["status"] == "ready"
    assert result["app_artifact_path"].endswith("app.asar")
    assert result["app_artifact_sha256"] == file_hash(resources / "app.asar")
    assert result["installed_tool_schema_count"] == 1
    assert result["source_roots"][0]["readable"] is True
    assert all(result["capabilities"][name]["supported"] for name in CAPABILITIES)
    assert load(tmp_path / "reports/capabilities.json")["render"]["status"] == (
        "live_validated_g1"
    )


def test_doctor_records_missing_app_without_partial_failure(tmp_path: Path):
    (tmp_path / "media").mkdir()
    config = _config(tmp_path, tmp_path / "Missing.app")

    result = doctor(tmp_path, config)

    assert result["status"] == "needs_attention"
    assert result["app_present"] is False
    assert result["app_artifact_sha256"] is None
    assert (tmp_path / "reports/doctor.json").is_file()
    assert set(load(tmp_path / "reports/capabilities.json")) == set(CAPABILITIES)
    assert os.path.isfile(tmp_path / "reports/dependency_audit.json")


def test_runtime_does_not_report_stopped_when_process_visibility_is_denied(
    tmp_path: Path, monkeypatch
):
    token = tmp_path / "token"
    token.write_text("secret")
    token.chmod(0o600)

    class Result:
        returncode = 3

    monkeypatch.setattr("mixcut.engine.subprocess.run", lambda *args, **kwargs: Result())

    result = local_runtime_status(token)

    assert result["openchatcut_running"] is None
    assert result["credential_available"] is True
    assert result["ready"] is None
    assert result["blockers"] == []
    assert result["unknowns"] == ["OpenChatCut process visibility is unavailable"]
