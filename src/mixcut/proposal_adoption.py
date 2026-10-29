from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .analyze import proposal_set_path
from .contracts import require_approval, validate_tags
from .core import Blocked, atomic_write, digest, load, now
from .feishu import invoke, pull_rows, unwrap


PREFILL_KEYS = (
    "source_type",
    "content_tags",
    "action_tags",
    "shot_size_tags",
    "risk_tags",
    "usable_start_sec",
    "usable_end_sec",
    "description",
)


def _write_immutable_report(
    target: Path,
    value: dict,
    *,
    identity: Callable[[dict], object],
) -> bool:
    """Write a report once; preserve prior evidence when a later run differs."""
    if target.exists():
        existing = load(target)
        if identity(existing) != identity(value):
            raise Blocked(
                "REPORT_CONFLICT",
                f"Report already exists with different evidence; choose --report: {target}",
                6,
            )
        return False
    atomic_write(target, value)
    return True


def proposal_fields(record: dict, taxonomy: dict) -> dict:
    proposal = record.get("proposal") or {}
    # New mixed-source batches must not inherit the first store-only sample's origin.
    # Preserve the historical default so existing approved content is not rewritten.
    source_type = record.get("source_type_suggestion", "门店实拍")
    if source_type not in {"product_shot", "livestream_clip", "other", "门店实拍"}:
        raise Blocked("PROPOSAL_SOURCE_TYPE", "Unsupported suggested material type", 2)
    tags = proposal.get("tags") or {}
    normalized = {
        axis: list(tags.get(axis) or []) for axis in ("content", "action", "shot_size", "risk")
    }
    validate_tags(normalized, taxonomy)
    start = proposal.get("start_sec")
    end = proposal.get("end_sec")
    duration = record.get("source_duration_sec")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
        or not isinstance(duration, (int, float))
        or not 0 <= start < end <= duration
    ):
        raise Blocked("PROPOSAL_RANGE", "AI proposal range is invalid", 2)
    description = proposal.get("description")
    if not isinstance(description, str) or not description.strip():
        raise Blocked("PROPOSAL_DESCRIPTION", "AI proposal description is missing", 2)
    return {
        "source_type": source_type,
        "content_tags": normalized["content"],
        "action_tags": normalized["action"],
        "shot_size_tags": normalized["shot_size"],
        "risk_tags": normalized["risk"],
        "usable_start_sec": start,
        "usable_end_sec": end,
        "description": description.strip(),
    }


def _empty(value) -> bool:
    return value is None or value == "" or value == []


def prepare_prefill(
    workspace: Path,
    proposals_path: Path | None = None,
    *,
    report_path: Path | None = None,
) -> dict:
    proposal_file = proposal_set_path(workspace, proposals_path)
    proposals = load(proposal_file)
    snapshot = load(workspace / "catalog/feishu_snapshot.json")
    mapping = load(workspace / "config/feishu_mapping.local.json")
    taxonomy = load(workspace / "config/taxonomy.json")
    catalog = {clip["clip_id"]: clip for clip in load(workspace / "catalog/clips.json")}
    adoption_path = workspace / "catalog/ai_proposal_adoptions.json"
    adoptions = (
        {row["clip_id"]: row for row in load(adoption_path).get("records", [])}
        if adoption_path.exists()
        else {}
    )
    confirmation_path = workspace / "catalog/material_confirmations.json"
    confirmations = (
        {row["clip_id"]: row for row in load(confirmation_path).get("records", [])}
        if confirmation_path.exists()
        else {}
    )
    rows = {row["fields"]["clip_id"]: row for row in snapshot["tables"]["clips"]}
    field_ids = mapping["tables"]["clips"]["field_ids"]
    updates = {}
    records = []
    conflicts = []
    for proposal in proposals.get("records", []):
        clip_id = proposal.get("clip_id")
        clip = catalog.get(clip_id)
        row = rows.get(clip_id)
        if not clip or not row:
            raise Blocked("PROPOSAL_CLIP_MISSING", f"Missing catalog or Feishu row: {clip_id}", 4)
        adoption = adoptions.get(clip_id, {})
        confirmation = confirmations.get(clip_id, {})
        original_binding = proposal.get("catalog_revision_at_analysis") == clip.get(
            "revision"
        ) and proposal.get("catalog_content_sha256_at_analysis") == clip.get("content_sha256")
        adopted_binding = (
            adoption.get("analysis_sha256") == proposal.get("analysis_sha256")
            and adoption.get("adopted_catalog_revision") == clip.get("revision")
            and adoption.get("adopted_catalog_content_sha256") == clip.get("content_sha256")
        )
        confirmed_binding = (
            confirmation.get("analysis_sha256") == proposal.get("analysis_sha256")
            and confirmation.get("confirmed_catalog_revision") == clip.get("revision")
            and confirmation.get("confirmed_catalog_content_sha256") == clip.get("content_sha256")
        )
        if not (original_binding or adopted_binding or confirmed_binding):
            raise Blocked("PROPOSAL_STALE", f"Visual proposal is stale: {clip_id}", 6)
        fields = proposal_fields(proposal, taxonomy)
        changed = {}
        conflict_fields = []
        remote_fields = row["fields"]
        human_locked = _single(remote_fields.get("review_status")) in {
            "approved",
            "rejected",
            "stale",
        }
        if not human_locked:
            for key, value in fields.items():
                current = remote_fields.get(key)
                if _empty(current):
                    changed[key] = value
                elif current != value:
                    conflict_fields.append(key)
        if conflict_fields:
            conflicts.append({"clip_id": clip_id, "fields": conflict_fields})
            continue
        if changed:
            updates[row["record_id"]] = {field_ids[key]: value for key, value in changed.items()}
        records.append(
            {
                "clip_id": clip_id,
                "record_id": row["record_id"],
                "analysis_sha256": proposal["analysis_sha256"],
                "fields": fields,
                "changed_fields": sorted(changed),
                "human_locked": human_locked,
            }
        )
    result = {
        "schema_version": "1.0.0",
        "entity": "ai_material_prefill_plan",
        "created_at": now(),
        "source": str(proposal_file),
        "records": records,
        "conflicts": conflicts,
        "update_records": updates,
        "protected_fields": [
            "brand_id",
            "goods_id",
            "review_status",
            "approved_content_sha256",
        ],
    }
    result["plan_sha256"] = digest({k: v for k, v in result.items() if k != "created_at"})
    target = report_path or workspace / "reports/material_prefill_plan_20261015.json"
    _write_immutable_report(target, result, identity=lambda value: value.get("plan_sha256"))
    return {
        "status": "ready"
        if updates and not conflicts
        else "conflict"
        if conflicts
        else "no_change",
        "path": str(target),
        "records": len(records),
        "updates": len(updates),
        "conflicts": conflicts,
        "plan_sha256": result["plan_sha256"],
    }


def apply_prefill(
    workspace: Path,
    plan_path: Path | None = None,
    *,
    call: Callable[[list[str]], dict] | None = None,
    report_path: Path | None = None,
) -> dict:
    """Apply only missing prefill fields after a fresh remote read; never approve materials."""
    plan_file = plan_path or workspace / "reports/material_prefill_plan_20261015.json"
    plan = load(plan_file)
    if plan.get("conflicts"):
        raise Blocked("PREFILL_CONFLICT", "Resolve preserved human values before applying", 6)
    mapping = load(workspace / "config/feishu_mapping.local.json")
    table = mapping["tables"]["clips"]
    field_ids = table["field_ids"]
    api = call or (lambda argv: invoke(argv, workspace))
    rows = {
        row["record_id"]: row for row in pull_rows(api, mapping["base_token"], table["table_id"])
    }
    updates = {}
    conflicts = []
    for record in plan["records"]:
        row = rows.get(record["record_id"])
        if not row:
            raise Blocked("PREFILL_ROW_MISSING", record["record_id"], 4)
        patch = {}
        for key, expected in record["fields"].items():
            actual = row["fields"].get(field_ids[key])
            if _empty(actual) and _empty(expected):
                continue
            if _empty(actual):
                patch[field_ids[key]] = expected
            elif actual != expected:
                conflicts.append({"clip_id": record["clip_id"], "field": key})
        if patch:
            updates[record["record_id"]] = patch
    if conflicts:
        raise Blocked(
            "PREFILL_CONFLICT",
            "Remote human fields changed after planning: "
            + ",".join(f"{item['clip_id']}:{item['field']}" for item in conflicts),
            6,
        )
    status = "already_applied"
    response_ids = []
    if updates:
        response = unwrap(
            api(
                [
                    "+record-batch-update",
                    "--base-token",
                    mapping["base_token"],
                    "--table-id",
                    table["table_id"],
                    "--json",
                    json.dumps({"update_records": updates}, ensure_ascii=False),
                ]
            )
        )
        response_ids = response.get("record_id_list", [])
        if response.get("ignored_fields"):
            raise Blocked("PREFILL_FIELDS_IGNORED", "Read back partially ignored prefill fields", 5)
        # Installed CLI may acknowledge with only ignored_fields, without returning IDs.
        # Absence of IDs is still only an acknowledgement; fresh readback remains required.
        if "record_id_list" in response and set(response_ids) != set(updates):
            raise Blocked("PREFILL_ACK_MISMATCH", "Unexpected Feishu acknowledgement", 5)
        status = "acknowledged_pending_readback"
    report = {
        "schema_version": "1.0.0",
        "entity": "ai_material_prefill_apply",
        "at": now(),
        "plan_sha256": plan["plan_sha256"],
        "status": status,
        "updated_record_ids": sorted(updates),
        "acknowledged_record_ids": sorted(response_ids),
        "protected_fields": plan["protected_fields"],
    }
    target = report_path or workspace / "reports/material_prefill_apply_20261015.json"
    _write_immutable_report(
        target,
        report,
        identity=lambda value: {key: item for key, item in value.items() if key != "at"},
    )
    return {
        "status": status,
        "path": str(target),
        "updated": len(updates),
        "readback_required": bool(updates),
    }


def record_adoptions(
    workspace: Path,
    plan_path: Path | None = None,
    *,
    receipt_path: Path | None = None,
) -> dict:
    plan_file = plan_path or workspace / "reports/material_prefill_plan_20261015.json"
    plan = load(plan_file)
    snapshot = load(workspace / "catalog/feishu_snapshot.json")
    rows = {row["fields"]["clip_id"]: row for row in snapshot["tables"]["clips"]}
    catalog = {clip["clip_id"]: clip for clip in load(workspace / "catalog/clips.json")}
    adopted = []
    for record in plan["records"]:
        clip_id = record["clip_id"]
        row = rows.get(clip_id)
        clip = catalog.get(clip_id)
        if not row or not clip:
            raise Blocked("ADOPTION_READBACK_MISSING", clip_id, 4)
        expected = record["fields"]
        for key, value in expected.items():
            remote = row["fields"].get(key)
            if _empty(remote) and _empty(value):
                continue
            if remote != value:
                raise Blocked("ADOPTION_READBACK_MISMATCH", f"{clip_id}:{key}", 6)
        local_expected = {
            "source_type": expected["source_type"],
            "tags": {
                "content": expected["content_tags"],
                "action": expected["action_tags"],
                "shot_size": expected["shot_size_tags"],
                "risk": expected["risk_tags"],
            },
            "usable_start_sec": expected["usable_start_sec"],
            "usable_end_sec": expected["usable_end_sec"],
            "description": expected["description"],
        }
        if any(clip.get(key) != value for key, value in local_expected.items()):
            raise Blocked("ADOPTION_LOCAL_MISMATCH", clip_id, 6)
        adopted.append(
            {
                "clip_id": clip_id,
                "analysis_sha256": record["analysis_sha256"],
                "adopted_catalog_revision": clip["revision"],
                "adopted_catalog_content_sha256": clip["content_sha256"],
                "adopted_fields_sha256": digest(expected),
            }
        )
    receipt = {
        "schema_version": "1.0.0",
        "entity": "ai_material_prefill_receipt",
        "recorded_at": now(),
        "plan_sha256": plan["plan_sha256"],
        "records": adopted,
        "approval_count": 0,
    }
    target = receipt_path or workspace / "catalog/ai_proposal_adoptions.json"
    atomic_write(target, receipt)
    return {"status": "recorded", "path": str(target), "records": len(adopted)}


def _single(value):
    if isinstance(value, list):
        return value[0] if len(value) == 1 else None
    return value


def record_confirmations(
    workspace: Path,
    actor: str,
    confirmation_plan_path: Path | None = None,
    prefill_plan_path: Path | None = None,
    *,
    receipt_path: Path | None = None,
) -> dict:
    """Bind an explicit human confirmation to the current approved catalog content."""
    if not isinstance(actor, str) or not actor.strip():
        raise Blocked("CONFIRMATION_ACTOR_REQUIRED", "A human confirmation actor is required", 2)
    confirmation_file = (
        confirmation_plan_path or workspace / "reports/material_confirmation_plan_20261015.json"
    )
    prefill_file = prefill_plan_path or workspace / "reports/material_prefill_plan_20261015.json"
    confirmation = load(confirmation_file)
    prefill = load(prefill_file)
    expected_plan_hash = digest(
        {key: value for key, value in confirmation.items() if key != "plan_sha256"}
    )
    if confirmation.get("plan_sha256") != expected_plan_hash:
        raise Blocked("CONFIRMATION_PLAN_HASH", "Material confirmation plan changed", 6)
    if confirmation.get("actor") != actor:
        raise Blocked("CONFIRMATION_ACTOR_MISMATCH", "Actor does not match user confirmation", 6)

    catalog = {clip["clip_id"]: clip for clip in load(workspace / "catalog/clips.json")}
    snapshot = load(workspace / "catalog/feishu_snapshot.json")
    rows = {row["fields"]["clip_id"]: row for row in snapshot["tables"]["clips"]}
    prefills = {record["clip_id"]: record for record in prefill["records"]}
    confirmed = []
    for item in confirmation.get("records", []):
        clip_id = item["clip_id"]
        clip = catalog.get(clip_id)
        row = rows.get(clip_id)
        prefill_record = prefills.get(clip_id)
        if not clip or not row or not prefill_record:
            raise Blocked("CONFIRMATION_READBACK_MISSING", clip_id, 4)
        require_approval(clip)
        approval = clip["approval"]
        expected_goods = Path(item["source_path"]).parent.name
        if item.get("goods_id") != expected_goods:
            raise Blocked("CONFIRMATION_DIRECTORY_MISMATCH", clip_id, 6)
        if item.get("analysis_sha256") != prefill_record.get("analysis_sha256"):
            raise Blocked("CONFIRMATION_ANALYSIS_MISMATCH", clip_id, 6)

        expected_prefill = prefill_record["fields"]
        expected_local = {
            "source_type": expected_prefill["source_type"],
            "usable_start_sec": expected_prefill["usable_start_sec"],
            "usable_end_sec": expected_prefill["usable_end_sec"],
            "description": expected_prefill["description"],
        }
        if any(clip.get(key) != value for key, value in expected_local.items()):
            raise Blocked("CONFIRMATION_PREFILL_CHANGED", clip_id, 6)
        expected_tags = {
            "content": expected_prefill["content_tags"],
            "action": expected_prefill["action_tags"],
            "shot_size": expected_prefill["shot_size_tags"],
        }
        if any(
            (clip.get("tags") or {}).get(axis) != value for axis, value in expected_tags.items()
        ):
            raise Blocked("CONFIRMATION_PREFILL_CHANGED", f"{clip_id}:tags", 6)

        if (
            clip.get("brand_id") != item.get("brand_id")
            or clip.get("goods_id") != expected_goods
            or (clip.get("tags") or {}).get("risk")
        ):
            raise Blocked("CONFIRMATION_FACT_MISMATCH", clip_id, 6)
        if approval.get("actor_id") != actor or approval.get("channel") != "feishu":
            raise Blocked("CONFIRMATION_APPROVAL_MISMATCH", clip_id, 6)

        fields = row["fields"]
        if (
            fields.get("brand_id") != clip["brand_id"]
            or fields.get("goods_id") != clip["goods_id"]
            or fields.get("risk_tags") not in (None, [])
            or _single(fields.get("review_status")) != "approved"
            or fields.get("content_sha256") != clip["content_sha256"]
            or fields.get("approved_content_sha256") != clip["content_sha256"]
        ):
            raise Blocked("CONFIRMATION_REMOTE_MISMATCH", clip_id, 6)

        confirmed.append(
            {
                "clip_id": clip_id,
                "analysis_sha256": item["analysis_sha256"],
                "confirmed_catalog_revision": clip["revision"],
                "confirmed_catalog_content_sha256": clip["content_sha256"],
                "brand_id": clip["brand_id"],
                "goods_id": clip["goods_id"],
                "approval_actor": actor,
                "approval_channel": approval["channel"],
                "approved_at": approval["approved_at"],
            }
        )
    receipt = {
        "schema_version": "1.0.0",
        "entity": "material_confirmation_receipt",
        "recorded_at": now(),
        "confirmation_plan_sha256": confirmation["plan_sha256"],
        "prefill_plan_sha256": prefill["plan_sha256"],
        "actor": actor,
        "records": confirmed,
        "approval_count": len(confirmed),
    }
    target = receipt_path or workspace / "catalog/material_confirmations.json"
    atomic_write(target, receipt)
    return {
        "status": "recorded",
        "path": str(target),
        "records": len(confirmed),
        "approval_count": len(confirmed),
    }
