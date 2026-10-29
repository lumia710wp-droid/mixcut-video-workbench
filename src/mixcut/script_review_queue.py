"""Explicit Feishu review/edit buttons; captured actor and revision are mandatory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .contracts import content_hash
from .core import Blocked, atomic_write, digest, load, now
from .feishu import cell_equal, pull_rows, unwrap, enqueue, TASK_FIELDS
from .review import review_script
from .relocation import runtime_path
from .script_input import active_request
from .script_revision import (
    apply_edit,
    editable_text,
    newest_outline,
    revision_request,
    rewrite_once,
    save_retimed,
    script_patch,
    timing_verified,
    measure_review_timing,
)

ACTIONS = {"approve", "reject", "retime", "rewrite"}
CAPTURE_KEYS = [
    "script_action",
    "script_action_revision",
    "script_action_sha256",
    "script_action_actor",
    "script_action_draft",
    "script_action_feedback",
    "script_action_duration",
    "script_action_input_sha256",
]
LIVE_KEYS = [
    "task_id",
    "request_group_id",
    "script_ordinal",
    "variation_strategy",
    "variation_angle",
    "script_revision",
    "script_sha256",
    "script_input_sha256",
    "script_edit_draft",
    "script_change_request",
    "target_duration_sec",
    "creative_keywords",
    "seed_sentences",
]


def actor_id(value) -> str:
    # Workflow $.button.user is captured into a native single-user field.
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        actor = value[0].get("id")
        if isinstance(actor, str) and actor.startswith("ou_"):
            return actor
    raise Blocked("REVIEW_ACTOR_REQUIRED", "按钮未带回真实触发人，请检查工作流的操作触发人字段", 3)


def process_reviews(
    workspace: Path, mapping: dict, call: Callable, *, measure=None, rewriter=None
) -> dict:
    table = mapping["tables"]["tasks"]
    fm = table["field_ids"]
    report: dict = {"at": now(), "handled": [], "pending_sync": [], "errors": []}
    if "script_action" not in fm:
        return report
    common = ["--base-token", mapping["base_token"], "--table-id", table["table_id"]]
    rows = pull_rows(call, mapping["base_token"], table["table_id"])
    by_id = {r["record_id"]: r for r in rows}
    journal = workspace / "ledger/script_review_buttons"
    journal.mkdir(parents=True, exist_ok=True)

    def decode(row):
        return {k: row["fields"].get(fid) for k, fid in fm.items()}

    def fresh(rid):
        raw = unwrap(call(["+record-get", *common, "--record-id", rid, "--format", "json"]))
        if raw.get("record_id_list") != [rid] or len(raw.get("data", [])) != 1:
            raise Blocked("BUTTON_ROW_MISSING", "当前任务行不可读", 6)
        return decode(
            {
                "fields": dict(
                    zip(
                        raw.get("field_id_list") or raw.get("fields", []),
                        raw["data"][0],
                        strict=True,
                    )
                )
            }
        )

    def fingerprint(f):
        return digest({k: f.get(k) for k in CAPTURE_KEYS + LIVE_KEYS})

    def queue_program_result(receipt):
        owners = {k: owner for k, _, _, owner in TASK_FIELDS}
        program = {k: v for k, v in receipt["patch"].items() if owners.get(k) == "program"}
        event_id = enqueue(
            workspace,
            "tasks",
            receipt["task_id"],
            program,
            supersede_group="script_review_state",
            supersede_groups={
                "script_generation_state",
                "production_state",
                "material_index",
                "edit_plan",
            },
        )
        receipt["outbox_ids"] = [event_id]
        revision = program.get("script_revision")
        for old_path in (workspace / "ledger/outbox").glob("*.json"):
            old = load(old_path)
            old_rev = old.get("fields", {}).get("script_revision")
            if (
                old.get("business_key") == receipt["task_id"]
                and old["status"] not in {"synced", "superseded"}
                and isinstance(revision, int)
                and isinstance(old_rev, int)
                and old_rev < revision
            ):
                old.update(status="superseded", superseded_by=event_id)
                atomic_write(old_path, old)

    pending = set()
    for path in journal.glob("*.json"):
        receipt = load(path)
        if receipt.get("status") not in {"acknowledged", "write_unknown"}:
            continue
        row = by_id.get(receipt["record_id"])
        patch = receipt["patch"]
        if row and all(cell_equal(row["fields"].get(fm[k]), v) for k, v in patch.items()):
            receipt.update(status="synced", verified_at=now())
            atomic_write(path, receipt)
            for event_id in receipt.get("outbox_ids", []):
                event_path = workspace / "ledger/outbox" / f"{event_id}.json"
                event = load(event_path)
                event.update(status="synced", verified_at=now(), readback_evidence=str(path))
                atomic_write(event_path, event)
        else:
            pending.add(receipt["record_id"])
            report["pending_sync"].append(str(path))

    for row in rows:
        f = decode(row)
        action = f.get("script_action")
        if action not in ACTIONS or row["record_id"] in pending:
            continue
        rid = row["record_id"]
        key = digest({"record_id": rid, "captured": {k: f.get(k) for k in CAPTURE_KEYS}})
        receipt_path = journal / f"{key}.json"
        prior = load(receipt_path) if receipt_path.exists() else {}
        if prior.get("status") == "local_result_prepared":
            result_path = runtime_path(workspace, prior["result_path"])
            if (
                fingerprint(fresh(rid)) != prior["bound_fingerprint"]
                or not result_path.is_file()
                or not (result_path.parent / "draft_timing.json").is_file()
                or load(result_path).get("content_sha256") != prior["result_sha256"]
            ):
                report["errors"].append({"record_id": rid, "code": "SCRIPT_COMMIT_RECONCILE"})
                continue
            prior.update(status="write_unknown")
            queue_program_result(prior)
            atomic_write(receipt_path, prior)
            ack = call(
                [
                    "+record-batch-update",
                    *common,
                    "--json",
                    json.dumps(
                        {"update_records": {rid: {fm[k]: v for k, v in prior["patch"].items()}}},
                        ensure_ascii=False,
                    ),
                ]
            )
            prior.update(status="acknowledged", ack=ack)
            atomic_write(receipt_path, prior)
            report["handled"].append(
                {"task_id": f.get("task_id"), "action": action, "recovered": True}
            )
            continue
        if receipt_path.exists() and load(receipt_path).get("status") == "synced":
            # Native button replay after response loss is not a new edit/model call.
            if (
                action in {"approve", "reject"}
                and prior.get("error")
                and prior.get("patch", {}).get("script_action") == "blocked:" + action
            ):
                # A new captured click has changed blocked:approve back to approve.
                # Preserve the failed attempt and retry only this non-model decision.
                atomic_write(journal / "history" / f"{key}_{digest(prior)[:16]}.json", prior)
            else:
                continue
        receipt = {
            "at": now(),
            "status": "processing",
            "record_id": rid,
            "action": action,
            "capture": {k: f.get(k) for k in CAPTURE_KEYS},
            "task_id": f.get("task_id"),
        }
        atomic_write(receipt_path, receipt)
        patch = {"script_action": "blocked:" + action}
        try:
            fresh_fields = fresh(rid)
            if fingerprint(fresh_fields) != fingerprint(f):
                report["errors"].append({"record_id": rid, "code": "SCRIPT_INPUT_STALE"})
                continue
            f = fresh_fields
            before = fingerprint(f)

            def fresh_check():
                if fingerprint(fresh(rid)) != before:
                    raise Blocked(
                        "SCRIPT_INPUT_STALE", "操作期间文字、时长或版本变化，请核对后重新点击", 6
                    )

            from .executable import uses_v3

            if uses_v3(workspace, f):
                raise Blocked(
                    "V3_REVIEW_REQUIRED", "请在工作台审核完整执行脚本，旧版按钮不能修改历史批准", 3
                )
            actor = actor_id(f.get("script_action_actor"))
            path, outline = newest_outline(workspace, f.get("task_id"))
            if (
                f.get("script_action_revision") != outline["revision"]
                or f.get("script_action_sha256") != outline["content_sha256"]
                or f.get("script_action_input_sha256") != outline["request_sha256"]
                or f.get("script_revision") != outline["revision"]
                or f.get("script_sha256") != outline["content_sha256"]
            ):
                raise Blocked("REVIEW_VERSION_STALE", "按钮绑定的是旧脚本，请刷新后审核当前版本", 6)
            for capture, live in [
                ("script_action_draft", "script_edit_draft"),
                ("script_action_feedback", "script_change_request"),
                ("script_action_duration", "target_duration_sec"),
            ]:
                if f.get(capture) != f.get(live):
                    raise Blocked("SCRIPT_INPUT_STALE", "点击后输入已变化，请核对再点击", 6)
            if action in {"retime", "rewrite"}:
                patch.update(
                    script_review_action="none",
                    script_review_status="pending",
                    stage="waiting_script_changes",
                )
                feedback = f.get("script_action_feedback") or ""
                request = revision_request(workspace, f, outline, action=action, feedback=feedback)
                if action == "retime":
                    edited = apply_edit(
                        outline, f.get("script_action_draft") or editable_text(outline)
                    )
                else:
                    # Persist and revoke the current production approval before the
                    # explicit paid rewrite, even if the call's outcome is unknown.
                    revoked = dict(outline, approval=None, review_status="pending")
                    atomic_write(path, revoked)
                    edited = (rewriter or rewrite_once)(workspace, request)
                    edited.update(
                        provider="deepseek",
                        model=load(workspace / "config/deepseek.local.json")["model"],
                    )
                # A manual edit also revokes the previous current approval before TTS.
                atomic_write(path, dict(outline, approval=None, review_status="pending"))

                def prepare_commit(new_path, revised, timing):
                    prepared_patch = {
                        **patch,
                        **script_patch(revised, timing),
                        "script_action": "completed:" + action,
                        "script_model": f"{revised.get('provider', 'manual')}/{revised.get('model', 'local')}",
                    }
                    receipt.update(
                        status="local_result_prepared",
                        result_path=str(new_path),
                        result_sha256=revised["content_sha256"],
                        patch=prepared_patch,
                        actor_id=actor,
                        bound_fingerprint=before,
                    )
                    atomic_write(receipt_path, receipt)

                new_path, revised, timing = save_retimed(
                    workspace,
                    load(workspace / "config/config.local.json"),
                    outline,
                    request,
                    edited,
                    measure=measure,
                    fresh_check=fresh_check,
                    prepare_commit=prepare_commit,
                )
                patch = {**patch, **script_patch(revised, timing)}
                patch.update(
                    script_action="completed:" + action,
                    script_model=f"{revised.get('provider', 'manual')}/{revised.get('model', 'local')}",
                )
                receipt.update(
                    result_path=str(new_path),
                    result_sha256=revised["content_sha256"],
                    timing_status=timing["status"],
                )
            else:
                if action == "approve":
                    if f.get("script_edit_draft"):
                        edited = apply_edit(outline, f["script_edit_draft"])
                        if content_hash(edited) != outline["content_sha256"]:
                            raise Blocked(
                                "UNSAVED_SCRIPT_EDIT", "修改稿尚未保存，请先修改并重新编排", 3
                            )
                    request = load(
                        workspace
                        / "requests"
                        / outline["task_id"]
                        / f"{outline['request_sha256']}.json"
                    )
                    creative = active_request(workspace, f)
                    if creative["request_sha256"] != request.get(
                        "creative_request_sha256", request["request_sha256"]
                    ):
                        raise Blocked(
                            "SCRIPT_INPUT_STALE", "创意或目标时长已变化，请先重新生成或编排", 6
                        )
                    if outline.get("narration_mode"):
                        timing = measure_review_timing(workspace, path, outline, measure=measure)
                        timing_fields = script_patch(outline, timing)
                        patch.update(
                            {
                                k: timing_fields[k]
                                for k in ("tts_measured_sec", "script_timing_status")
                            }
                        )
                    else:
                        timing_verified(workspace, path, outline)
                decision = {
                    **f,
                    "script_review_action": action,
                    "script_review_actor": actor,
                    "script_review_revision": outline["revision"],
                }
                fresh_check()
                reviewed = review_script(outline, decision)
                if reviewed["review_status"] != ("approved" if action == "approve" else "rejected"):
                    raise Blocked(
                        "REVIEW_CHANGE_PENDING",
                        "还有未处理的修改意见，请先修改并重新编排或AI重写",
                        3,
                    )
                atomic_write(path, reviewed)
                # These three human fields are a transcription of THIS captured
                # native button decision, with actor/version evidence in the journal.
                patch.update(
                    script_action="completed:" + action,
                    script_review_action=action,
                    script_review_actor=actor,
                    script_review_revision=outline["revision"],
                    script_review_status=reviewed["review_status"],
                    stage="ready_for_material_index"
                    if action == "approve"
                    else "waiting_script_changes",
                    error_summary="当前版本已通过审核，可进行素材索引。"
                    if action == "approve"
                    else "已退回修改；可编辑修改稿或填写意见后AI重写，不自动调用模型。",
                )
            receipt["actor_id"] = actor
        except Blocked as exc:
            receipt["error"] = {"code": exc.code, "message": str(exc)}
            patch.update(error_summary=f"{exc.code}: {exc}")
            report["errors"].append({"record_id": rid, "code": exc.code})
        allowed = {
            "script_review_action",
            "script_review_actor",
            "script_review_revision",
            "script_edit_draft",
        }
        human = {k for k, _, _, owner in TASK_FIELDS if owner == "human"}
        assert not (set(patch) & human) - allowed
        if "script_review_actor" in patch:
            assert receipt.get("actor_id") == patch["script_review_actor"] and action in {
                "approve",
                "reject",
            }
        receipt.update(status="write_unknown", patch=patch)
        queue_program_result(receipt)
        atomic_write(receipt_path, receipt)
        ack = call(
            [
                "+record-batch-update",
                *common,
                "--json",
                json.dumps(
                    {"update_records": {rid: {fm[k]: v for k, v in patch.items()}}},
                    ensure_ascii=False,
                ),
            ]
        )
        receipt.update(status="acknowledged", ack=ack)
        atomic_write(receipt_path, receipt)
        if unwrap(ack).get("ignored_fields"):
            report["errors"].append({"record_id": rid, "code": "REVIEW_FIELDS_IGNORED"})
        report["handled"].append(
            {"task_id": f.get("task_id"), "action": action, "receipt": str(receipt_path)}
        )
    atomic_write(workspace / "reports/script_review_button_status.json", report)
    return report
