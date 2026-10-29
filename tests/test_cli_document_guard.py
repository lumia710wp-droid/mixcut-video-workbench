import pytest
from mixcut.core import Blocked, file_hash
from mixcut.director_cli_runner import verify_document


def document_fixture(tmp_path):
    video = tmp_path / "video.mp4"
    voice = tmp_path / "voice.wav"
    video.write_bytes(b"video")
    voice.write_bytes(b"voice")
    packet = {
        "total_frames": 300,
        "shots": [
            {
                "source_id": "s1",
                "timeline_start_frame": 0,
                "duration_frames": 300,
                "source_in_frame": 30,
                "speed": 1.1,
            }
        ],
    }
    audio = {"audio": {"duration_frames": 270, "sha256": file_hash(voice)}}
    template = {"code": "title code", "properties": [{"key": "hook", "defaultValue": "hello"}]}
    doc = {
        "activeTimelineId": "t",
        "assets": [
            {"id": "a", "name": "s1.mp4", "src": "/media/uploads/video.mp4"},
            {"id": "b", "src": "/media/uploads/voice.wav"},
            {"id": "c", "code": "title code"},
        ],
        "timelines": [
            {
                "id": "t",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "trackOrder": ["title", "video", "audio"],
                "tracks": {
                    "title": {"kind": "video"},
                    "video": {"kind": "video"},
                    "audio": {"kind": "audio"},
                },
                "items": [
                    {
                        "kind": "video",
                        "track": "video",
                        "sourceAssetId": "a",
                        "startFrame": 0,
                        "durationInFrames": 300,
                        "srcInFrame": 30,
                        "playbackRate": 1.1,
                        "volume": 0,
                    },
                    {
                        "kind": "audio",
                        "track": "audio",
                        "sourceAssetId": "b",
                        "startFrame": 6,
                        "durationInFrames": 270,
                        "volume": 1,
                    },
                    {
                        "kind": "motion-graphic",
                        "track": "title",
                        "sourceAssetId": "c",
                        "startFrame": 0,
                        "durationInFrames": 300,
                        "props": {"hook": "hello"},
                    },
                ],
            }
        ],
    }
    return doc, packet, audio, template, {"s1": file_hash(video)}


def test_title_must_remain_above_video(tmp_path):
    doc, packet, audio, template, hashes = document_fixture(tmp_path)
    assert verify_document(doc, packet, audio, template, hashes, tmp_path)["status"] == "passed"
    doc["timelines"][0]["trackOrder"] = ["video", "title", "audio"]
    with pytest.raises(Blocked, match="title_layer"):
        verify_document(doc, packet, audio, template, hashes, tmp_path)


def test_cli_executable_uses_public_adapter_or_explicit_override(tmp_path, monkeypatch):
    from mixcut.director_cli_runner import cli_executable
    from mixcut.core import Blocked
    import pytest

    monkeypatch.delenv("MIXCUT_OCC_CLI", raising=False)
    with pytest.raises(Blocked, match="未找到 OpenChatCut CLI"):
        cli_executable(tmp_path)
    default = tmp_path / "runtime/openchatcut/cli-dist/occ.mjs"
    default.parent.mkdir(parents=True)
    default.write_text("example")
    assert cli_executable(tmp_path) == default
    override = tmp_path / "external.mjs"
    override.write_text("example")
    monkeypatch.setenv("MIXCUT_OCC_CLI", str(override))
    assert cli_executable(tmp_path) == override
