import copy
import json
from pathlib import Path

import pytest

from mixcut.contracts import content_hash, require_approval
from mixcut.core import Blocked, atomic_write, load, read_jsonl
from mixcut.feishu import CLIP_FIELDS
from mixcut.material_review import (
    MATERIAL_KEYS,
    material_action,
    material_snapshot,
    reconcile_material_reviews,
    material_status,
)

ROOT = Path(__file__).resolve().parents[1]
ACTOR = {"id": "ou_test_real_session", "name": "Verified test actor"}


class Remote:
    def __init__(self, fields):
        self.fields = fields
        self.writes = 0
        self.timeout_after_apply = False
        self.change_after_apply = False

    def __call__(self, args):
        if args[0] == "+record-list":
            keys = list(self.fields)
            return {
                "data": {
                    "field_id_list": keys,
                    "record_id_list": ["rec1"],
                    "data": [[self.fields[k] for k in keys]],
                    "has_more": False,
                }
            }
        assert args[0] == "+record-batch-update"
        self.writes += 1
        data = json.loads(args[args.index("--json") + 1])
        assert data["record_id_list"] == ["rec1"]
        assert not {"content_revision", "review_revision", "review_actor", "review_action"} & set(
            data["patch"]
        )
        self.fields.update(data["patch"])
        if self.change_after_apply:
            self.fields["description"] = "concurrent human edit"
        if self.timeout_after_apply:
            raise TimeoutError("applied, response unknown")
        return {"data": {}}


@pytest.fixture
def setup(tmp_path):
    clip = copy.deepcopy(read_jsonl(ROOT / "examples/clip.fixture.jsonl")[0])
    clip.update(is_fixture=False, approval=None, review_status="pending")
    clip["content_sha256"] = content_hash(clip)
    fields = {key: None for key, _, _, _ in CLIP_FIELDS}
    for key in MATERIAL_KEYS:
        if key in clip:
            fields[key] = clip[key]
    fields.update(source_duration_sec=30, media_ref="local:clip.mp4", comment="existing note")
    for axis, key in (
        ("content", "content_tags"),
        ("action", "action_tags"),
        ("shot_size", "shot_size_tags"),
        ("risk", "risk_tags"),
    ):
        fields[key] = clip["tags"][axis]
    mapping = {
        "base_token": "test",
        "tables": {"clips": {"table_id": "tbl_test", "field_ids": {k: k for k in fields}}},
    }
    atomic_write(tmp_path / "catalog/clips.json", [clip])
    atomic_write(
        tmp_path / "catalog/sources.json", [{"source_id": clip["source_id"], "duration_sec": 30}]
    )
    atomic_write(tmp_path / "config/taxonomy.json", load(ROOT / "config/taxonomy.json"))
    remote = Remote(fields)
    body = {
        "action": "approve",
        "expected": material_snapshot(fields),
        "submission_id": "material_review_request_1",
        "confirmed": True,
        "reason": "",
    }
    return tmp_path, mapping, remote, body


def run(setup, **overrides):
    root, mapping, remote, body = setup
    return material_action(root, mapping, remote, ACTOR, remote.fields["clip_id"], body | overrides)


def test_approve_and_replay_keep_actor_and_hash_without_legacy_columns(setup):
    root, _, remote, _ = setup
    assert run(setup)["status"] == "verified"
    assert run(setup)["status"] == "verified" and remote.writes == 1
    clip = load(root / "catalog/clips.json")[0]
    require_approval(clip)
    assert clip["approval"]["actor_id"] == ACTOR["id"]
    assert clip["approval"]["approved_content_sha256"] == remote.fields["content_sha256"]


def test_user_identity_in_body_cannot_spoof_reviewer(setup):
    root, _, _, _ = setup
    run(setup, actor={"id": "ou_spoof"})
    assert load(root / "catalog/clips.json")[0]["approval"]["actor_id"] == ACTOR["id"]


@pytest.mark.parametrize(
    "change",
    [
        {"description": "changed after display"},
        {"usable_end_sec": 3},
        {"risk_tags": ["uncertain_product"]},
    ],
)
def test_stale_display_blocks_any_write(setup, change):
    remote = setup[2]
    remote.fields.update(change)
    with pytest.raises(Blocked) as exc:
        run(setup)
    assert exc.value.code == "MATERIAL_INPUT_STALE" and remote.writes == 0


@pytest.mark.parametrize(
    "change,code",
    [
        ({"goods_id": None}, "PRODUCT_UNCONFIRMED"),
        ({"risk_tags": ["uncertain_product"]}, "BLOCKING_RISK"),
        ({"usable_end_sec": 31}, "CLIP_RANGE"),
        ({"usable_start_sec": None}, "REVIEW_RANGE_REQUIRED"),
    ],
)
def test_invalid_current_content_cannot_be_approved(setup, change, code):
    remote = setup[2]
    remote.fields.update(change)
    with pytest.raises(Blocked) as exc:
        run(setup, expected=material_snapshot(remote.fields))
    assert exc.value.code == code and remote.writes == 0


def test_reject_preserves_notes_records_reason_and_clears_approval(setup):
    root, _, remote, _ = setup
    run(setup, action="reject", reason="商品归属需要核实")
    clip = load(root / "catalog/clips.json")[0]
    assert clip["review_status"] == "rejected" and clip["approval"] is None
    assert (
        "existing note" in remote.fields["comment"]
        and "商品归属需要核实" in remote.fields["comment"]
    )


def test_confirmation_and_rejection_reason_required(setup):
    with pytest.raises(Blocked, match="明确确认"):
        run(setup, confirmed=False)
    with pytest.raises(Blocked, match="具体原因"):
        run(setup, action="reject")
    assert setup[2].writes == 0


def test_timeout_is_read_reconciled_never_rewritten(setup):
    root, _, remote, _ = setup
    remote.timeout_after_apply = True
    with pytest.raises(TimeoutError):
        run(setup)
    assert load(root / "catalog/clips.json")[0]["approval"] is None
    assert run(setup)["status"] == "verified" and remote.writes == 1


def test_concurrent_change_after_write_does_not_commit_local_approval(setup):
    root, _, remote, _ = setup
    remote.change_after_apply = True
    with pytest.raises(Blocked) as exc:
        run(setup)
    assert exc.value.code == "MATERIAL_REVIEW_PENDING"
    assert load(root / "catalog/clips.json")[0]["approval"] is None
    with pytest.raises(Blocked):
        run(setup)
    assert remote.writes == 1


def test_changed_submission_content_is_not_replayed(setup):
    run(setup)
    with pytest.raises(Blocked) as exc:
        run(setup, reason="different request")
    assert exc.value.code == "SUBMISSION_CONFLICT" and setup[2].writes == 1


def test_sync_reconciles_prior_authorized_write_without_resubmitting(setup):
    root, mapping, remote, _ = setup
    remote.timeout_after_apply = True
    with pytest.raises(TimeoutError):
        run(setup)
    reconcile_material_reviews(root, mapping, remote)
    require_approval(load(root / "catalog/clips.json")[0])
    assert remote.writes == 1


def test_read_only_reconciliation_cannot_create_new_review(setup):
    root, mapping, remote, body = setup
    with pytest.raises(Blocked) as exc:
        material_action(
            root, mapping, remote, ACTOR, remote.fields["clip_id"], body, reconcile_only=True
        )
    assert exc.value.code == "MATERIAL_REVIEW_PENDING" and remote.writes == 0


def test_edited_approved_content_is_shown_stale_before_sync(setup):
    root, _, remote, _ = setup
    run(setup)
    current = load(root / "catalog/clips.json")[0]
    taxonomy = load(root / "config/taxonomy.json")
    assert material_status(remote.fields, current, taxonomy, 30) == "approved"
    remote.fields["description"] = "human edit not synced yet"
    assert material_status(remote.fields, current, taxonomy, 30) == "stale"
    # The read-only display check preserves both the old evidence and table.
    assert load(root / "catalog/clips.json")[0] == current and remote.writes == 1


def test_material_approval_without_legacy_confirmation_field(setup):
    root, mapping, remote, *_ = setup
    clips = load(root / "catalog/clips.json")
    clips[0].pop("rights_confirmed", None)
    clips[0]["content_sha256"] = content_hash(clips[0])
    atomic_write(root / "catalog/clips.json", clips)
    remote.fields["content_sha256"] = clips[0]["content_sha256"]
    remote.fields.pop("rights_confirmed", None)
    result = run(setup, expected=material_snapshot(remote.fields))
    assert result["status"] == "verified"
    assert load(root / "catalog/clips.json")[0]["review_status"] == "approved"
