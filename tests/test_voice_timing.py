"""Duration is measured; bounded corrections never change the requested target."""

import pytest
from mixcut.core import Blocked, atomic_write, load
from mixcut.voice_timing import calibrate_voice, calibrated_copy
from mixcut.copy_drafts import timing_bounds
from mixcut.executable import generate_copy
from mixcut import deepseek


def context():
    rows = [{"fields": {"task_id": t, "target_duration_sec": 10}} for t in ["a", "b"]]
    copy = {
        "scripts": [
            {
                "task_id": t,
                "title": "水果推荐",
                "narration": text,
                "headline": ["水果", "芒果", "果肉", "软糯"],
            }
            for t, text in [("a", "芒果"), ("b", "多种水果糯米糍")]
        ]
    }
    request = {
        "tasks": [r["fields"] for r in rows],
        "settings": {},
        "visual_context_policy": "selected_shots_v1",
        "available_visuals": [
            {"task_id": t, "description": "真实果肉", "events": []} for t in ["a", "b"]
        ],
    }
    return rows, copy, request


def test_ten_second_brief_excludes_six_second_voice():
    assert timing_bounds(10) == (255, 285)
    assert timing_bounds(6) == (135, 165)


def test_only_failed_copy_is_corrected_and_successful_voice_is_reused(tmp_path):
    rows, copy, request = context()
    calls, spoken = [], []

    def tts(root, config, text):
        spoken.append(text)
        seconds = 6.168 if text == "芒果" else 9.1
        return {"duration_seconds": seconds, "duration_frames": round(seconds * 30)}

    def correct(root, req, folder):
        calls.append(req)
        return {"scripts": [{**copy["scripts"][0], "narration": "现做芒果糯米糍"}]}

    updated, audio = calibrate_voice(tmp_path, {}, tmp_path, request, copy, rows, tts, correct)
    assert len(calls) == 1
    assert [s["task_id"] for s in calls[0]["tasks"]] == ["a"]
    assert [v["task_id"] for v in calls[0]["available_visuals"]] == ["a"]
    feedback = calls[0]["voice_calibration"][0]
    assert feedback["direction"] == "expand" and feedback["target_seconds"] == 10
    assert spoken.count("多种水果糯米糍") == 1
    assert updated["scripts"][1] == copy["scripts"][1]
    assert audio["a"]["duration_seconds"] == 9.1
    assert calibrated_copy(tmp_path, copy) == (updated, 1)
    assert load(tmp_path / "timing_report.json")["status"] == "passed"


def test_correction_limit_persists_across_resume(tmp_path):
    rows, copy, request = context()
    calls = []

    def tts(*args):
        return {"duration_seconds": 6, "duration_frames": 180}

    def correct(root, req, folder):
        calls.append(req)
        return {
            "scripts": [
                {
                    **s,
                    "narration": next(
                        f["narration"]
                        for f in req["voice_calibration"]
                        if f["task_id"] == s["task_id"]
                    )
                    + "香甜",
                }
                for s in copy["scripts"]
            ]
        }

    for _ in range(2):
        with pytest.raises(Blocked, match="已自动校准2轮"):
            calibrate_voice(tmp_path, {}, tmp_path, request, copy, rows, tts, correct)
    assert len(calls) == 2
    assert load(tmp_path / "timing_report.json")["status"] == "correction_limit"


def test_unknown_tts_stops_without_copy_call(tmp_path):
    rows, copy, request = context()

    def tts(*args):
        raise Blocked("V3_TTS_UNKNOWN", "unknown", 5)

    def forbidden(*args):
        pytest.fail("Unknown outcomes must not trigger copy correction")

    with pytest.raises(Blocked) as error:
        calibrate_voice(tmp_path, {}, tmp_path, request, copy, rows, tts, forbidden)
    assert error.value.code == "V3_TTS_UNKNOWN"


def test_resume_after_accepted_correction_does_not_rewrite_again(tmp_path):
    rows, copy, request = context()
    calls = []

    def first_tts(root, config, text):
        if text == "现做芒果糯米糍":
            raise Blocked("V3_TTS_UNKNOWN", "unknown", 5)
        seconds = 6 if text == "芒果" else 9
        return {"duration_seconds": seconds, "duration_frames": seconds * 30}

    def correct(root, req, folder):
        calls.append(req)
        return {"scripts": [{**copy["scripts"][0], "narration": "现做芒果糯米糍"}]}

    with pytest.raises(Blocked):
        calibrate_voice(tmp_path, {}, tmp_path, request, copy, rows, first_tts, correct)
    updated, _ = calibrate_voice(
        tmp_path,
        {},
        tmp_path,
        request,
        copy,
        rows,
        lambda *args: {"duration_seconds": 9, "duration_frames": 270},
        correct,
    )
    assert len(calls) == 1 and updated["scripts"][0]["narration"] == "现做芒果糯米糍"


def test_unknown_correction_dispatch_is_not_resent(tmp_path, monkeypatch):
    rows, copy, request = context()

    def cfg(*args):
        return {
            "max_input_chars": 6000,
            "model": "fake",
            "max_output_tokens": 2048,
            "endpoint": "https://example.invalid",
            "timeout_seconds": 1,
        }

    monkeypatch.setattr(deepseek, "load_model_config", cfg)
    monkeypatch.setattr(deepseek, "_read_key", lambda *args: "test-only")
    calls = []

    def transport(*args):
        calls.append(1)
        raise TimeoutError("test")

    monkeypatch.setattr(deepseek, "urllib_transport", transport)

    def tts(*args):
        return {"duration_seconds": 12, "duration_frames": 360}

    with pytest.raises(TimeoutError):
        calibrate_voice(tmp_path, {}, tmp_path, request, copy, rows, tts, generate_copy)
    with pytest.raises(Blocked) as error:
        calibrate_voice(tmp_path, {}, tmp_path, request, copy, rows, tts, generate_copy)
    assert error.value.code == "V3_MODEL_UNKNOWN" and len(calls) == 1


def test_accepted_correction_is_hash_checked(tmp_path):
    rows, copy, request = context()

    def tts(*args):
        return {"duration_seconds": 6, "duration_frames": 180}

    def correct(root, req, folder):
        return {"scripts": [{**s, "narration": s["narration"] + "香甜"} for s in copy["scripts"]]}

    with pytest.raises(Blocked):
        calibrate_voice(tmp_path, {}, tmp_path, request, copy, rows, tts, correct)
    path = next(tmp_path.glob("timing_calibration/*/round_1/accepted.json"))
    record = load(path)
    record["copy"]["scripts"][0]["narration"] = "changed"
    atomic_write(path, record)
    with pytest.raises(Blocked) as error:
        calibrated_copy(tmp_path, copy)
    assert error.value.code == "V3_CALIBRATION_CHANGED"


def test_unchanged_completed_response_consumes_round_budget(tmp_path):
    rows, copy, request = context()
    calls = []

    def tts(*args):
        return {"duration_seconds": 6, "duration_frames": 180}

    def correct(root, req, folder):
        calls.append(req)
        return copy

    for _ in range(2):
        with pytest.raises(Blocked, match="已自动校准2轮"):
            calibrate_voice(tmp_path, {}, tmp_path, request, copy, rows, tts, correct)
    assert len(calls) == 2
    assert "calibration_followup" not in calls[0]
    assert "calibration_followup" in calls[1]


def test_only_exact_zero_padding_within_one_frame_may_be_trimmed(tmp_path):
    import wave
    from mixcut.doubao import _trim_zero_rounding_tail

    def pcm(path, seconds, last_sample):
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x01\x00" * 456000 + last_sample * round((seconds - 9.5) * 48000))

    source, output = tmp_path / "input.wav", tmp_path / "output.wav"
    pcm(source, 9.504, b"\x00\x00")
    assert _trim_zero_rounding_tail(source, output, 285)
    with wave.open(str(output)) as stream:
        assert stream.getnframes() / stream.getframerate() == 9.5
        assert stream.readframes(stream.getnframes()) == b"\x01\x00" * 456000
    pcm(source, 9.504, b"\x01\x00")
    assert not _trim_zero_rounding_tail(source, output, 285)
    pcm(source, 9.6, b"\x00\x00")
    assert not _trim_zero_rounding_tail(source, output, 285)
