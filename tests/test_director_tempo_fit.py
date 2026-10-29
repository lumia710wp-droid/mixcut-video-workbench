import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from mixcut.core import file_hash
from mixcut.director_execution import fit_voice_to_target, captions


def test_tempo_fit_preserves_source_and_retimes_words(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        candidate = Path(
            "/nonexistent/ffmpeg"
        )
        ffmpeg = str(candidate) if candidate.is_file() else None
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    source = tmp_path / "source.wav"
    rate = 8000
    with wave.open(str(source), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(
            b"".join(
                struct.pack("<h", int(5000 * math.sin(2 * math.pi * 220 * i / rate)))
                for i in range(rate * 3)
            )
        )
    voice = {
        "path": str(source),
        "sha256": file_hash(source),
        "duration_seconds": 3.0,
        "duration_frames": 90,
        "sentences": [
            {
                "words": [
                    {"word": "甲", "startTime": 0.1, "endTime": 1.0},
                    {"word": "乙", "startTime": 1.0, "endTime": 2.8},
                ]
            }
        ],
    }
    config = {"paths": {"ffmpeg": ffmpeg}, "audio": {"voice_target_ratio": 0.8}}
    output = fit_voice_to_target(tmp_path, config, voice, 3)
    assert output["duration_seconds"] <= 2.4
    assert output["tempo_fit"]["speed"] > 1
    assert file_hash(source) == voice["sha256"]
    assert output["sentences"][0]["words"][1]["endTime"] < 2.8
    assert captions("甲乙", output, 90)
    assert fit_voice_to_target(tmp_path, config, voice, 3)["sha256"] == output["sha256"]
    assert fit_voice_to_target(tmp_path, config, output, 3) == output
