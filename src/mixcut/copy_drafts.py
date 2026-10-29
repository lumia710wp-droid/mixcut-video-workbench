"""Versioned editable copy after generation/timing failures; no provider calls."""

from __future__ import annotations
import json
import math
from pathlib import Path
from jsonschema import Draft202012Validator
from .core import Blocked, atomic_write, digest, file_hash, load, now
from .doubao import clean_text, request_parameters, spoken_char_hint


def timing_bounds(target_seconds: float) -> tuple[int, int]:
    total = round(target_seconds * 30)
    return max(math.ceil(total * 0.75), total - 45), total - 15


def cached_voice(root: Path, text: str) -> dict | None:
    path = root / "audio/doubao_v3" / digest(request_parameters(text)) / "receipt.json"
    if not path.is_file():
        return None
    voice = load(path)
    if voice.get("status") != "complete":
        return {"status": "unknown_or_failed"}
    audio = Path(voice["path"]).resolve()
    if (
        not audio.is_relative_to(root.resolve())
        or not audio.is_file()
        or file_hash(audio) != voice["sha256"]
    ):
        return {"status": "changed"}
    return voice


def _folder(root: Path, group: str) -> Path:
    from .executable import latest_batch
    from .core import safe_id

    prior = latest_batch(root, group)
    version = (
        prior["version"] + 1
        if prior and prior["state"] == "released"
        else (prior["version"] if prior else 1)
    )
    return root / "requests/executable_v3" / f"{safe_id(group)}_v3_{version}"


def original_copy(folder: Path) -> dict | None:
    from .executable import COPY_SCHEMA

    path = folder / "model_response.json"
    if not path.is_file():
        return None
    response = load(path)
    if response.get("status") != "completed":
        raise Blocked("V3_MODEL_INCOMPLETE", "已有文案响应不完整，请先对账", 5)
    try:
        copy = json.loads(
            "".join(
                c["text"]
                for m in response.get("output", [])
                if m.get("type") == "message"
                for c in m.get("content", [])
                if c.get("type") == "output_text"
            )
        )
    except (ValueError, TypeError) as error:
        raise Blocked("V3_COPY_FORMAT", "已有文案响应无法解析，请先对账", 4) from error
    if list(Draft202012Validator(COPY_SCHEMA).iter_errors(copy)):
        raise Blocked("V3_COPY_SCHEMA", "已有文案格式不符", 4)
    return copy


def effective_copy(
    root: Path, group: str, rows: list[dict], original: dict, *, include_calibration: bool = True
) -> tuple[dict, int]:
    from .executable import input_snapshot, policy, COPY_SCHEMA
    from .voice_timing import calibrated_copy

    if len(original["scripts"]) != len(rows) or {s["task_id"] for s in original["scripts"]} != {
        r["fields"]["task_id"] for r in rows
    }:
        raise Blocked("V3_COPY_DRAFT_TASKS", "原稿任务与当前批次不符，请对账", 6)
    pointer = _folder(root, group) / "copy_drafts/current.json"
    if not pointer.is_file():
        return (
            calibrated_copy(pointer.parent.parent, original)[0] if include_calibration else original
        ), 0
    revision = load(pointer)["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise Blocked("V3_COPY_DRAFT_HASH", "修改稿版本无效", 6)
    record = load(pointer.parent / f"r{revision:04d}.json")
    if (
        record.get("content_sha256")
        != digest({k: v for k, v in record.items() if k != "content_sha256"})
        or record["original_sha256"] != digest(original)
        or record["input_sha256"] != digest(input_snapshot(rows, policy(root, group)))
        or list(Draft202012Validator(COPY_SCHEMA).iter_errors(record["copy"]))
    ):
        raise Blocked("V3_COPY_DRAFT_STALE", "文案修改稿与当前需求/原稿不一致，请对账", 6)
    if {s["task_id"] for s in record["copy"]["scripts"]} != {r["fields"]["task_id"] for r in rows}:
        raise Blocked("V3_COPY_DRAFT_TASKS", "修改稿任务不符", 6)
    return (
        calibrated_copy(pointer.parent.parent, record["copy"])[0]
        if include_calibration
        else record["copy"]
    ), revision


def save_copy_draft(
    root: Path, group: str, rows: list[dict], texts: dict, expected_sha256: str, actor: dict
) -> dict:
    from .executable import latest_batch, input_snapshot, policy

    prior = latest_batch(root, group)
    if prior and prior["state"] != "released":
        raise Blocked("V3_COPY_DRAFT_LOCKED", "已有完整执行包，先退回整批再改稿", 6)
    folder = _folder(root, group)
    original = original_copy(folder)
    if not original:
        raise Blocked("V3_COPY_MISSING", "尚无文案回执", 3)
    current, revision = effective_copy(root, group, rows, original)
    if digest(current) != expected_sha256:
        raise Blocked("V3_COPY_DRAFT_STALE", "文案版本已变，请刷新后再保存", 6)
    if set(texts) != {r["fields"]["task_id"] for r in rows} or any(
        not isinstance(t, str) or not clean_text(t) or len(t) > 1000 for t in texts.values()
    ):
        raise Blocked("V3_COPY_DRAFT_TASKS", "须提交整批每条任务的有效旁白", 2)
    updated = {
        "scripts": [{**s, "narration": texts[s["task_id"]].strip()} for s in current["scripts"]]
    }
    if updated == current:
        return {
            "status": "saved",
            "revision": revision,
            "copy_sha256": digest(current),
            "reused": True,
        }
    record = {
        "version": 1,
        "revision": revision + 1,
        "group_id": group,
        "input_sha256": digest(input_snapshot(rows, policy(root, group))),
        "original_sha256": digest(original),
        "copy": updated,
        "actor": actor,
        "created_at": now(),
    }
    record["content_sha256"] = digest(record)
    path = folder / "copy_drafts" / f"r{revision + 1:04d}.json"
    if path.exists():
        raise Blocked("V3_COPY_DRAFT_CONFLICT", "保留已有修改稿，须先对账", 6)
    atomic_write(path, record)
    atomic_write(path.parent / "current.json", {"revision": revision + 1})
    return {
        "status": "saved",
        "revision": revision + 1,
        "copy_sha256": digest(updated),
        "reused": False,
    }


def draft_status(root: Path, group: str, rows: list[dict]) -> dict | None:
    from .executable import input_snapshot, policy

    folder = _folder(root, group)
    original = original_copy(folder)
    if original is None:
        return None
    saved_input = folder / "input.selected_v1.json"
    if not saved_input.exists():
        saved_input = folder / "input.json"
    if saved_input.exists():
        saved = load(saved_input)
        if {k: saved[k] for k in ("settings", "tasks")} != input_snapshot(
            rows, policy(root, group)
        ):
            raise Blocked("V3_INPUT_CHANGED", "需求已变更，旧文案需先对账", 6)
    copy, revision = effective_copy(root, group, rows, original)
    originals = {s["task_id"]: s for s in original["scripts"]}
    fields = {r["fields"]["task_id"]: r["fields"] for r in rows}
    scripts = []
    for script in copy["scripts"]:
        voice = cached_voice(root, script["narration"])
        old_voice = cached_voice(root, originals[script["task_id"]]["narration"])
        target = fields[script["task_id"]]["target_duration_sec"]
        lo, hi = timing_bounds(target)
        measured = None
        timing_status = "not_measured"
        if voice and voice.get("status") == "complete":
            measured = voice.get("duration_seconds", voice["duration_frames"] / 30)
            timing_status = (
                "short"
                if voice["duration_frames"] < lo
                else "long"
                if voice["duration_frames"] > hi
                else "passed"
            )
        scripts.append(
            {
                **script,
                "characters": len(clean_text(script["narration"])),
                "spoken_char_hint": spoken_char_hint(target),
                "target_seconds": target,
                "minimum_seconds": lo / 30,
                "maximum_seconds": hi / 30,
                "measured_seconds": measured,
                "voice_status": voice.get("status") if voice else "not_generated",
                "timing_status": timing_status,
                "original_measured_seconds": old_voice.get("duration_seconds")
                if old_voice and old_voice.get("status") == "complete"
                else None,
            }
        )
    scripts.sort(
        key=lambda s: (
            fields[s["task_id"]].get("script_ordinal") is None,
            fields[s["task_id"]].get("script_ordinal") or 0,
            s["task_id"],
        )
    )
    return {
        "revision": revision,
        "copy_sha256": digest(copy),
        "scripts": scripts,
        "model_response_reused": True,
        "maximum_correction_rounds": 2,
    }
