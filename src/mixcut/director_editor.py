"""Compile reviewed director decisions to fixed MCP calls; no AI authors editor JSON."""

from pathlib import Path
from copy import deepcopy
import hashlib
import json
import urllib.request
from .core import Blocked, load
from .director_stage import gate, folder
from .director_execution import media_preflight


def context(root, task):
    record = gate(root, task)
    audio = load(folder(root, task) / f"audio_r{record['revision']:04d}.json")
    if (
        audio["director_sha256"] != record["packet_sha256"]
        or audio["status"] != "ready_for_editing"
    ):
        raise Blocked("DIRECTOR_AUDIO_REQUIRED", "先完成当前审核版本配音与时长检查", 3)
    return record, audio


def template_command(root, task, session):
    from .script_stage import latest

    record, audio = context(root, task)
    script = next(s for s in latest(root, record["group_id"])["scripts"] if s["task_id"] == task)
    from .title_style import validate as validate_title_style

    args = deepcopy(validate_title_style(script)["template"])
    args["name"] = f"{task}_director_typography"
    args["editSessionId"] = session
    args["durationInFrames"] = record["packet"]["total_frames"]
    text = dict(zip(("hook", "product", "sellingPoint", "taste"), script["headline"], strict=True))
    text["captionJson"] = json.dumps(
        [
            {"text": c["text"], "start": c["start_frame"], "end": c["end_frame"]}
            for c in audio["captions"]
        ],
        ensure_ascii=False,
    )
    for p in args["properties"]:
        if p["key"] in text:
            p["defaultValue"] = text[p["key"]]
    return {"name": "create_motion_graphic_from_code", "arguments": args}


def compile_adds(root, task, session, native, config, *, verify_uploads=True, recovering=False):
    record, audio = context(root, task)
    native = native.get("structuredContent", native)
    if (native["timeline"]["items"] and not recovering) or (
        native["timeline"]["width"],
        native["timeline"]["height"],
        native["timeline"]["fps"],
    ) != (1080, 1920, 30):
        raise Blocked("DIRECTOR_EDITOR_START", "须使用本任务的独立空时间线1080×1920/30fps", 3)
    check = media_preflight(root, record["packet"], config)
    if check["status"] != "passed":
        raise Blocked("DIRECTOR_MEDIA_CROP", "须先确认裁切参数", 3)
    assets = native["mediaPool"]["assets"]

    def asset(name, expected_hash=None):
        matches = [a for a in assets if a["name"] == name and not a.get("offline")]
        if len(matches) != 1:
            raise Blocked("DIRECTOR_ASSET", "素材缺失、离线或同名，请对账", 6)
        a = matches[0]
        if expected_hash:
            src = a.get("src", "")
            if not src.startswith("/media/uploads/") or ".." in src:
                raise Blocked(
                    "DIRECTOR_ASSET_VOLATILE", "须使用持久化上传引用，不能使用blob临时素材", 6
                )
            if verify_uploads:
                h = hashlib.sha256()
                with urllib.request.urlopen("http://127.0.0.1:5199" + src, timeout=45) as response:
                    while chunk := response.read(1024 * 1024):
                        h.update(chunk)
                if h.hexdigest() != expected_hash:
                    raise Blocked("DIRECTOR_ASSET_HASH", "上传素材与本地文件不符", 6)
        return a["id"]

    media = {m["source_id"]: m for m in check["items"]}
    adds = []
    for s in record["packet"]["shots"]:
        adds.append(
            {
                "type": "video",
                "assetId": asset(s["source_id"] + ".mp4", media[s["source_id"]]["working_sha256"]),
                "track": "V1",
                "fromFrame": s["timeline_start_frame"],
                "sourceStartFrame": s["source_in_frame"],
                "sourceDurationInFrames": s["source_out_frame"] - s["source_in_frame"],
            }
        )
    adds.extend(
        [
            {
                "type": "audio",
                "assetId": asset(Path(audio["audio"]["path"]).name, audio["audio"]["sha256"]),
                "track": "A1",
                "fromFrame": 6,
                "durationInFrames": audio["audio"]["duration_frames"],
            },
            {
                "type": "motion-graphic",
                "assetId": asset(f"{task}_director_typography"),
                "track": "V2",
                "fromFrame": 0,
                "durationInFrames": record["packet"]["total_frames"],
            },
        ]
    )
    return [
        {
            "name": "edit_track",
            "arguments": {
                "action": "create",
                "json": json.dumps({"trackType": kind, "name": name}, ensure_ascii=False),
                "editSessionId": session,
            },
        }
        for kind, name in [("audio", "确认稿配音"), ("video", "常驻标题与同步字幕")]
    ] + [{"name": "edit_item", "arguments": {"editSessionId": session, "adds": adds}}]


def verify_placement(native, commands, receipt):
    """Adopt only the exact successful placement represented by a durable receipt."""
    native = native.get("structuredContent", native)
    receipt = receipt.get("structuredContent", receipt)
    adds = commands[-1]["arguments"]["adds"]
    results = receipt.get("results", [])
    timeline = native.get("timeline", {})
    items = timeline.get("items", [])
    if (timeline.get("width"), timeline.get("height"), timeline.get("fps")) != (1080, 1920, 30):
        raise Blocked("RUNNER_PLACEMENT_CHANGED", "恢复时间线画幅或帧率不符", 6)
    ids = [r.get("placed", {}).get("itemId") for r in results]
    if (
        len(results) != len(adds)
        or len(items) != len(adds)
        or None in ids
        or len(set(ids)) != len(ids)
        or set(ids) != {i.get("id") for i in items}
    ):
        raise Blocked("RUNNER_PLACEMENT_CHANGED", "落轨回执与实际片段数量/编号不符", 6)
    by_id = {i["id"]: i for i in items}
    for addition, result in zip(adds, results, strict=True):
        item = by_id[result["placed"]["itemId"]]
        expected = {
            "kind": addition["type"],
            "sourceAssetId": addition["assetId"],
            "track": addition["track"],
            "startFrame": addition["fromFrame"],
            "durationInFrames": addition.get(
                "durationInFrames", addition.get("sourceDurationInFrames")
            ),
            "linkStatus": "linked",
        }
        if addition["type"] != "motion-graphic":
            expected["sourceStartFrame"] = addition.get("sourceStartFrame", 0)
        if "sourceDurationInFrames" in addition:
            expected["sourceDurationInFrames"] = addition["sourceDurationInFrames"]
        if (
            not result.get("ok")
            or result.get("kind") != addition["type"]
            or any(item.get(k) != v for k, v in expected.items())
            or item.get("offline")
            or timeline.get("transitions")
        ):
            raise Blocked("RUNNER_PLACEMENT_CHANGED", "实际落轨内容与已保存调用不符，不重加片段", 6)
    return receipt


def compile_updates(root, task, session, added):
    record, audio = context(root, task)
    added = added.get("structuredContent", added)
    results = added["results"]
    shots = record["packet"]["shots"]
    if len(results) != len(shots) + 2 or not all(r.get("ok") for r in results):
        raise Blocked("DIRECTOR_EDITOR_RESULT", "新增片段结果异常，请回读对账", 6)
    speed = []
    timing = []
    for r, s in zip(results, shots):
        if r["kind"] != "video":
            raise Blocked("DIRECTOR_EDITOR_RESULT", "片段类型不符", 6)
        iid = r["placed"]["itemId"]
        speed.append({"type": "video", "itemId": iid, "speed": s["speed"], "volume": 0})
        timing.append(
            {
                "type": "video",
                "itemId": iid,
                "fromFrame": s["timeline_start_frame"],
                "durationInFrames": s["duration_frames"],
                "sourceStartFrame": s["source_in_frame"],
                "volume": 0,
                **({"transform": s["transform"]} if s.get("transform") else {}),
            }
        )
    # Speed must be applied before final duration, in separate calls; same-call retiming shrinks twice.
    return [
        {"name": "edit_item", "arguments": {"editSessionId": session, "updates": updates}}
        for updates in (speed, timing)
    ]
