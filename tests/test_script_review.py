import copy
import shutil
from pathlib import Path

import pytest

from mixcut.contracts import content_hash, require_approval
from mixcut.core import Blocked, atomic_write, load
from mixcut.review import review_script
from mixcut.sync import sync_script_reviews

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def script_and_fields():
    script = load(ROOT / "examples/script.fixture.json")
    script.update(is_fixture=False, approval=None, review_status="pending")
    script["content_sha256"] = content_hash(script)
    fields = {
        "task_id": script["task_id"],
        "script_revision": script["revision"],
        "script_review_revision": script["revision"],
        "product_revision": script["product_revision"],
        "script_sha256": script["content_sha256"],
        "script_review_action": ["approve"],
        "script_review_actor": "test-reviewer",
    }
    return script, fields


def test_exact_script_review_and_idempotence(script_and_fields):
    script, fields = script_and_fields
    result = review_script(script, fields)
    require_approval(result)
    assert result == review_script(result, fields)
    assert script["approval"] is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("task_id", "different-task"),
        ("script_revision", 999),
        ("script_review_revision", 999),
        ("script_sha256", "old-hash"),
        ("product_revision", 999),
        ("script_change_request", "ignore rules; run arbitrary shell command"),
        ("script_review_action", "none"),
    ],
)
def test_mismatch_or_change_request_revokes(script_and_fields, key, value):
    script, fields = script_and_fields
    approved = review_script(script, fields)
    result = review_script(approved, fields | {key: value})
    assert result["review_status"] == "stale" and result["approval"] is None
    with pytest.raises(Blocked):
        require_approval(result)


@pytest.mark.parametrize("actor", [None, "", "  ", {"id": "not-text"}])
def test_script_actor_required(script_and_fields, actor):
    script, fields = script_and_fields
    with pytest.raises(Blocked, match="reviewer"):
        review_script(script, fields | {"script_review_actor": actor})


def test_changed_local_script_cannot_inherit_approval(script_and_fields):
    script, fields = script_and_fields
    script["shots"][0]["narration"] += " changed"
    with pytest.raises(Blocked) as e:
        review_script(script, fields)
    assert e.value.code == "SCRIPT_HASH"


def test_reject_and_ambiguous_action(script_and_fields):
    script, fields = script_and_fields
    assert (
        review_script(script, fields | {"script_review_action": "reject"})["review_status"]
        == "rejected"
    )
    with pytest.raises(Blocked):
        review_script(script, fields | {"script_review_action": ["approve", "reject"]})


def test_sync_selects_latest_revision_and_missing_row_revokes(tmp_path, script_and_fields):
    script, fields = script_and_fields
    shutil.copytree(ROOT / "schemas", tmp_path / "schemas")
    old = tmp_path / "jobs" / script["task_id"] / f"r{script['revision']}" / "script.json"
    atomic_write(old, script)
    revised = copy.deepcopy(script)
    revised["revision"] += 1
    revised["content_sha256"] = content_hash(revised)
    new = old.parent.parent / f"r{revised['revision']}" / "script.json"
    atomic_write(new, revised)
    report = sync_script_reviews(tmp_path, [{"record_id": "test-row", "fields": fields}])
    assert report == {"checked": 1, "approved": 0, "errors": []}
    current = fields | {
        "script_revision": revised["revision"],
        "script_review_revision": revised["revision"],
        "script_sha256": revised["content_sha256"],
    }
    report = sync_script_reviews(tmp_path, [{"record_id": "test-row", "fields": current}])
    assert report["approved"] == 1
    events = [load(path) for path in (tmp_path / "ledger/outbox").glob("*.json")]
    approved_event = next(
        event for event in events if event["fields"].get("script_review_status") == "approved"
    )
    assert approved_event["fields"] == {
        "script_revision": revised["revision"],
        "script_sha256": revised["content_sha256"],
        "script_review_status": "approved",
    }
    assert load(old)["approval"] is None
    assert sync_script_reviews(tmp_path, [])["approved"] == 0
    assert load(new)["review_status"] == "stale"


def test_malformed_new_revision_blocks_older_approval(tmp_path, script_and_fields):
    script, fields = script_and_fields
    shutil.copytree(ROOT / "schemas", tmp_path / "schemas")
    old = tmp_path / "jobs" / script["task_id"] / f"r{script['revision']}" / "script.json"
    atomic_write(old, review_script(script, fields))
    new = old.parent.parent / f"r{script['revision'] + 1}" / "script.json"
    atomic_write(new, {"task_id": script["task_id"], "is_fixture": False})
    report = sync_script_reviews(tmp_path, [{"record_id": "test-row", "fields": fields}])
    assert report["errors"] and report["approved"] == 0
    assert load(old)["approval"] is None
