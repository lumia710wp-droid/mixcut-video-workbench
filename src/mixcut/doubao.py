"""Bounded continuous TTS with durable receipts; encoded audio never enters logs."""

from __future__ import annotations

import base64
import json
import math
import re
import subprocess
import urllib.request
import uuid
import wave
from pathlib import Path
from .core import Blocked, atomic_write, digest, file_hash, load, now

SPEAKER = "zh_male_yangguangqingnian_uranus_bigtts"


def spoken_char_hint(target_seconds: float) -> int:
    """Approximate Chinese copy length; real measured duration remains authoritative."""
    return max(1, round(max(0.5, target_seconds - 0.8) * 5.5))


def request_parameters(text: str, *, speaker: str | None = None, speech_rate: int = 8) -> dict:
    """Stable cache identity, also usable by read-only timing diagnostics."""
    return {
        "text": text,
        "speaker": speaker or SPEAKER,
        "audio_params": {
            "format": "mp3",
            "sample_rate": 24000,
            "bit_rate": 160000,
            "speech_rate": speech_rate,
            "enable_subtitle": True,
        },
        "additions": json.dumps(
            {
                "context_texts": [
                    "像朋友推荐好吃的，亲切自然，带一点笑意，连贯口语，不要播音腔，不要夸张叫卖。"
                ]
            },
            ensure_ascii=False,
        ),
    }


def clean_text(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text))


def aligned_captions(text: str, events: list, duration: float, total_frames: int) -> list[dict]:
    clean = clean_text
    sentences = [s for s in events if s.get("words")]
    if clean("".join(s["text"] for s in sentences)) != clean(text):
        raise Blocked("V3_TTS_TEXT", "提供方字幕与口播稿不一致", 4)
    words = [w for s in sentences for w in s["words"]]
    if not words or any(not 0 <= w["startTime"] <= w["endTime"] <= duration for w in words):
        raise Blocked("V3_TTS_TIMESTAMPS", "提供方字级时间戳越界", 4)
    if any(a["startTime"] > b["startTime"] for a, b in zip(words, words[1:])):
        raise Blocked("V3_TTS_TIMESTAMPS", "提供方时间戳顺序无效", 4)
    if clean("".join(w["word"] for w in words)) != clean(text):
        raise Blocked("V3_TTS_TEXT", "字级文本与口播稿不一致", 4)
    # Punctuation cuts and bounded display length; never interpolate word timestamps.
    groups: list[list] = []
    current: list = []
    for word in words:
        if current and len(clean("".join(w["word"] for w in current) + word["word"])) > 12:
            groups.append(current)
            current = []
        current.append(word)
        if re.search("[，。！？；]", word["word"]):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    captions = []
    for n, group in enumerate(groups):
        start = 6 + round(group[0]["startTime"] * 30)
        end = 6 + round(groups[n + 1][0]["startTime"] * 30) if n + 1 < len(groups) else total_frames
        if start >= end:
            raise Blocked("V3_TTS_TIMESTAMPS", "字幕切换点不足一帧，需重新分句", 4)
        captions.append(
            {
                "text": "".join(w["word"] for w in group).rstrip("，。！？；"),
                "start_frame": start,
                "end_frame": end,
            }
        )
    return captions


def synthesize(
    root: Path, config: dict, text: str, *, speaker: str | None = None, speech_rate: int = 8
) -> dict:
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
        raise Blocked("V3_TTS_TEXT", "配音稿长度无效", 2)
    credentials_path = root / "config/doubao_tts.local.json"
    if not credentials_path.is_file() or credentials_path.stat().st_mode & 0o077:
        raise Blocked("V3_TTS_CONFIG", "请在服务端保存0600豆包语音配置", 3)
    secret = load(credentials_path).get("api_key")
    if not secret:
        raise Blocked("V3_TTS_CONFIG", "豆包语音API Key尚未配置", 3)
    params = request_parameters(text, speaker=speaker, speech_rate=speech_rate)
    key = digest(params)
    folder = root / "audio/doubao_v3" / key
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / "receipt.json"
    audio, pcm = folder / "speech.mp3", folder / "speech.wav"
    if receipt.exists():
        prior = load(receipt)
        if (
            prior.get("status") == "complete"
            and audio.is_file()
            and file_hash(audio) == prior["sha256"]
        ):
            return prior
        raise Blocked("V3_TTS_UNKNOWN", "已有语音请求结果未知/失败，先对账，禁止自动重发", 5)
    state = {
        "status": "dispatching",
        "request_sha256": key,
        "request_id": str(uuid.uuid4()),
        "at": now(),
        "speaker": params["speaker"],
        "speech_rate": speech_rate,
        "text": text,
    }
    atomic_write(receipt, state)
    request = urllib.request.Request(
        "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
        method="POST",
        data=json.dumps({"req_params": params}, ensure_ascii=False).encode(),
        headers={
            "X-Api-Key": secret,
            "X-Api-Resource-Id": "seed-tts-2.0",
            "X-Api-Request-Id": str(state["request_id"]),
            "Content-Type": "application/json",
        },
    )
    events = []
    terminal = False
    part = folder / "speech.mp3.part"
    try:
        with urllib.request.urlopen(request, timeout=90) as response, part.open("xb") as dest:
            for line in response:
                line = line.strip()
                if not line or line.startswith(b"event:"):
                    continue
                event = json.loads(line[5:].strip() if line.startswith(b"data:") else line)
                if event.get("code") not in {0, 20000000}:
                    raise Blocked("V3_TTS_REJECTED", "语音提供方拒绝请求", 4)
                if event.get("data"):
                    dest.write(base64.b64decode(event["data"], validate=True))
                if event.get("sentence"):
                    events.append(event["sentence"])
                terminal = terminal or event.get("code") == 20000000
        if not terminal or part.stat().st_size == 0:
            raise Blocked("V3_TTS_INCOMPLETE", "语音流不完整", 5)
        part.rename(audio)
        subprocess.run(
            [
                config["paths"]["ffmpeg"],
                "-v",
                "error",
                "-i",
                str(audio),
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(pcm),
            ],
            check=True,
            capture_output=True,
        )
        with wave.open(str(pcm)) as stream:
            duration = stream.getnframes() / stream.getframerate()
        state.update(
            status="complete",
            path=str(audio),
            sha256=file_hash(audio),
            duration_seconds=duration,
            duration_frames=math.ceil(duration * 30),
            sentences=events,
        )
        atomic_write(receipt, state)
        return state
    except Exception as exc:
        # Never persist raw provider errors or audio events with their encoded data.
        state.update(status="failed_or_unknown", error_type=type(exc).__name__)
        atomic_write(receipt, state)
        if isinstance(exc, Blocked):
            raise
        raise Blocked("V3_TTS_FAILED", "配音未完成，请检查服务端回执；不会自动重发", 5) from exc


def _trim_zero_rounding_tail(pcm: Path, output: Path, maximum_frames: int) -> bool:
    """Remove at most one video frame of exact digital zero, never spoken samples."""
    with wave.open(str(pcm), "rb") as stream:
        if stream.getsampwidth() != 2 or stream.getnchannels() != 1:
            return False
        rate, count = stream.getframerate(), stream.getnframes()
        keep = maximum_frames * rate // 30
        if not 0 < count - keep <= math.ceil(rate / 30):
            return False
        stream.setpos(keep)
        if any(stream.readframes(count - keep)):
            return False
        stream.rewind()
        samples = stream.readframes(keep)
    with wave.open(str(output), "wb") as dest:
        dest.setnchannels(1)
        dest.setsampwidth(2)
        dest.setframerate(rate)
        dest.writeframes(samples)
    return True


def trim_silent_rounding_tail(root: Path, config: dict, voice: dict, maximum_frames: int) -> dict:
    """Keep the gate exact when an MP3's zero padding exceeds it by <1 frame.

    Original MP3/receipt remain immutable. A separate PCM derivative has provenance;
    nonzero audio, missing word boundaries, or larger overruns need real copy correction.
    """
    actual = voice.get("duration_seconds", voice["duration_frames"] / 30)
    maximum = maximum_frames / 30
    if not 0 < actual - maximum <= 1 / 30:
        return voice
    words = [w for s in voice.get("sentences", []) for w in s.get("words", [])]
    if not words or max(w["endTime"] for w in words) > maximum - 0.05:
        return voice
    source = Path(voice["path"]).resolve()
    if not source.is_relative_to(root.resolve()) or file_hash(source) != voice["sha256"]:
        raise Blocked("V3_AUDIO_CHANGED", "配音原文件变化，不能规范化尾部", 6)
    identity = {"source_sha256": voice["sha256"], "maximum_frames": maximum_frames}
    folder = root / "audio/timing_zero_tail" / digest(identity)
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / "receipt.json"
    if receipt.exists():
        saved = load(receipt)
        path = folder / "speech.wav"
        if not path.is_file() or file_hash(path) != saved["sha256"]:
            raise Blocked("V3_AUDIO_CHANGED", "规范化配音文件变化，请先对账", 6)
        return saved
    pcm, output = folder / "decoded.wav", folder / "speech.wav"
    subprocess.run(
        [
            config["paths"]["ffmpeg"],
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(pcm),
        ],
        check=True,
        capture_output=True,
    )
    if not _trim_zero_rounding_tail(pcm, output, maximum_frames):
        return voice
    result = {
        **voice,
        "path": str(output),
        "sha256": file_hash(output),
        "duration_seconds": maximum,
        "duration_frames": maximum_frames,
        "normalization": {
            "type": "trim_exact_zero_rounding_tail",
            **identity,
            "source_path": str(source),
            "removed_seconds": actual - maximum,
        },
    }
    atomic_write(receipt, result)
    return result
