"""Material review bound to displayed content; revisions and actors stay local."""

from __future__ import annotations

import math
import json
from pathlib import Path
from typing import Callable

from .core import Blocked, atomic_write, digest, load, now, safe_id
from .feishu import cell_equal, pull_rows, unwrap
from .review import review_clip

MATERIAL_KEYS = frozenset(
    {
        "clip_id",
        "source_id",
        "media_ref",
        "source_duration_sec",
        "source_type",
        "brand_id",
        "goods_id",
        "description",
        "usable_start_sec",
        "usable_end_sec",
        "content_tags",
        "action_tags",
        "shot_size_tags",
        "risk_tags",
        "content_sha256",
        "approved_content_sha256",
        "review_status",
        "comment",
    }
)


def material_snapshot(fields: dict) -> dict:
    return {key: fields.get(key) for key in MATERIAL_KEYS}


def effective_fields(fields: dict) -> dict:
    effective = {
        key: fields.get(key)
        for key in (
            "brand_id",
            "goods_id",
            "source_type",
            "usable_start_sec",
            "usable_end_sec",
            "description",
        )
    }
    effective["tags"] = {
        axis: fields.get(field) or []
        for axis, field in (
            ("content", "content_tags"),
            ("action", "action_tags"),
            ("shot_size", "shot_size_tags"),
            ("risk", "risk_tags"),
        )
    }
    for key in ("usable_start_sec", "usable_end_sec"):
        value = effective[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise Blocked("REVIEW_RANGE_REQUIRED", "请填写明确、有效的可用开始秒和结束秒", 2)
    return effective


def material_status(fields: dict, current: dict | None, taxonomy: dict, duration: float) -> str:
    value = fields.get("review_status")
    status = value[0] if isinstance(value, list) and len(value) == 1 else value
    if status != "approved":
        return str(status or "pending")
    if not current:
        return "stale"
    try:
        checked = review_clip(
            current, effective_fields(fields), {"action": "none"}, taxonomy, duration
        )
    except (Blocked, TypeError, KeyError):
        return "stale"
    approval = checked.get("approval") or {}
    return (
        "approved"
        if (
            approval.get("approved_content_sha256")
            == checked["content_sha256"]
            == fields.get("content_sha256")
            == fields.get("approved_content_sha256")
        )
        else "stale"
    )


def material_action(
    root: Path,
    mapping: dict,
    call: Callable,
    actor: dict,
    clip_id: str,
    body: dict,
    *,
    reconcile_only: bool = False,
) -> dict:
    """Called only under writer_lock, with an independently verified CLI actor.

    A timed-out write is reconciled from the same submission; it is never resent.
    No request can provide its own reviewer identity or approval hash.
    """
    safe_id(clip_id)
    action, expected = body.get("action"), body.get("expected")
    if action not in {"approve", "reject"} or body.get("confirmed") is not True:
        raise Blocked("REVIEW_CONFIRM_REQUIRED", "请明确确认本次素材审核操作", 2)
    if not isinstance(expected, dict) or set(expected) != MATERIAL_KEYS:
        raise Blocked("MATERIAL_INPUT_STALE", "缺少当前内容快照，请刷新后重新核对", 6)
    submission = body.get("submission_id")
    if not isinstance(submission, str) or not 8 <= len(submission) <= 100:
        raise Blocked("REVIEW_REQUEST_INVALID", "缺少有效提交标识", 2)
    if not str(actor.get("id", "")).startswith("ou_"):
        raise Blocked("USER_AUTH_REQUIRED", "请核对本机飞书用户登录", 4)
    reason = body.get("reason", "")
    if (
        not isinstance(reason, str)
        or len(reason) > 2000
        or (action == "reject" and not reason.strip())
    ):
        raise Blocked("REVIEW_REASON_REQUIRED", "退回时请填写具体原因（最多2000字）", 2)
    table = mapping["tables"]["clips"]
    fm = table["field_ids"]
    common = ["--base-token", mapping["base_token"], "--table-id", table["table_id"]]

    def read():
        matches = [
            r
            for r in pull_rows(call, mapping["base_token"], table["table_id"])
            if r["fields"].get(fm["clip_id"]) == clip_id
        ]
        if len(matches) != 1:
            raise Blocked("MATERIAL_NOT_UNIQUE", "素材不存在或编号重复", 6)
        row = matches[0]
        return row["record_id"], {key: row["fields"].get(fid) for key, fid in fm.items()}

    def matches(fields, wanted):
        return all(cell_equal(fields.get(key), value) for key, value in wanted.items())

    catalog_path = root / "catalog/clips.json"
    path = (
        root
        / "ledger/material_reviews"
        / (digest({"clip": clip_id, "submission": submission}) + ".json")
    )
    request_hash = digest(body)

    def commit(receipt):
        records = load(catalog_path)
        found = [c for c in records if c["clip_id"] == clip_id]
        if len(found) != 1 or digest(found[0]) not in {
            receipt["local_before_digest"],
            digest(receipt["result_record"]),
        }:
            raise Blocked("MATERIAL_LOCAL_STALE", "本地素材内容已变化，请重新核对", 6)
        atomic_write(
            catalog_path,
            [receipt["result_record"] if c["clip_id"] == clip_id else c for c in records],
        )
        receipt.update(status="verified", verified_at=now())
        atomic_write(path, receipt)
        return {"status": "verified", "clip_id": clip_id, "action": action}

    rid, fields = read()
    if path.exists():
        receipt = load(path)
        if receipt["request_sha256"] != request_hash or receipt["actor"]["id"] != actor["id"]:
            raise Blocked("SUBMISSION_CONFLICT", "相同提交标识的内容或操作者变化", 6)
        wanted = {**expected, **receipt["patch"]}
        if rid != receipt["record_id"] or not matches(fields, wanted):
            raise Blocked("MATERIAL_REVIEW_PENDING", "审核结果待对账或内容已变化；未重复写入", 6)
        return commit(receipt)
    if reconcile_only:
        raise Blocked("MATERIAL_REVIEW_PENDING", "审核回执不存在，禁止以对账名义发起新审核", 6)
    if not matches(fields, expected):
        raise Blocked("MATERIAL_INPUT_STALE", "素材内容或审核结果已变化，请刷新后重新核对", 6)
    records = load(catalog_path)
    current = [c for c in records if c["clip_id"] == clip_id]
    if len(current) != 1 or current[0].get("is_fixture"):
        raise Blocked("MATERIAL_LOCAL_MISSING", "真实素材索引缺失，请先完成素材入库", 6)
    current = current[0]
    sources = {s["source_id"]: s for s in load(root / "catalog/sources.json")}
    if fields.get("source_id") != current["source_id"] or current["source_id"] not in sources:
        raise Blocked("MATERIAL_SOURCE_STALE", "原文件绑定已变化", 6)
    tax = load(root / "config/taxonomy.json")
    duration = sources[current["source_id"]]["duration_sec"]
    prospective = review_clip(current, effective_fields(fields), {"action": "none"}, tax, duration)
    if action == "approve" and any(
        not str(prospective.get(k) or "").strip() for k in ("brand_id", "goods_id")
    ):
        raise Blocked("PRODUCT_UNCONFIRMED", "请先确认品牌、商品", 3)
    result = review_clip(
        prospective,
        {},
        {"action": action, "revision": prospective["revision"], "actor": actor["id"]},
        tax,
        duration,
    )
    patch = {
        "content_sha256": result["content_sha256"],
        "review_status": result["review_status"],
        "approved_content_sha256": (result.get("approval") or {}).get("approved_content_sha256"),
    }
    if action == "reject":
        patch["comment"] = ((fields.get("comment") or "") + "\n退回原因：" + reason.strip()).strip()
    receipt = {
        "at": now(),
        "source": "authenticated_local_material_review",
        "actor": actor,
        "action": action,
        "reason": reason,
        "clip_id": clip_id,
        "record_id": rid,
        "request_sha256": request_hash,
        "request": body,
        "expected": expected,
        "patch": patch,
        "local_before_digest": digest(current),
        "result_record": result,
        "status": "write_unknown",
    }
    atomic_write(path, receipt)
    response = call(
        [
            "+record-batch-update",
            *common,
            "--json",
            json.dumps(
                {
                    "record_id_list": [rid],
                    "patch": {fm[key]: value for key, value in patch.items()},
                },
                ensure_ascii=False,
            ),
        ]
    )
    if unwrap(response).get("ignored_fields"):
        raise Blocked("WRITE_IGNORED_FIELDS", "审核写入字段被忽略，请对账", 6)
    fresh_rid, fresh = read()
    if fresh_rid != rid or not matches(fresh, {**expected, **patch}):
        raise Blocked("MATERIAL_REVIEW_PENDING", "审核请求已发出，读回尚未匹配；未重复写入", 6)
    return commit(receipt)


def reconcile_material_reviews(root: Path, mapping: dict, call: Callable) -> None:
    """Finish previously authorized writes using readback only; never resubmit."""
    for path in (root / "ledger/material_reviews").glob("*.json"):
        receipt = load(path)
        if receipt.get("status") == "write_unknown":
            material_action(
                root,
                mapping,
                call,
                receipt["actor"],
                receipt["clip_id"],
                receipt["request"],
                reconcile_only=True,
            )
