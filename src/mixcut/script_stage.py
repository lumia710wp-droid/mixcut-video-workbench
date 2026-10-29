"""Text-only script stage. No material allocation, TTS, audio, or editing calls."""

from __future__ import annotations
import math
from jsonschema import Draft202012Validator
from .core import Blocked, atomic_write, digest, load, now, safe_id

MODE = "script_first_v1"


def normalize_model_copy(request, result):
    """Accept only a redundant, exactly matching target echo from text models.

    Keep unknown keys subject to the strict copy schema. The human target stays
    authoritative; a changed or invented target is never silently discarded.
    """
    if not isinstance(result, dict) or not isinstance(result.get("scripts"), list):
        return result
    targets = {t["task_id"]: t["target_duration_sec"] for t in request["tasks"]}
    scripts = []
    for script in result["scripts"]:
        if not isinstance(script, dict) or "target_duration_sec" not in script:
            scripts.append(script)
            continue
        echoed = script["target_duration_sec"]
        if (
            isinstance(echoed, bool)
            or not isinstance(echoed, (int, float))
            or script.get("task_id") not in targets
            or echoed != targets[script["task_id"]]
        ):
            raise Blocked("SCRIPT_TARGET_MISMATCH", "模型返回的目标时长与需求不符，已保留响应", 4)
        scripts.append({k: v for k, v in script.items() if k != "target_duration_sec"})
    return {**result, "scripts": scripts}


def length_budget(seconds):
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not 6 <= seconds <= 120
    ):
        raise Blocked("SCRIPT_DURATION", "目标时长须为6—120秒", 2)
    return {
        "minimum_characters": math.ceil(seconds * 4.2),
        "maximum_characters": math.ceil(seconds * 5.8),
        "suggested_characters": round(seconds * 5),
        "assumed_characters_per_second": 5,
    }


def length_review(script):
    """Audition-based word estimates allow half a second of uncertainty, never measured TTS."""
    from .doubao import clean_text

    check = script.get("length_check")
    if not check:
        return {"status": "passed"}
    count = len(clean_text(script["narration"]))
    low, high = check["minimum_characters"], check["maximum_characters"]
    tolerance = check["assumed_characters_per_second"] * 0.5
    return {
        "status": "passed"
        if low <= count <= high
        else "estimate_warning"
        if low - tolerance <= count <= high + tolerance
        else "needs_revision",
        "direction": "short" if count < low else "long",
        "characters": count,
        "minimum_characters": low,
        "maximum_characters": high,
        "estimate_tolerance_seconds": 0.5,
    }


def approval_disabled_reason(script, input_changed=False):
    if not script:
        return "尚未生成文字稿"
    if script["review_status"] == "approved":
        return "此条文字与标题样式已确认"
    if input_changed:
        return "需求已变化，请退回当前稿后重新生成"
    if not script.get("title_style"):
        return "标题样式尚未绑定"
    if length_review(script)["status"] == "needs_revision":
        return "本条文案明显偏短或偏长，请修改正文并保存后再确认"
    return ""


def folder(root, group):
    return root / "jobs/script_drafts_v1" / safe_id(group)


def script_hash(script):
    return digest(
        {
            k: v
            for k, v in script.items()
            if k not in {"content_sha256", "approval", "review_status"}
        }
    )


def latest(root, group):
    path = folder(root, group) / "current.json"
    if not path.exists():
        return None
    record = load(path.parent / f"r{load(path)['revision']:04d}.json")
    if any(s.get("content_sha256") != script_hash(s) for s in record["scripts"]):
        raise Blocked("SCRIPT_DRAFT_CHANGED", "脚本文件内容变化，请先对账", 6)
    if record.get("content_sha256") != digest([s["content_sha256"] for s in record["scripts"]]):
        raise Blocked("SCRIPT_DRAFT_CHANGED", "脚本批次摘要变化，请先对账", 6)
    return record


def snapshot(root, group, rows):
    from .executable import input_snapshot, policy

    return input_snapshot(rows, policy(root, group))


def _copy(record):
    return {
        "scripts": [
            {k: s[k] for k in ("task_id", "title", "narration", "headline")}
            for s in record["scripts"]
        ]
    }


def _validate_copy(copy, rows, root=None, preserve=None):
    from .executable import COPY_SCHEMA
    from .doubao import clean_text

    if list(Draft202012Validator(COPY_SCHEMA).iter_errors(copy)):
        raise Blocked("SCRIPT_COPY_SCHEMA", "脚本文字结构无效", 4)
    by_task = {s["task_id"]: s for s in copy["scripts"]}
    if len(by_task) != len(copy["scripts"]) or set(by_task) != {
        r["fields"]["task_id"] for r in rows
    }:
        raise Blocked("SCRIPT_TASK_MISMATCH", "脚本任务有遗漏或重复", 4)
    failures = []
    for row in rows:
        f = row["fields"]
        s = by_task[f["task_id"]]
        old = (preserve or {}).get(f["task_id"])
        if old and old["narration"] == s["narration"]:
            continue
        budget = length_budget(f["target_duration_sec"])
        if root is not None:
            from .director_execution import estimated_budget

            budget = estimated_budget(root, f["task_id"], f["target_duration_sec"]) or budget
        count = len(clean_text(s["narration"]))
        if not budget["minimum_characters"] <= count <= budget["maximum_characters"]:
            failures.append(
                {
                    "task_id": f["task_id"],
                    "narration": s["narration"],
                    "characters": count,
                    "target_seconds": f["target_duration_sec"],
                    **budget,
                }
            )
    return failures


def _save(root, group, record):
    atomic_write(folder(root, group) / f"r{record['revision']:04d}.json", record)
    atomic_write(folder(root, group) / "current.json", {"revision": record["revision"]})


def _state(scripts):
    if all(s["review_status"] == "approved" for s in scripts):
        return "approved"
    return "changes" if any(s["review_status"] == "changes" for s in scripts) else "pending"


def store_copy(root, group, rows, copy, *, provenance, allow_length_warning=False):
    from .doubao import clean_text

    prior = latest(root, group)
    bound = snapshot(root, group, rows)
    preserve = (
        {s["task_id"]: s for s in prior["scripts"]}
        if prior and prior["input_sha256"] == digest(bound)
        else {}
    )
    failures = _validate_copy(copy, rows, root, preserve)
    if not allow_length_warning and any(
        length_review({"narration": f["narration"], "length_check": f})["status"]
        == "needs_revision"
        for f in failures
    ):
        raise Blocked("SCRIPT_TEXT_LENGTH", "文案字数与目标时长不符，已保留文字回执；未生成配音", 3)
    length_failures = {f["task_id"]: f for f in failures}
    revision = prior["revision"] + 1 if prior else 1
    if (folder(root, group) / f"r{revision:04d}.json").exists():
        raise Blocked("SCRIPT_DRAFT_CONFLICT", "保留已有脚本版本，请先对账", 6)
    bound = snapshot(root, group, rows)
    by_task = {s["task_id"]: s for s in copy["scripts"]}
    scripts = []
    for task in bound["tasks"]:
        s = by_task[task["task_id"]]
        seconds = task["target_duration_sec"]
        old = next(
            (x for x in (prior or {}).get("scripts", []) if x["task_id"] == task["task_id"]), None
        )
        if (
            old
            and prior["input_sha256"] == digest(bound)
            and old["review_status"] != "changes"
            and all(old[k] == s[k] for k in ("title", "narration", "headline"))
        ):
            scripts.append(old)
            continue
        item = {
            **s,
            "target_seconds": seconds,
            "characters": len(clean_text(s["narration"])),
            "estimated_seconds": round(len(clean_text(s["narration"])) / 5, 2),
            "timing_basis": "text_length_estimate_not_audio_measurement",
            "length_budget": length_budget(seconds),
            "input_sha256": digest(bound),
            "revision": revision,
            "group_id": group,
            "review_status": "pending",
            "approval": None,
        }
        if task["task_id"] in length_failures:
            failure = length_failures[task["task_id"]]
            item["length_check"] = {
                "status": "needs_revision",
                "direction": "short"
                if failure["characters"] < failure["minimum_characters"]
                else "long",
                **{k: v for k, v in failure.items() if k not in {"task_id", "narration"}},
            }
            item["length_check"]["status"] = length_review(item)["status"]
        if "goods_scope" in bound["settings"]:
            from .product_scope import scope_values

            item["material_scope"] = scope_values(bound["settings"])
        if (root / "config/director_typography_v1.json").is_file():
            from .title_style import snapshot as title_style_snapshot

            item["title_style"] = title_style_snapshot(root)
        from .director_execution import estimated_budget

        budget = estimated_budget(root, task["task_id"], seconds)
        if budget:
            item["length_budget"] = budget
            item["estimated_seconds"] = round(
                item["characters"] / budget["assumed_characters_per_second"], 2
            )
        item["content_sha256"] = script_hash(item)
        scripts.append(item)
    record = {
        "entity": "text_script_batch",
        "group_id": group,
        "revision": revision,
        "state": _state(scripts),
        "input_sha256": digest(bound),
        "scripts": scripts,
        "created_at": now(),
        "provenance": provenance,
        "content_sha256": digest([s["content_sha256"] for s in scripts]),
    }
    atomic_write(folder(root, group) / f"input_r{revision:04d}.json", bound)
    _save(root, group, record)
    return record


def generate(root, group, rows, *, copy_fn=None):
    from .executable import generate_copy

    prior = latest(root, group)
    bound = snapshot(root, group, rows)
    if prior and prior["state"] != "changes":
        if prior["input_sha256"] != digest(bound):
            raise Blocked("SCRIPT_INPUT_CHANGED", "需求已变更，先退回当前脚本", 6)
        return prior
    revision = prior["revision"] + 1 if prior else 1
    target = root / "requests/script_drafts_v1" / f"{safe_id(group)}_r{revision:04d}"
    # Only creative requirements and explicitly supplied product scope go to the copy AI.
    selected_rows = [
        r
        for r in rows
        if not prior
        or any(
            s["task_id"] == r["fields"]["task_id"] and s["review_status"] == "changes"
            for s in prior["scripts"]
        )
    ]
    selected_ids = {r["fields"]["task_id"] for r in selected_rows}
    if (root / "config/director_voice_policy.json").exists():
        from .director_execution import assign_voice

        for row in sorted(
            selected_rows,
            key=lambda r: (r["fields"].get("script_ordinal", 0), r["fields"]["task_id"]),
        ):
            assign_voice(root, row["fields"]["task_id"])
    from .director_execution import estimated_budget

    request = {
        "script_stage": "text_only_v1",
        "settings": {
            k: v
            for k, v in bound["settings"].items()
            if k in {"brand_id", "goods_id", "goods_scope", "goods_ids", "narration_mode"}
        },
        "tasks": [t for t in bound["tasks"] if t["task_id"] in selected_ids],
        "script_length": [
            {
                "task_id": t["task_id"],
                **(
                    estimated_budget(root, t["task_id"], t["target_duration_sec"])
                    or length_budget(t["target_duration_sec"])
                ),
            }
            for t in bound["tasks"]
            if t["task_id"] in selected_ids
        ],
    }
    input_file = target / "input_snapshot.json"
    if input_file.exists() and load(input_file) != bound:
        raise Blocked("SCRIPT_INPUT_CHANGED", "已有脚本请求绑定其他需求，请先对账", 6)
    atomic_write(input_file, bound)
    provider = copy_fn or generate_copy
    copy = provider(root, request, target)
    failures = _validate_copy(copy, selected_rows, root)
    if failures:
        # A completed text response may receive one distinct length correction, never TTS.
        correction = {**request, "text_length_correction": failures, "previous_copy": copy}
        corrected = provider(root, correction, target / "length_correction_1")
        _validate_copy(corrected, selected_rows, root)
        failed_ids = {f["task_id"] for f in failures}
        replacements = {s["task_id"]: s for s in corrected["scripts"]}
        copy = {
            "scripts": [
                replacements[s["task_id"]] if s["task_id"] in failed_ids else s
                for s in copy["scripts"]
            ]
        }
    if prior:
        replacement = {s["task_id"]: s for s in copy["scripts"]}
        copy = {"scripts": [replacement.get(s["task_id"], s) for s in _copy(prior)["scripts"]]}
    return store_copy(
        root,
        group,
        rows,
        copy,
        provenance={"type": "script_ai", "audio_generated": False},
        allow_length_warning=True,
    )


def edit(root, group, rows, texts, expected_hash, *, headlines=None):
    record = latest(root, group)
    if not record or record["content_sha256"] != expected_hash:
        raise Blocked("SCRIPT_DRAFT_STALE", "脚本版本已变，请刷新", 6)
    if record["input_sha256"] != digest(snapshot(root, group, rows)):
        raise Blocked("SCRIPT_INPUT_CHANGED", "需求变化，先退回旧稿", 6)
    copy = _copy(record)
    if (
        not isinstance(texts, dict)
        or not texts
        or not set(texts) <= {s["task_id"] for s in copy["scripts"]}
    ):
        raise Blocked("SCRIPT_TASK_MISMATCH", "须提交本批任务的脚本正文", 2)
    for s in copy["scripts"]:
        if s["task_id"] not in texts:
            continue
        text = texts[s["task_id"]]
        if not isinstance(text, str):
            raise Blocked("SCRIPT_COPY_SCHEMA", "脚本正文须为文字", 2)
        s["narration"] = text.strip()
    if headlines is not None:
        if not isinstance(headlines, dict) or not set(headlines) <= {
            s["task_id"] for s in copy["scripts"]
        }:
            raise Blocked("SCRIPT_TASK_MISMATCH", "标题必须属于当前任务", 2)
        for s in copy["scripts"]:
            if s["task_id"] in headlines:
                s["headline"] = headlines[s["task_id"]]
    if copy == _copy(record):
        return record
    return store_copy(
        root,
        group,
        rows,
        copy,
        provenance={"type": "manual_edit", "prior_revision": record["revision"]},
    )


def review(root, group, rows, task_id, expected_hash, action, actor):
    record = latest(root, group)
    if not record or not actor.get("id") or action not in {"approve", "reject"}:
        raise Blocked("SCRIPT_REVIEW_REQUIRED", "脚本或审核身份无效", 3)
    script = next((s for s in record["scripts"] if s["task_id"] == task_id), None)
    if not script or script["content_sha256"] != expected_hash:
        raise Blocked("SCRIPT_DRAFT_STALE", "脚本版本变化，请刷新", 6)
    if action == "reject":
        script.update(review_status="changes", approval=None)
        record["state"] = _state(record["scripts"])
    else:
        if length_review(script)["status"] == "needs_revision":
            raise Blocked(
                "SCRIPT_TEXT_LENGTH",
                "当前单条文案估计偏短或偏长，请修改正文后再审核；其他稿件可独立审核",
                3,
            )
        if (root / "config/director_typography_v1.json").is_file():
            from .title_style import validate as validate_title_style

            validate_title_style(script)
        if record["input_sha256"] != digest(snapshot(root, group, rows)):
            raise Blocked("SCRIPT_INPUT_CHANGED", "当前脚本已退回或需求已变，不能批准", 6)
        script.update(
            review_status="approved",
            approval={"actor_id": actor["id"], "at": now(), "content_sha256": expected_hash},
        )
        record["state"] = _state(record["scripts"])
        if all(s["review_status"] == "approved" for s in record["scripts"]):
            record["state"] = "approved"
            atomic_write(
                folder(root, group) / f"handoff_r{record['revision']:04d}.json",
                {
                    "entity": "codex_director_script_handoff",
                    "group_id": group,
                    "script_batch_sha256": record["content_sha256"],
                    "scripts": record["scripts"],
                    "next_stage": "codex_director_material_search",
                    "voice_generation_stage": "editing",
                    "rule": "剪辑阶段根据已确认脚本生成配音；不得为了配音实测自动改写已确认文字。",
                },
            )
    _save(root, group, record)
    return record
