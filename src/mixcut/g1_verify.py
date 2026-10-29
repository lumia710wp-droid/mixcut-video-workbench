"""Read the synthetic G1 source identity/frame markers and separate audio tones.

Binary decode buffers stay in-process. Reports contain only numbers and paths.
This checker supplements timeline/readback/full-decode QC; it is not human review.
"""

from __future__ import annotations
import array
import math
import subprocess
import sys
from pathlib import Path
from .core import Blocked, file_hash

PALETTE = [(38, 60, 128), (17, 121, 93), (147, 56, 73), (162, 106, 11), (98, 64, 148)]


def read_markers(path: Path, ffmpeg: str, frames: list[int]) -> list[dict]:
    if not frames or min(frames) < 0 or len(set(frames)) != len(frames):
        raise Blocked("FRAME_SELECTION", "Require unique nonnegative frame numbers", 2)
    frames = sorted(frames)
    select = "+".join(f"eq(n\\,{n})" for n in frames)
    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-vf",
        f"select={select},scale=540:960",
        "-fps_mode",
        "passthrough",
        "-frames:v",
        str(len(frames)),
        "-an",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    proc = subprocess.run(command, capture_output=True, timeout=90)
    size = 540 * 960 * 3
    if proc.returncode or len(proc.stdout) != size * len(frames):
        raise Blocked("MARKER_DECODE", "Could not decode all requested frames", 4)
    rows = []
    for index, frame in enumerate(frames):
        raw = memoryview(proc.stdout)[index * size : (index + 1) * size]

        def pixel(x, y):
            pos = (y * 540 + x) * 3
            return tuple(raw[pos : pos + 3])

        background = pixel(20, 20)
        distances = [sum((a - b) ** 2 for a, b in zip(background, color)) for color in PALETTE]
        color_id = min(range(5), key=lambda k: distances[k])
        if distances[color_id] > 30**2:
            raise Blocked("MARKER_COLOR", f"Unrecognized source at output frame {frame}", 4)
        number = sum((1 << bit) for bit in range(9) if min(pixel(45 + bit * 54, 110)) > 210)
        rows.append(
            {"output_frame": frame, "source_letter": "ABCDE"[color_id], "source_frame": number}
        )
    return rows


def verify_markers(path: Path, plan: dict, ffmpeg: str) -> dict:
    media = {m["media_id"]: m for m in plan["media"]}
    expected = {}
    for shot in plan["shots"]:
        letter = Path(media[shot["media_id"]]["relative_path"]).stem
        for offset in (0, shot["duration_frames"] // 2, shot["duration_frames"] - 1):
            expected[shot["timeline_start_frame"] + offset] = (
                letter,
                shot["source_in_frame"] + offset,
            )
    rows = read_markers(path, ffmpeg, list(expected))
    errors = []
    for row in rows:
        letter, number = expected[row["output_frame"]]
        row["expected_letter"] = letter
        row["expected_source_frame"] = number
        row["passed"] = (row["source_letter"], row["source_frame"]) == (letter, number)
        if not row["passed"]:
            errors.append(row["output_frame"])
    return {
        "status": "failed" if errors else "passed",
        "scope": "G1_synthetic_markers_only",
        "samples": rows,
        "failed_frames": errors,
        "output_sha256": file_hash(path),
    }


def tone_amplitude(samples, frequency, sample_rate=8000) -> float:
    if not samples:
        return 0.0
    coeff = 2 * math.cos(2 * math.pi * frequency / sample_rate)
    before = previous = 0.0
    for x in samples:
        current = x + coeff * previous - before
        before, previous = previous, current
    power = max(0, previous * previous + before * before - coeff * previous * before)
    return 2 * math.sqrt(power) / len(samples)


def verify_audio(path: Path, ffmpeg: str, expected_seconds=24) -> dict:
    proc = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-t",
            str(expected_seconds + 1),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        timeout=90,
    )
    if proc.returncode or len(proc.stdout) % 4:
        raise Blocked("AUDIO_DECODE", "Could not decode audio samples", 4)
    samples = array.array("f")
    samples.frombytes(proc.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    windows = []
    for second in range(expected_seconds):
        # Exclude boundary codec transients, analyze the middle 0.8 seconds.
        chunk = samples[second * 8000 + 800 : (second + 1) * 8000 - 800]
        amplitude = tone_amplitude(chunk, 440)
        leaked = max(tone_amplitude(chunk, f) for f in (800, 900, 1000, 1100, 1200))
        db = 20 * math.log10(max(leaked, 1e-12) / max(amplitude, 1e-12))
        windows.append(
            {
                "second": second,
                "samples": len(chunk),
                "tone_440_amplitude": amplitude,
                "source_leak_db_relative": db,
                "passed": len(chunk) == 6400 and amplitude > 0.01 and db < -30,
            }
        )
    duration_ok = abs(len(samples) / 8000 - expected_seconds) <= 0.1
    return {
        "status": "passed" if duration_ok and all(w["passed"] for w in windows) else "failed",
        "scope": "G1_known_tones_only",
        "duration_seconds": len(samples) / 8000,
        "windows": windows,
        "output_sha256": file_hash(path),
    }
