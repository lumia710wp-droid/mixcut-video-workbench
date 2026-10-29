from __future__ import annotations
import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from .core import atomic_write, file_hash


QC_PROFILE = "mixcut-technical-qc-2.1"


def _intervals(text: str, start_key: str, end_key: str) -> list[dict]:
    starts = [float(value) for value in re.findall(rf"{start_key}:\s*([0-9.]+)", text)]
    ends = [float(value) for value in re.findall(rf"{end_key}:\s*([0-9.]+)", text)]
    return [
        {"start_sec": start, "end_sec": end, "duration_sec": round(end - start, 3)}
        for start, end in zip(starts, ends)
        if end >= start
    ]


def content_warning_scan(path: Path, ffmpeg: str) -> dict:
    process = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=0.5:pix_th=0.10:pic_th=0.98",
            "-af",
            "silencedetect=noise=-45dB:d=3.0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if process.returncode:
        return {"status": "unavailable", "silence_intervals": [], "black_intervals": []}
    return {
        "status": "completed",
        "silence_intervals": _intervals(process.stderr, "silence_start", "silence_end"),
        "black_intervals": _intervals(process.stderr, "black_start", "black_end"),
    }


def technical_qc(path: Path, plan: dict, config: dict, readback: dict) -> dict:
    errors = []
    warnings = []
    if not path.is_file() or path.stat().st_size == 0:
        return {"status": "failed", "errors": ["OUTPUT_MISSING"]}
    p = subprocess.run(
        [
            config["paths"]["ffprobe"],
            "-v",
            "error",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if p.returncode:
        return {"status": "failed", "errors": ["FFPROBE_FAILED"]}
    info = json.loads(p.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not video:
        errors.append("NO_VIDEO")
    else:
        if (video["width"], video["height"]) != (1080, 1920):
            errors.append("CANVAS")
        if Fraction(video["avg_frame_rate"]) != 30:
            errors.append("FPS")
        if abs(int(video.get("nb_read_frames", -10000)) - plan["total_frames"]) > 1:
            errors.append("FRAME_COUNT")
    if plan["audio_tracks"] and not audio:
        errors.append("NO_AUDIO")
    if readback.get("status") != "passed":
        errors.append("READBACK")
    p = subprocess.run(
        [
            config["paths"]["ffmpeg"],
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if p.returncode or p.stderr.strip():
        errors.append("FULL_DECODE")
    warning_scan = content_warning_scan(path, config["paths"]["ffmpeg"])
    if warning_scan["status"] != "completed":
        warnings.append({"code": "CONTENT_WARNING_SCAN_UNAVAILABLE"})
    for interval in warning_scan["silence_intervals"]:
        warnings.append({"code": "LONG_AUDIO_SILENCE", **interval})
    for interval in warning_scan["black_intervals"]:
        warnings.append({"code": "LONG_BLACK_FRAME", **interval})
    report = {
        "schema_version": "1.0.0",
        "qc_profile": QC_PROFILE,
        "status": "failed" if errors else "passed",
        "errors": errors,
        "warnings": warnings,
        "output_sha256": file_hash(path),
        "probe": info,
        "decode_exit": p.returncode,
        "readback": readback,
        "human_review": "pending",
        "content_warning_scan": warning_scan,
        "not_covered": ["caption text semantics", "speech naturalness", "scene identity"],
        "at_source": "real_media",
    }
    atomic_write(path.parent / "qc.json", report)
    return report
