from __future__ import annotations
import copy
from .contracts import content_hash, validate_tags
from .core import Blocked, now, digest

CLIP_HUMAN = {
    "brand_id",
    "goods_id",
    "source_type",
    "tags",
    "usable_start_sec",
    "usable_end_sec",
    "description",
}


def review_clip(
    current: dict, effective: dict, decision: dict, taxonomy: dict, duration: float
) -> dict:
    unknown = set(effective) - CLIP_HUMAN
    if unknown:
        raise Blocked("REVIEW_FIELDS", "Unknown human semantic fields: " + ",".join(unknown), 2)
    record = copy.deepcopy(current)
    changed = any(record.get(k) != v for k, v in effective.items())
    record.update(effective)
    validate_tags(record["tags"], taxonomy)
    if not 0 <= record["usable_start_sec"] < record["usable_end_sec"] <= duration:
        raise Blocked("CLIP_RANGE", "Human interval exceeds source duration", 2)
    if changed:
        record["revision"] += 1
        record["review_status"] = "stale" if record.get("approval") else "pending"
        record["approval"] = None
    record["content_sha256"] = content_hash(record)
    action = decision.get("action", "none")
    if action == "none":
        return record
    if decision.get("revision") != record["revision"]:
        return record  # Sticky old approve never approves changed content.
    if not decision.get("actor"):
        raise Blocked("REVIEW_ACTOR_REQUIRED", "Human identity must be present")
    if action == "reject":
        record["review_status"] = "rejected"
        record["approval"] = None
        return record
    if action != "approve":
        raise Blocked("REVIEW_ACTION", "Unknown action", 2)
    if not record["brand_id"] or not record["goods_id"]:
        raise Blocked("PRODUCT_UNCONFIRMED", "Product and brand must be confirmed")
    if set(record["tags"]["risk"]) & set(taxonomy["blocking_risks"]):
        raise Blocked("BLOCKING_RISK", "Resolve blocking risks before approving")
    record["review_status"] = "approved"
    record["approval"] = {
        "actor_id": decision["actor"],
        "channel": "feishu",
        "approved_at": now(),
        "approved_content_sha256": record["content_sha256"],
    }
    return record


def review_script(current: dict, fields: dict) -> dict:
    """Consume a real versioned table decision, never a sticky approve alone.

    Script text changes are imported as a new immutable revision elsewhere.
    Feishu change requests remain inert data and suspend the current approval.
    Product facts and material freshness are checked again before production.
    """
    record = copy.deepcopy(current)
    actual = content_hash(record)
    if record.get("content_sha256") != actual:
        raise Blocked("SCRIPT_HASH", "Local script changed without a new revision", 6)
    action = fields.get("script_review_action") or "none"
    if isinstance(action, list):
        action = action[0] if len(action) == 1 else "invalid"
    if action not in {"none", "approve", "reject"}:
        raise Blocked("REVIEW_ACTION", "Unknown script review action", 2)
    matches = (
        fields.get("task_id") == record["task_id"]
        and fields.get("script_revision") == record["revision"]
        and fields.get("script_review_revision") == record["revision"]
        and fields.get("script_sha256") == actual
    )
    if record.get("entity") == "script_outline":
        matches = matches and fields.get("script_input_sha256") == record["request_sha256"]
    else:
        matches = matches and fields.get("product_revision") == record["product_revision"]
    change_request = fields.get("script_change_request")
    unresolved_change = bool(change_request) and (
        digest(change_request) != record.get("applied_change_request_sha256")
    )
    if not matches or action == "none" or (unresolved_change and action != "reject"):
        record["review_status"] = "stale" if record.get("approval") else "pending"
        record["approval"] = None
        return record
    actor = fields.get("script_review_actor")
    if not isinstance(actor, str) or not actor.strip():
        raise Blocked("REVIEW_ACTOR_REQUIRED", "Script reviewer must be present")
    if action == "reject":
        record["review_status"] = "rejected"
        record["approval"] = None
        return record
    if record.get("timing_check") and record["timing_check"].get("status") != "passed":
        raise Blocked("SCRIPT_TIMING_BLOCKED", "配音时长未通过，不可批准", 3)
    prior = record.get("approval") or {}
    if (
        record.get("review_status") == "approved"
        and prior.get("approved_content_sha256") == actual
        and prior.get("actor_id") == actor
        and prior.get("channel") == "feishu"
    ):
        return record  # Repeated sync must not rewrite the approval time.
    record["review_status"] = "approved"
    record["approval"] = {
        "actor_id": actor,
        "channel": "feishu",
        "approved_at": now(),
        "approved_content_sha256": actual,
    }
    return record
