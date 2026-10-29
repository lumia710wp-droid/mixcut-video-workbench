"""Deterministic V3 OpenChatCut adapter; selects no footage or text."""

from __future__ import annotations

import json
from pathlib import Path
from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id
from .executable import latest_batch, validate_current


def find_script(root: Path, group: str, task: str) -> dict:
    batch = latest_batch(root, group)
    script = next((s for s in (batch or {}).get("scripts", []) if s["task_id"] == task), None)
    if not script:
        raise Blocked("V3_SCRIPT_MISSING", "当前完整执行脚本不存在", 3)
    validate_current(root, script, approved=True)
    return script


def packet(root: Path, config: dict, group: str, task: str) -> dict:
    from .production_plan import normalize_source

    script = find_script(root, group, task)
    sources = {s["source_id"]: s for s in load(root / "catalog/sources.json")}
    media = []
    for sid in sorted({s["source_id"] for s in script["shots"]}):
        receipt = normalize_source(root, config, sources[sid])
        base = next(
            Path(r["path"]) for r in config["source_roots"] if r["root_id"] == receipt["root_id"]
        )
        path = (base / receipt["relative_path"]).resolve()
        if not path.is_file() or file_hash(path) != receipt["working_sha256"]:
            raise Blocked("V3_MEDIA_CHANGED", "制作副本缺失或内容变化", 6)
        media.append(
            {
                "source_id": sid,
                "source_sha256": sources[sid]["file_sha256"],
                "path": str(path),
                "working_sha256": receipt["working_sha256"],
                "total_frames": receipt["total_frames"],
            }
        )
    result = {
        "version": 3,
        "script_sha256": script["content_sha256"],
        "script": script,
        "media": media,
        "requires": "OpenChatCut exact project, verified import bindings, applied readback",
    }
    result["packet_sha256"] = digest(result)
    target = (
        root
        / "jobs/executable_v3"
        / safe_id(group)
        / safe_id(script["batch_id"])
        / f"{safe_id(task)}.packet.json"
    )
    atomic_write(target, result)
    return {
        "status": "prepared",
        "packet_path": str(target),
        "script_sha256": script["content_sha256"],
    }


def motion_props(script: dict) -> dict:
    style = script["typography"]["props"]
    return {
        **style,
        **dict(zip(["hook", "product", "sellingPoint", "taste"], script["headline"], strict=True)),
        "captionJson": json.dumps(
            [
                {"text": c["text"], "start": c["start_frame"], "end": c["end_frame"]}
                for c in script["captions"]
            ],
            ensure_ascii=False,
        ),
    }


def verify_native(script: dict, native: dict, bindings: dict) -> dict:
    """Compare actual editor fields, never a self-authored normalized DTO."""
    diffs = []
    items = native.get("timeline", {}).get("items", [])
    actual = sorted([i for i in items if i.get("kind") == "video"], key=lambda i: i["startFrame"])
    if len(actual) != len(script["shots"]):
        diffs.append("video_count")
    for n, (wanted, item) in enumerate(zip(script["shots"], actual)):
        expected = {
            "sourceAssetId": bindings[wanted["source_id"]]["asset_id"],
            "sourceStartFrame": wanted["source_in_frame"],
            "sourceDurationInFrames": wanted["source_out_frame"] - wanted["source_in_frame"],
            "durationInFrames": wanted["duration_frames"],
            "startFrame": wanted["timeline_start_frame"],
            "volume": 0,
        }
        for k, v in expected.items():
            if item.get(k) != v:
                diffs.append(f"shot_{n + 1}.{k}")
        if (
            item.get("offline")
            or item.get("effects")
            or item.get("filters")
            or item.get("keyframes")
        ):
            diffs.append(f"shot_{n + 1}.unplanned_effect_or_offline")
        transform = item.get("transform")
        if transform not in (None, {}, {"scale": 1, "x": 0, "y": 0, "rotation": 0, "opacity": 1}):
            diffs.append(f"shot_{n + 1}.transform")
    audio = [i for i in items if i.get("kind") == "audio"]
    voice = script["voiceover"]
    expected_voice = {
        "sourceAssetId": bindings["voiceover"]["asset_id"],
        "startFrame": voice["start_frame"],
        "sourceStartFrame": 0,
        "durationInFrames": voice["duration_frames"],
        "volume": voice["gain"],
    }
    if len(audio) != 1 or any(audio[0].get(k) != v for k, v in expected_voice.items()):
        diffs.append("voiceover")
    graphics = [i for i in items if i.get("kind") == "motion-graphic"]
    if (
        len(graphics) != 1
        or graphics[0].get("props") != motion_props(script)
        or graphics[0].get("sourceAssetId") != bindings["typography"]["asset_id"]
        or graphics[0].get("startFrame") != 0
        or graphics[0].get("durationInFrames") != script["total_frames"]
    ):
        diffs.append("typography_or_captions")
    if len(items) != len(actual) + len(audio) + len(graphics):
        diffs.append("unplanned_items")
    if native.get("timeline", {}).get("transitions"):
        diffs.append("unplanned_transition")
    # read_project exposes settings in project in current editor builds.
    settings = next(
        (t for t in native.get("timelines", []) if t.get("id") == native.get("activeTimelineId")),
        None,
    ) or native.get("settings", {})
    for key, value in [("width", 1080), ("height", 1920), ("fps", 30)]:
        if settings.get(key) != value:
            diffs.append(f"canvas.{key}")
    return {
        "status": "failed" if diffs else "passed",
        "differences": diffs,
        "kind": "native_editor_readback",
        "script_sha256": script["content_sha256"],
    }


def validate_packet(root: Path, bundle: dict, bindings: dict) -> dict:
    if bundle["packet_sha256"] != digest({k: v for k, v in bundle.items() if k != "packet_sha256"}):
        raise Blocked("V3_PACKET_HASH", "执行包内容变化", 6)
    script = find_script(root, bundle["script"]["group_id"], bundle["script"]["task_id"])
    if script != bundle["script"]:
        raise Blocked("V3_PACKET_STALE", "执行包与当前批准不一致", 6)
    for media in bundle["media"] + [
        {
            "source_id": "voiceover",
            "path": script["voiceover"]["path"],
            "working_sha256": script["voiceover"]["sha256"],
        }
    ]:
        path = Path(media["path"]).resolve()
        bound = bindings.get(media["source_id"], {})
        if (
            not path.is_relative_to(root.resolve())
            or not path.is_file()
            or file_hash(path) != media["working_sha256"]
            or bound.get("file_sha256") != media["working_sha256"]
            or not bound.get("asset_id")
            or not bound.get("import_receipt")
        ):
            raise Blocked("V3_ASSET_BINDING", "必须使用已核对本地文件及真实上传回执的素材绑定", 6)
    if bindings.get("typography", {}).get("asset_id") != script["typography"]["asset_id"]:
        raise Blocked("V3_STYLE_CHANGED", "必须沿用批准的文字模板资产", 6)
    return script


def stage(root: Path, bundle: dict, bindings: dict, project_id: str, session_id: str, tool) -> dict:
    """tool(name,args) is a connected MCP caller returning structuredContent.

    Media must already be imported (with receipts). Template is duplicated into a
    separate project. This adapter neither imports by filename nor guesses IDs.
    """
    script = validate_packet(root, bundle, bindings)
    if project_id == script["typography"]["template_project_id"]:
        raise Blocked("V3_TEMPLATE_READONLY", "请在独立项目剪辑，保留批准样片项目", 3)
    common = {"editSessionId": session_id, "editorProjectId": project_id}
    current = tool("read_project", common)
    if current.get("projectId") != project_id:
        raise Blocked("V3_EDITOR_TARGET", "编辑器项目不匹配", 6)
    graphics = [
        i
        for i in current["timeline"]["items"]
        if i["kind"] == "motion-graphic"
        and i["sourceAssetId"] == bindings["typography"]["asset_id"]
    ]
    if len(graphics) != 1:
        raise Blocked("V3_STYLE_MISSING", "项目中必须有唯一的批准文字模板实例", 3)
    other = [i for i in current["timeline"]["items"] if i["id"] != graphics[0]["id"]]
    additions = []
    cursor = 0
    # Add at source duration with a gap; then retime and pack in explicit order.
    for shot in script["shots"]:
        count = shot["source_out_frame"] - shot["source_in_frame"]
        additions.append(
            {
                "type": "video",
                "assetId": bindings[shot["source_id"]]["asset_id"],
                "track": "V1",
                "fromFrame": cursor,
                "sourceStartFrame": shot["source_in_frame"],
                "sourceDurationInFrames": count,
            }
        )
        cursor += count + 30
    additions.append(
        {
            "type": "audio",
            "assetId": bindings["voiceover"]["asset_id"],
            "track": "A1",
            "fromFrame": 6,
            "sourceStartFrame": 0,
            "sourceDurationInFrames": script["voiceover"]["duration_frames"],
        }
    )
    changes = {
        "deletes": [{"type": i["kind"], "itemId": i["id"], "ripple": False} for i in other],
        "adds": additions,
        "updates": [
            {
                "type": "motion-graphic",
                "itemId": graphics[0]["id"],
                "fromFrame": 0,
                "durationInFrames": script["total_frames"],
                "props": motion_props(script),
            }
        ],
    }
    tool("edit_item", {**common, **changes, "validateOnly": True})
    tool("edit_item", {**common, **changes})
    draft = tool("read_project", common)
    videos = sorted(
        [i for i in draft["timeline"]["items"] if i["kind"] == "video"],
        key=lambda i: i["startFrame"],
    )
    if len(videos) != len(script["shots"]):
        raise Blocked("V3_EDITOR_CLAMPED", "新增镜头数量异常，保留草稿并先回读对账", 6)
    updates = []
    for actual, wanted in zip(videos, script["shots"], strict=True):
        if (
            actual["sourceAssetId"] != bindings[wanted["source_id"]]["asset_id"]
            or actual["sourceStartFrame"] != wanted["source_in_frame"]
        ):
            raise Blocked("V3_EDITOR_CLAMPED", "源入点或原片绑定未按脚本设置", 6)
        updates.append(
            {"type": "video", "itemId": actual["id"], "speed": wanted["speed"], "volume": 0}
        )
    updates += [
        {"type": "audio", "itemId": i["id"], "volume": 1}
        for i in draft["timeline"]["items"]
        if i["kind"] == "audio"
    ]
    tool("edit_item", {**common, "updates": updates})
    tool(
        "edit_track",
        {
            **common,
            "action": "reorder_items",
            "trackId": "V1",
            "json": json.dumps({"itemIds": [i["id"] for i in videos]}),
        },
    )
    result = verify_native(script, tool("read_project", common), bindings)
    atomic_write(
        root
        / "jobs/executable_v3"
        / script["group_id"]
        / script["batch_id"]
        / f"{script['task_id']}.draft_readback.json",
        result,
    )
    if result["status"] != "passed":
        raise Blocked(
            "V3_READBACK_MISMATCH", "草稿参数与批准脚本不一致，禁止应用/导出，查看差异报告", 6
        )
    # Never silently apply: caller uses existing user-authorized manual/auto session policy.
    return {
        "status": "draft_verified",
        "script_sha256": script["content_sha256"],
        "session_id": session_id,
    }


def export_guard(root: Path, bundle: dict, bindings: dict, native: dict, *, applied: bool) -> dict:
    script = validate_packet(root, bundle, bindings)
    report = verify_native(script, native, bindings)
    if not applied or report["status"] != "passed":
        raise Blocked("V3_EXPORT_BLOCKED", "必须先应用且正式时间线逐项一致，才能导出", 6)
    report.update(verified_at=now(), native_project_id=native.get("projectId"))
    return report
