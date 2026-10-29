from __future__ import annotations
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Callable
from .core import Blocked, atomic_write, digest, load, now

# Stable internal keys are mapped to actual field IDs after schema readback.
CLIP_FIELDS = [
    ("clip_id", "镜头编号", "text", "program"),
    ("source_id", "原文件编号", "text", "program"),
    ("brand_id", "品牌", "text", "human"),
    ("goods_id", "商品", "text", "human"),
    ("source_type", "来源类型", "text", "human"),
    ("media_ref", "文件定位", "text", "program"),
    ("source_duration_sec", "原片时长", "number", "program"),
    ("preview", "预览", "attachment", "program"),
    ("ai_proposal", "AI全片事实标记", "text", "program"),
    ("content_tags", "有效内容标签", "tags:content", "human"),
    ("action_tags", "有效动作标签", "tags:action", "human"),
    ("shot_size_tags", "有效景别", "tags:shot_size", "human"),
    ("risk_tags", "风险标签", "tags:risk", "human"),
    ("usable_start_sec", "可用开始秒", "number", "human"),
    ("usable_end_sec", "可用结束秒", "number", "human"),
    ("description", "可用描述", "text", "human"),
    ("content_sha256", "当前内容摘要", "text", "program"),
    ("review_status", "审核结果", "select:pending,approved,rejected,stale", "program"),
    ("approved_content_sha256", "已批准摘要", "text", "program"),
    ("taxonomy_version", "词表版本", "text", "program"),
    ("used_count", "使用次数", "number", "program"),
    ("sync_info", "最后同步与错误", "text", "program"),
    ("comment", "人工备注", "text", "human"),
]
TASK_FIELDS = [
    ("title_style_preview", "标题样式与文字", "text", "program"),
    ("title_style_sha256", "标题样式摘要", "text", "program"),
    ("title_style_review_status", "标题样式审核结果", "text", "program"),
    ("director_run_status", "固定执行状态", "text", "program"),
    ("director_run_preview", "固定执行进度", "text", "program"),
    ("director_preview", "编导剪辑脚本", "text", "program"),
    ("director_revision", "编导版本", "number", "program"),
    ("director_sha256", "编导摘要", "text", "program"),
    ("director_review_status", "编导审核结果", "text", "program"),
    ("task_id", "任务编号", "text", "program"),
    ("request_group_id", "需求编号", "text", "program"),
    ("request_group_name", "需求名称", "text", "program"),
    ("script_ordinal", "脚本序号", "number", "program"),
    ("variation_strategy", "脚本差异策略", "text", "program"),
    ("variation_angle", "脚本切入角度", "text", "program"),
    ("batch_id", "批次编号", "text", "program"),
    ("brand_id", "品牌", "text", "program"),
    ("goods_id", "商品或门店主题", "text", "program"),
    ("template_id", "模板", "text", "program"),
    ("variant_id", "变体", "text", "program"),
    ("audio_mode", "音频模式", "select:music_only,provided_voice,tts_uniform", "program"),
    ("creative_keywords", "创意关键词", "text", "human"),
    ("seed_sentences", "参考句子", "text", "human"),
    ("target_duration_sec", "目标时长（秒）", "number", "human"),
    ("script_input_sha256", "创意输入摘要", "text", "program"),
    ("script_generation_status", "脚本生成状态", "text", "program"),
    ("generation_request", "生成请求", "text", "program"),
    ("script_model", "脚本模型", "text", "program"),
    ("script_preview", "脚本展示", "text", "program"),
    ("script_change_request", "脚本修改请求", "text", "human"),
    ("script_edit_draft", "脚本修改稿", "text", "human"),
    ("tts_measured_sec", "配音实测时长", "number", "program"),
    ("script_timing_status", "时间校验结果", "text", "program"),
    ("script_action", "脚本操作请求", "text", "program"),
    ("script_action_revision", "操作绑定版本", "number", "program"),
    ("script_action_sha256", "操作绑定摘要", "text", "program"),
    ("script_action_actor", "操作触发人", "user", "program"),
    ("script_action_draft", "操作修改稿快照", "text", "program"),
    ("script_action_feedback", "操作意见快照", "text", "program"),
    ("script_action_duration", "操作目标时长", "number", "program"),
    ("script_action_input_sha256", "操作输入摘要", "text", "program"),
    ("script_revision", "脚本版本", "number", "program"),
    ("script_sha256", "脚本摘要", "text", "program"),
    ("product_revision", "商品事实版本", "number", "program"),
    ("script_review_revision", "脚本审核版本", "number", "human"),
    ("script_review_action", "脚本审核动作", "select:none,approve,reject", "human"),
    ("script_review_actor", "脚本审核人", "text", "human"),
    ("script_review_status", "脚本审核结果", "select:pending,approved,rejected,stale", "program"),
    ("plan_preview", "剪辑计划摘要", "text", "program"),
    ("material_index_preview", "素材索引结果", "text", "program"),
    ("build_id", "当前构建编号", "text", "program"),
    ("stage", "业务阶段", "text", "program"),
    ("render_status", "渲染状态", "text", "program"),
    ("qc_status", "技术质检", "text", "program"),
    ("sync_status", "同步状态", "text", "program"),
    ("project_ref", "工程定位", "text", "program"),
    ("candidate_preview", "候选成片", "attachment", "program"),
    ("final_review_build_id", "成片审核构建", "text", "human"),
    ("final_review_action", "成片审核动作", "select:none,approve,reject", "human"),
    ("final_review_actor", "成片审核人", "text", "human"),
    ("human_review", "成片审核结果", "text", "program"),
    ("final_review_comment", "成片修改意见", "text", "human"),
    ("final_path", "最终成片位置", "text", "program"),
    ("error_summary", "错误与恢复建议", "text", "program"),
    ("created_at", "创建时间", "text", "program"),
    ("updated_at", "更新时间", "text", "program"),
]


def schema_plan(taxonomy: dict) -> dict:
    tables = {}
    for name, specs in [("clips", CLIP_FIELDS), ("tasks", TASK_FIELDS)]:
        fields = []
        for key, label, kind, owner in specs:
            f: dict = {
                "name": label,
                "type": kind,
                "description": f"{key} | {owner} owns this field",
            }
            if kind.startswith("tags:"):
                f.update(
                    type="select",
                    multiple=True,
                    options=[{"name": x["id"]} for x in taxonomy["axes"][kind[5:]]],
                )
            elif kind.startswith("select:"):
                f.update(
                    type="select",
                    multiple=False,
                    options=[{"name": x} for x in kind[7:].split(",")],
                )
            fields.append(f)
        tables[name] = {
            "name": "素材镜头审核" if name == "clips" else "视频生产任务",
            "fields": fields,
            "ownership": {k: o for k, _, _, o in specs},
            "labels": {key: label for key, label, _, _ in specs},
        }
    return {"version": "1.0", "tables": tables, "destructive_changes": []}


def invoke(argv: list[str], workspace: Path, timeout: int = 60) -> dict:
    # Internal queue handlers use per-record patches; current CLI accepts one
    # shared patch + ID list. Normalize the single-record operation centrally,
    # including calls from the native button worker (outside the web adapter).
    argv = list(argv)
    if argv and argv[0] == "+record-batch-update" and "--json" in argv:
        position = argv.index("--json") + 1
        body = json.loads(argv[position])
        if "update_records" in body:
            updates = body["update_records"]
            if not isinstance(updates, dict) or len(updates) != 1:
                raise Blocked("PATCH_SCOPE_INVALID", "逐行补丁必须单条串行写入", 2)
            record_id, patch = next(iter(updates.items()))
            argv[position] = json.dumps(
                {"record_id_list": [record_id], "patch": patch}, ensure_ascii=False
            )
    env = {
        **os.environ,
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
    }
    try:
        p = subprocess.run(
            ["lark-cli", "base", *argv, "--as", "user"],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise Blocked("SYNC_UNKNOWN", "CLI timed out; reconcile before any write retry", 5) from e
    try:
        d = json.loads(p.stdout)
    except ValueError as e:
        raise Blocked(
            "SYNC_FAILED", f"Non-JSON response, exit={p.returncode}; no data assumed", 4
        ) from e
    if p.returncode or d.get("ok") is False or d.get("code", 0) != 0:
        raise Blocked("SYNC_FAILED", json.dumps(d, ensure_ascii=False)[:600], 4)
    return d


def unwrap(d: dict) -> dict:
    # CLI envelope stores business results in data; a raw matrix also has a data key.
    if isinstance(d.get("data"), dict):
        return d["data"]
    return d


def pull_rows(call: Callable, base: str, table: str, page_size: int = 100) -> list[dict]:
    rows = []
    seen = set()
    offset = 0
    while True:
        raw = unwrap(
            call(
                [
                    "+record-list",
                    "--base-token",
                    base,
                    "--table-id",
                    table,
                    "--limit",
                    str(page_size),
                    "--offset",
                    str(offset),
                    "--format",
                    "json",
                ]
            )
        )
        ids = raw.get("record_id_list", [])
        fields = raw.get("field_id_list") or raw.get("fields", [])
        matrix = raw.get("data", [])
        if not isinstance(matrix, list) or len(ids) != len(matrix):
            raise Blocked("SYNC_SHAPE", "Unexpected matrix shape; no partial snapshot accepted", 4)
        for rid, vals in zip(ids, matrix):
            if rid in seen:
                raise Blocked("SYNC_CONFLICT", "Repeated record while paginating", 6)
            seen.add(rid)
            rows.append({"record_id": rid, "fields": dict(zip(fields, vals, strict=True))})
        count = len(ids)
        has_more = raw.get("has_more")
        total = raw.get("total")
        if (
            has_more is False
            or (isinstance(total, int) and len(rows) >= total)
            or (has_more is None and count < page_size)
        ):
            break
        if not count:
            raise Blocked("SYNC_SHAPE", "Pagination says more but returned zero", 4)
        offset += count
    return rows


def index_rows(rows: list[dict], field: str) -> dict:
    result = {}
    for row in rows:
        key = row["fields"].get(field)
        if not key:
            continue
        if key in result:
            raise Blocked("SYNC_CONFLICT", f"Duplicate business key: {key}", 6)
        result[key] = row
    return result


def program_patch(fields: dict, table: str) -> dict:
    specs = CLIP_FIELDS if table == "clips" else TASK_FIELDS
    owners = {k: o for k, _, _, o in specs}
    forbidden = [k for k in fields if owners.get(k) != "program"]
    if forbidden:
        raise Blocked("HUMAN_FIELD_PROTECTED", ",".join(forbidden), 6)
    return fields


def enqueue(
    workspace: Path,
    table: str,
    business_key: str,
    fields: dict,
    *,
    supersede_group: str | None = None,
    supersede_groups: set[str] | None = None,
) -> str:
    program_patch(fields, table)
    identity = {"table": table, "business_key": business_key, "fields": fields}
    if supersede_group:
        identity["supersede_group"] = supersede_group
    if supersede_groups:
        identity["supersede_groups"] = sorted(supersede_groups)
    key = digest(identity)
    outbox = workspace / "ledger/outbox"
    p = outbox / f"{key}.json"
    # Preserve obsolete events as evidence, but never let an older complete patch
    # overwrite its replacement merely because hash filenames sort differently.
    for old_path in outbox.glob("*.json"):
        old = load(old_path)
        allowed_groups = set(supersede_groups or set())
        if supersede_group:
            allowed_groups.add(supersede_group)
        same_group = old.get("supersede_group") in allowed_groups
        if "material_index" in allowed_groups and "material_index_preview" in old.get("fields", {}):
            same_group = True  # Migrate events created before explicit grouping existed.
        if (
            supersede_group
            and old.get("id") != key
            and old.get("status") not in {"synced", "superseded"}
            and old.get("table") == table
            and old.get("business_key") == business_key
            and same_group
        ):
            old["status"] = "superseded"
            old["superseded_by"] = key
            old.pop("error", None)
            atomic_write(old_path, old)
    if not p.exists():
        event = {
            "id": key,
            "table": table,
            "business_key": business_key,
            "fields": fields,
            "status": "pending",
            "at": now(),
        }
        if supersede_group:
            event["supersede_group"] = supersede_group
        if supersede_groups:
            event["supersede_groups"] = sorted(supersede_groups)
        atomic_write(p, event)
    return key


def push_outbox(
    workspace: Path, mapping: dict, call: Callable, *, event_ids: set[str] | None = None
) -> dict:
    results = []
    awaiting = []
    for path in sorted((workspace / "ledger/outbox").glob("*.json")):
        event = load(path)
        if event_ids is not None and event["id"] not in event_ids:
            continue
        if event["status"] in {"synced", "superseded"}:
            continue
        table = mapping["tables"][event["table"]]
        keyname = "clip_id" if event["table"] == "clips" else "task_id"
        fieldmap = table["field_ids"]
        patch = {fieldmap[k]: v for k, v in program_patch(event["fields"], event["table"]).items()}
        # Full read reconciles unknown prior creates by business key before writing.
        try:
            rows = pull_rows(call, mapping["base_token"], table["table_id"])
            row = index_rows(rows, fieldmap[keyname]).get(event["business_key"])
            if not row and event["status"] in {"unknown", "conflict", "acknowledged"}:
                raise Blocked(
                    "SYNC_RECONCILE_REQUIRED",
                    "Prior create may exist but is not yet visible; no second create sent",
                    5,
                )
            event["status"] = "unknown"
            atomic_write(path, event)
            common = ["--base-token", mapping["base_token"], "--table-id", table["table_id"]]
            if row:
                if all(cell_equal(row["fields"].get(k), v) for k, v in patch.items()):
                    event["status"] = "synced"
                    event.pop("error", None)
                    atomic_write(path, event)
                    results.append(event["id"])
                    continue
                body = {"record_id_list": [row["record_id"]], "patch": patch}
                cmd = "+record-batch-update"
            else:
                patch[fieldmap[keyname]] = event["business_key"]
                body = {"fields": list(patch), "rows": [list(patch.values())]}
                cmd = "+record-batch-create"
            call([cmd, *common, "--json", json.dumps(body, ensure_ascii=False)])
            # Acknowledgement is not a readback. Feishu is observably eventually consistent.
            # The next explicit sync checks the business key; no automatic repeat mutation.
            event["status"] = "acknowledged"
            event.pop("error", None)
            atomic_write(path, event)
            awaiting.append(event["id"])
        except Blocked as e:
            event["status"] = (
                "conflict" if e.exit_code == 6 else "unknown" if e.exit_code == 5 else "failed"
            )
            event["error"] = {"code": e.code, "message": str(e)}
            atomic_write(path, event)
            raise
    return {"synced_event_ids": results, "awaiting_readback_event_ids": awaiting}


def cell_equal(actual, expected):
    # Feishu v3 returns single-select cells as singleton arrays.
    if isinstance(expected, str) and isinstance(actual, list):
        return actual == [expected]
    # Number cells are read back with fewer decimal digits than Python floats.
    # Keep integer revision/identity comparisons exact; allow only float rounding.
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        return not isinstance(actual, bool) and math.isclose(
            actual, expected, rel_tol=1e-14, abs_tol=1e-12
        )
    return actual == expected
