"""Observable title review binding and fixed-run order/recovery, without paid calls."""

import copy
import io
import json
from pathlib import Path
import pytest
from mixcut import title_style as style, script_stage, director_stage as stage
from mixcut import director_editor as editor, director_runner as runner
from mixcut.director_native import Native, media_free
from mixcut.core import Blocked, atomic_write, file_hash, load
from test_director_stage import setup, approve, Bench

TEMPLATE = Path(__file__).parents[1] / "config/director_typography_v1.json"


def ready(root, monkeypatch):
    atomic_write(root / "config/director_typography_v1.json", load(TEMPLATE))
    rows, batch, packets = setup(root, monkeypatch)
    bench = Bench(root)
    record = approve(root, packets[0], bench)
    bench.mapping["tables"]["tasks"]["field_ids"].update(
        {k: k for k in {**style.FIELDS, **runner.FIELDS}}
    )
    style.publish(bench, batch["scripts"][0])
    return rows, batch, packets, bench, record


def test_review_snapshot_survives_global_style_changes_and_preserves_exact_title(
    tmp_path, monkeypatch
):
    _, batch, _, bench, record = ready(tmp_path, monkeypatch)
    script = batch["scripts"][0]
    before = style.validate(script)
    atomic_write(
        stage.folder(tmp_path, "t0") / "audio_r0001.json",
        {
            "director_sha256": record["packet_sha256"],
            "status": "ready_for_editing",
            "captions": [],
            "audio": {},
        },
    )
    # Editing uses the reviewed code even after the global template changes.
    global_template = load(TEMPLATE)
    global_template["code"] += "\n// New draft template, not approved for this script"
    atomic_write(tmp_path / "config/director_typography_v1.json", global_template)
    command = editor.template_command(tmp_path, "t0", "real-session")
    assert command["arguments"]["code"] == before["template"]["code"]
    assert {p["key"]: p["defaultValue"] for p in command["arguments"]["properties"]}[
        "hook"
    ] == script["headline"][0]
    assert command["arguments"]["durationInFrames"] == 300
    altered = copy.deepcopy(script)
    altered["title_style"]["template"]["code"] += "change"
    with pytest.raises(Blocked, match="标题样式"):
        style.validate(altered)
    assert bench.data["t0"]["title_style_review_status"] == "approved"


def test_title_edit_only_invalidates_this_item_and_director(tmp_path, monkeypatch):
    rows, batch, packets, bench, _ = ready(tmp_path, monkeypatch)
    approve(tmp_path, packets[1], bench)
    changed = script_stage.edit(
        tmp_path,
        "g",
        rows,
        {"t0": batch["scripts"][0]["narration"]},
        batch["content_sha256"],
        headlines={"t0": ["新标题", "原料", "现做", "果香"]},
    )
    assert changed["scripts"][0]["review_status"] == "pending"
    assert changed["scripts"][1:] == batch["scripts"][1:]
    assert changed["scripts"][0]["narration"] == batch["scripts"][0]["narration"]
    with pytest.raises(Blocked):
        stage.gate(tmp_path, "t0")
    assert stage.gate(tmp_path, "t1")["review_status"] == "approved"


def test_bind_existing_creates_pending_style_revision_and_keeps_original(tmp_path, monkeypatch):
    _, batch, _ = setup(tmp_path, monkeypatch)
    original = (script_stage.folder(tmp_path, "g") / "r0001.json").read_bytes()
    atomic_write(tmp_path / "config/director_typography_v1.json", load(TEMPLATE))
    revised = style.bind_existing(tmp_path, "g")
    assert revised["revision"] == 2 and revised["state"] == "pending"
    assert [s["narration"] for s in revised["scripts"]] == [
        s["narration"] for s in batch["scripts"]
    ]
    assert all(s["approval"] is None for s in revised["scripts"])
    assert (script_stage.folder(tmp_path, "g") / "r0001.json").read_bytes() == original
    assert style.bind_existing(tmp_path, "g") == revised


def test_style_unknown_write_reconciles_without_repeat(tmp_path, monkeypatch):
    _, batch, _, bench, _ = ready(tmp_path, monkeypatch)
    script = copy.deepcopy(batch["scripts"][0])
    script["review_status"] = "pending"
    bench.unknown = True
    with pytest.raises(TimeoutError):
        style.publish(bench, script)
    bench.unknown = False
    monkeypatch.setattr(bench, "patch", lambda *a: pytest.fail("Do not resend a matching write"))
    style.publish(bench, script)
    bench.data["t0"]["title_style_review_status"] = "wrong"
    # Force an unresolved first attempt instead of assuming a write failed.
    for p in (tmp_path / "ledger/title_style_publish").glob("*.json"):
        atomic_write(p, {**load(p), "status": "unknown"})
    with pytest.raises(Blocked, match="先对账"):
        style.publish(bench, script)


def test_journal_unknown_confirmation_and_receipt_tampering(tmp_path):
    native = Native(tmp_path, lambda: None)
    calls = []

    def uncertain():
        calls.append(1)
        raise TimeoutError("unknown")

    with pytest.raises(TimeoutError):
        native.once("render", {"params": 1}, uncertain)
    with pytest.raises(Blocked, match="禁止自动重发"):
        native.once("render", {"params": 1}, uncertain)
    assert len(calls) == 1

    def confirmation():
        raise Blocked("RUNNER_CONFIRMATION", "Editor requires a real confirmation")

    with pytest.raises(Blocked):
        native.once("edit", {"params": 1}, confirmation)
    result = native.once("edit", {"params": 1}, lambda: {"actual_id": "from-real-response"})
    assert native.once("edit", {"params": 1}, lambda: pytest.fail("Already completed")) == result
    path = native.directory / "edit.json"
    value = load(path)
    value["result"]["actual_id"] = "tampered"
    atomic_write(path, value)
    with pytest.raises(Blocked):
        native.completed("edit")
    assert media_free({"type": "image", "data": "encoded"}) == {"type": "image", "media": "omitted"}


class FakeNative:
    """Explicit editor double; it does not prove actual OpenChatCut acceptance."""

    events = []
    fail_readback = False
    project_calls = 0

    def __init__(self, directory, guard):
        self.directory = directory
        self.guard = guard

    def effect(self, label, name, args):
        self.guard()
        self.events.append(name)
        if name == "create_project":
            type(self).project_calls += 1
            return {"id": "test-project"}
        if name == "import_media":
            return {"uploadUrl": "http://127.0.0.1:5199/test"}
        if name == "review_edit_session":
            return {"status": "applied"}
        if name == "submit_render_job":
            return {"renderId": "test-render"}
        return {"ok": True}

    def completed(self, label):
        return None

    def read(self, name, args):
        self.events.append(name)
        if name == "get_edit_session":
            return {
                "status": "drafting"
                if args["editSessionId"].startswith("export_session")
                else "applied",
                "baseRevision": "isolated-reviewed-revision",
            }
        if name == "track_export":
            return {
                "status": "completed",
                "downloadUrl": "/media/uploads/test-output.mp4",
                "sizeBytes": 6,
            }
        return {"timeline": {}, "mediaPool": {"assets": []}}

    def begin(self, state, key):
        return {"editSessionId": key + "_id"}

    def bind(self, project):
        pass

    def connect(self, **kw):
        pass

    def close(self):
        pass

    def upload(self, label, path, slot):
        return {"receipt": "test-only", "assetType": "audio" if path.suffix == ".mp3" else "video"}


def fixed_fixture(root, monkeypatch):
    rows, batch, packets, bench, record = ready(root, monkeypatch)
    video = root / "s0.mp4"
    video.write_bytes(b"video")
    audio = root / "speech.mp3"
    audio.write_bytes(b"voice")
    monkeypatch.setattr(
        runner,
        "media_preflight",
        lambda *a: {
            "status": "passed",
            "items": [{"path": str(video), "source_id": "s0", "working_sha256": file_hash(video)}],
        },
    )

    def voice(*args):
        FakeNative.events.append("TTS")
        return {
            "status": "ready_for_editing",
            "audio": {"path": str(audio), "sha256": file_hash(audio), "duration_frames": 270},
            "captions": [],
        }

    monkeypatch.setattr(runner, "prepare_audio", voice)
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        lambda *a, **kw: json.dumps(
            {"format": {"duration": 10}, "streams": [{"codec_type": "video"}]}
        ),
    )
    commands = [
        {
            "name": "edit_item",
            "arguments": {
                "adds": [{"assetId": "source"}, {"assetId": "voice"}, {"assetId": "title"}]
            },
        }
    ]
    monkeypatch.setattr(runner, "compile_adds", lambda *a, **kw: commands)
    monkeypatch.setattr(
        runner,
        "compile_updates",
        lambda *a: [
            {"name": "edit_item", "arguments": {"updates": [{"speed": 1.1}]}},
            {"name": "edit_item", "arguments": {"updates": [{"durationInFrames": 300}]}},
        ],
    )
    monkeypatch.setattr(
        runner,
        "template_command",
        lambda *a: {"name": "create_motion_graphic_from_code", "arguments": {}},
    )

    def verify(*a, **kw):
        return {"status": "failed" if FakeNative.fail_readback else "passed"}

    monkeypatch.setattr(runner, "verify_native", verify)
    monkeypatch.setattr(
        "mixcut.qc.technical_qc",
        lambda *a: {"status": "passed", "warnings": [], "output_sha256": file_hash(a[0])},
    )
    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"output"))
    FakeNative.events = []
    FakeNative.project_calls = 0
    FakeNative.fail_readback = False
    return bench


def test_fixed_runner_completes_order_and_reuses_audio_project_render(tmp_path, monkeypatch):
    bench = fixed_fixture(tmp_path, monkeypatch)
    result = runner.run(
        tmp_path,
        "t0",
        {"paths": {"ffprobe": "fake"}},
        bench,
        native_factory=FakeNative,
        budget_seconds=100,
    )
    assert result["status"] == "awaiting_content_review"
    assert result["completed"] == list(runner.STEPS)
    assert (
        FakeNative.events.index("TTS")
        < FakeNative.events.index("create_project")
        < FakeNative.events.index("edit_item")
        < FakeNative.events.index("review_edit_session")
        < FakeNative.events.index("submit_render_job")
    )
    calls = list(FakeNative.events)
    resumed = runner.run(tmp_path, "t0", {}, bench, native_factory=FakeNative, budget_seconds=100)
    assert resumed["status"] == "awaiting_content_review" and FakeNative.events == calls
    assert FakeNative.project_calls == 1
    assert load(stage.folder(tmp_path, "t0") / "runs/r0001/state.json")["state_sha256"]


def test_runner_blocks_render_on_readback_mismatch_and_blocks_style_retraction(
    tmp_path, monkeypatch
):
    bench = fixed_fixture(tmp_path, monkeypatch)
    FakeNative.fail_readback = True
    result = runner.run(
        tmp_path,
        "t0",
        {"paths": {"ffprobe": "fake"}},
        bench,
        native_factory=FakeNative,
        budget_seconds=100,
    )
    assert result["blocked_code"] == "RUNNER_READBACK"
    assert (
        "submit_render_job" not in FakeNative.events
        and "review_edit_session" not in FakeNative.events
    )
    bench.data["t0"]["title_style_review_status"] = "pending"
    before = list(FakeNative.events)
    with pytest.raises(Blocked):
        runner.run(tmp_path, "t0", {}, bench, native_factory=FakeNative)
    assert FakeNative.events == before


def test_tampered_state_cannot_skip_to_export(tmp_path, monkeypatch):
    bench = fixed_fixture(tmp_path, monkeypatch)
    runner.run(tmp_path, "t0", {}, bench, native_factory=FakeNative, budget_seconds=0)
    p = stage.folder(tmp_path, "t0") / "runs/r0001/state.json"
    value = load(p)
    value["completed"] = ["preflight", "audio", "readback"]
    atomic_write(p, value)
    with pytest.raises(Blocked, match="状态文件变化"):
        runner.run(tmp_path, "t0", {}, bench, native_factory=FakeNative)


def test_real_compiler_readback_checks_title_code_text_style_and_source(tmp_path, monkeypatch):
    _, batch, _, _, record = ready(tmp_path, monkeypatch)
    audio = {
        "director_sha256": record["packet_sha256"],
        "status": "ready_for_editing",
        "captions": [{"text": "果", "start_frame": 6, "end_frame": 300}],
        "audio": {"duration_frames": 270},
    }
    atomic_write(stage.folder(tmp_path, "t0") / "audio_r0001.json", audio)
    recipe = editor.template_command(tmp_path, "t0", "test")["arguments"]
    items = [
        {
            "kind": "video",
            "sourceAssetId": "video",
            "sourceStartFrame": 0,
            "sourceDurationInFrames": 330,
            "startFrame": 0,
            "durationInFrames": 300,
            "volume": 0,
            "linkStatus": "linked",
        },
        {
            "kind": "audio",
            "volume": 1,
            "sourceAssetId": "voice",
            "startFrame": 6,
            "sourceStartFrame": 0,
            "durationInFrames": 270,
            "linkStatus": "linked",
        },
        {
            "kind": "motion-graphic",
            "sourceAssetId": "title",
            "startFrame": 0,
            "durationInFrames": 300,
            "linkStatus": "linked",
            "props": {p["key"]: p["defaultValue"] for p in recipe["properties"]},
        },
    ]
    native = {"timeline": {"width": 1080, "height": 1920, "fps": 30, "items": items}}
    commands = [
        {"arguments": {"adds": [{"assetId": "video"}, {"assetId": "voice"}, {"assetId": "title"}]}}
    ]
    code = {"assets": [{"id": "title", "code": recipe["code"]}]}
    assert (
        runner.verify_native(tmp_path, "t0", native, commands, code_readback=code)["status"]
        == "passed"
    )
    for mutation in [
        lambda x: x[0].update(sourceStartFrame=1),
        lambda x: x[-1]["props"].update(fontFamily="wrong"),
        lambda x: x[-1]["props"].update(hook="unapproved title"),
        lambda x: x[0].update(filters=["unreviewed effect"]),
    ]:
        altered = copy.deepcopy(native)
        mutation(altered["timeline"]["items"])
        assert (
            runner.verify_native(tmp_path, "t0", altered, commands, code_readback=code)["status"]
            == "failed"
        )
    assert (
        "title_code"
        in runner.verify_native(tmp_path, "t0", native, commands, code_readback={})["differences"]
    )


def test_revoked_review_stops_before_audio(tmp_path, monkeypatch):
    bench = fixed_fixture(tmp_path, monkeypatch)
    original = runner.media_preflight

    def revoke(*args):
        value = original(*args)
        bench.data["t0"]["director_review_status"] = "rejected"
        return value

    monkeypatch.setattr(runner, "media_preflight", revoke)
    result = runner.run(tmp_path, "t0", {}, bench, native_factory=FakeNative, budget_seconds=100)
    assert result["blocked_code"] == "RUNNER_LIVE_REVIEW_CHANGED" and "TTS" not in FakeNative.events


def test_workbench_keeps_execution_progress_on_the_correct_task(tmp_path, monkeypatch):
    from mixcut.workbench import Workbench

    rows, _, _, _, _ = ready(tmp_path, monkeypatch)
    policy = load(tmp_path / "config/script_project_policies/g.json")
    policy["generation_policy_version"] = 3
    atomic_write(tmp_path / "config/script_project_policies/g.json", policy)
    atomic_write(tmp_path / "config/executable_montage_v3.json", {"enabled": True})
    for name, value in [
        ("catalog/clips.json", []),
        ("catalog/sources.json", []),
        ("config/taxonomy.json", {}),
    ]:
        atomic_write(tmp_path / name, value)
    for i, row in enumerate(rows):
        row["fields"].update(
            director_run_status=f"status_{i}", director_run_preview=f"progress_{i}"
        )
    w = Workbench.__new__(Workbench)
    w.root = tmp_path
    w.mapping = {
        "url": "https://example.invalid/base/test",
        "tables": {"tasks": {"table_id": "tasks"}, "clips": {"table_id": "clips"}},
    }
    monkeypatch.setattr(w, "rows", lambda kind="tasks": rows if kind == "tasks" else [])
    result = w.state()
    assert {t["id"]: t["fixedRunPreview"] for t in result["tasks"]} == {
        f"t{i}": f"progress_{i}" for i in range(3)
    }


def test_controller_advances_and_polls_only_known_render_jobs(monkeypatch, tmp_path):
    outcomes = iter(
        [
            {"status": "ready_to_resume"},
            {"status": "blocked", "blocked_code": "RUNNER_RENDER_PENDING"},
            {"status": "awaiting_content_review"},
        ]
    )
    calls = []

    def advance(*args):
        calls.append(1)
        return next(outcomes)

    monkeypatch.setattr(runner.time, "sleep", lambda *args: None)
    assert (
        runner.run_until_pause(tmp_path, "task", {}, None, advance=advance)["status"]
        == "awaiting_content_review"
    )
    assert len(calls) == 3
    for code in ["RUNNER_RESULT_UNKNOWN", "RUNNER_CONFIRMATION", "RUNNER_RENDER_FAILED"]:
        calls = []

        def stop(*args):
            calls.append(1)
            return {"status": "blocked", "blocked_code": code}

        assert (
            runner.run_until_pause(tmp_path, "task", {}, None, advance=stop)["blocked_code"] == code
        )
        assert len(calls) == 1


@pytest.mark.parametrize(
    "result,exit_code",
    [
        ({"status": "awaiting_content_review"}, 0),
        ({"status": "ready_to_resume"}, 3),
        ({"status": "blocked", "blocked_code": "RUNNER_RESULT_UNKNOWN"}, 6),
        ({"status": "blocked", "blocked_code": "RUNNER_RENDER_PENDING"}, 3),
    ],
)
def test_runner_cli_does_not_report_unknown_or_incomplete_as_success(
    monkeypatch, result, exit_code, capsys
):
    import sys
    from mixcut import cli

    monkeypatch.setattr(sys, "argv", ["mixcut", "director", "run", "--task", "test"])
    monkeypatch.setattr(cli, "execute", lambda args: result)
    assert cli.main() == exit_code
    assert json.loads(capsys.readouterr().out) == result
