import copy
import json

import pytest

from mixcut.core import Blocked, atomic_write, digest
from mixcut.script_input import model_prompt, request_from_task_fields
from mixcut.workbench import EXPECTED_KEYS, GROUP_FIELDS, Workbench, task_status


class Remote:
    def __init__(self, mapping):
        self.mapping = mapping
        self.rows = []
        self.creates = 0
        self.unknown = False

    def __call__(self, args):
        if args[0] == "+record-list":
            columns = list(self.mapping["tables"]["tasks"]["field_ids"].values())
            return {
                "data": {
                    "field_id_list": columns,
                    "record_id_list": [r["id"] for r in self.rows],
                    "data": [[r["fields"].get(k) for k in columns] for r in self.rows],
                    "has_more": False,
                }
            }
        if args[0] == "+record-batch-create":
            self.creates += 1
            if self.unknown:
                raise TimeoutError("unknown remote outcome")
            data = json.loads(args[args.index("--json") + 1])
            for vals in data["rows"]:
                self.rows.append(
                    {
                        "id": "rec" + str(len(self.rows)),
                        "fields": dict(zip(data["fields"], vals, strict=True)),
                    }
                )
            return {"data": {}}
        raise AssertionError(args[0])


@pytest.fixture
def app(tmp_path):
    keys = set(GROUP_FIELDS) | EXPECTED_KEYS | {"task_id", "script_generation_status", "created_at"}
    mapping = {
        "base_token": "test",
        "tables": {"tasks": {"table_id": "tbl", "field_ids": {k: k for k in keys}}},
    }
    atomic_write(tmp_path / "config/feishu_mapping.local.json", mapping)
    remote = Remote(mapping)
    return Workbench(tmp_path, call=remote), remote


def brief(**extra):
    return {
        "submission_id": "submission_123",
        "keywords": "测试关键词",
        "seed": "",
        "count": 2,
        "duration": 24,
        "title": "仅测试",
        "strategy": "distinct",
    } | extra


def test_create_one_project_independent_tasks_and_replay(app):
    work, remote = app
    first = work.create_project(brief())
    second = work.create_project(brief())
    assert first["task_ids"] == second["task_ids"]
    assert second["reused"] and remote.creates == 1
    assert len(remote.rows) == 2
    fields = [r["fields"] for r in remote.rows]
    assert fields[0]["variation_angle"] != fields[1]["variation_angle"]
    assert all(f["script_generation_status"] == "not_requested" for f in fields)
    assert all(f.get("generation_request") != "queued" for f in fields)


def test_unknown_create_is_not_retried(app):
    work, remote = app
    remote.unknown = True
    with pytest.raises(TimeoutError):
        work.create_project(brief())
    with pytest.raises(Blocked, match="创建请求已发出"):
        work.create_project(brief())
    assert remote.creates == 1


def test_replay_rejects_changed_content_and_duplicate_key(app):
    work, remote = app
    work.create_project(brief())
    with pytest.raises(Blocked) as exc:
        work.create_project(brief(count=3))
    assert exc.value.code == "SUBMISSION_CONFLICT"
    duplicate = copy.deepcopy(remote.rows[0])
    duplicate["id"] = "rec_duplicate"
    remote.rows.append(duplicate)
    with pytest.raises(Blocked) as exc:
        work.create_project(brief())
    assert exc.value.code == "TASK_NOT_UNIQUE"
    assert remote.creates == 1


def test_existing_row_changed_content_not_success(app):
    work, remote = app
    work.create_project(brief())
    remote.rows[0]["fields"]["creative_keywords"] = "someone edited"
    with pytest.raises(Blocked) as exc:
        work.create_project(brief())
    assert exc.value.code == "SUBMISSION_CONFLICT"


@pytest.mark.parametrize("count", [0, 31, 1.5, True, "2"])
def test_count_boundary(count):
    with pytest.raises(Blocked):
        Workbench.project_input(brief(count=count))


@pytest.mark.parametrize("ordinal", [float("inf"), float("nan"), 0, 31, True, 1.5])
def test_variation_boundary(ordinal):
    with pytest.raises(Blocked) as exc:
        request_from_task_fields(
            {
                "task_id": "T",
                "creative_keywords": "创意",
                "request_group_id": "REQ",
                "script_ordinal": ordinal,
                "variation_strategy": "distinct",
                "variation_angle": "角度",
            }
        )
    assert exc.value.code == "SCRIPT_VARIATION_INVALID"


def test_legacy_input_hash_stable_and_variation_bound():
    fields = {"task_id": "T", "creative_keywords": "创意"}
    legacy = request_from_task_fields(fields)
    assert legacy["request_sha256"] == digest(
        {k: v for k, v in legacy.items() if k != "request_sha256"}
    )
    assert "variation" not in legacy
    new = request_from_task_fields(
        fields
        | {
            "request_group_id": "REQ",
            "script_ordinal": 1,
            "variation_strategy": "distinct",
            "variation_angle": "角度",
        }
    )
    changed = request_from_task_fields(
        fields
        | {
            "request_group_id": "REQ",
            "script_ordinal": 2,
            "variation_strategy": "distinct",
            "variation_angle": "角度二",
        }
    )
    assert len({legacy["request_sha256"], new["request_sha256"], changed["request_sha256"]}) == 3
    assert model_prompt(new)["variation"]["angle"] == "角度"


def test_action_requires_complete_fresh_snapshot_before_auth_or_mutation(app):
    work, remote = app
    first = work.create_project(brief())
    work.auth_user = lambda: pytest.fail("stale action must stop before authentication")
    for expected in ({"script_revision": None}, dict.fromkeys(EXPECTED_KEYS)):
        with pytest.raises(Blocked) as exc:
            work.action(first["task_ids"][0], {"expected": expected, "action": "approve"})
        assert exc.value.code == "INPUT_STALE"
    assert remote.creates == 1


def test_current_delivery_overrides_legacy_generation_pending():
    f = {
        "stage": "delivered",
        "human_review": "approved",
        "final_path": "/final.mp4",
        "script_generation_status": "generated_pending_review",
        "script_review_status": ["approved"],
    }
    assert task_status(f) == "delivered"
    assert task_status(f | {"human_review": "pending"}) != "delivered"


def test_media_traversal_rejected(app):
    work, _ = app
    with pytest.raises(Blocked):
        work.media_path(
            {"task_id": "../secret", "script_revision": 1, "build_id": "build"}, "final"
        )


def test_delayed_readback_only_rereads_never_recreates(app, monkeypatch):
    work, remote = app
    monkeypatch.setattr("mixcut.workbench.time.sleep", lambda _: None)
    original = work.rows
    reads = 0

    def delayed(kind="tasks"):
        nonlocal reads
        reads += 1
        # Preflight and first post-write read miss the new rows.
        return [] if reads <= 2 else original(kind)

    work.rows = delayed
    assert work.create_project(brief())["status"] == "verified"
    assert remote.creates == 1 and reads == 3


def test_issued_schema_create_is_not_repeated_on_stale_read(app):
    work, _ = app
    creates = []

    def call(args):
        if args[0] == "+field-list":
            return {"data": {"fields": []}}
        if args[0] == "+field-create":
            payload = json.loads(args[args.index("--json") + 1])
            assert isinstance(payload, dict)
            creates.append(payload)
            return {"data": {}}
        raise AssertionError(args[0])

    work.call = call
    for _ in range(2):
        with pytest.raises(Blocked) as exc:
            work.ensure_schema()
        assert exc.value.code == "SCHEMA_PENDING_READBACK"
    assert len(creates) == 5


def test_delivery_script_is_readonly_before_auth(app):
    work, remote = app
    result = work.create_project(brief())
    remote.rows[0]["fields"]["stage"] = "delivered"
    f = work.task(result["task_ids"][0])["fields"]
    work.auth_user = lambda: pytest.fail("locked stage must stop before auth")
    with pytest.raises(Blocked) as exc:
        work.action(
            result["task_ids"][0],
            {"action": "approve", "expected": {k: f.get(k) for k in EXPECTED_KEYS}},
        )
    assert exc.value.code == "TASK_SCRIPT_LOCKED"


def test_project_copy_mode_persisted_without_new_feishu_columns(app):
    from mixcut.core import load

    work, remote = app
    result = work.create_project(brief(narration_mode="material_based"))
    policy = load(work.root / "config/script_project_policies" / f"{result['group_id']}.json")
    assert policy["narration_mode"] == "material_based"
    assert work.create_project(brief(narration_mode="material_based"))["reused"]
    with pytest.raises(Blocked, match="同一提交标识"):
        work.create_project(brief(narration_mode="sales_independent"))
    assert remote.creates == 1


def test_cli_delivery_needs_matching_archive_approval_and_attachment(tmp_path):
    import json
    from mixcut.core import atomic_write, file_hash
    from mixcut.workbench import verified_cli_delivery

    task = "VIDEO_A"
    d = tmp_path / "jobs/cli_delivery" / task / "r0001"
    d.mkdir(parents=True)
    final = d / "final.mp4"
    final.write_bytes(b"video")
    sha = file_hash(final)
    receipt = {
        "status": "archived",
        "review_source": "user_chat",
        "qc_status": "passed",
        "task_id": task,
        "director_sha256": "director-hash",
        "candidate_sha256": sha,
        "attachment_token": "file-1",
    }
    atomic_write(d / "receipt.json", receipt)
    director = {"revision": 1, "review_status": "approved", "packet_sha256": "director-hash"}
    fields = {
        "director_run_status": "delivered_cli",
        "director_run_preview": json.dumps({"candidate_sha256": sha}),
        "candidate_preview": [{"file_token": "file-1"}],
    }
    assert verified_cli_delivery(tmp_path, task, director, fields)
    fields["candidate_preview"] = [{"file_token": "different"}]
    assert not verified_cli_delivery(tmp_path, task, director, fields)
    fields["candidate_preview"] = [{"file_token": "file-1"}]
    final.write_bytes(b"replaced")
    assert not verified_cli_delivery(tmp_path, task, director, fields)


def test_cli_delivered_task_rejects_mutation_before_any_review(app, monkeypatch):
    from mixcut.core import Blocked

    app, _ = app

    monkeypatch.setattr(
        app, "task", lambda task: {"fields": {"director_run_status": "delivered_cli"}}
    )
    for action in (
        "director_approve",
        "director_reject",
        "save_copy",
        "generate",
        "approve",
        "reject",
    ):
        with pytest.raises(Blocked, match="已交付"):
            app.action("VIDEO_A", {"action": action})
