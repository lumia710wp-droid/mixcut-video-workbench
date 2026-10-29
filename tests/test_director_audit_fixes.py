"""Audit regressions: real compilers and durable receipts, isolated editor/authority."""

import copy
import shutil
import subprocess
from pathlib import Path
import pytest
from mixcut import director_runner as runner, director_editor as editor, script_stage
from mixcut import director_stage as stage
from mixcut.core import Blocked, atomic_write, load
from mixcut.director_native import Native
from test_fixed_runner_title import ready, fixed_fixture, FakeNative
from test_director_stage import Bench, approve

REAL_VERIFY = runner.verify_native


def integration(root, monkeypatch, *, cut=None, pause_render=False):
    """Only media generation/network/QC are doubles; compile/verify/journal are real."""
    bench = fixed_fixture(root, monkeypatch)
    old_voice = runner.prepare_audio

    def voice(*args):
        value = old_voice(*args)
        record = stage.latest(root, "t0")
        value["director_sha256"] = record["packet_sha256"]
        atomic_write(stage.folder(root, "t0") / f"audio_r{record['revision']:04d}.json", value)
        return value

    monkeypatch.setattr(runner, "prepare_audio", voice)
    preflight = runner.media_preflight
    monkeypatch.setattr(editor, "media_preflight", lambda *args: preflight(*args))
    monkeypatch.setattr(
        runner,
        "compile_adds",
        lambda *args, **kw: editor.compile_adds(*args, verify_uploads=False, **kw),
    )
    monkeypatch.setattr(runner, "compile_updates", editor.compile_updates)
    monkeypatch.setattr(runner, "template_command", editor.template_command)
    monkeypatch.setattr(runner, "verify_native", REAL_VERIFY)

    class JournalEditor(FakeNative):
        items = []
        assets = []
        sent = []
        applied = False
        code = None
        props = {}
        cut_done = False
        render_pause_done = False
        pending_name = None

        def __init__(self, directory, guard):
            super().__init__(directory, guard)
            self.journal = Native(directory, guard)

        def completed(self, label):
            return self.journal.completed(label)

        def effect(self, label, name, args):
            def send():
                cls = type(self)
                if name == "submit_render_job" and pause_render and not cls.render_pause_done:
                    cls.render_pause_done = True
                    raise Blocked(
                        "RUNNER_CONFIRMATION", "Synthetic platform confirmation: no render sent"
                    )
                cls.sent.append((label, name, copy.deepcopy(args)))
                if name == "create_project":
                    return {"id": "isolated-project"}
                if name == "import_media":
                    cls.pending_name = args["filename"]
                    return {"uploadUrl": "http://127.0.0.1:5199/isolated-slot"}
                if name == "finalize_uploaded_asset":
                    aid = (
                        "voice"
                        if cls.pending_name.endswith(".mp3")
                        else "source_" + Path(cls.pending_name).stem
                    )
                    cls.assets.append(
                        {
                            "name": cls.pending_name,
                            "id": aid,
                            "src": "/media/uploads/" + cls.pending_name,
                        }
                    )
                    return {"assetId": aid}
                if name == "create_motion_graphic_from_code":
                    cls.code = args["code"]
                    cls.props = {p["key"]: p["defaultValue"] for p in args["properties"]}
                    cls.assets.append({"name": args["name"], "id": "title"})
                    return {"assetId": "title"}
                if name == "edit_item" and args.get("adds"):
                    results = []
                    for n, a in enumerate(args["adds"]):
                        item = {
                            "id": "item_" + str(n),
                            "kind": a["type"],
                            "track": a["track"],
                            "sourceAssetId": a["assetId"],
                            "startFrame": a["fromFrame"],
                            "durationInFrames": a.get(
                                "durationInFrames", a.get("sourceDurationInFrames")
                            ),
                            "sourceStartFrame": a.get("sourceStartFrame", 0),
                            "linkStatus": "linked",
                        }
                        if a["type"] != "motion-graphic":
                            item["sourceDurationInFrames"] = a.get(
                                "sourceDurationInFrames",
                                a["durationInFrames"] if "durationInFrames" in a else 0,
                            )
                            item["volume"] = 1
                        else:
                            item["props"] = copy.deepcopy(cls.props)
                        cls.items.append(item)
                        results.append(
                            {"ok": True, "kind": a["type"], "placed": {"itemId": item["id"]}}
                        )
                    return {"results": results}
                if name == "edit_item" and args.get("updates"):
                    for update in args["updates"]:
                        item = next(i for i in cls.items if i["id"] == update["itemId"])
                        if "speed" in update:
                            item["durationInFrames"] = round(
                                item["sourceDurationInFrames"] / update["speed"]
                            )
                        for k, v in update.items():
                            if k == "fromFrame":
                                item["startFrame"] = v
                            elif k not in {"type", "itemId", "speed"}:
                                item[k] = v
                    return {"ok": True}
                if name == "review_edit_session":
                    cls.applied = True
                    return {"status": "applied"}
                if name == "submit_render_job":
                    return {"renderId": "isolated-render"}
                return {"ok": True}

            value = self.journal.once(label, {"tool": name, "args": args}, send)
            if label == cut and not type(self).cut_done:
                type(self).cut_done = True
                raise RuntimeError("interrupt after durable successful RPC receipt")
            return value

        def read(self, name, args):
            cls = type(self)
            if name == "get_edit_session":
                export = args["editSessionId"].startswith("export_session")
                return {
                    "status": "drafting" if export else "applied" if cls.applied else "drafting",
                    "baseRevision": "reviewed-revision",
                    "stale": False,
                }
            if name == "read_project":
                if args.get("code"):
                    return {"assets": [{"id": "title", "code": cls.code}]}
                return {
                    "timeline": {
                        "width": 1080,
                        "height": 1920,
                        "fps": 30,
                        "items": copy.deepcopy(cls.items),
                    },
                    "mediaPool": {"assets": copy.deepcopy(cls.assets)},
                }
            return super().read(name, args)

    return bench, JournalEditor


def test_authoritative_withdrawal_blocks_effect_even_when_base_cells_are_still_approved(
    tmp_path, monkeypatch
):
    local = tmp_path / "local"
    local.mkdir()
    rows, batch, _, bench, _ = ready(local, monkeypatch)
    authority = tmp_path / "authority"
    shutil.copytree(local, authority)
    remote_bench = Bench(authority)
    remote_bench.data = copy.deepcopy(bench.data)

    def request(root, task, action):
        if root == authority:
            return None
        r, s = runner.check_live(authority, task, remote_bench)
        return {"entity": "director_approval_binding", "binding": runner.approval_binding(r, s)}

    monkeypatch.setattr(runner, "authority_request", request)
    assert runner.check_live(local, "t0", bench)[0]["review_status"] == "approved"
    script_stage.review(
        authority,
        "g",
        rows,
        "t0",
        batch["scripts"][0]["content_sha256"],
        "reject",
        {"id": "synthetic_test_actor"},
    )
    sent = []
    native = Native(tmp_path / "calls", lambda: runner.check_live(local, "t0", bench))
    with pytest.raises(Blocked, match="先审核通过"):
        native.once("tts_or_edit", {}, lambda: sent.append(1))
    assert not sent and bench.data["t0"]["title_style_review_status"] == "approved"


def test_authority_timeout_fails_closed_without_sending_effect(tmp_path, monkeypatch):
    _, _, _, bench, _ = ready(tmp_path, monkeypatch)
    atomic_write(
        tmp_path / "config/director_authority.local.json",
        {"host": "isolated", "workspace": "/isolated"},
    )

    def timeout(*args, **kw):
        raise subprocess.TimeoutExpired("isolated authority read", 45)

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    with pytest.raises(Blocked) as e:
        runner.check_live(tmp_path, "t0", bench)
    assert e.value.code == "RUNNER_AUTHORITY_UNAVAILABLE"


def test_authority_approved_but_different_binding_is_rejected(tmp_path, monkeypatch):
    _, _, _, bench, _ = ready(tmp_path, monkeypatch)
    monkeypatch.setattr(
        runner,
        "authority_request",
        lambda *a: {"entity": "director_approval_binding", "binding": {"task_id": "another"}},
    )
    with pytest.raises(Blocked) as e:
        runner.check_live(tmp_path, "t0", bench)
    assert e.value.code == "RUNNER_AUTHORITY_CHANGED"


def test_render_resume_rechecks_actual_title_and_never_sends_changed_project(tmp_path, monkeypatch):
    bench, native = integration(tmp_path, monkeypatch, pause_render=True)
    cfg = {"paths": {"ffprobe": "isolated"}}
    first = runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    assert first["blocked_code"] == "RUNNER_CONFIRMATION" and first["current_step"] == "render"
    native.items[-1]["props"]["hook"] = "Changed while waiting for confirmation"
    second = runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    assert second["blocked_code"] == "RUNNER_RENDER_READBACK"
    assert not any(name == "submit_render_job" for _, name, _ in native.sent)


@pytest.mark.parametrize("damage", ["stale_status", "missing_revision", "revision_changed"])
def test_render_preflight_rejects_stale_unknown_or_changing_revision(tmp_path, monkeypatch, damage):
    bench, native = integration(tmp_path, monkeypatch, pause_render=True)
    cfg = {"paths": {"ffprobe": "isolated"}}
    runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    original_read = native.read
    reads = []

    def read(self, name, args):
        value = original_read(self, name, args)
        if name == "get_edit_session" and args["editSessionId"].startswith("export_session"):
            reads.append(1)
            if damage == "stale_status":
                value["status"] = "stale"
            elif damage == "missing_revision":
                value.pop("baseRevision")
            elif len(reads) > 1:
                value["baseRevision"] = "external-change"
        return value

    monkeypatch.setattr(native, "read", read)
    second = runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    assert second["blocked_code"] in {"RUNNER_RENDER_REVIEW", "RUNNER_RENDER_READBACK"}
    assert not any(name == "submit_render_job" for _, name, _ in native.sent)


@pytest.mark.parametrize("cut", ["add_0", "add_1", "add_2"])
def test_successful_placement_and_track_receipts_resume_once_with_real_compiler(
    tmp_path, monkeypatch, cut
):
    bench, native = integration(tmp_path, monkeypatch, cut=cut)
    cfg = {"paths": {"ffprobe": "isolated"}}
    first = runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    assert first["blocked_code"] == "RUNNER_EXCEPTION" and first["current_step"] == "add_items"
    second = runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    assert second["status"] == "awaiting_content_review", second
    labels = [label for label, _, _ in native.sent]
    assert all(
        labels.count(label) == 1 for label in ["project", "add_0", "add_1", "add_2", "render"]
    )
    assert len(native.items) == 3


def test_placement_receipt_cannot_adopt_unrelated_items(tmp_path, monkeypatch):
    bench, native = integration(tmp_path, monkeypatch, cut="add_2")
    cfg = {"paths": {"ffprobe": "isolated"}}
    runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    native.items[0]["sourceStartFrame"] = 99
    second = runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    assert second["blocked_code"] == "RUNNER_PLACEMENT_CHANGED"
    assert sum(label == "add_2" for label, _, _ in native.sent) == 1


def test_valid_two_windows_share_one_import_and_complete(tmp_path, monkeypatch):
    bench, native = integration(tmp_path, monkeypatch)
    packet = copy.deepcopy(stage.latest(tmp_path, "t0")["packet"])
    shot = packet["shots"][0]
    packet["shots"] = [
        {**shot, "source_out_frame": 165, "duration_frames": 150},
        {
            **shot,
            "source_in_frame": 165,
            "source_out_frame": 330,
            "timeline_start_frame": 150,
            "duration_frames": 150,
        },
    ]
    approve(tmp_path, packet, bench)
    preflight = runner.media_preflight

    def twice(*a):
        v = preflight(*a)
        return {**v, "items": [v["items"][0], copy.deepcopy(v["items"][0])]}

    monkeypatch.setattr(runner, "media_preflight", twice)
    monkeypatch.setattr(editor, "media_preflight", twice)
    result = runner.run(
        tmp_path,
        "t0",
        {"paths": {"ffprobe": "isolated"}},
        bench,
        native_factory=native,
        budget_seconds=100,
    )
    assert result["status"] == "awaiting_content_review", result
    imports = [args for _, name, args in native.sent if name == "import_media"]
    assert [x["assetType"] for x in imports].count("video") == 1
    video = [x for x in native.items if x["kind"] == "video"]
    assert len(video) == 2 and video[0]["sourceAssetId"] == video[1]["sourceAssetId"]
    assert [x["sourceStartFrame"] for x in video] == [0, 165]


def test_conflicting_copies_of_same_source_are_not_silently_deduplicated():
    with pytest.raises(Blocked):
        runner.unique_media(
            [
                {"source_id": "s", "path": "a", "working_sha256": "x"},
                {"source_id": "s", "path": "b", "working_sha256": "y"},
            ]
        )


@pytest.mark.parametrize("damage", ["replace", "delete", "receipt"])
def test_changed_candidate_invalidates_run_and_read_only_status(tmp_path, monkeypatch, damage):
    bench, native = integration(tmp_path, monkeypatch)
    cfg = {"paths": {"ffprobe": "isolated"}}
    done = runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    assert done["status"] == "awaiting_content_review", done
    candidate = Path(done["candidate"]["path"])
    directory = stage.folder(tmp_path, "t0") / "runs/r0001"
    if damage == "replace":
        candidate.write_bytes(b"changed")
    elif damage == "delete":
        candidate.unlink()
    else:
        atomic_write(directory / "download.json", {"sha256": "different"})
    before = (directory / "state.json").read_bytes()
    status = runner.run_status(tmp_path, "t0")
    assert (
        status["blocked_code"] == "RUNNER_OUTPUT_CHANGED"
        and (directory / "state.json").read_bytes() == before
    )
    resumed = runner.run(tmp_path, "t0", cfg, bench, native_factory=native, budget_seconds=100)
    assert resumed["blocked_code"] == "RUNNER_OUTPUT_CHANGED"
    assert sum(name == "submit_render_job" for _, name, _ in native.sent) == 1
    assert load(directory / "state.json")["artifact_valid"] is False


def test_not_sent_schema_error_can_resume_and_completed_effect_needs_no_connection(tmp_path):
    native = Native(tmp_path, lambda: None)

    class Adapter:
        sent = []

        def rpc(self, method, params):
            self.sent.append((method, params))
            return {"structuredContent": {"id": "project"}}

    adapter = Adapter()
    native.adapter = adapter
    native.schemas = {
        "create_project": {
            "type": "object",
            "properties": {"name": {"type": "integer"}},
            "required": ["name"],
        }
    }
    with pytest.raises(Blocked) as e:
        native.effect("create", "create_project", {"name": "test"})
    assert e.value.code == "RUNNER_TOOL_SCHEMA" and not adapter.sent
    assert load(native.directory / "create.json")["status"] == "not_sent"
    native.schemas["create_project"]["properties"]["name"]["type"] = "string"
    assert native.effect("create", "create_project", {"name": "test"}) == {"id": "project"}
    native.schemas = {}
    assert native.effect("create", "create_project", {"name": "test"}) == {"id": "project"}
    assert len(adapter.sent) == 1


def test_ambiguous_send_failure_stays_unknown_and_never_retries(tmp_path):
    native = Native(tmp_path, lambda: None)

    class Adapter:
        sent = 0

        def rpc(self, *args):
            self.sent += 1
            raise TimeoutError("possibly sent")

    adapter = Adapter()
    native.adapter = adapter
    native.schemas = {"create_project": {"type": "object"}}
    with pytest.raises(TimeoutError):
        native.effect("create", "create_project", {})
    with pytest.raises(Blocked) as e:
        native.effect("create", "create_project", {})
    assert e.value.code == "RUNNER_RESULT_UNKNOWN" and adapter.sent == 1
