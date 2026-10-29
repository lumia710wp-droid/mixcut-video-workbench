"""A loopback-only workbench adapter. Feishu is authoritative; no demo fallback.

The adapter reuses the installed lark-cli user session. No application credentials
are returned to browsers. Native Base page/plugin installation is a separate gate.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import secrets
import subprocess
import time
from http import cookies
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id, writer_lock
from .feishu import cell_equal, invoke, pull_rows, unwrap
from .material_review import material_snapshot, material_action, material_status
from .script_input import active_request, narration_mode

GROUP_FIELDS = {
    "request_group_id": ("需求编号", "text"),
    "request_group_name": ("需求名称", "text"),
    "script_ordinal": ("脚本序号", "number"),
    "variation_strategy": ("脚本差异策略", "text"),
    "variation_angle": ("脚本切入角度", "text"),
}
EXPECTED_KEYS = frozenset(
    {
        "request_group_id",
        "script_revision",
        "script_sha256",
        "script_input_sha256",
        "creative_keywords",
        "seed_sentences",
        "target_duration_sec",
        "script_edit_draft",
        "script_change_request",
        "variation_angle",
        "script_ordinal",
        "variation_strategy",
        "script_review_status",
        "stage",
        "build_id",
    }
)

ANGLES = [
    "制作过程",
    "顾客视角",
    "门店日常",
    "细节观察",
    "提问开场",
    "一句话故事",
    "场景切入",
    "产品展示",
]


def field_equal(actual, expected):
    # Empty text is sometimes normalized to null by Feishu. Never coerce numbers.
    if actual in (None, "") and expected in (None, ""):
        return True
    return cell_equal(actual, expected)


def scalar(value):
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    return value


def verified_cli_delivery(root, task, director, fields):
    """Only display delivery when archive, attachment and current review agree."""
    if fields.get("director_run_status") != "delivered_cli" or not director:
        return False
    try:
        revision = int(director["revision"])
        directory = root / "jobs/cli_delivery" / safe_id(task) / f"r{revision:04d}"
        receipt = load(directory / "receipt.json")
        preview = json.loads(fields.get("director_run_preview") or "{}")
        final = directory / "final.mp4"
        attachments = fields.get("candidate_preview") or []
        return bool(
            receipt.get("status") == "archived"
            and receipt.get("review_source") == "user_chat"
            and receipt.get("qc_status") == "passed"
            and receipt.get("task_id") == task
            and director.get("review_status") == "approved"
            and receipt.get("director_sha256") == director.get("packet_sha256")
            and preview.get("candidate_sha256") == receipt.get("candidate_sha256")
            and final.is_file()
            and file_hash(final) == receipt.get("candidate_sha256")
            and any(
                x.get("file_token") == receipt.get("attachment_token")
                for x in attachments
                if isinstance(x, dict)
            )
        )
    except (OSError, ValueError, KeyError, TypeError, Blocked):
        return False


def delivery_view(root, task, fields):
    """Do not show approval when the files or Feishu packet have changed."""
    if not fields.get("delivery_manifest"):
        return None
    try:
        from .delivery_package import COVERS, verify, verify_video_attachment

        record = verify(root, task)
        content = record["content"]
        manifest = json.loads(fields["delivery_manifest"])
        sha = record["package_sha256"]
        if manifest.get("package_sha256") != sha or fields.get("delivery_copy") != json.dumps(
            content["copy"], ensure_ascii=False, sort_keys=True
        ):
            return "stale"
        if verify_video_attachment(root, task, content["video"], fields) != manifest.get(
            "video_token"
        ):
            return "stale"
        for name in COVERS:
            attached = fields.get("delivery_cover_" + name) or []
            if len(attached) != 1 or attached[0].get("file_token") != manifest.get(
                "covers", {}
            ).get(name):
                return "stale"
        status = fields.get("delivery_review_status")
        if status in ("approved", "rejected") and fields.get("delivery_review_hash") != sha:
            return "stale"
        return status if status in ("pending", "approved", "rejected") else "stale"
    except (Blocked, OSError, ValueError, KeyError, TypeError):
        return "stale"


def task_status(f):
    review = scalar(f.get("script_review_status"))
    stage = f.get("stage") or ""
    if stage == "delivered" and f.get("final_path") and f.get("human_review") == "approved":
        return "delivered"
    if stage == "waiting_final_review":
        return "final"
    if review == "approved":
        return "matching" if "缺" in (f.get("error_summary") or "") else "ready"
    if "unknown" in (f.get("script_generation_status") or ""):
        return "unknown"
    if (f.get("generation_request") or "") == "queued":
        return "generating"
    if not f.get("script_preview"):
        if "blocked" in (f.get("script_generation_status") or ""):
            return "failed"
        return "not_generated"
    return "changes" if review == "rejected" else "review"


class Workbench:
    def __init__(self, workspace: Path, call=None):
        self.root = workspace.resolve()
        self.call = call or (lambda args: invoke(args, self.root))
        self.mapping = load(self.root / "config/feishu_mapping.local.json")
        self.journal = self.root / "ledger/workbench"
        self.journal.mkdir(parents=True, exist_ok=True)

    def common(self, kind="tasks"):
        return [
            "--base-token",
            self.mapping["base_token"],
            "--table-id",
            self.mapping["tables"][kind]["table_id"],
        ]

    def rows(self, kind="tasks"):
        table = self.mapping["tables"][kind]
        return [
            {
                "record_id": r["record_id"],
                "fields": {key: r["fields"].get(fid) for key, fid in table["field_ids"].items()},
            }
            for r in pull_rows(self.call, self.mapping["base_token"], table["table_id"])
        ]

    def task(self, task_id):
        safe_id(task_id)
        matches = [r for r in self.rows() if r["fields"].get("task_id") == task_id]
        if len(matches) != 1:
            raise Blocked("TASK_NOT_UNIQUE", "任务不存在或编号冲突，请核对飞书", 6)
        return matches[0]

    def patch(self, record_id, patch):
        fm = self.mapping["tables"]["tasks"]["field_ids"]
        payload = {"record_id_list": [record_id], "patch": {fm[k]: v for k, v in patch.items()}}
        result = self.call(
            [
                "+record-batch-update",
                *self.common(),
                "--json",
                json.dumps(payload, ensure_ascii=False),
            ]
        )
        if unwrap(result).get("ignored_fields"):
            raise Blocked("WRITE_IGNORED_FIELDS", "写入字段被忽略，需对账", 6)
        return result

    def verify_patch(self, task_id, patch):
        # Bounded read-only polling handles Feishu's delayed read visibility.
        for attempt in range(4):
            row = self.task(task_id)
            if all(field_equal(row["fields"].get(k), v) for k, v in patch.items()):
                return row
            if attempt < 3:
                time.sleep(0.35)
        raise Blocked("WRITE_PENDING_READBACK", "写入已发出，读回尚未确认；不要重复提交", 6)

    def ensure_schema(self, specs=None):
        """Add only missing group fields; never rebuild mappings or rename old fields."""
        table = self.mapping["tables"]["tasks"]
        specs = GROUP_FIELDS if specs is None else specs
        data = unwrap(self.call(["+field-list", *self.common(), "--limit", "200"]))
        fields = {f["name"]: f for f in data["fields"]}
        missing = [
            {"name": name, "type": kind, "description": f"{key} | Mixcut需求分组"}
            for key, (name, kind) in specs.items()
            if name not in fields
        ]
        for name, kind in specs.values():
            if name in fields and fields[name]["type"] != kind:
                raise Blocked("SCHEMA_CONFLICT", f"{name}字段类型不符，未覆盖", 6)
        if missing:
            for field in missing:
                receipt_path = self.journal / ("field_" + digest(field) + ".json")
                if receipt_path.exists():
                    raise Blocked(
                        "SCHEMA_PENDING_READBACK", "字段创建已发出，等待读回；未重复创建", 6
                    )
                atomic_write(receipt_path, {"at": now(), "status": "write_unknown", "field": field})
                ack = self.call(
                    [
                        "+field-create",
                        *self.common(),
                        "--json",
                        json.dumps(field, ensure_ascii=False),
                    ]
                )
                atomic_write(
                    receipt_path,
                    {"at": now(), "status": "acknowledged", "field": field, "ack": ack},
                )
            for attempt in range(4):
                data = unwrap(self.call(["+field-list", *self.common(), "--limit", "200"]))
                fields = {f["name"]: f for f in data["fields"]}
                if all(name in fields for name, _ in specs.values()):
                    break
                if attempt < 3:
                    time.sleep(0.5)
        for name, kind in specs.values():
            if name not in fields:
                raise Blocked("SCHEMA_PENDING_READBACK", "字段创建已发出，等待读回；未重复创建", 6)
            if fields[name]["type"] != kind:
                raise Blocked("SCHEMA_CONFLICT", "字段类型不符，未保存映射", 6)
        for key, (name, _) in specs.items():
            table["field_ids"][key] = fields[name]["id"]
            table.setdefault("labels", {})[key] = name
        atomic_write(self.root / "config/feishu_mapping.local.json", self.mapping)
        return {k: table["field_ids"][k] for k in specs}

    @staticmethod
    def project_input(body):
        keywords, seed = body.get("keywords", ""), body.get("seed", "")
        if not isinstance(keywords, str) or not isinstance(seed, str):
            raise Blocked("INPUT_INVALID", "创意必须是文字", 2)
        if not keywords.strip() and not seed.strip():
            raise Blocked("INPUT_MISSING", "关键词或参考句子至少填写一项", 2)
        n, duration = body.get("count"), body.get("duration", 24)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 30:
            raise Blocked("COUNT_INVALID", "脚本数量须为1—30整数", 2)
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or not 6 <= duration <= 120
        ):
            raise Blocked("DURATION_INVALID", "时长须为6—120秒", 2)
        key = body.get("submission_id", "")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
            raise Blocked("SUBMISSION_ID_INVALID", "提交标识缺失", 2)
        name = body.get("title") or (keywords or seed)[:30]
        mode = body.get("narration_mode", "sales_independent")
        if mode not in {"sales_independent", "material_based"}:
            raise Blocked("SCRIPT_MODE_INVALID", "文案方式无效", 2)
        if (
            not isinstance(name, str)
            or len(name) > 80
            or len(keywords) > 500
            or len(seed) > 2000
            or body.get("strategy", "distinct") not in {"distinct", "opening"}
        ):
            raise Blocked("INPUT_INVALID", "名称、创意长度或策略无效", 2)
        return {
            "submission_id": key,
            "title": name.strip() or (keywords.strip() or seed.strip())[:30],
            "keywords": keywords.strip(),
            "seed": seed.strip(),
            "count": n,
            "duration": round(duration * 30) / 30,
            "strategy": body.get("strategy", "distinct"),
            **({"narration_mode": mode} if "narration_mode" in body else {}),
        }

    def create_project(self, body):
        request = self.project_input(body)
        v3_path = self.root / "config/executable_montage_v3.json"
        v3 = load(v3_path) if v3_path.exists() else {}
        if v3.get("enabled"):
            from .product_scope import project_scope

            request.update(project_scope(self.root, body), generation_policy_version=3)
        path = self.journal / (digest({"submission_id": request["submission_id"]}) + ".json")
        group = "REQ_" + digest({"submission_id": request["submission_id"]})[:16].upper()
        ids = [
            "VIDEO_" + digest({"group": group, "ordinal": i})[:16].upper()
            for i in range(1, request["count"] + 1)
        ]
        prior = load(path) if path.exists() else None
        if prior and prior["request"] != request:
            raise Blocked("SUBMISSION_CONFLICT", "同一提交标识的内容变化，未新建任务", 6)
        all_rows = self.rows()
        if any(sum(r["fields"].get("task_id") == task_id for r in all_rows) > 1 for task_id in ids):
            raise Blocked("TASK_NOT_UNIQUE", "存在重复任务编号，未创建", 6)
        current = {r["fields"].get("task_id"): r for r in all_rows}
        found = [i for i in ids if i in current]
        if len(found) == len(ids):
            for i, task_id in enumerate(ids, 1):
                f = current[task_id]["fields"]
                wanted = {
                    "request_group_id": group,
                    "request_group_name": request["title"],
                    "script_ordinal": i,
                    "variation_strategy": request["strategy"],
                    "creative_keywords": request["keywords"],
                    "seed_sentences": request["seed"],
                    "target_duration_sec": request["duration"],
                }
                if not all(field_equal(f.get(k), v) for k, v in wanted.items()):
                    raise Blocked("SUBMISSION_CONFLICT", "已存在任务的内容不同，未重复创建", 6)
            if prior:
                prior.update(status="verified", verified_at=now())
                atomic_write(path, prior)
            return {"status": "verified", "group_id": group, "task_ids": ids, "reused": True}
        if prior and prior.get("status") in {"write_unknown", "acknowledged", "verified"}:
            raise Blocked("CREATE_NEEDS_RECONCILE", "创建请求已发出，数量尚未对账；未重复创建", 6)
        if found:
            raise Blocked("CREATE_PARTIAL", "已有部分任务，需对账；未重复创建", 6)
        intent = {
            "at": now(),
            "status": "prepared",
            "request": request,
            "group_id": group,
            "task_ids": ids,
        }
        atomic_write(path, intent)
        if "narration_mode" in request or request.get("generation_policy_version") == 3:
            atomic_write(
                self.root / "config/script_project_policies" / f"{group}.json",
                {
                    "group_id": group,
                    "narration_mode": request.get("narration_mode", "sales_independent"),
                    **(
                        {
                            "generation_policy_version": 3,
                            "generation_task_ids": ids,
                            "workflow_mode": "script_first_v1",
                            "brand_id": request["brand_id"],
                            "goods_id": request["goods_id"],
                            **{k: request[k] for k in ("goods_scope", "goods_ids") if k in request},
                            "speed": v3.get("speed", 1.1),
                            "minimum_unique": v3.get("minimum_unique", 0.7),
                        }
                        if request.get("generation_policy_version") == 3
                        else {}
                    ),
                },
            )
        fm = self.mapping["tables"]["tasks"]["field_ids"]
        keys = [
            "task_id",
            "request_group_id",
            "request_group_name",
            "script_ordinal",
            "variation_strategy",
            "variation_angle",
            "creative_keywords",
            "seed_sentences",
            "target_duration_sec",
            "script_generation_status",
            "stage",
            "created_at",
        ]
        rows = []
        for i, task_id in enumerate(ids, 1):
            angle = (
                f"开场版本{i}"
                if request["strategy"] == "opening"
                else ANGLES[(i - 1) % len(ANGLES)] + f"，表达版本{i}"
            )
            rows.append(
                [
                    task_id,
                    group,
                    request["title"],
                    i,
                    request["strategy"],
                    angle,
                    request["keywords"],
                    request["seed"],
                    request["duration"],
                    "not_requested",
                    "waiting_script_generation",
                    now(),
                ]
            )
        payload = {"fields": [fm[k] for k in keys], "rows": rows}
        intent["status"] = "write_unknown"
        atomic_write(path, intent)
        ack = self.call(
            [
                "+record-batch-create",
                *self.common(),
                "--json",
                json.dumps(payload, ensure_ascii=False),
            ]
        )
        intent.update(status="acknowledged", ack=ack)
        atomic_write(path, intent)
        for attempt in range(4):
            readback = self.rows()
            if any(
                sum(r["fields"].get("task_id") == task_id for r in readback) > 1 for task_id in ids
            ):
                raise Blocked("TASK_NOT_UNIQUE", "读回存在重复任务编号，需要对账", 6)
            actual = {r["fields"].get("task_id"): r for r in readback}
            if all(
                task_id in actual
                and all(
                    field_equal(actual[task_id]["fields"].get(k), v)
                    for k, v in zip(keys, vals, strict=True)
                )
                for task_id, vals in zip(ids, rows, strict=True)
            ):
                break
            if attempt < 3:
                time.sleep(0.35)
        else:
            raise Blocked("CREATE_PENDING_READBACK", "创建已发出，等待真实读回；未重复提交", 6)
        intent.update(status="verified", verified_at=now())
        atomic_write(path, intent)
        return {"status": "verified", "group_id": group, "task_ids": ids, "reused": False}

    def v3_rows(self, group):
        from .executable import policy

        targets = policy(self.root, group).get("generation_task_ids", [])
        rows = [
            r
            for r in self.rows()
            if r["fields"].get("request_group_id") == group
            and (not targets or r["fields"].get("task_id") in targets)
        ]
        if (
            not rows
            or (targets and {r["fields"]["task_id"] for r in rows} != set(targets))
            or len({r["fields"]["task_id"] for r in rows}) != len(rows)
        ):
            raise Blocked("V3_BATCH_ROWS", "需求任务缺失或重复", 6)
        return rows

    def v3_material_gate(self, settings):
        from .fact_planning import planning_segments, uses_facts
        from .review import review_clip
        from .material_review import effective_fields

        clips = {c["clip_id"]: c for c in load(self.root / "catalog/clips.json")}
        sources = {c["source_id"]: c for c in load(self.root / "catalog/sources.json")}
        taxonomy = load(self.root / "config/taxonomy.json")
        live = {r["fields"].get("clip_id"): r["fields"] for r in self.rows("clips")}
        for segment in planning_segments(self.root, settings):
            cid = segment["parent_clip_id"]
            if uses_facts(self.root, settings):
                current = clips[cid]
                fields = live.get(cid)
                if not fields or fields.get("source_id") != segment["source_id"]:
                    raise Blocked("V3_MATERIAL_LIVE_STALE", "飞书素材身份变化，请刷新同步", 6)
                checked = review_clip(
                    current,
                    effective_fields(fields),
                    {"action": "none"},
                    taxonomy,
                    sources[segment["source_id"]]["duration_sec"],
                )
                value = fields.get("review_status")
                if (
                    checked["content_sha256"] != current["content_sha256"]
                    or fields.get("content_sha256") != current["content_sha256"]
                    or value in ("rejected", ["rejected"])
                ):
                    raise Blocked(
                        "V3_MATERIAL_LIVE_STALE", "飞书商品或素材内容变化，请先同步核对", 6
                    )
                continue
            if (
                cid not in live
                or material_status(
                    live[cid], clips[cid], taxonomy, sources[segment["source_id"]]["duration_sec"]
                )
                != "approved"
            ):
                raise Blocked("V3_MATERIAL_LIVE_STALE", "飞书素材批准已变更，请先同步核对", 6)

    def text_script_action(self, group, rows, body):
        from . import script_stage

        action = body["action"]
        if action == "generate":
            if body.get("confirm_model_call") is not True:
                raise Blocked("MODEL_CONFIRM_REQUIRED", "生成文字脚本需确认模型调用", 3)
            record = script_stage.generate(self.root, group, rows)
        elif action == "save_copy":
            record = script_stage.edit(
                self.root,
                group,
                rows,
                body.get("texts"),
                body.get("copy_sha256"),
                headlines=body.get("headlines"),
            )
        else:
            record = script_stage.review(
                self.root,
                group,
                rows,
                body["task_id"],
                body.get("script_sha256"),
                action,
                self.auth_user(),
            )
        for script in record["scripts"]:
            target = next(r for r in rows if r["fields"]["task_id"] == script["task_id"])
            from .product_scope import describe

            material_note = (
                "素材范围：" + describe(script["material_scope"]) + "\n"
                if script.get("material_scope")
                else ""
            )
            patch = {
                "plan_preview": (
                    f"文字脚本 r{record['revision']} · {script['review_status']}\n"
                    f"{material_note}"
                    f"{script['title']}｜目标{script['target_seconds']:g}秒\n"
                    f"{script['narration']}\n"
                    f"{script['characters']}字，按字数估计约{script['estimated_seconds']:g}秒（非配音实测）。\n"
                    "脚本确认后由Codex检索选材；配音在剪辑阶段根据脚本生成。"
                )
            }
            if script_stage.length_review(script)["status"] == "needs_revision":
                check = script_stage.length_review(script)
                patch["plan_preview"] += (
                    f"\n本条字数需调整：当前{script['characters']}字，建议{check['minimum_characters']}–{check['maximum_characters']}字。"
                    "请修改正文后审核；其他稿件可独立审核，未生成配音。"
                )
            elif script_stage.length_review(script)["status"] == "estimate_warning":
                patch["plan_preview"] += (
                    "\n字数略有偏差，处于文字估计约0.5秒容差内，可确认文字；真实配音时长在剪辑阶段实测。"
                )
            self.patch(target["record_id"], patch)
            self.verify_patch(script["task_id"], patch)
            if script.get("title_style"):
                from .title_style import publish as publish_title_style

                publish_title_style(self, script)
        return {
            "status": "verified",
            "stage": "text_script",
            "revision": record["revision"],
            "audio_generated": False,
            "material_reserved": False,
        }

    def v3_action(self, row, body):
        from .executable import generate_batch, input_snapshot, policy, review_script

        f = row["fields"]
        group = f["request_group_id"]
        settings = policy(self.root, group)
        rows = self.v3_rows(group)
        if body.get("batch_input_sha256") != digest(input_snapshot(rows, settings)):
            raise Blocked("V3_INPUT_CHANGED", "同批需求内容变化，请刷新", 6)
        if settings.get("workflow_mode") == "script_first_v1":
            return self.text_script_action(group, rows, {**body, "task_id": f["task_id"]})
        if body["action"] in {"generate", "approve"}:
            self.v3_material_gate(settings)
        if body["action"] == "save_copy":
            from .copy_drafts import save_copy_draft

            return save_copy_draft(
                self.root,
                group,
                rows,
                body.get("texts", {}),
                body.get("copy_sha256", ""),
                self.auth_user(),
            )
        if body["action"] == "generate":
            if body.get("confirm_model_call") is not True:
                raise Blocked("MODEL_CONFIRM_REQUIRED", "整批文案及配音调用需确认", 3)
            batch = generate_batch(
                self.root, load(self.root / "config/config.local.json"), group, rows
            )
        else:
            actor = self.auth_user()
            batch = review_script(
                self.root,
                group,
                f["task_id"],
                body.get("executable_sha256"),
                body["action"],
                actor,
                rows,
            )
        # A durable local decision can be synchronized again without re-generating/re-approving.
        for script in batch["scripts"]:
            target = next(r for r in rows if r["fields"]["task_id"] == script["task_id"])
            preview = (
                f"执行脚本 V3 · r{script['revision']} · {script['review_status']}\n"
                f"{script['title']}｜{script['total_frames'] / 30:g}秒｜{len(script['shots'])}镜头\n"
                f"{script['narration']}\n"
                + "\n".join(
                    f"{n + 1}. {x['source_id']} {x['source_in_frame'] / 30:.2f}–{x['source_out_frame'] / 30:.2f}s "
                    f"→ {x['timeline_start_frame'] / 30:.2f}s / {x['duration_frames'] / 30:.2f}s "
                    f"速度{x['speed']} · 硬切"
                    for n, x in enumerate(script["shots"])
                )
                + f"\n完整参数/配音/字幕请在工作台脚本抽屉查看；摘要 {script['content_sha256']}"
            )
            patch = {"plan_preview": preview}
            self.patch(target["record_id"], patch)
            self.verify_patch(script["task_id"], patch)
        return {"status": "verified", "batch_id": batch["batch_id"], "state": batch["state"]}

    def segment_action(self, clip_id, body):
        from .segments import review_segments

        clip = next(
            (c for c in load(self.root / "catalog/clips.json") if c["clip_id"] == clip_id), None
        )
        if not clip:
            raise Blocked("MATERIAL_MISSING", "素材不存在", 6)
        live = next(
            (r["fields"] for r in self.rows("clips") if r["fields"].get("clip_id") == clip_id), None
        )
        source = next(
            s
            for s in load(self.root / "catalog/sources.json")
            if s["source_id"] == clip["source_id"]
        )
        if (
            not live
            or material_status(
                live, clip, load(self.root / "config/taxonomy.json"), source["duration_sec"]
            )
            != "approved"
        ):
            raise Blocked("MATERIAL_STALE", "请先核对并批准父素材当前内容", 6)
        return review_segments(self.root, clip_id, body, self.auth_user())

    def state(self):
        from .script_revision import editable_text

        from .executable import policy, latest_batch, input_snapshot, uses_v3
        from .segments import read_segments

        segment_index = read_segments(self.root)
        task_rows, clips = self.rows(), self.rows("clips")
        tasks = []
        groups: dict[str, dict[str, Any]] = {}
        excluded = {"blank": 0, "test": 0}
        for row in task_rows:
            f = row["fields"]
            task_id = f.get("task_id")
            if not task_id:
                excluded["blank"] += 1
                continue
            if str(task_id).startswith(("BUTTON_", "WORKBENCH_TEST_")):
                excluded["test"] += 1
                continue
            group = f.get("request_group_id") or f.get("batch_id") or "LEGACY_" + task_id
            groups.setdefault(
                group,
                {
                    "id": group,
                    "title": f.get("request_group_name") or f.get("batch_id") or task_id,
                    "keywords": f.get("creative_keywords") or "",
                    "seed": f.get("seed_sentences") or "",
                    "duration": f.get("target_duration_sec"),
                    "count": 0,
                    "created": f.get("created_at") or "时间未填写",
                },
            )["count"] += 1
            outline = None
            policy_stale = False
            if f.get("script_revision"):
                source = (
                    self.root
                    / "jobs"
                    / safe_id(task_id)
                    / f"r{int(f['script_revision'])}"
                    / "script_outline.json"
                )
                if source.is_file():
                    candidate = load(source)
                    if candidate.get("content_sha256") == f.get("script_sha256"):
                        outline = candidate
                        saved = load(
                            self.root / "requests" / task_id / f"{outline['request_sha256']}.json"
                        )
                        try:
                            policy_stale = active_request(self.root, f)[
                                "request_sha256"
                            ] != saved.get("creative_request_sha256", saved["request_sha256"])
                        except Blocked:
                            policy_stale = True
            tasks.append(
                {
                    "id": task_id,
                    "recordId": row["record_id"],
                    "batchId": group,
                    "title": (outline or {}).get("title")
                    or f"脚本 {f.get('script_ordinal') or task_id}",
                    "angle": f.get("variation_angle") or "历史任务",
                    "duration": f.get("target_duration_sec"),
                    "revision": f.get("script_revision"),
                    "status": task_status(f),
                    "timing": f.get("script_timing_status") or "未提供测时结果",
                    "preview": f.get("script_preview") or "",
                    "draft": f.get("script_edit_draft")
                    or (editable_text(outline) if outline else ""),
                    "feedback": f.get("script_change_request") or "",
                    "error": f.get("error_summary") or "",
                    "rawStage": f.get("stage"),
                    "build": f.get("build_id"),
                    "qc": f.get("qc_status"),
                    "reviewStatus": scalar(f.get("script_review_status")),
                    "shots": (outline or {}).get("shots", []),
                    "narrationMode": narration_mode(self.root, f),
                    "policyStale": policy_stale,
                    "expected": {k: f.get(k) for k in EXPECTED_KEYS},
                    "sourceUrl": f"{self.mapping['url']}?table={self.mapping['tables']['tasks']['table_id']}&record={row['record_id']}",
                    "candidateUrl": self.media_url(f, "candidate"),
                    "finalUrl": self.media_url(f, "final"),
                }
            )
        materials = []
        local_clips = {c["clip_id"]: c for c in load(self.root / "catalog/clips.json")}
        local_sources = {s["source_id"]: s for s in load(self.root / "catalog/sources.json")}
        taxonomy = load(self.root / "config/taxonomy.json")
        facts_path = self.root / "catalog/material_facts_v1.json"
        facts_by_source = {
            a["source"]["source_id"]: a
            for a in (load(facts_path).get("materials", []) if facts_path.is_file() else [])
        }
        for row in clips:
            f = row["fields"]
            clip = f.get("clip_id")
            if not clip:
                continue
            materials.append(
                {
                    "id": clip,
                    "sourceId": f.get("source_id"),
                    "description": facts_by_source.get(f.get("source_id"), {}).get("summary")
                    or f.get("description")
                    or "描述未填写",
                    "facts": {
                        key: facts_by_source.get(f.get("source_id"), {}).get(key)
                        for key in ("summary", "phases", "identity", "similarity", "content_sha256")
                    },
                    "proposal": f.get("ai_proposal") or "",
                    "brand": f.get("brand_id"),
                    "goods": f.get("goods_id"),
                    "status": material_status(
                        f,
                        local_clips.get(clip),
                        taxonomy,
                        local_sources.get(f.get("source_id"), {}).get("duration_sec", 0),
                    ),
                    "expected": material_snapshot(f),
                    "tags": {
                        "content": f.get("content_tags") or [],
                        "action": f.get("action_tags") or [],
                        "shot_size": f.get("shot_size_tags") or [],
                        "risk": f.get("risk_tags") or [],
                    },
                    "comment": f.get("comment") or "",
                    "start": f.get("usable_start_sec"),
                    "end": f.get("usable_end_sec"),
                    "risks": f.get("risk_tags"),
                    "used": f.get("used_count"),
                    "previewUrl": f"/api/previews/{clip}"
                    if (self.root / "previews" / clip / "contact.jpg").is_file()
                    else None,
                    "sourceUrl": f"{self.mapping['url']}?table={self.mapping['tables']['clips']['table_id']}&record={row['record_id']}",
                }
            )
        for group, info in groups.items():
            settings = policy(self.root, group)
            if settings.get("generation_policy_version") != 3:
                continue
            info["executableV3"] = True
            batch = latest_batch(self.root, group)
            from .copy_drafts import draft_status

            target_rows = [
                r
                for r in task_rows
                if r["fields"].get("request_group_id") == group and uses_v3(self.root, r["fields"])
            ]
            if settings.get("workflow_mode") == "script_first_v1":
                from . import script_stage
                from .script_stage import latest as latest_text

                text_batch = latest_text(self.root, group)
                bound = digest(input_snapshot(target_rows, settings))
                for task in (
                    t for t in tasks if t["id"] in {r["fields"]["task_id"] for r in target_rows}
                ):
                    task_fields = next(
                        r["fields"] for r in target_rows if r["fields"]["task_id"] == task["id"]
                    )
                    from .director_stage import (
                        latest as latest_director,
                        preview as director_preview,
                    )

                    director = latest_director(self.root, task["id"])
                    script = next(
                        (
                            s
                            for s in (text_batch or {}).get("scripts", [])
                            if s["task_id"] == task["id"]
                        ),
                        None,
                    )
                    task.update(
                        textScriptMode=True,
                        executableV3=False,
                        executable=None,
                        voiceUrl=None,
                        copyDraft=None,
                        textScript=script,
                        textLengthReview=script_stage.length_review(script) if script else None,
                        textApprovalDisabledReason=script_stage.approval_disabled_reason(
                            script, bool(text_batch and text_batch["input_sha256"] != bound)
                        ),
                        textScripts=(text_batch or {}).get("scripts", []),
                        textBatchSha256=(text_batch or {}).get("content_sha256"),
                        batchInputSha256=bound,
                        inputChanged=bool(text_batch and text_batch["input_sha256"] != bound),
                        title=script["title"] if script else f"待生成脚本 {task['id']}",
                        status=(
                            "delivered"
                            if script
                            and text_batch["input_sha256"] == bound
                            and director
                            and director["packet"]["text_sha256"] == script["content_sha256"]
                            and verified_cli_delivery(self.root, task["id"], director, task_fields)
                            else "ready"
                            if script and script["review_status"] == "approved"
                            else "changes"
                            if script and script["review_status"] == "changes"
                            else "review"
                            if script
                            else "not_generated"
                        ),
                        revision=f"文字-{text_batch['revision']}" if text_batch else None,
                        timing="配音已生成；文字时长为估算值"
                        if task_fields.get("candidate_preview")
                        else "按字数估时；配音在剪辑阶段生成",
                        director=director,
                        fixedRunStatus=task_fields.get("director_run_status"),
                        fixedRunPreview=task_fields.get("director_run_preview"),
                        candidateAttached=bool(task_fields.get("candidate_preview")),
                        deliveryManifest=task_fields.get("delivery_manifest"),
                        deliveryCopy=task_fields.get("delivery_copy"),
                        deliveryReviewStatus=delivery_view(self.root, task["id"], task_fields),
                        directorPreview=director_preview(director, script)
                        if director and script
                        else None,
                        directorStale=bool(
                            director
                            and script
                            and director["packet"]["text_sha256"] != script["content_sha256"]
                        ),
                    )
                    if task["status"] == "delivered":
                        task["build"] = f"CLI-r{director['revision']}"
                        task["qc"] = "passed"
                continue
            copy_draft = None
            if not batch or batch["state"] == "released":
                try:
                    copy_draft = draft_status(self.root, group, target_rows)
                except Blocked as error:
                    copy_draft = {"error": str(error), "code": error.code}
            bound = digest(
                input_snapshot(
                    [
                        r
                        for r in task_rows
                        if r["fields"].get("request_group_id") == group
                        and uses_v3(self.root, r["fields"])
                    ],
                    settings,
                )
            )
            for task in (
                t
                for t in tasks
                if t["batchId"] == group
                and (
                    not settings.get("generation_task_ids")
                    or t["id"] in settings["generation_task_ids"]
                )
            ):
                task["executableV3"] = True
                task["copyDraft"] = copy_draft
                task["status"] = "not_generated"
                task["title"] = (
                    f"待生成执行脚本 {next(r['fields'].get('script_ordinal') for r in task_rows if r['fields'].get('task_id') == task['id'])}"
                )
                task["revision"] = None
                task["timing"] = "阳光青年2.0 · 生成后实测时长"
                task["batchInputSha256"] = bound
                task["executable"] = next(
                    (x for x in (batch or {}).get("scripts", []) if x["task_id"] == task["id"]),
                    None,
                )
                task["diversity"] = (batch or {}).get("diversity")
                task["batchState"] = (batch or {}).get("state")
                task["inputChanged"] = bool(batch and batch["input_sha256"] != bound)
                task["voiceUrl"] = f"/api/v3-audio/{task['id']}" if task["executable"] else None
                if task["executable"] and batch:
                    task["title"] = task["executable"]["title"]
                    task["status"] = (
                        "changes"
                        if batch["state"] == "released"
                        else (
                            "ready"
                            if task["executable"]["review_status"] == "approved"
                            else "review"
                        )
                    )
                    task["revision"] = f"V3-{task['executable']['revision']}"
        for material in materials:
            material["segments"] = [
                x for x in segment_index["segments"] if x["parent_clip_id"] == material["id"]
            ]
            material["previewUrl"] = (
                f"/api/previews/{material['id']}"
                if self.material_media(material["id"], "image", False)
                else material["previewUrl"]
            )
            material["materialVideoUrl"] = (
                f"/api/material-video/{material['id']}"
                if self.material_media(material["id"], "video", False)
                else None
            )
            material["segmentIndexSha256"] = digest(segment_index)
            material["parentSha256"] = local_clips.get(material["id"], {}).get("content_sha256")
            material["sourceName"] = next(
                (
                    x["relative_path"]
                    for x in local_sources.get(
                        local_clips.get(material["id"], {}).get("source_id"), {}
                    ).get("locations", [])
                ),
                "",
            )
        from .product_scope import available_scopes

        material_scopes = available_scopes(self.root)
        return {
            "v3Enabled": (
                load(self.root / "config/executable_montage_v3.json").get("enabled")
                if (self.root / "config/executable_montage_v3.json").exists()
                else False
            ),
            "materialScopes": material_scopes,
            "productScopes": [
                {"brand": x["brand"], "goods": g} for x in material_scopes for g in x["goods"]
            ],
            "schema": 1,
            "mode": "feishu_live",
            "baseUrl": self.mapping["url"],
            "fetchedAt": now(),
            "batches": list(groups.values()),
            "tasks": tasks,
            "materials": materials,
            "excluded": excluded,
        }

    def material_media(self, clip_id, kind, verify=True):
        from .core import file_hash

        safe_id(clip_id)
        clip = next(
            (c for c in load(self.root / "catalog/clips.json") if c["clip_id"] == clip_id), None
        )
        if not clip:
            return None
        source = next(
            s
            for s in load(self.root / "catalog/sources.json")
            if s["source_id"] == clip["source_id"]
        )
        if kind == "video":
            path = self.root / "reviews/G2_sample_v1" / f"{clip_id}.mp4"
            receipt = path.with_suffix(".json")
            if not receipt.is_file():
                return None
            row = load(receipt)
            if row.get("source_sha256") != source["file_sha256"] or row.get("profile") != {
                "version": 1,
                "width": 540,
                "height": 960,
                "fps": 30,
                "audio": "muted",
            }:
                return None
            expected = row["output_sha256"]
        else:
            ref = next(
                (
                    r
                    for r in load(self.root / "catalog/ai_analysis_index.json")["records"]
                    if r["clip_id"] == clip_id
                ),
                None,
            )
            if not ref:
                return None
            proposal = Path(ref["proposal_path"])
            if not proposal.is_relative_to(self.root):
                try:
                    proposal = self.root.joinpath(
                        *proposal.parts[proposal.parts.index("analysis") :]
                    )
                except ValueError:
                    return None
            if not proposal.resolve().is_relative_to(self.root) or not proposal.is_file():
                return None
            record = load(proposal)
            if record.get("source_sha256") != source["file_sha256"]:
                return None
            path = Path(record["evidence"]["candidate_sheet_path"])
            if not path.is_relative_to(self.root):
                try:
                    path = self.root.joinpath(*path.parts[path.parts.index("analysis") :])
                except ValueError:
                    return None
            expected = record["evidence"]["candidate_sheet_sha256"]
        path = path.resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        if verify and file_hash(path) != expected:
            raise Blocked("PREVIEW_CHANGED", "审核证据已变化，请重新核对", 6)
        return path

    def media_path(self, fields, kind):
        if (
            kind not in {"candidate", "final"}
            or not fields.get("script_revision")
            or not fields.get("build_id")
        ):
            return None
        task_id, build = safe_id(fields["task_id"]), safe_id(fields["build_id"])
        path = (
            self.root
            / "jobs"
            / task_id
            / f"r{int(fields['script_revision'])}"
            / build
            / f"{kind}.mp4"
        ).resolve()
        if not path.is_relative_to(self.root / "jobs"):
            raise Blocked("MEDIA_PATH_INVALID", "文件路径无效", 6)
        if kind == "final" and not (
            fields.get("stage") == "delivered" and fields.get("human_review") == "approved"
        ):
            return None
        return path if path.is_file() else None

    def media_url(self, fields, kind):
        return (
            f"/api/media/{fields['task_id']}/{fields['build_id']}/{kind}"
            if self.media_path(fields, kind)
            else None
        )

    def auth_user(self):
        r = subprocess.run(
            ["lark-cli", "auth", "status", "--json", "--verify"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(r.stdout)
        user = data.get("identities", {}).get("user", {})
        if not data.get("verified") or not user.get("openId", "").startswith("ou_"):
            raise Blocked("USER_AUTH_REQUIRED", "请核对本机飞书用户登录", 4)
        return {"id": user["openId"], "name": user.get("userName") or "当前飞书用户"}

    def action(self, task_id, body):
        from .script_button import process_once

        row = self.task(task_id)
        if row["fields"].get("director_run_status") == "delivered_cli" and body.get("action") in {
            "director_approve",
            "director_reject",
            "generate",
            "approve",
            "reject",
            "save_copy",
        }:
            raise Blocked("TASK_SCRIPT_LOCKED", "当前任务已交付，修改请新建任务", 3)
        if body.get("action") in {"director_approve", "director_reject"}:
            from .director_stage import review as director_review

            return {
                "status": "verified",
                "stage": "director_review",
                "director": director_review(
                    self,
                    task_id,
                    body.get("director_sha256"),
                    "approve" if body["action"] == "director_approve" else "reject",
                    self.auth_user(),
                ),
            }
        f, rid = row["fields"], row["record_id"]
        expected = body.get("expected")
        if (
            not isinstance(expected, dict)
            or set(expected) != EXPECTED_KEYS
            or any(not field_equal(f.get(k), v) for k, v in expected.items())
        ):
            raise Blocked("INPUT_STALE", "内容或版本已变化，请刷新后操作", 6)
        action = body.get("action")
        from .executable import uses_v3

        if uses_v3(self.root, f):
            if action not in {"generate", "approve", "reject", "save_copy"}:
                raise Blocked("V3_ACTION", "执行脚本修改需退回本批并生成新版本", 3)
            return self.v3_action(row, body)
        if action not in {"generate", "save_draft", "retime", "approve", "reject", "rewrite"}:
            raise Blocked("ACTION_INVALID", "操作无效", 2)
        if f.get("stage") in {"delivered", "waiting_final_review"}:
            raise Blocked(
                "TASK_SCRIPT_LOCKED", "当前任务已进入成片阶段，脚本只读；变更请新建任务", 3
            )
        actor = self.auth_user()
        if action in {"generate", "rewrite"} and body.get("confirm_model_call") is not True:
            raise Blocked("MODEL_CONFIRM_REQUIRED", "本次模型请求须明确确认", 3)
        if f.get("script_action") in {"approve", "reject", "retime", "rewrite"}:
            raise Blocked("ACTION_PENDING", "当前任务已有待处理操作，请先刷新", 6)
        patch: dict[str, Any] = {}
        if action == "save_draft":
            draft, feedback = body.get("draft"), body.get("feedback", "")
            if (
                not isinstance(draft, str)
                or not 0 < len(draft) <= 20000
                or not isinstance(feedback, str)
            ):
                raise Blocked("DRAFT_INVALID", "请保留脚本分镜格式", 2)
            patch = {"script_edit_draft": draft, "script_change_request": feedback}
        elif action != "generate":
            if not f.get("script_revision") or not f.get("script_sha256"):
                raise Blocked("SCRIPT_MISSING", "请先生成脚本", 3)
            patch = {
                "script_action": action,
                "script_action_revision": f["script_revision"],
                "script_action_sha256": f["script_sha256"],
                "script_action_actor": [{"id": actor["id"]}],
                "script_action_draft": f.get("script_edit_draft") or "",
                "script_action_feedback": f.get("script_change_request") or "",
                "script_action_duration": f.get("target_duration_sec") or 24,
                "script_action_input_sha256": f.get("script_input_sha256") or "",
            }
        receipt = {
            "at": now(),
            "source": "authenticated_local_workbench_not_native_button",
            "actor": actor,
            "task_id": task_id,
            "record_id": rid,
            "action": action,
            "bound_input": expected,
            "status": "write_unknown",
            "patch": patch,
        }
        path = self.journal / ("action_" + digest(receipt) + ".json")
        atomic_write(path, receipt)
        if patch:
            self.patch(rid, patch)
            self.verify_patch(task_id, patch)
        if action == "save_draft":
            receipt.update(status="verified", verified_at=now())
            atomic_write(path, receipt)
            return {"status": "verified", "action": action}
        # Scope shared handlers to exactly this record. Explicit generation is
        # passed separately; every remote read remains a truthful readback.
        fm = self.mapping["tables"]["tasks"]["field_ids"]

        def scoped_call(args):
            args = list(args)
            if args[0] == "+record-list":
                args += [
                    "--filter-json",
                    json.dumps({"logic": "and", "conditions": [[fm["task_id"], "==", task_id]]}),
                ]
                data = self.call(args)
                return data
            if args[0] == "+record-get" and args[args.index("--record-id") + 1] != rid:
                raise Blocked("ACTION_SCOPE", "禁止读取其他任务", 6)
            if args[0] == "+record-batch-update":
                body_index = args.index("--json") + 1
                payload = json.loads(args[body_index])
                if "update_records" in payload:
                    updates = payload["update_records"]
                    if set(updates) != {rid}:
                        raise Blocked("ACTION_SCOPE", "禁止修改其他任务", 6)
                    args[body_index] = json.dumps(
                        {"record_id_list": [rid], "patch": updates[rid]}, ensure_ascii=False
                    )
                elif payload.get("record_id_list") != [rid]:
                    raise Blocked("ACTION_SCOPE", "禁止修改其他任务", 6)
            return self.call(args)

        result = process_once(
            self.root, call=scoped_call, requested_task_id=task_id if action == "generate" else None
        )
        errors = result.get("errors", []) + result.get("script_reviews", {}).get("errors", [])
        # Feishu may acknowledge a write before its next read reflects it.
        fresh = self.task(task_id)["fields"]
        for attempt in range(3):
            done = (
                fresh.get("generation_request") == "completed"
                if action == "generate"
                else fresh.get("script_action") not in {"approve", "reject", "retime", "rewrite"}
            )
            if done or errors:
                break
            time.sleep(0.35)
            fresh = self.task(task_id)["fields"]
        verified = not errors and (
            fresh.get("generation_request") == "completed"
            if action == "generate"
            else fresh.get("script_action") not in {"approve", "reject", "retime", "rewrite"}
        )
        receipt.update(
            status="verified" if verified else "needs_reconcile", result=result, processed_at=now()
        )
        atomic_write(path, receipt)
        if not verified:
            if errors:
                raise Blocked(
                    errors[0]["code"],
                    fresh.get("error_summary") or "本次处理受阻，请核对错误后再操作",
                    6,
                )
            raise Blocked(
                "ACTION_NEEDS_RECONCILE", "处理尚未确认成功，请刷新查看错误；不要重复提交", 6
            )
        return {"status": "verified", "action": action}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(root: Path, port: int, *, embedded: bool = False):
    app = Workbench(root)
    public = root / "web/feishu-workbench"
    sessions: dict[str, dict] = {}
    media_grants: dict[str, dict] = {}
    expected_host = f"127.0.0.1:{port}"
    # Opt-in for this tenant only. Both the Base and its official embed plugin
    # are ancestors of the workbench. The ordinary 8767 entry remains self-only.
    ancestors = "'self'"
    if embedded:
        base_url = urlparse(app.mapping["url"])
        if base_url.scheme != "https" or not base_url.hostname.endswith(".feishu.cn"):
            raise ValueError("Embedded mode requires an HTTPS Feishu tenant URL")
        ancestors += f" https://{base_url.hostname} https://ext.baseopendev.com"

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(public), **kwargs)

        def log_message(self, fmt, *args):
            pass  # Avoid logging user text, request bodies, paths or credentials.

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                f"img-src 'self' blob:; media-src 'self'; frame-ancestors {ancestors}",
            )
            super().end_headers()

        def host_ok(self):
            return self.headers.get("Host") == expected_host

        def session(self):
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            item = jar.get("mixcut_workbench")
            # Cross-site iframes cannot use SameSite=Strict cookies. An in-memory
            # header session retains authentication without relaxing cookie/CORS
            # policy or sending any Feishu credential into the browser.
            token = self.headers.get("X-Mixcut-Session") if embedded else None
            result = sessions.get(token or (item.value if item else ""))
            return result if result and time.time() - result["at"] < 8 * 3600 else None

        def media_authorized(self, path):
            from urllib.parse import parse_qs

            if not embedded or not path.startswith(
                ("/api/media/", "/api/previews/", "/api/v3-audio/", "/api/material-video/")
            ):
                return False
            grant = parse_qs(urlparse(self.path).query).get("media_grant", [""])[0]
            item = media_grants.get(grant)
            return bool(item and item["path"] == path and time.time() - item["at"] < 3600)

        def grant_media(self, state):
            expired = [k for k, v in media_grants.items() if time.time() - v["at"] >= 3600]
            for key in expired:
                del media_grants[key]
            for rows, keys in (
                (state["tasks"], ("candidateUrl", "finalUrl", "voiceUrl")),
                (state["materials"], ("previewUrl", "materialVideoUrl")),
            ):
                for row in rows:
                    for key in keys:
                        path = row.get(key)
                        if path:
                            token = secrets.token_urlsafe(32)
                            media_grants[token] = {"path": path, "at": time.time()}
                            row[key] = f"{path}?media_grant={token}"
            return state

        def send_json(self, data, status=200):
            content = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def error_json(self, exc):
            if isinstance(exc, Blocked):
                self.send_json({"error": {"code": exc.code, "message": str(exc)}}, 409)
            else:
                self.send_json(
                    {
                        "error": {
                            "code": "READ_OR_PROCESS_FAILED",
                            "message": "请求未完成，请刷新对账；不会将错误当作空数据或成功。",
                        }
                    },
                    503,
                )

        def list_directory(self, path):
            self.send_error(403)
            return None

        def do_GET(self):
            if not self.host_ok():
                self.send_json({"error": {"code": "HOST_REJECTED"}}, 403)
                return
            path = unquote(urlparse(self.path).path)
            if path == "/api/session":
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                sessions[token] = {"csrf": csrf, "at": time.time()}
                data: dict[str, Any] = {"csrf": csrf, "mode": "feishu_live"}
                if embedded:
                    data.update(sessionToken=token, embedded=True)
                content = json.dumps(data).encode()
                self.send_response(200)
                self.send_header(
                    "Set-Cookie", f"mixcut_workbench={token}; HttpOnly; SameSite=Strict; Path=/"
                )
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            if path.startswith("/api/"):
                if not self.session() and not self.media_authorized(path):
                    self.send_json({"error": {"code": "SESSION_REQUIRED"}}, 403)
                    return
                try:
                    if path == "/api/state":
                        state = app.state()
                        self.send_json(self.grant_media(state) if embedded else state)
                    elif path.startswith("/api/media/"):
                        _, _, _, task_id, build, kind = path.split("/")
                        f = app.task(task_id)["fields"]
                        media = app.media_path(f, kind)
                        if f.get("build_id") != build or not media:
                            raise Blocked("MEDIA_NOT_CURRENT", "当前版本无可播放文件", 6)
                        self.send_file(media, "video/mp4")
                    elif path.startswith("/api/v3-audio/"):
                        from .executable import latest_batch

                        task_id = safe_id(path.rsplit("/", 1)[-1])
                        f = app.task(task_id)["fields"]
                        from .executable import policy

                        if (
                            policy(root, f["request_group_id"]).get("workflow_mode")
                            == "script_first_v1"
                        ):
                            raise Blocked(
                                "SCRIPT_AUDIO_NOT_CURRENT",
                                "当前是文字脚本阶段，配音在剪辑时生成",
                                3,
                            )
                        batch = latest_batch(root, f["request_group_id"])
                        if not batch:
                            raise Blocked("V3_SCRIPT_MISSING", "当前执行脚本不存在", 3)
                        script = next(x for x in batch["scripts"] if x["task_id"] == task_id)
                        media = Path(script["voiceover"]["path"]).resolve()
                        from .core import file_hash

                        if (
                            not media.is_relative_to(root)
                            or not media.is_file()
                            or file_hash(media) != script["voiceover"]["sha256"]
                        ):
                            raise Blocked("V3_AUDIO_CHANGED", "配音文件已变化或不可读", 6)
                        self.send_file(
                            media, "audio/wav" if media.suffix.lower() == ".wav" else "audio/mpeg"
                        )
                    elif path.startswith("/api/material-video/"):
                        media = app.material_media(safe_id(path.rsplit("/", 1)[-1]), "video")
                        if not media:
                            raise Blocked("PREVIEW_MISSING", "审核视频不可用", 3)
                        self.send_file(media, "video/mp4")
                    elif path.startswith("/api/previews/"):
                        clip = safe_id(path.rsplit("/", 1)[-1])
                        media = (
                            app.material_media(clip, "image")
                            or (root / "previews" / clip / "contact.jpg").resolve()
                        )
                        if not media.is_relative_to(root) or not media.is_file():
                            raise Blocked("PREVIEW_MISSING", "预览不可用", 3)
                        self.send_file(media, "image/jpeg")
                    else:
                        self.send_json({"error": {"code": "NOT_FOUND"}}, 404)
                except (Exception,) as exc:
                    self.error_json(exc)
                return
            super().do_GET()

        def send_file(self, path, content_type):
            size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            value = self.headers.get("Range")
            if value:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", value)
                if not match:
                    self.send_error(416)
                    return
                start = int(match[1])
                end = min(size - 1, int(match[2])) if match[2] else size - 1
                if start > end or start >= size:
                    self.send_error(416)
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(remaining, 256 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def do_POST(self):
            session = self.session()
            if (
                not self.host_ok()
                or self.headers.get("Origin") != f"http://{expected_host}"
                or not session
                or self.headers.get("X-Mixcut-CSRF") != session["csrf"]
            ):
                self.send_json({"error": {"code": "REQUEST_REJECTED"}}, 403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 50000:
                    raise Blocked("BODY_INVALID", "请求大小无效", 2)
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise Blocked("BODY_INVALID", "请求格式无效", 2)
                with writer_lock(root, wait_seconds=20):
                    if self.path == "/api/projects":
                        result = app.create_project(body)
                    elif self.path.startswith("/api/delivery/"):
                        from .delivery_package import review

                        task_id = self.path.rsplit("/", 1)[-1]
                        if not body.get("confirmed"):
                            raise Blocked("DELIVERY_REVIEW", "请先确认审核操作", 2)
                        result = review(
                            root,
                            task_id,
                            body.get("package_sha256", ""),
                            body.get("action", ""),
                            app,
                            app.auth_user(),
                            body.get("reason", ""),
                        )
                    elif self.path.startswith("/api/segments/"):
                        result = app.segment_action(self.path.rsplit("/", 1)[-1], body)
                    elif self.path.startswith("/api/materials/"):
                        result = material_action(
                            root,
                            app.mapping,
                            app.call,
                            app.auth_user(),
                            self.path.rsplit("/", 1)[-1],
                            body,
                        )
                    elif self.path.startswith("/api/tasks/"):
                        result = app.action(self.path.rsplit("/", 1)[-1], body)
                    else:
                        raise Blocked("NOT_FOUND", "接口不存在", 2)
                self.send_json(result)
            except (Exception,) as exc:
                self.error_json(exc)

    httpd = Server(("127.0.0.1", port), Handler)
    expected_host = f"127.0.0.1:{httpd.server_port}"
    return httpd


def serve(root: Path, port: int, *, embedded: bool = False):
    httpd = create_server(root, port, embedded=embedded)
    expected_host = f"127.0.0.1:{httpd.server_port}"
    print(f"Mixcut Feishu workbench: http://{expected_host}/", flush=True)
    httpd.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--embedded", action="store_true", help="Allow this Feishu tenant's embed plugin"
    )
    args = parser.parse_args()
    serve(args.workspace.resolve(), args.port, embedded=args.embedded)


if __name__ == "__main__":
    main()
