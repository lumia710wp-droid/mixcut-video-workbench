import struct
from pathlib import Path

import pytest
from mixcut.core import Blocked, file_hash
from mixcut.delivery_package import COVERS, PLATFORMS, prepare, verify


def make_inputs(root):
    video = root / "candidate.mp4"
    video.write_bytes(b"example video")
    covers = {}
    for name, (w, h) in COVERS.items():
        p = root / (name + ".png")
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h))
        covers[name] = str(p)
    return {"path": str(video), "sha256": file_hash(video)}, {
        "covers": covers,
        "copy": {p: {"body": p, "titles": ["标题"], "hashtags": []} for p in PLATFORMS},
    }


def test_package_binds_video_images_copy_and_versions(tmp_path):
    candidate, spec = make_inputs(tmp_path)
    a = prepare(tmp_path, "VIDEO_TEST", candidate, "director-v1", "script-v1", spec)
    assert verify(tmp_path, "VIDEO_TEST", a["package_sha256"]) == a
    spec["copy"]["douyin"]["body"] = "修改后的文案"
    b = prepare(tmp_path, "VIDEO_TEST", candidate, "director-v1", "script-v1", spec)
    assert a["package_sha256"] != b["package_sha256"]
    with pytest.raises(Blocked, match="验收包已变化"):
        verify(tmp_path, "VIDEO_TEST", a["package_sha256"])
    assert (
        tmp_path / "jobs/delivery_packages/VIDEO_TEST" / (a["package_sha256"] + ".json")
    ).is_file()


def test_package_blocks_missing_cover_and_changed_file(tmp_path):
    candidate, spec = make_inputs(tmp_path)
    old = spec["covers"].pop("douyin")
    with pytest.raises(Blocked, match="四张封面"):
        prepare(tmp_path, "VIDEO_TEST", candidate, "d", "s", spec)
    spec["covers"]["douyin"] = old
    package = prepare(tmp_path, "VIDEO_TEST", candidate, "d", "s", spec)
    Path(old).write_bytes(b"changed")
    with pytest.raises(Blocked, match="文件已变化"):
        verify(tmp_path, "VIDEO_TEST", package["package_sha256"])


def test_package_rejects_wrong_cover_size_and_external_path(tmp_path):
    candidate, spec = make_inputs(tmp_path)
    spec["covers"]["douyin"] = spec["covers"]["channels_landscape"]
    with pytest.raises(Blocked, match="尺寸"):
        prepare(tmp_path, "VIDEO_TEST", candidate, "d", "s", spec)
    spec["covers"]["douyin"] = "/etc/hosts"
    with pytest.raises(Blocked, match="工作区"):
        prepare(tmp_path, "VIDEO_TEST", candidate, "d", "s", spec)


def test_publish_and_review_bind_current_feishu_packet(tmp_path, monkeypatch):
    from mixcut.delivery_package import publish, review
    from mixcut import director_backend, director_runner
    import json

    candidate, spec = make_inputs(tmp_path)
    packet = prepare(tmp_path, "VIDEO_TEST", candidate, "director-v1", "script-v1", spec)
    h = packet["package_sha256"]
    monkeypatch.setattr(director_runner, "pull_authority", lambda *a: None)
    monkeypatch.setattr(
        director_runner,
        "check_live",
        lambda *a: ({"packet_sha256": "director-v1"}, {"content_sha256": "script-v1"}),
    )
    monkeypatch.setattr(director_backend, "cli_status", lambda *a: {"candidate": candidate})
    from mixcut import delivery_package

    monkeypatch.setattr(delivery_package, "verify_video_attachment", lambda *a: "video")

    class Bench:
        def __init__(self):
            from mixcut.delivery_package import FIELDS

            self.fields = {"candidate_preview": [{"file_token": "video"}]}
            self.mapping = {
                "base_token": "test",
                "tables": {"tasks": {"table_id": "tasks", "field_ids": {k: k for k in FIELDS}}},
            }

        def task(self, task):
            return {"record_id": "r", "fields": self.fields}

        def call(self, args):
            field = args[args.index("--field-id") + 1]
            path = tmp_path / args[args.index("--file") + 1]
            self.fields[field] = [
                {"file_token": field + "-token", "name": path.name, "size": path.stat().st_size}
            ]

        def patch(self, record_id, values):
            self.fields.update(values)

        def verify_patch(self, task, values):
            assert all(self.fields.get(k) == v for k, v in values.items())

    bench = Bench()
    assert publish(tmp_path, "VIDEO_TEST", bench)["status"] == "verified"
    assert len(json.loads(bench.fields["delivery_manifest"])["covers"]) == 4
    assert (
        review(tmp_path, "VIDEO_TEST", h, "approve", bench, {"id": "test-actor"})["status"]
        == "approved"
    )
    assert bench.fields["delivery_review_hash"] == h
    from mixcut.workbench import delivery_view

    assert delivery_view(tmp_path, "VIDEO_TEST", bench.fields) == "approved"
    bench.fields["delivery_cover_douyin"] = [{"file_token": "tampered"}]
    assert delivery_view(tmp_path, "VIDEO_TEST", bench.fields) == "stale"
    bench.fields["delivery_cover_douyin"] = [{"file_token": "delivery_cover_douyin-token"}]
    spec["copy"]["douyin"]["body"] = "更新后的正文"
    changed = prepare(tmp_path, "VIDEO_TEST", candidate, "director-v1", "script-v1", spec)
    assert publish(tmp_path, "VIDEO_TEST", bench)["status"] == "verified"
    assert bench.fields["delivery_review_status"] == "pending"
    assert bench.fields["delivery_review_hash"] == ""
    assert (
        json.loads(bench.fields["delivery_manifest"])["package_sha256"] == changed["package_sha256"]
    )
    with pytest.raises(Blocked, match="验收包已变化"):
        review(tmp_path, "VIDEO_TEST", h, "approve", bench, {"id": "test-actor"})


def test_publish_never_overwrites_existing_cover(tmp_path, monkeypatch):
    from mixcut.delivery_package import publish, FIELDS
    from mixcut import director_backend, director_runner

    candidate, spec = make_inputs(tmp_path)
    prepare(tmp_path, "VIDEO_TEST", candidate, "d", "s", spec)
    monkeypatch.setattr(director_runner, "pull_authority", lambda *a: None)
    monkeypatch.setattr(
        director_runner, "check_live", lambda *a: ({"packet_sha256": "d"}, {"content_sha256": "s"})
    )
    monkeypatch.setattr(director_backend, "cli_status", lambda *a: {"candidate": candidate})
    from mixcut import delivery_package

    monkeypatch.setattr(delivery_package, "verify_video_attachment", lambda *a: "video")

    class Bench:
        mapping = {
            "base_token": "test",
            "tables": {"tasks": {"table_id": "tasks", "field_ids": {k: k for k in FIELDS}}},
        }

        def task(self, task):
            return {
                "record_id": "r",
                "fields": {
                    "candidate_preview": [{"file_token": "v"}],
                    "delivery_cover_douyin": [{"file_token": "existing"}],
                },
            }

    with pytest.raises(Blocked, match="已有内容"):
        publish(tmp_path, "VIDEO_TEST", Bench())


def test_remote_stages_verified_files_and_expected_package(tmp_path, monkeypatch):
    import json
    import shutil
    import subprocess
    import sys
    import shlex
    from types import SimpleNamespace
    from mixcut import delivery_remote
    from mixcut.core import atomic_write

    local = tmp_path / "local"
    remote = tmp_path / "remote"
    materials = local / "jobs" / "test-media"
    materials.mkdir(parents=True)
    remote.mkdir()
    (remote / "src").mkdir()
    shutil.copytree(Path(__file__).resolve().parents[1] / "src/mixcut", remote / "src/mixcut")
    atomic_write(
        local / "config/director_authority.local.json",
        {"host": "test-host", "workspace": str(remote)},
    )
    candidate, spec = make_inputs(materials)
    package = prepare(local, "VIDEO_TEST", candidate, "d", "s", spec)
    real_run = subprocess.run
    calls = []

    def fake_ssh(host, root, code, payload=None, timeout=60):
        if "from mixcut.delivery_package import publish" in code:
            calls.append(payload)
            verified = verify(remote, payload["task"], payload["package_sha256"])
            return {"status": "verified", "package_sha256": verified["package_sha256"]}
        result = real_run(
            [sys.executable, "-c", code],
            cwd=remote,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def fake_scp(args, **kwargs):
        assert args[0] == "scp"
        destination = shlex.split(args[-1].split(":", 1)[1])[0]
        shutil.copyfile(args[-2], destination)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(delivery_remote, "_ssh", fake_ssh)
    monkeypatch.setattr(delivery_remote.subprocess, "run", fake_scp)
    result = delivery_remote.publish_remote(local, "VIDEO_TEST")
    assert result["package_sha256"] == package["package_sha256"]
    assert calls[0]["package_sha256"] == package["package_sha256"]
    assert verify(remote, "VIDEO_TEST", package["package_sha256"])["content"] == package["content"]
