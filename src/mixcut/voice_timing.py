"""Measured copy calibration, with two durable semantic correction rounds.

Each model dispatch still has one request and zero transport retries. An unknown
request stops immediately. Completed short/long speech may trigger a new, distinct
copy request; accepted corrections and audio survive refreshes and interruptions.
"""

from __future__ import annotations
import math
from pathlib import Path
from .core import Blocked, atomic_write, digest, load
from .doubao import clean_text, trim_silent_rounding_tail

MAX_CORRECTION_ROUNDS = 2


def _session(folder: Path, copy: dict) -> Path:
    return folder / "timing_calibration" / digest(copy)


def _accepted(path: Path, copy: dict) -> dict:
    record = load(path)
    if record.get("content_sha256") != digest(
        {k: v for k, v in record.items() if k != "content_sha256"}
    ) or record.get("input_copy_sha256") != digest(copy):
        raise Blocked("V3_CALIBRATION_CHANGED", "文案校准记录变化，请先对账", 6)
    updated = record["copy"]
    # Only narration may change; metadata and exact task set stay bound to the base.
    if len(updated["scripts"]) != len(copy["scripts"]) or [
        {k: v for k, v in s.items() if k != "narration"} for s in updated["scripts"]
    ] != [{k: v for k, v in s.items() if k != "narration"} for s in copy["scripts"]]:
        raise Blocked("V3_CALIBRATION_CHANGED", "校准只能修改当前任务旁白", 6)
    return updated


def calibrated_copy(folder: Path, base: dict) -> tuple[dict, int]:
    copy = base
    completed = 0
    for n in range(1, MAX_CORRECTION_ROUNDS + 1):
        path = _session(folder, base) / f"round_{n}" / "accepted.json"
        if not path.exists():
            break
        copy = _accepted(path, copy)
        completed = n
    return copy, completed


def calibrate_voice(root, config, folder, request, base, rows, tts_fn, correction_fn):
    from .copy_drafts import timing_bounds

    copy, completed = calibrated_copy(folder, base)
    session = _session(folder, base)
    voices: dict = {}
    for round_number in range(completed, MAX_CORRECTION_ROUNDS + 1):
        by_task = {s["task_id"]: s for s in copy["scripts"]}
        audio, failures = {}, []
        timing: dict = {
            "status": "measuring",
            "correction_rounds": round_number,
            "maximum_correction_rounds": MAX_CORRECTION_ROUNDS,
            "tasks": {r["fields"]["task_id"]: {"status": "not_measured"} for r in rows},
        }
        for row in rows:
            f = row["fields"]
            task = f["task_id"]
            text = by_task[task]["narration"]
            if text not in voices:
                # synthesize uses a durable, hash-verified cache and blocks unknown outcomes.
                voices[text] = tts_fn(root, config, text)
            voice = voices[text]
            minimum, maximum = timing_bounds(f["target_duration_sec"])
            voice = trim_silent_rounding_tail(root, config, voice, maximum)
            actual = voice.get("duration_seconds", voice["duration_frames"] / 30)
            if not isinstance(actual, (int, float)) or not math.isfinite(actual) or actual <= 0:
                raise Blocked("V3_VOICE_MEASUREMENT", "配音实测时长无效，请先对账", 6)
            passed = minimum <= voice["duration_frames"] <= maximum
            timing["tasks"][task] = {
                "status": "passed" if passed else "blocked",
                "actual_seconds": actual,
                "target_seconds": f["target_duration_sec"],
                "minimum_seconds": minimum / 30,
                "maximum_seconds": maximum / 30,
                "text_sha256": digest(text),
            }
            atomic_write(folder / "timing_report.json", timing)
            audio[task] = voice
            if not passed:
                desired = max(minimum / 30, maximum / 30 - 0.2)
                failures.append(
                    {
                        "task_id": task,
                        "narration": text,
                        "measured_seconds": actual,
                        "target_seconds": f["target_duration_sec"],
                        "desired_voice_seconds": desired,
                        "spoken_char_hint": max(
                            1, min(950, round(len(clean_text(text)) * desired / actual))
                        ),
                        "direction": "expand" if voice["duration_frames"] < minimum else "shorten",
                    }
                )
        timing["status"] = "passed" if not failures else "calibrating"
        atomic_write(folder / "timing_report.json", timing)
        atomic_write(session / f"measurement_{round_number}.json", timing)
        if not failures:
            return copy, audio
        if round_number == MAX_CORRECTION_ROUNDS:
            timing["status"] = "correction_limit"
            atomic_write(folder / "timing_report.json", timing)
            first = failures[0]
            raise Blocked(
                "V3_VOICE_DURATION",
                f"已自动校准{MAX_CORRECTION_ROUNDS}轮，{first['task_id']}实测{first['measured_seconds']:.3f}秒仍未接近目标{first['target_seconds']:g}秒；已保留全部文案与音频，不再继续调用",
                3,
            )
        ids = {f["task_id"] for f in failures}
        correction_request = {
            **request,
            "tasks": [t for t in request["tasks"] if t["task_id"] in ids],
            "available_visuals": [
                v for v in request.get("available_visuals", []) if v.get("task_id") in ids
            ],
            "voice_calibration": failures,
        }
        correction_request.pop("allocation", None)
        if round_number:
            correction_request["calibration_followup"] = (
                "上一轮校准后仍未达标。本轮必须按实测反馈重写旁白，"
                "统计不含标点字数，达到spoken_char_hint前后1字；不得原样返回。"
            )
        next_folder = session / f"round_{round_number + 1}"
        next_folder.mkdir(parents=True, exist_ok=True)
        result = correction_fn(root, correction_request, next_folder)
        from .executable import COPY_SCHEMA
        from jsonschema import Draft202012Validator

        if list(Draft202012Validator(COPY_SCHEMA).iter_errors(result)):
            raise Blocked("V3_COPY_SCHEMA", "校准文案格式无效，已保留响应", 4)
        replacements = {s["task_id"]: s["narration"] for s in result["scripts"]}
        if len(replacements) != len(result["scripts"]) or set(replacements) != ids:
            raise Blocked("V3_TASK_MISMATCH", "校准响应任务不符，已保留响应", 4)
        if any(not clean_text(t) for t in replacements.values()):
            raise Blocked("V3_CALIBRATION_NO_CHANGE", "校准没有产生有效旁白，已停止调用", 4)
        updated = {
            "scripts": [
                {**s, "narration": replacements.get(s["task_id"], s["narration"])}
                for s in copy["scripts"]
            ]
        }
        # Even an unchanged completed response consumes this semantic round.
        # Persist it so refresh cannot silently reset the call budget.
        record = {"input_copy_sha256": digest(copy), "copy": updated}
        record["content_sha256"] = digest(record)
        atomic_write(next_folder / "accepted.json", record)
        copy = updated
    raise AssertionError("unreachable")
