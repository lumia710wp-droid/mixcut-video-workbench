"""Local macOS TTS with immutable files and measured, bounded shot timing."""

from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path

from .contracts import require_approval
from .core import Blocked, atomic_write, digest, file_hash, load, safe_id
from .relocation import runtime_path, preserved_timing


def voice_config_hash(audio: dict) -> str:
    return digest(
        {k: audio.get(k) for k in ["provider", "provider_version", "voice", "words_per_minute"]}
    )


def measure_wav(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as audio:
            frames, rate = audio.getnframes(), audio.getframerate()
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or rate != 48000
                or frames <= 0
            ):
                raise Blocked("AUDIO_FORMAT", "Expected nonempty 48kHz mono PCM16")
            consumed = 0
            peak = 0
            import array

            while data := audio.readframes(48000):
                values = array.array("h", data)
                peak = max(peak, max(abs(x) for x in values))
                consumed += len(values)
            if consumed != frames or peak == 0:
                raise Blocked("AUDIO_DECODE", "Truncated or silent audio")
    except (wave.Error, EOFError) as exc:
        raise Blocked("AUDIO_DECODE", "Unreadable WAV") from exc
    return {
        "duration_seconds": frames / rate,
        "samples": frames,
        "sample_rate": rate,
        "duration_frames_30fps": math.ceil(frames / 1600),
        "peak_pcm16": peak,
        "sha256": file_hash(path),
    }


def synthesize_local(workspace: Path, config: dict, text: str) -> dict:
    audio = config["audio"]
    if audio.get("provider") != "macos_say" or audio.get("voice") != "Tingting":
        raise Blocked("TTS_PROVIDER", "Only the locally validated Tingting provider is enabled")
    rate = audio.get("words_per_minute", 180)
    if not isinstance(rate, int) or isinstance(rate, bool) or not 120 <= rate <= 240:
        raise Blocked("TTS_RATE", "Rate must be an integer from 120 to 240")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > 2000
        or "[[" in text
        or any(ord(c) < 32 and c not in "\n\t" for c in text)
    ):
        raise Blocked("TTS_TEXT", "Plain narration required; embedded speech commands prohibited")
    request = {
        "provider": "macos_say",
        "provider_version": audio["provider_version"],
        "voice": audio["voice"],
        "words_per_minute": rate,
        "text": text,
        "output": "pcm16_48k_mono",
        "adapter_version": 1,
    }
    key = digest(request)
    folder = workspace / "audio/tts_cache" / key
    receipt_path = folder / "receipt.json"
    output = folder / "speech.wav"
    if receipt_path.exists():
        receipt = load(receipt_path)
        if receipt["request_sha256"] != key or file_hash(output) != receipt["sha256"]:
            raise Blocked("TTS_CACHE_CHANGED", "Cached audio does not match its receipt")
        return receipt
    if folder.exists():
        raise Blocked("TTS_RECONCILE", "Incomplete prior synthesis; preserve files and reconcile")
    folder.mkdir(parents=True)
    atomic_write(folder / "request.json", request)
    text_path = folder / "text.txt"
    text_path.write_text(text, encoding="utf-8")
    # argv + text file: no shell or speech-control interpretation of narration.
    result = subprocess.run(
        [
            "/usr/bin/say",
            "-v",
            audio["voice"],
            "-r",
            str(rate),
            "-o",
            str(folder / "speech.aiff"),
            "-f",
            str(text_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise Blocked("TTS_FAILED", result.stderr[-300:], 4)
    result = subprocess.run(
        [
            config["paths"]["ffmpeg"],
            "-hide_banner",
            "-v",
            "error",
            "-n",
            "-i",
            str(folder / "speech.aiff"),
            "-ar",
            "48000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise Blocked("TTS_CONVERSION_FAILED", result.stderr[-300:], 4)
    receipt = {
        "status": "succeeded",
        "request_sha256": key,
        "path": str(output),
        "text_path": str(text_path),
        **measure_wav(output),
    }
    atomic_write(receipt_path, receipt)
    return receipt


def lock_narration_timing(script: dict, measurements: list[dict], max_tail: int = 12) -> dict:
    if len(measurements) != len(script["shots"]):
        raise Blocked("AUDIO_SEGMENTS", "One measured narration per shot is required")
    cursor = 0
    segments = []
    for shot, measurement in zip(script["shots"], measurements, strict=True):
        samples = measurement["samples"]
        if (
            not isinstance(samples, int)
            or isinstance(samples, bool)
            or samples <= 0
            or measurement["sample_rate"] != 48000
        ):
            raise Blocked("AUDIO_MEASUREMENT", "Invalid PCM sample count or sample rate")
        speech_frames = (samples + 1599) // 1600
        timing = shot["timing"]
        if not 0 < timing["min_frames"] <= timing["target_frames"] <= timing["max_frames"]:
            raise Blocked("SCRIPT_TIMING", "Invalid approved timing bounds")
        duration = max(timing["min_frames"], speech_frames)
        if duration > timing["max_frames"] or duration - speech_frames > max_tail:
            raise Blocked(
                "TIMING_UNSATISFIABLE",
                f"{shot['shot_id']}: actual speech does not fit approved timing; revise script, do not stretch audio",
            )
        segments.append(
            {
                "shot_id": shot["shot_id"],
                "start_frame": cursor,
                "duration_frames": duration,
                "speech_samples": samples,
                "padding_samples": duration * 1600 - samples,
                "audio_path": measurement["path"],
                "audio_sha256": measurement["sha256"],
            }
        )
        cursor += duration
    if not max(660, script["total_min_frames"]) <= cursor <= min(840, script["total_max_frames"]):
        raise Blocked("TIMING_UNSATISFIABLE", "Measured total outside approved 22–28 second range")
    return {"total_frames": cursor, "segments": segments, "basis": "actual_48kHz_PCM_samples"}


def lock_outline_narration_timing(outline: dict, measurements: list[dict]) -> dict:
    """Place measured speech in approved fixed shot targets without stretching audio."""
    if len(measurements) != len(outline["shots"]):
        raise Blocked("AUDIO_SEGMENTS", "One measured narration per shot is required")
    cursor = 0
    segments = []
    for shot, measurement in zip(outline["shots"], measurements, strict=True):
        samples = measurement["samples"]
        if (
            not isinstance(samples, int)
            or isinstance(samples, bool)
            or samples <= 0
            or measurement["sample_rate"] != 48000
        ):
            raise Blocked("AUDIO_MEASUREMENT", "Invalid PCM sample count or sample rate")
        target_frames = shot["target_frames"]
        speech_frames = (samples + 1599) // 1600
        if speech_frames > target_frames:
            raise Blocked(
                "TIMING_UNSATISFIABLE",
                f"{shot['shot_id']}: actual speech exceeds the approved shot target; revise script, do not stretch audio",
            )
        segments.append(
            {
                "shot_id": shot["shot_id"],
                "start_frame": cursor,
                "duration_frames": target_frames,
                "speech_frames": speech_frames,
                "speech_samples": samples,
                "padding_samples": target_frames * 1600 - samples,
                "audio_path": measurement["path"],
                "audio_sha256": measurement["sha256"],
            }
        )
        cursor += target_frames
    if cursor != outline["total_frames"] or not 180 <= cursor <= 3600:
        raise Blocked("TIMING_UNSATISFIABLE", "Narration must match the requested 6–120 seconds")
    return {
        "total_frames": cursor,
        "segments": segments,
        "basis": "approved_script_outline_target_frames_and_actual_48kHz_PCM_samples",
    }


def _validate_voice_approval(workspace: Path, config: dict, task_id: str) -> None:
    audio = config["audio"]
    if task_id not in audio.get("approved_task_ids", []):
        raise Blocked("VOICE_APPROVAL", "Voice choice is limited to the first three tasks")
    if audio.get("approved_voice_sha256") != voice_config_hash(audio):
        raise Blocked("VOICE_APPROVAL_STALE", "Provider, voice or rate changed after approval")
    evidence_path = workspace / audio.get("approval_evidence", "")
    if not evidence_path.is_file():
        raise Blocked("VOICE_APPROVAL", "Voice approval evidence is missing")
    evidence = load(evidence_path)
    if evidence.get("voice_config_sha256") != audio["approved_voice_sha256"] or evidence.get(
        "batch_id"
    ) not in audio.get("approved_batch_ids", []):
        raise Blocked("VOICE_APPROVAL_STALE", "Voice approval evidence does not match config")


def _combine_narration(
    workspace: Path,
    script: dict,
    timing: dict,
) -> dict:
    key = digest(
        {
            "entity": script["entity"],
            "script_sha256": script["content_sha256"],
            "timing": timing,
        }
    )
    folder = workspace / "audio/narration" / key
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "narration.wav"
    receipt_path = folder / "receipt.json"
    if receipt_path.exists():
        prior = load(receipt_path)
        if file_hash(output) != prior["sha256"]:
            raise Blocked("AUDIO_CHANGED", "Combined narration changed")
        return prior
    if output.exists():
        raise Blocked("AUDIO_RECONCILE", "Unreceipted combined audio exists")
    with wave.open(str(output), "wb") as dest:
        dest.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        for segment in timing["segments"]:
            with wave.open(str(runtime_path(workspace, segment["audio_path"])), "rb") as source:
                dest.writeframes(source.readframes(source.getnframes()))
            dest.writeframes(b"\x00\x00" * segment["padding_samples"])
    result = {
        "status": "succeeded",
        "task_id": script["task_id"],
        "script_entity": script["entity"],
        "script_sha256": script["content_sha256"],
        "path": str(output),
        **timing,
        **measure_wav(output),
    }
    atomic_write(receipt_path, result)
    return result


def _srt_timestamp(frame: int) -> str:
    if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
        raise Blocked("CAPTION_RANGE", "Caption frame must be a nonnegative integer", 2)
    milliseconds = (frame * 1000 + 15) // 30
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def prepare_outline_package(
    workspace: Path,
    config: dict,
    outline: dict,
    narration: dict,
) -> dict:
    """Lock approved captions, voiceover and style before media gaps are resolved."""
    require_approval(outline)
    task_id = safe_id(outline["task_id"])
    if (
        narration.get("script_sha256") != outline["content_sha256"]
        or narration.get("total_frames") != outline["total_frames"]
    ):
        raise Blocked("AUDIO_STALE", "Narration is not bound to the approved script", 6)
    audio_path = runtime_path(workspace, narration["path"])
    if not audio_path.is_file() or file_hash(audio_path) != narration.get("sha256"):
        raise Blocked("AUDIO_CHANGED", "Narration file does not match its receipt", 6)
    measured = measure_wav(audio_path)
    if measured["duration_frames_30fps"] != outline["total_frames"]:
        raise Blocked("AUDIO_DURATION", "Narration duration does not match the script", 6)

    font_path = Path(config["paths"]["font_file"])
    if not font_path.is_absolute() or not font_path.is_file():
        raise Blocked("FONT_MISSING", "Configured Chinese font file is unavailable", 3)
    style = {
        "version": 1,
        "font_path": str(font_path),
        "font_sha256": file_hash(font_path),
        "font_size_px": 64,
        "fill_color": "#FFFFFF",
        "stroke_color": "#000000",
        "stroke_width_px": 4,
        "max_width_px": 880,
        "bottom_margin_px": 220,
        "alignment": "bottom_center",
        "status": "technical_default_pending_first_business_render_review",
    }
    style["style_sha256"] = digest(style)

    segments = narration.get("segments", [])
    if len(segments) != len(outline["shots"]):
        raise Blocked("AUDIO_SEGMENTS", "Narration and script shot counts differ", 6)
    captions = []
    srt_blocks = []
    for number, (shot, segment) in enumerate(zip(outline["shots"], segments, strict=True), 1):
        if shot["shot_id"] != segment.get("shot_id"):
            raise Blocked("AUDIO_SEGMENTS", "Narration shot order differs from script", 6)
        start = segment.get("start_frame")
        speech_frames = segment.get("speech_frames")
        duration = segment.get("duration_frames")
        if (
            not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in [start, speech_frames, duration]
            )
            or start < 0
            or speech_frames <= 0
            or speech_frames > duration
            or start + duration > outline["total_frames"]
        ):
            raise Blocked("CAPTION_RANGE", f"Invalid measured caption range: {shot['shot_id']}", 6)
        end = start + speech_frames
        caption = {
            "shot_id": shot["shot_id"],
            "start_frame": start,
            "end_frame": end,
            "text": shot["caption"],
            "timing_basis": "actual_tts_speech_frames",
        }
        captions.append(caption)
        srt_blocks.extend(
            [
                str(number),
                f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}",
                shot["caption"],
                "",
            ]
        )

    bgm_path = config["audio"].get("bgm_path")
    if bgm_path:
        bgm_file = Path(bgm_path)
        if not bgm_file.is_absolute():
            bgm_file = workspace / bgm_file
        if not bgm_file.is_file():
            raise Blocked("BGM_MISSING", "Configured BGM file is unavailable", 3)
        bgm = {
            "enabled": True,
            "path": str(bgm_file),
            "sha256": file_hash(bgm_file),
            "gain": 0.18,
        }
    else:
        bgm = {
            "enabled": False,
            "reason": "No local BGM configured; voiceover-only is valid for tts_uniform.",
        }

    revision = outline["revision"]
    job = workspace / "jobs" / task_id / f"r{revision}"
    material_index_path = job / "material_index_preview.json"
    if not material_index_path.is_file():
        raise Blocked("MATERIAL_INDEX_MISSING", "Run material indexing before packaging", 3)
    material_index = load(material_index_path)
    if (
        material_index.get("script_revision") != revision
        or material_index.get("script_sha256") != outline["content_sha256"]
    ):
        raise Blocked("MATERIAL_INDEX_STALE", "Material index belongs to another script", 6)
    gaps = [
        {"shot_id": shot["shot_id"], "caption": shot["caption"]}
        for shot in material_index["shots"]
        if not shot["candidates"]
    ]
    package_dir = job / "preproduction"
    srt_path = package_dir / "captions.srt"
    manifest_path = package_dir / "audio_caption_manifest.json"
    package_dir.mkdir(parents=True, exist_ok=True)
    srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")
    manifest = {
        "schema_version": "1.0.0",
        "entity": "audio_caption_manifest",
        "task_id": task_id,
        "script_revision": revision,
        "script_sha256": outline["content_sha256"],
        "total_frames": outline["total_frames"],
        "voiceover": {
            "path": str(audio_path),
            "sha256": narration["sha256"],
            "sample_rate": measured["sample_rate"],
            "samples": measured["samples"],
            "duration_frames": measured["duration_frames_30fps"],
            "gain": 1.0,
            "voice_approval_evidence": str(workspace / config["audio"]["approval_evidence"]),
        },
        "captions": captions,
        "captions_srt": str(srt_path),
        "style": style,
        "bgm": bgm,
        "material_index": {
            "path": str(material_index_path),
            "index_id": material_index["index_id"],
            "production_ready": material_index["production_ready"],
            "gaps": gaps,
        },
        "status": "ready_for_edit_plan" if not gaps else "blocked_material_gap",
        "formal_edit_plan_created": False,
    }
    manifest["manifest_sha256"] = digest(manifest)
    atomic_write(manifest_path, manifest)
    return {
        "status": manifest["status"],
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest["manifest_sha256"],
        "captions_srt": str(srt_path),
        "caption_count": len(captions),
        "material_gap_count": len(gaps),
        "bgm_enabled": bgm["enabled"],
    }


def prepare_narration(workspace: Path, config: dict, script: dict) -> dict:
    require_approval(script)
    if script["audio_mode"] != "tts_uniform":
        raise Blocked("AUDIO_MODE", "Narration preparation requires explicit tts_uniform")
    audio = config["audio"]
    if script["batch_id"] not in audio.get("approved_batch_ids", []):
        raise Blocked("VOICE_APPROVAL", "Voice choice is not approved for this batch")
    _validate_voice_approval(workspace, config, script["task_id"])
    measurements = [
        synthesize_local(workspace, config, shot["narration"]) for shot in script["shots"]
    ]
    timing = lock_narration_timing(script, measurements, audio.get("max_tail_padding_frames", 12))
    return _combine_narration(workspace, script, timing)


def prepare_outline_narration(workspace: Path, config: dict, outline: dict) -> dict:
    """Generate fixed-duration narration for an approved creative-first script outline."""
    require_approval(outline)
    if outline["task_id"] in config["audio"].get("approved_task_ids", []):
        _validate_voice_approval(workspace, config, outline["task_id"])
    else:
        # The new review button approves the exact measured timing/voice hash.
        # Existing task/batch voice approvals retain their original scope.
        from .script_revision import timing_verified

        path = (
            workspace
            / "jobs"
            / outline["task_id"]
            / f"r{outline['revision']}"
            / "script_outline.json"
        )
        timing_verified(workspace, path, outline)
    timing_path = (
        workspace / "jobs" / outline["task_id"] / f"r{outline['revision']}" / "draft_timing.json"
    )
    if timing_path.exists() and preserved_timing(workspace, timing_path, outline):
        # An OS migration cannot silently replace the voice in an approved script.
        measurements = []
        for segment in load(timing_path)["segments"]:
            audio_path = runtime_path(workspace, segment["audio_path"])
            measured = measure_wav(audio_path)
            if measured["sha256"] != segment["audio_sha256"]:
                raise Blocked("AUDIO_CHANGED", "Migrated narration changed", 6)
            measurements.append({**measured, "path": str(audio_path)})
    else:
        measurements = [
            synthesize_local(workspace, config, shot["narration"]) for shot in outline["shots"]
        ]
    timing = lock_outline_narration_timing(outline, measurements)
    narration = _combine_narration(workspace, outline, timing)
    return {
        **narration,
        "preproduction": prepare_outline_package(workspace, config, outline, narration),
    }
