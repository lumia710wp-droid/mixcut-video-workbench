"""Fixed voice rotation, word-time captions, timing rules and media preflight."""

from __future__ import annotations
import math
import re
import subprocess
import json
import copy
import os
import tempfile
import wave
from pathlib import Path
from .core import Blocked, atomic_write, digest, file_hash, load
from .doubao import clean_text


def assign_voice(root, task):
    policy = load(root / "config/director_voice_policy.json")
    voices = policy["voices"]
    if len(voices) != 4 or len({v["speaker"] for v in voices}) != 4 or policy["speech_rate"] != 8:
        raise Blocked("DIRECTOR_VOICE_POLICY", "四音色及+8配置不符", 2)
    p = root / "ledger/director_voice_assignments.json"
    state = load(p) if p.exists() else {"next_index": 0, "tasks": {}}
    if task not in state["tasks"]:
        selected = voices[state["next_index"] % len(voices)]
        state["tasks"][task] = {
            "name": selected["name"],
            "speaker": selected["speaker"],
            "speech_rate": 8,
            "policy_sha256": digest(policy),
            "calibration": selected.get("calibration"),
        }
        state["next_index"] += 1
        atomic_write(p, state)
    return state["tasks"][task]


def estimated_budget(root, task, seconds):
    """Reuse a measured speaker estimate; never synthesize to estimate script length."""
    path = root / "ledger/director_voice_assignments.json"
    voice = load(path)["tasks"].get(task) if path.is_file() else None
    calibration = (voice or {}).get("calibration")
    if not voice or not calibration:
        return None
    cps = calibration["characters"] / calibration["duration_seconds"]
    return {
        "minimum_characters": math.ceil(seconds * 0.85 * cps),
        "maximum_characters": math.floor(seconds * 0.95 * cps),
        "suggested_characters": round(seconds * 0.9 * cps),
        "assumed_characters_per_second": round(cps, 4),
        "estimate_basis": "speaker_audition_not_current_script_audio",
        "speaker": voice["speaker"],
    }


def timing(duration, target):
    if not all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0
        for x in (duration, target)
    ):
        raise Blocked("DIRECTOR_AUDIO_TIMING", "实际配音时长无效", 2)
    low, high = round(target * 0.85, 3), round(target * 0.95, 3)
    return {
        "minimum_seconds": low,
        "maximum_seconds": high,
        "measured_seconds": duration,
        "status": "short" if duration < low else "long" if duration > high else "passed",
        "voice_start_frame": 6,
        "target_frames": round(target * 30),
        "fits_video": math.ceil(duration * 30) + 6 <= round(target * 30),
    }


def fit_voice_to_target(root, config, voice, target_seconds):
    """Create a pitch-preserving, measured derivative and retime word boundaries."""
    ratio = config.get("audio", {}).get("voice_target_ratio", 0.95)
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 < ratio <= 1:
        raise Blocked("DIRECTOR_AUDIO_TARGET", "配音目标比例无效", 2)
    limit = min(target_seconds * ratio, (round(target_seconds * 30) - 6) / 30)
    actual = voice["duration_seconds"]
    if actual <= limit:
        return voice
    source = Path(voice["path"]).resolve()
    if not source.is_relative_to(root.resolve()) or file_hash(source) != voice["sha256"]:
        raise Blocked("DIRECTOR_AUDIO_CHANGED", "原始配音已变化", 6)
    # Small margin absorbs the resampler's rounding without cutting speech.
    speed = actual / max(limit - min(0.05, limit * 0.01), limit * 0.9)
    identity = {"source_sha256": voice["sha256"], "target_seconds": limit, "algorithm": "atempo-v1"}
    directory = root / "audio/tempo_fit" / digest(identity)
    directory.mkdir(parents=True, exist_ok=True)
    receipt_path, output = directory / "receipt.json", directory / "speech.wav"
    if receipt_path.exists():
        saved = load(receipt_path)
        if not output.is_file() or file_hash(output) != saved["sha256"]:
            raise Blocked("DIRECTOR_AUDIO_CHANGED", "加速配音文件已变化", 6)
        return saved
    if output.exists():
        raise Blocked("DIRECTOR_AUDIO_UNKNOWN", "已有无回执配音文件，须先核对", 6)
    factors = []
    remaining = speed
    while remaining > 2:
        factors.append(2.0)
        remaining /= 2
    factors.append(remaining)
    filter_text = ",".join(f"atempo={x:.10f}" for x in factors)
    fd, temp_name = tempfile.mkstemp(prefix="tempo-", suffix=".wav", dir=directory)
    os.close(fd)
    try:
        subprocess.run(
            [
                config["paths"]["ffmpeg"],
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-af",
                filter_text,
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                temp_name,
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        with wave.open(temp_name, "rb") as stream:
            duration = stream.getnframes() / stream.getframerate()
        if duration > limit or duration <= 0:
            raise Blocked("DIRECTOR_AUDIO_TARGET", "加速后实测时长仍超过目标", 6)
        result = copy.deepcopy(voice)
        scale = duration / actual
        for sentence in result.get("sentences", []):
            for word in sentence.get("words", []):
                word["startTime"] *= scale
                word["endTime"] *= scale
        os.replace(temp_name, output)
        result.update(
            path=str(output),
            sha256=file_hash(output),
            duration_seconds=duration,
            duration_frames=math.ceil(duration * 30),
            tempo_fit={
                **identity,
                "source_path": str(source),
                "source_seconds": actual,
                "speed": speed,
                "timestamp_scale": scale,
            },
        )
        atomic_write(receipt_path, result)
        return result
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def captions(text, receipt, total_frames):
    words = [w for s in receipt["sentences"] for w in s.get("words", []) if clean_text(w["word"])]
    if clean_text("".join(w["word"] for w in words)) != clean_text(text):
        raise Blocked("DIRECTOR_WORD_TEXT", "字级字幕与原稿不符", 6)
    duration = receipt["duration_seconds"]
    if any(not 0 <= w["startTime"] <= w["endTime"] <= duration for w in words) or any(
        a["startTime"] > b["startTime"] for a, b in zip(words, words[1:])
    ):
        raise Blocked("DIRECTOR_WORD_TIMES", "字级字幕时间戳无效", 6)
    groups = [x for x in re.split(r"[\s，。！？；,!?;]+", text) if clean_text(x)]
    positions = [w["startTime"] for w in words for _ in clean_text(w["word"])]
    result = []
    cursor = 0
    for text in groups:
        start = 6 + math.floor(positions[cursor] * 30)
        cursor += len(clean_text(text))
        end = 6 + math.floor(positions[cursor] * 30) if cursor < len(positions) else total_frames
        if not 0 <= start < end <= total_frames:
            raise Blocked("DIRECTOR_CAPTION_TIMING", "字幕超出成片或切点不足一帧", 6)
        result.append({"text": text, "start_frame": start, "end_frame": end})
    return result


def media_preflight(root, packet, config):
    report = []
    facts = {
        f["source"]["source_id"]: f
        for f in load(root / "catalog/material_facts_v1.json")["materials"]
    }
    for shot in packet["shots"]:
        sid = shot["source_id"]
        path = root / "media/normalized_cfr30_v1" / f"{sid}.mp4"
        receipt = path.with_suffix(".json")
        if not path.is_file() or not receipt.is_file():
            raise Blocked("DIRECTOR_MEDIA_MISSING", f"缺少完整工作副本：{sid}", 3)
        meta = load(receipt)
        if meta.get("source_sha256") != facts[sid]["source"]["file_sha256"]:
            raise Blocked("DIRECTOR_MEDIA_SOURCE", "工作副本不属于当前原片", 6)
        if (
            file_hash(path) != meta["working_sha256"]
            or shot["source_out_frame"] > meta["total_frames"]
        ):
            raise Blocked("DIRECTOR_MEDIA_CHANGED", "工作副本摘要或帧数不符", 6)
        probe = json.loads(
            subprocess.check_output(
                [
                    config["paths"]["ffprobe"],
                    "-v",
                    "error",
                    "-show_streams",
                    "-of",
                    "json",
                    str(path),
                ],
                text=True,
            )
        )
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        if (video["width"], video["height"], video["avg_frame_rate"]) != (1080, 1920, "30/1"):
            raise Blocked("DIRECTOR_MEDIA_PROFILE", "工作副本不是1080×1920/30fps", 6)
        # Preflight sees baked letterbox before the reviewed plan is executed.
        check = subprocess.run(
            [
                config["paths"]["ffmpeg"],
                "-hide_banner",
                "-ss",
                str(shot["source_in_frame"] / 30),
                "-i",
                str(path),
                "-t",
                str(min(1, shot["duration_frames"] / 30)),
                "-vf",
                "cropdetect=24:2:0",
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
        )
        if check.returncode:
            raise Blocked("DIRECTOR_MEDIA_DECODE", "镜头预检解码失败", 6)
        crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", check.stderr)
        letterbox = any(int(w) == 1080 and int(h) < 1900 and int(y) > 5 for w, h, x, y in crops)
        recommended = None
        if crops and letterbox:
            w, h, x, y = map(int, max(set(crops), key=crops.count))
            if w == 1080 and x == 0 and abs((1920 - h - y) - y) <= 8 and 1 < 1920 / h <= 1.4:
                recommended = {"scale": math.ceil(1920 / h * 100) / 100}
        resolved = not letterbox or bool(recommended and shot.get("transform") == recommended)
        report.append(
            {
                "source_id": sid,
                "path": str(path),
                "working_sha256": meta["working_sha256"],
                "letterbox_detected": letterbox,
                "recommended_transform": recommended,
                "crop_resolved": resolved,
            }
        )
    return {
        "status": "needs_crop_review" if any(not x["crop_resolved"] for x in report) else "passed",
        "items": report,
    }


def prepare_audio(root, task, config):
    from .director_stage import gate, folder
    from .script_stage import latest
    from .doubao import synthesize

    record = gate(root, task)
    script = next(s for s in latest(root, record["group_id"])["scripts"] if s["task_id"] == task)
    voice = record["packet"]["voice"]
    existing_path = folder(root, task) / f"audio_r{record['revision']:04d}.json"
    if existing_path.exists():
        existing = load(existing_path)
        if existing.get("status") == "ready_for_editing":
            if (
                existing.get("director_sha256") != record["packet_sha256"]
                or not Path(existing["audio"]["path"]).is_file()
                or file_hash(Path(existing["audio"]["path"])) != existing["audio"]["sha256"]
            ):
                raise Blocked("DIRECTOR_AUDIO_CHANGED", "已完成配音或审核版本变化，先对账", 6)
            return existing
    # synthesize has a durable intent/cache: unknown/failed results are never resent.
    receipt = synthesize(
        root,
        config,
        script["narration"],
        speaker=voice["speaker"],
        speech_rate=voice["speech_rate"],
    )
    receipt = fit_voice_to_target(root, config, receipt, script["target_seconds"])
    measured = timing(receipt["duration_seconds"], script["target_seconds"])
    result = {
        "task_id": task,
        "director_sha256": record["packet_sha256"],
        "voice": voice,
        "audio": receipt,
        "timing": measured,
        "status": "ready_for_editing"
        if measured["status"] != "short" and measured["fits_video"]
        else "needs_timing_review",
    }
    if result["status"] == "ready_for_editing":
        result["captions"] = captions(
            script["narration"], receipt, record["packet"]["total_frames"]
        )
    atomic_write(folder(root, task) / f"audio_r{record['revision']:04d}.json", result)
    return result
