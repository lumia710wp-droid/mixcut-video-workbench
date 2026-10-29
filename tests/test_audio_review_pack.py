import struct
import wave
from pathlib import Path

import pytest

from mixcut.audio import (
    lock_narration_timing,
    lock_outline_narration_timing,
    measure_wav,
    prepare_narration,
    prepare_outline_package,
    synthesize_local,
)
from mixcut.audio import voice_config_hash
from mixcut.contracts import content_hash
from mixcut.core import Blocked, atomic_write, file_hash, load
from mixcut.review_pack import render_review_html, suggested_window

ROOT = Path(__file__).resolve().parents[1]


def test_review_window_is_in_bounds():
    for duration in [0.1, 4.9, 7.668333, 42.661667]:
        result = suggested_window(duration)
        assert 0 <= result["start_sec"] < result["end_sec"] <= duration
        assert result["human_review"] == "pending"
    with pytest.raises(Blocked):
        suggested_window(float("nan"))


def test_review_filename_cannot_inject_html():
    page = render_review_html(
        {
            "outputs": [
                {
                    "relative_source_path": "<script>alert(1)</script>",
                    "output_path": "/tmp/video.mp4",
                    "duration_sec": 10,
                    "revision": 1,
                    "suggested_window": suggested_window(10),
                    "clip_id": "c_test",
                }
            ]
        }
    )
    assert "<script>" not in page and "&lt;script&gt;" in page


def audio_inputs():
    script = load(ROOT / "examples/script.fixture.json")
    measurements = [
        {
            "samples": s["timing"]["target_frames"] * 1600 - 1,
            "sample_rate": 48000,
            "path": "/unused/test.wav",
            "sha256": "fixture",
        }
        for s in script["shots"]
    ]
    return script, measurements


def test_timing_uses_samples_and_padding():
    script, measurements = audio_inputs()
    result = lock_narration_timing(script, measurements)
    assert result["total_frames"] == 720
    assert [s["start_frame"] for s in result["segments"]] == [0, 120, 270, 450, 600]
    assert all(s["padding_samples"] == 1 for s in result["segments"])


def test_outline_timing_preserves_approved_fixed_targets():
    outline = {
        "total_frames": 720,
        "shots": [{"shot_id": f"S{index}", "target_frames": 144} for index in range(1, 6)],
    }
    measurements = [
        {
            "samples": (40 + index) * 1600,
            "sample_rate": 48000,
            "path": "/unused/test.wav",
            "sha256": "fixture",
        }
        for index in range(5)
    ]
    result = lock_outline_narration_timing(outline, measurements)
    assert result["total_frames"] == 720
    assert [segment["start_frame"] for segment in result["segments"]] == [0, 144, 288, 432, 576]
    assert all(segment["duration_frames"] == 144 for segment in result["segments"])


def test_outline_timing_rejects_speech_longer_than_shot():
    outline = {"total_frames": 720, "shots": [{"shot_id": "S1", "target_frames": 720}]}
    measurements = [
        {
            "samples": 721 * 1600,
            "sample_rate": 48000,
            "path": "/unused/test.wav",
            "sha256": "fixture",
        }
    ]
    with pytest.raises(Blocked) as exc:
        lock_outline_narration_timing(outline, measurements)
    assert exc.value.code == "TIMING_UNSATISFIABLE"


def test_outline_package_uses_measured_speech_and_preserves_material_gap(tmp_path):
    task_id = "TASK_001"
    shots = [
        {
            "shot_id": f"S{index}",
            "caption": f"第{index}段字幕",
            "narration": f"第{index}段旁白",
            "target_frames": 144,
        }
        for index in range(1, 6)
    ]
    outline = {
        "entity": "script_outline",
        "task_id": task_id,
        "revision": 1,
        "total_frames": 720,
        "shots": shots,
        "review_status": "approved",
        "approval": None,
    }
    outline["content_sha256"] = content_hash(outline)
    outline["approval"] = {
        "actor_id": "reviewer",
        "approved_at": "2026-10-16T00:00:00+00:00",
        "approved_content_sha256": outline["content_sha256"],
        "channel": "local_manual",
    }
    audio_path = tmp_path / "narration.wav"
    with wave.open(str(audio_path), "wb") as out:
        out.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        out.writeframes(struct.pack("<h", 1) * 1_152_000)
    narration = {
        "path": str(audio_path),
        "sha256": file_hash(audio_path),
        "script_sha256": outline["content_sha256"],
        "total_frames": 720,
        "segments": [
            {
                "shot_id": shot["shot_id"],
                "start_frame": (index - 1) * 144,
                "duration_frames": 144,
                "speech_frames": 90,
            }
            for index, shot in enumerate(shots, 1)
        ],
    }
    material_index = {
        "script_revision": 1,
        "script_sha256": outline["content_sha256"],
        "index_id": "mi_test",
        "production_ready": False,
        "shots": [
            {
                "shot_id": shot["shot_id"],
                "caption": shot["caption"],
                "candidates": [] if index == 2 else [{}],
            }
            for index, shot in enumerate(shots, 1)
        ],
    }
    job = tmp_path / "jobs" / task_id / "r1"
    job.mkdir(parents=True)
    atomic_write(job / "material_index_preview.json", material_index)
    font = tmp_path / "font.ttc"
    font.write_bytes(b"test-font")
    result = prepare_outline_package(
        tmp_path,
        {
            "paths": {"font_file": str(font)},
            "audio": {"bgm_path": None, "approval_evidence": "voice.json"},
        },
        outline,
        narration,
    )
    assert result["status"] == "blocked_material_gap"
    assert result["caption_count"] == 5 and result["material_gap_count"] == 1
    manifest = load(Path(result["manifest_path"]))
    assert manifest["captions"][0]["end_frame"] == 90
    assert manifest["formal_edit_plan_created"] is False
    assert "00:00:03,000" in Path(result["captions_srt"]).read_text()


@pytest.mark.parametrize("change", ["too_long", "too_short", "missing", "bad_rate", "nonfinite"])
def test_timing_rejects_invalid_or_unfittable_audio(change):
    script, measurements = audio_inputs()
    if change == "too_long":
        measurements[0]["samples"] += 48000
    if change == "too_short":
        measurements[0]["samples"] = 1
    if change == "missing":
        measurements.pop()
    if change == "bad_rate":
        measurements[0]["sample_rate"] = 44100
    if change == "nonfinite":
        measurements[0]["samples"] = float("inf")
    with pytest.raises(Blocked):
        lock_narration_timing(script, measurements)


def test_wav_measured_non_silent_and_silence_rejected(tmp_path):
    for filename, value in [("tone.wav", 1), ("silence.wav", 0)]:
        path = tmp_path / filename
        with wave.open(str(path), "wb") as out:
            out.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            out.writeframes(struct.pack("<h", value) * 48000)
    assert measure_wav(tmp_path / "tone.wav")["duration_seconds"] == 1
    with pytest.raises(Blocked):
        measure_wav(tmp_path / "silence.wav")


def test_no_tts_without_script_and_voice_batch_approval(tmp_path):
    script, _ = audio_inputs()
    with pytest.raises(Blocked):
        prepare_narration(tmp_path, {}, script)
    script.update(is_fixture=False, audio_mode="tts_uniform")
    script["content_sha256"] = content_hash(script)
    script["approval"].update(
        channel="local_manual", approved_content_sha256=script["content_sha256"]
    )
    with pytest.raises(Blocked) as e:
        prepare_narration(tmp_path, {"audio": {"approved_batch_ids": []}}, script)
    assert e.value.code == "VOICE_APPROVAL"
    assert not (tmp_path / "audio").exists()


@pytest.mark.parametrize("text", ["", "[[voice Alex]]test", "abc\x00def"])
def test_narration_speech_commands_rejected(tmp_path, text):
    config = {"audio": {"provider": "macos_say", "voice": "Tingting", "provider_version": "test"}}
    with pytest.raises(Blocked):
        synthesize_local(tmp_path, config, text)
    assert not (tmp_path / "audio").exists()


def test_voice_setting_change_revokes_approval(tmp_path):
    script, _ = audio_inputs()
    script.update(is_fixture=False, audio_mode="tts_uniform")
    script["content_sha256"] = content_hash(script)
    script["approval"].update(
        channel="local_manual", approved_content_sha256=script["content_sha256"]
    )
    audio = {
        "provider": "macos_say",
        "provider_version": "test",
        "voice": "Tingting",
        "words_per_minute": 180,
        "approved_batch_ids": [script["batch_id"]],
        "approved_task_ids": [script["task_id"]],
    }
    audio["approved_voice_sha256"] = voice_config_hash(audio)
    audio["words_per_minute"] = 200
    with pytest.raises(Blocked) as exc:
        prepare_narration(tmp_path, {"audio": audio}, script)
    assert exc.value.code == "VOICE_APPROVAL_STALE"
    assert not (tmp_path / "audio").exists()
