import copy
import os
import subprocess
import sys
from pathlib import Path
import pytest
from mixcut.core import (
    Blocked,
    atomic_write,
    load,
    file_hash,
    resolve_media,
    writer_lock,
    read_jsonl,
)
from mixcut.contracts import content_hash, require_approval, inward_frames, validate_plan, plan_hash
from mixcut.review import review_clip
from mixcut.matcher import match, validate_script
from mixcut.feishu import pull_rows, index_rows, program_patch, push_outbox, enqueue, cell_equal
from mixcut.ledger import reserve, counts, commit_usage, event_key
from mixcut.analyze import cloud_gate, validate_proposal, analysis_key
from mixcut.engine import verify_readback

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bundle():
    return {
        k: load(ROOT / f"examples/{k}.fixture.json")
        for k in ["script", "product", "taxonomy", "edit_plan"]
    } | {k: read_jsonl(ROOT / f"examples/{k}.fixture.jsonl") for k in ["clip", "source"]}


def seal(record):
    record["content_sha256"] = content_hash(record)
    if record.get("approval"):
        record["approval"]["approved_content_sha256"] = record["content_sha256"]
    return record


def test_path_escape_and_symlink(tmp_path):
    root = tmp_path / "authorized"
    root.mkdir()
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"keep")
    (root / "link.mp4").symlink_to(outside)
    for rel in ["../secret.mp4", "link.mp4", str(outside)]:
        with pytest.raises(Blocked):
            resolve_media([{"root_id": "r", "path": str(root)}], "r", rel)
    assert outside.read_bytes() == b"keep"


def test_atomic_preserves_old_on_invalid_json(tmp_path):
    p = tmp_path / "state.json"
    atomic_write(p, {"a": 1})
    with pytest.raises(ValueError):
        atomic_write(p, {"a": float("nan")})
    assert load(p) == {"a": 1}


def test_jsonl_damage_preserved(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text('{"a":1}\n{"partial":')
    old = p.read_bytes()
    with pytest.raises(Blocked, match="original preserved"):
        read_jsonl(p)
    assert p.read_bytes() == old


def test_two_process_lock(tmp_path):
    code = (
        "from pathlib import Path;from mixcut.core import writer_lock;\nwith writer_lock(Path("
        + repr(str(tmp_path))
        + ")): pass"
    )
    with writer_lock(tmp_path):
        p = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
        )
    assert p.returncode != 0 and "本次尚未执行" in p.stderr
    with writer_lock(tmp_path):
        pass


def test_fractional_inward_rounding():
    assert inward_frames("0.1000000001", "1.9999999999") == (4, 59)
    with pytest.raises(Blocked):
        inward_frames("NaN", 2)


def test_approval_content_change_invalidates(bundle):
    clip = bundle["clip"][0]
    require_approval(clip, False)
    clip["description"] += "change"
    with pytest.raises(Blocked) as e:
        require_approval(clip, False)
    assert e.value.code == "APPROVAL_STALE"


def test_sticky_approve_does_not_approve_new_revision(bundle):
    c = bundle["clip"][0]
    r = review_clip(
        c,
        {"description": "human changed"},
        {"action": "approve", "revision": 1, "actor": "real-human"},
        bundle["taxonomy"],
        10,
    )
    assert r["revision"] == 2 and r["approval"] is None and r["review_status"] == "stale"
    r = review_clip(
        r, {}, {"action": "approve", "revision": 2, "actor": "real-human"}, bundle["taxonomy"], 10
    )
    assert r["review_status"] == "approved"


def test_fixture_cannot_produce(bundle):
    with pytest.raises(Blocked) as e:
        require_approval(bundle["script"])
    assert e.value.code == "FIXTURE_BLOCKED"


def test_match_deterministic_and_order(bundle):
    sources = {s["source_id"]: s for s in bundle["source"]}
    a = match(bundle["script"], bundle["clip"], bundle["taxonomy"], sources, production=False)
    b = match(
        bundle["script"],
        list(reversed(bundle["clip"])),
        bundle["taxonomy"],
        sources,
        production=False,
    )
    assert a["assignments"] == b["assignments"]
    assert [x["timeline_start_frame"] for x in a["assignments"]] == [0, 120, 270, 450, 600]


@pytest.mark.parametrize(
    "change",
    [
        {"goods_id": "other"},
        {"brand_id": "other"},
        {"review_status": "pending"},
        {"usable_end_sec": 3.0},
    ],
)
def test_matching_hard_filters(bundle, change):
    clips = bundle["clip"]
    clips[0].update(change)
    seal(clips[0])
    with pytest.raises(Blocked, match="no legal assignment"):
        match(
            bundle["script"],
            clips,
            bundle["taxonomy"],
            {s["source_id"]: s for s in bundle["source"]},
            production=False,
        )


def test_match_no_overlapping_source(bundle):
    clips = bundle["clip"]
    clips[1]["source_id"] = clips[0]["source_id"]
    seal(clips[1])
    with pytest.raises(Blocked):
        match(
            bundle["script"],
            clips,
            bundle["taxonomy"],
            {s["source_id"]: s for s in bundle["source"]},
            production=False,
        )


def test_expired_claim_uses_current_clock(bundle):
    p = bundle["product"]
    p["claims"][0]["valid_until"] = "2000-01-01T00:00:00Z"
    seal(p)
    s = bundle["script"]
    s["shots"][0]["claim_ids"] = [p["claims"][0]["claim_id"]]
    seal(s)
    with pytest.raises(Blocked) as e:
        validate_script(s, p, bundle["taxonomy"], False)
    assert e.value.code == "FACT_EXPIRED"


@pytest.mark.parametrize("kind", ["gap", "caption", "audio", "hash"])
def test_plan_tampering(bundle, kind):
    p = bundle["edit_plan"]
    if kind == "gap":
        p["shots"][1]["timeline_start_frame"] += 1
    if kind == "caption":
        p["captions"][0]["end_frame"] = 121
    if kind == "audio":
        p["shots"][0]["source_audio_gain"] = 1
    if kind != "hash":
        p["plan_sha256"] = plan_hash(p)
        p["build_id"] = "b_" + p["plan_sha256"][:16]
    else:
        p["shots"][0]["source_in_frame"] += 1
    with pytest.raises(Blocked):
        validate_plan(p, ROOT, False)


def test_readback_requires_actual_values(bundle):
    p = bundle["edit_plan"]
    a = copy.deepcopy(p)
    a["shots"][0]["source_in_frame"] = 0
    assert verify_readback(p, a)["status"] == "failed"


def test_feishu_multi_page_and_single_select():
    data = [
        {
            "data": {
                "field_id_list": ["fld"],
                "record_id_list": ["r1", "r2"],
                "data": [["one"], ["two"]],
                "has_more": True,
            }
        },
        {
            "data": {
                "field_id_list": ["fld"],
                "record_id_list": ["r3"],
                "data": [["three"]],
                "has_more": False,
            }
        },
    ]
    calls = []

    def api(args):
        calls.append(args)
        return data.pop(0)

    rows = pull_rows(api, "base", "table", 2)
    assert len(rows) == 3 and calls[1][calls[1].index("--offset") + 1] == "2"
    assert cell_equal(["pending"], "pending") and not cell_equal(["pending", "approved"], "pending")


def test_duplicate_remote_key_conflict():
    with pytest.raises(Blocked):
        index_rows([{"fields": {"id": "x"}}, {"fields": {"id": "x"}}], "id")


def test_human_fields_protected():
    with pytest.raises(Blocked):
        program_patch({"goods_id": "x"}, "clips")
    assert program_patch({"ai_proposal": "suggestion"}, "clips") == {"ai_proposal": "suggestion"}


def test_new_complete_patch_supersedes_unsynced_prior_patch(tmp_path):
    old_key = enqueue(
        tmp_path,
        "tasks",
        "TASK_1",
        {"stage": "old", "material_index_preview": "old"},
        supersede_group="material_index",
    )
    new_key = enqueue(
        tmp_path,
        "tasks",
        "TASK_1",
        {"stage": "new", "material_index_preview": "new", "error_summary": "current"},
        supersede_group="material_index",
    )
    old = load(tmp_path / "ledger/outbox" / f"{old_key}.json")
    new = load(tmp_path / "ledger/outbox" / f"{new_key}.json")
    assert old["status"] == "superseded" and old["superseded_by"] == new_key
    assert new["status"] == "pending"


def test_production_state_supersedes_unfinished_index_and_plan_events(tmp_path):
    index_key = enqueue(
        tmp_path,
        "tasks",
        "TASK_1",
        {"stage": "ready_for_edit_plan", "material_index_preview": "index"},
        supersede_group="material_index",
    )
    plan_key = enqueue(
        tmp_path,
        "tasks",
        "TASK_1",
        {"stage": "ready_for_openchatcut_import", "plan_preview": "old-plan"},
        supersede_group="edit_plan",
    )
    plan_path = tmp_path / "ledger/outbox" / f"{plan_key}.json"
    plan = load(plan_path)
    plan["status"] = "acknowledged"
    atomic_write(plan_path, plan)
    current_key = enqueue(
        tmp_path,
        "tasks",
        "TASK_1",
        {
            "stage": "ready_for_openchatcut_import",
            "material_index_preview": "index",
            "plan_preview": "current-plan",
        },
        supersede_group="production_state",
        supersede_groups={"material_index", "edit_plan", "production_state"},
    )
    assert load(tmp_path / "ledger/outbox" / f"{index_key}.json")["status"] == "superseded"
    assert load(plan_path)["status"] == "superseded"
    assert load(tmp_path / "ledger/outbox" / f"{current_key}.json")["status"] == "pending"


def test_unknown_create_reconciled_without_duplicate(tmp_path):
    mapping = {
        "base_token": "resource_id",
        "tables": {
            "clips": {"table_id": "tbl", "field_ids": {"clip_id": "id", "review_status": "status"}}
        },
    }
    enqueue(tmp_path, "clips", "C1", {"clip_id": "C1", "review_status": "pending"})
    rows = []
    creates = 0

    def api(args):
        nonlocal creates
        if args[0] == "+record-list":
            return {
                "data": {
                    "field_id_list": ["id", "status"],
                    "record_id_list": ["r1"] if rows else [],
                    "data": rows,
                    "has_more": False,
                }
            }
        if args[0] == "+record-batch-create":
            creates += 1
            rows.append(["C1", ["pending"]])
            raise Blocked("SYNC_UNKNOWN", "timeout after commit", 5)
        raise AssertionError("Unexpected write")

    with pytest.raises(Blocked):
        push_outbox(tmp_path, mapping, api)
    push_outbox(tmp_path, mapping, api)
    assert creates == 1
    assert load(next((tmp_path / "ledger/outbox").glob("*.json")))["status"] == "synced"


def test_ledger_event_first_crash_no_double_count(tmp_path):
    reserve(tmp_path, "batch", "task", 1, "plan", ["c1"])
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"simulated-QC-input")
    # This unit test injects QC, not a real video or real-media acceptance.
    key = event_key("task", 1, "c1")
    atomic_write(tmp_path / "ledger/events" / f"{key}.json", {"clip_id": "c1", "builds": {}})
    assert counts(tmp_path, True) == {"c1": 1}
    commit_usage(
        tmp_path,
        "task",
        1,
        "build",
        ["c1"],
        candidate,
        {"status": "passed", "output_sha256": file_hash(candidate)},
    )
    commit_usage(
        tmp_path,
        "task",
        1,
        "build2",
        ["c1"],
        candidate,
        {"status": "passed", "output_sha256": file_hash(candidate)},
    )
    assert counts(tmp_path, True) == {"c1": 1}


def test_cloud_gate_never_requests_without_permission():
    c = load(ROOT / "config/config.example.json")
    for g1 in [False, True]:
        with pytest.raises(Blocked):
            cloud_gate(c, g1, 0, 3)


def test_analysis_invalid_tag_and_nonfinite(bundle):
    p = {
        "description": "a",
        "tags": copy.deepcopy(bundle["clip"][0]["tags"]),
        "start_sec": 0,
        "end_sec": 2,
        "evidence_times": [1],
        "product_uncertain": False,
    }
    validate_proposal(p, bundle["taxonomy"], 10)
    p["tags"]["content"] = ["invented"]
    with pytest.raises(Blocked):
        validate_proposal(p, bundle["taxonomy"], 10)
    p["start_sec"] = float("nan")
    with pytest.raises(ValueError):
        validate_proposal(p, bundle["taxonomy"], 10)


def test_cache_versions_are_part_of_key():
    assert analysis_key("h", [0, 1], {}, "m", "p1", {}, {}) != analysis_key(
        "h", [0, 1], {}, "m", "p2", {}, {}
    )


def test_unknown_create_absent_never_retried(tmp_path):
    mapping = {
        "base_token": "resource_id",
        "tables": {"clips": {"table_id": "tbl", "field_ids": {"clip_id": "id"}}},
    }
    key = enqueue(tmp_path, "clips", "C1", {"clip_id": "C1"})
    p = tmp_path / "ledger/outbox" / f"{key}.json"
    event = load(p)
    event["status"] = "unknown"
    atomic_write(p, event)
    calls = []

    def api(args):
        calls.append(args)
        return {
            "data": {"field_id_list": ["id"], "record_id_list": [], "data": [], "has_more": False}
        }

    with pytest.raises(Blocked) as e:
        push_outbox(tmp_path, mapping, api)
    assert e.value.code == "SYNC_RECONCILE_REQUIRED"
    assert all(a[0] == "+record-list" for a in calls)


def test_write_ack_is_not_readback(tmp_path):
    mapping = {
        "base_token": "resource_id",
        "tables": {"clips": {"table_id": "tbl", "field_ids": {"clip_id": "id"}}},
    }
    key = enqueue(tmp_path, "clips", "C1", {"clip_id": "C1"})
    written = False

    def api(args):
        nonlocal written
        if args[0] == "+record-list":
            return {
                "data": {
                    "field_id_list": ["id"],
                    "record_id_list": [],
                    "data": [],
                    "has_more": False,
                }
            }
        written = True
        return {"ok": True}

    result = push_outbox(tmp_path, mapping, api)
    assert written and result["awaiting_readback_event_ids"] == [key]
    assert load(tmp_path / "ledger/outbox" / f"{key}.json")["status"] == "acknowledged"


def test_http_success_business_failure(monkeypatch, tmp_path):
    from mixcut.feishu import invoke

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, stdout='{"code":99991672,"msg":"permission denied"}', stderr=""
        ),
    )
    with pytest.raises(Blocked) as e:
        invoke(["+record-list"], tmp_path)
    assert e.value.code == "SYNC_FAILED"


@pytest.mark.skipif(not (ROOT / "test_media/g1_v1/A.mp4").is_file(), reason="可选本地G1媒体未配置")
def test_ingest_duplicates_unicode_and_corruption(tmp_path):
    from mixcut.ingest import scan
    import shutil

    media = tmp_path / "input"
    media.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shutil.copytree(ROOT / "schemas", workspace / "schemas")
    shutil.copy(ROOT / "test_media/g1_v1/A.mp4", media / "中文 空格.mp4")
    shutil.copy(ROOT / "test_media/g1_v1/A.mp4", media / "same.mov")
    (media / "broken.mp4").write_bytes(b"invalid-media")
    (media / "._same.mov").write_bytes(b"macos-resource-fork")
    c = load(ROOT / "config/config.local.json")
    c["source_roots"] = [{"root_id": "r", "path": str(media)}]
    c["ingest"]["stable_interval_sec"] = 0.01
    original = {p.name: file_hash(p) for p in media.iterdir()}
    r = scan(workspace, c, "r", 10)
    sources = load(workspace / "catalog/sources.json")
    assert len(sources) == 1 and len(sources[0]["locations"]) == 2
    assert len(r["errors"]) == 1 and r["errors"][0]["code"] == "CORRUPT_MEDIA"
    assert {p.name: file_hash(p) for p in media.iterdir()} == original


@pytest.mark.skipif(not (ROOT / "test_media/g1_v1/A.mp4").is_file(), reason="可选本地G1媒体未配置")
def test_file_being_copied_is_not_ingested(tmp_path, monkeypatch):
    from mixcut.ingest import scan
    import mixcut.ingest as module

    media = tmp_path / "input"
    media.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = media / "copying.mp4"
    path.write_bytes(b"part")
    c = load(ROOT / "config/config.local.json")
    c["source_roots"] = [{"root_id": "r", "path": str(media)}]
    monkeypatch.setattr(
        module.time, "sleep", lambda _: path.write_bytes(path.read_bytes() + b"part")
    )
    result = scan(workspace, c, "r", 1)
    assert result["added"] == [] and result["errors"][0]["code"] == "FILE_UNSTABLE"


def test_serial_ten_task_mock_resume_does_not_rerender(tmp_path):
    from mixcut.batch import run_serial

    calls = []

    def fake_render(task):
        calls.append(task)
        if task == "task3":
            raise Blocked("INSUFFICIENT_MATERIALS", "fixture failure")
        p = tmp_path / f"{task}.mp4"
        p.write_bytes(b"UNIT TEST NOT REAL VIDEO")
        return {
            "output_path": str(p),
            "output_sha256": file_hash(p),
            "render_status": "succeeded",
            "qc_status": "passed",
            "sync_status": "failed",
        }

    tasks = [f"task{i}" for i in range(10)]
    first = run_serial(tmp_path, "mock_batch", tasks, fake_render, min_free_bytes=0)
    second = run_serial(tmp_path, "mock_batch", tasks, fake_render, min_free_bytes=0)
    assert len(calls) == 10 and first["technical_success"] == 9 and not first["complete"]
    assert second["technical_success"] == 9 and second["human_approved"] == 0


def test_serial_crash_remains_unknown_no_automatic_repeat(tmp_path):
    from mixcut.batch import run_serial

    def crash(task):
        raise RuntimeError("crash after edit submission")

    with pytest.raises(RuntimeError):
        run_serial(tmp_path, "mock_crash", ["task"], crash, min_free_bytes=0)
    state = load(tmp_path / "batches/mock_crash/tasks/task.json")
    assert state["status"] == "needs_reconcile"

    def forbidden(task):
        raise AssertionError("Must not repeat unknown submission")

    result = run_serial(tmp_path, "mock_crash", ["task"], forbidden, min_free_bytes=0)
    assert result["tasks"][0]["status"] == "needs_reconcile"


def test_path_component_id_rejects_external_instructions():
    from mixcut.core import safe_id

    for value in ["../../output", "foo/bar", "$(curl evil)", "", "x\\y"]:
        with pytest.raises(Blocked):
            safe_id(value)


@pytest.mark.skipif(not (ROOT / "test_media/g1_v1/A.mp4").is_file(), reason="可选本地G1媒体未配置")
def test_g1_prepare_resume_preserves_existing_state(tmp_path):
    from mixcut.g1 import prepare
    import shutil

    shutil.copytree(ROOT / "schemas", tmp_path / "schemas")
    receipt = load(ROOT / "reports/g1/preparation.json")
    job = tmp_path / "jobs" / "g1"
    job.mkdir(parents=True)
    plan = load(Path(receipt["plan"]))
    atomic_write(job / "edit_plan.json", plan)
    state = {"render_status": "unknown", "human_review": "pending", "marker": "preserve"}
    atomic_write(job / "state.json", state)
    receipt["plan"] = str(job / "edit_plan.json")
    receipt["job_dir"] = str(job)
    atomic_write(tmp_path / "reports/g1/preparation.json", receipt)
    result = prepare(tmp_path, load(ROOT / "config/config.local.json"))
    assert result["reused_existing"] is True
    assert load(job / "state.json") == state
    assert not (tmp_path / "test_media").exists()


@pytest.mark.skipif(not (ROOT / "test_media/g1_v1/A.mp4").is_file(), reason="可选本地G1媒体未配置")
def test_g1_real_source_markers_and_audio_tone_detection():
    from mixcut.g1_verify import read_markers, verify_audio

    c = load(ROOT / "config/config.local.json")
    ffmpeg = c["paths"]["ffmpeg"]
    for letter in "ABCDE":
        rows = read_markers(ROOT / f"test_media/g1_v1/{letter}.mp4", ffmpeg, [0, 60, 179, 299])
        assert [(r["source_letter"], r["source_frame"]) for r in rows] == [
            (letter, n) for n in [0, 60, 179, 299]
        ]
    good = verify_audio(ROOT / "test_media/g1_v1/TEST_440Hz.wav", ffmpeg)
    assert good["status"] == "passed"
    wrong = verify_audio(ROOT / "test_media/g1_v1/A.mp4", ffmpeg, expected_seconds=10)
    assert wrong["status"] == "failed"


def test_interactive_writer_waits_for_existing_writer_and_keeps_exclusion(tmp_path):
    import threading
    import time

    acquired = threading.Event()
    release = threading.Event()

    def owner():
        with writer_lock(tmp_path):
            acquired.set()
            release.wait(2)

    thread = threading.Thread(target=owner)
    thread.start()
    assert acquired.wait(2)

    def later():
        time.sleep(0.1)
        release.set()

    timer = threading.Thread(target=later)
    timer.start()
    with writer_lock(tmp_path, wait_seconds=1):
        assert release.is_set()
    thread.join()
    timer.join()


def test_scoped_outbox_preserves_unselected_events_without_accessing_their_table(tmp_path):
    selected = enqueue(tmp_path, 'tasks', 'TASK1', {'stage': 'waiting_final_review'})
    untouched = enqueue(tmp_path, 'clips', 'CLIP1', {'clip_id': 'CLIP1'})
    mapping = {'base_token': 'base', 'tables': {
        'tasks': {'table_id': 'tasks', 'field_ids': {'task_id': 'id', 'stage': 'stage'}}
    }}
    writes = []
    def api(args):
        if args[0] == '+record-list':
            return {'data': {'field_id_list': ['id', 'stage'], 'record_id_list': ['R1'],
                             'data': [['TASK1', 'ready']], 'has_more': False}}
        writes.append(args)
        return {'ok': True}
    result = push_outbox(tmp_path, mapping, api, event_ids={selected})
    assert len(writes) == 1
    assert result['awaiting_readback_event_ids'] == [selected]
    assert load(tmp_path / 'ledger/outbox' / f'{untouched}.json')['status'] == 'pending'
    assert push_outbox(tmp_path, {}, lambda _: pytest.fail('Unexpected API'), event_ids=set()) == {
        'synced_event_ids': [], 'awaiting_readback_event_ids': []}


def test_legacy_confirmation_value_does_not_filter_matching(bundle):
    for clip in bundle["clip"]:
        clip["rights_confirmed"] = False
        seal(clip)
    result = match(bundle["script"], bundle["clip"], bundle["taxonomy"],
                   {s["source_id"]: s for s in bundle["source"]}, production=False)
    assert len(result["assignments"]) == len(bundle["script"]["shots"])
