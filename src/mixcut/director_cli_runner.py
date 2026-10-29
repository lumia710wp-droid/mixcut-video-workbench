"""Fixed headless CLI execution of an approved director packet.

Uses the official CLI and installed-template API; never writes project JSON.
Each mutation has a durable intent and receipt. Unknown effects stop on resume.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .core import Blocked, atomic_write, digest, file_hash, load, now
from .director_runner import check_live, pull_authority, unique_media
from .director_execution import media_preflight, prepare_audio
from .director_editor import template_command
from .director_stage import folder
from .qc import technical_qc


def verify_document(doc, packet, audio, template, source_hashes, media_dir):
    """Verify actual persisted document and local media against approved inputs."""
    errors = []
    tl = next((t for t in doc["timelines"] if t["id"] == doc["activeTimelineId"]), None)
    if not tl or (tl.get("width"), tl.get("height"), tl.get("fps")) != (1080, 1920, 30):
        raise Blocked("CLI_CANVAS", "实际工程画幅/帧率不符", 6)
    items = tl.get("items", [])
    assets = {a["id"]: a for a in doc["assets"]}
    videos = sorted((i for i in items if i["kind"] == "video"), key=lambda i: i["startFrame"])
    voices = [i for i in items if i["kind"] == "audio"]
    titles = [i for i in items if i["kind"] == "motion-graphic"]
    if len(items) != len(packet["shots"]) + 2 or len(videos) != len(packet["shots"]):
        errors.append("item_count")
    track_order = tl.get("trackOrder", [])
    tracks = tl.get("tracks", {})
    video_tracks = {v.get("track") for v in videos}
    if len(video_tracks) != 1 or any(
        tracks.get(track, {}).get("kind") != "video" for track in video_tracks
    ):
        errors.append("video_tracks")
    if len(voices) == 1 and tracks.get(voices[0].get("track"), {}).get("kind") != "audio":
        errors.append("voice_track")
    if len(titles) == 1 and len(video_tracks) == 1:
        title_track = titles[0].get("track")
        video_track = next(iter(video_tracks))
        if (
            tracks.get(title_track, {}).get("kind") != "video"
            or title_track == video_track
            or title_track not in track_order
            or video_track not in track_order
            or track_order.index(title_track) >= track_order.index(video_track)
        ):
            errors.append("title_layer")
        if titles[0].get("transform") not in (
            None,
            {},
            {"scale": 1, "x": 0, "y": 0, "rotation": 0, "opacity": 1},
        ):
            errors.append("title_transform")
    media_checks = []
    for shot, item in zip(packet["shots"], videos):
        asset = assets.get(item.get("sourceAssetId"), {})
        expected = {
            "startFrame": shot["timeline_start_frame"],
            "durationInFrames": shot["duration_frames"],
            "srcInFrame": shot["source_in_frame"],
            "playbackRate": shot["speed"],
            "volume": 0,
        }
        if any(item.get(k, 0 if k == "srcInFrame" else None) != v for k, v in expected.items()):
            errors.append("shot_timing_" + shot["source_id"])
        if asset.get("name") != shot["source_id"] + ".mp4":
            errors.append("shot_identity")
        if item.get("transform", {}) != shot.get("transform", {}):
            errors.append("shot_transform")
        media_checks.append((asset, source_hashes[shot["source_id"]]))
    if len(voices) != 1:
        errors.append("voice_count")
    else:
        v = voices[0]
        if (
            v.get("startFrame"),
            v.get("durationInFrames"),
            v.get("srcInFrame", 0),
            v.get("volume", 1),
            v.get("playbackRate", 1),
        ) != (6, audio["audio"]["duration_frames"], 0, 1, 1):
            errors.append("voice_timing")
        media_checks.append((assets.get(v.get("sourceAssetId"), {}), audio["audio"]["sha256"]))
    props = {p["key"]: p["defaultValue"] for p in template["properties"]}
    if len(titles) != 1:
        errors.append("title_count")
    else:
        t = titles[0]
        a = assets.get(t.get("sourceAssetId"), {})
        actual = {k: v for k, v in t.get("props", {}).items() if k != "__description"}
        if (a.get("code") or t.get("code", "")).strip() != template[
            "code"
        ].strip() or actual != props:
            errors.append("approved_title_code_or_props")
        if t.get("startFrame") != 0 or t.get("durationInFrames") != packet["total_frames"]:
            errors.append("title_timing")
    for item in items:
        if any(
            item.get(k)
            for k in [
                "effects",
                "filters",
                "keyframes",
                "fadeInFrames",
                "fadeOutFrames",
                "transitionIn",
                "transitionOut",
            ]
        ):
            errors.append("unplanned_effect")
    if tl.get("transitions"):
        errors.append("transitions")
    for asset, expected_hash in media_checks:
        src = asset.get("src", "")
        prefix = "/media/uploads/"
        if not src.startswith(prefix) or "/" in src[len(prefix) :] or ".." in src:
            errors.append("media_path")
            continue
        path = media_dir / src[len(prefix) :]
        if not path.is_file():
            ref = media_dir / ".references" / (path.name + ".json")
            if ref.is_file():
                data = load(ref)
                if data.get("version") == 1 and Path(data.get("sourcePath", "")).is_absolute():
                    path = Path(data["sourcePath"])
        if not path.is_file() or file_hash(path) != expected_hash:
            errors.append("media_hash")
    if errors:
        raise Blocked("CLI_READBACK", "实际CLI工程校验不符: " + ",".join(sorted(set(errors))), 6)
    return {
        "status": "passed",
        "kind": "cli_persisted_document",
        "document_sha256": digest(doc),
        "director_sha256": digest(packet),
        "differences": [],
    }


def cli_executable(root):
    cli_path = Path(
        os.environ.get("MIXCUT_OCC_CLI", str(root / "runtime/openchatcut/cli-dist/occ.mjs"))
    )
    if not cli_path.is_file():
        raise Blocked("CLI_MISSING", "未找到 OpenChatCut CLI；请先阅读安装说明", 6)
    return cli_path


def run(root, task, config, bench):
    pull_authority(root, task)
    record, script = check_live(root, task, bench)
    directory = folder(root, task) / "cli_runs" / f"r{record['revision']:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "state.json"
    cli_path = cli_executable(root)
    binding = {
        "director": record["packet_sha256"],
        "text": script["content_sha256"],
        "program": file_hash(Path(__file__)),
        "cli": file_hash(cli_path),
    }
    state = (
        load(state_path)
        if state_path.exists()
        else {"task_id": task, "binding": binding, "results": {}, "status": "running"}
    )
    if state.get("status") == "awaiting_content_review" and state.get("candidate"):
        from .director_backend import cli_status

        return cli_status(root, task)
    if state["binding"] != binding:
        raise Blocked("CLI_VERSION_CHANGED", "CLI执行版本变化，先对账", 6)

    def save():
        state["updated_at"] = now()
        atomic_write(state_path, state)

    def cli(args, *, timeout=180):
        try:
            proc = subprocess.run(
                [str(root / "tools/occ"), *args, "--json"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Blocked("CLI_EFFECT_UNKNOWN", "CLI中断或超时，保留回执并先对账", 6) from error
        if proc.returncode:
            atomic_write(
                directory / "last_cli_error.json",
                {"args": args, "exit": proc.returncode, "stderr": proc.stderr[-4000:]},
            )
            raise Blocked("CLI_FAILED", "CLI调用失败，已保留回执与错误", 6)
        return json.loads(proc.stdout)

    def once(label, args, call):
        receipt = directory / (label + ".json")
        if receipt.exists():
            saved = load(receipt)
            if saved["args_sha256"] != digest(args) or saved["status"] != "completed":
                raise Blocked("CLI_UNKNOWN_EFFECT", "未确认执行结果或参数变化: " + label, 6)
            return saved["result"]
        check_live(root, task, bench)
        atomic_write(receipt, {"status": "started", "args_sha256": digest(args), "at": now()})
        result = call()
        atomic_write(
            receipt,
            {"status": "completed", "args_sha256": digest(args), "result": result, "at": now()},
        )
        state["results"][label] = result
        save()
        return result

    def tool(label, name, args):
        argfile = directory / (label + ".args.json")
        atomic_write(argfile, args)

        def execute_tool():
            result = cli(
                [
                    "tools",
                    "call",
                    name,
                    "--project",
                    state["project_id"],
                    "--args",
                    "@" + str(argfile),
                    "--apply",
                ]
            )
            if not result.get("applied") and not (label == "imports" and result.get("noChanges")):
                raise Blocked("CLI_EFFECT_NOT_APPLIED", "CLI操作未应用，先核对", 6)
            for op in result.get("ops", []):
                if isinstance(op.get("result"), dict) and (
                    op["result"].get("error") or op["result"].get("ok") is False
                ):
                    raise Blocked("CLI_EFFECT_FAILED", "CLI操作返回错误，先核对", 6)
            return result

        return once(
            label,
            {"project": state["project_id"], "tool": name, "args": args},
            execute_tool,
        )

    def document():
        result = cli(["project", "show", state["project_id"], "--doc"])
        atomic_write(directory / "actual_document.json", result)
        return result["doc"]

    try:
        state.pop("blocked_code", None)
        state.pop("message", None)
        state["status"] = "running"
        save()
        preflight = media_preflight(root, record["packet"], config)
        if preflight["status"] != "passed":
            raise Blocked("CLI_PREFLIGHT", "素材预检未通过", 6)
        audio = prepare_audio(root, task, config)
        if audio["status"] != "ready_for_editing":
            raise Blocked("RUNNER_AUDIO_TIMING", "实测配音时长不符，保留结果等待处理", 3)
        template = template_command(root, task, "unused")["arguments"]
        template.pop("editSessionId", None)
        state["current_step"] = "project"
        save()
        old_file = folder(root, task) / "runs" / f"r{record['revision']:04d}" / "state.json"
        if "project_id" not in state and old_file.exists():
            old = load(old_file)
            if (
                old.get("director_sha256") != record["packet_sha256"]
                or "applied_readback" not in old.get("completed", [])
                or old.get("results", {}).get("applied_readback", {}).get("status") != "passed"
            ):
                raise Blocked("CLI_EXISTING_RUN", "已有执行未完成应用回读，须先对账", 6)
            state["project_id"] = old["project_id"]
            state["adopted_from"] = str(old_file)
            state["adopted_apply"] = old["results"]["apply"]
            save()
        if "project_id" not in state:
            created = once(
                "project",
                {"task": task, "revision": record["revision"]},
                lambda: cli(
                    [
                        "project",
                        "new",
                        f"Mixcut CLI {task} r{record['revision']}",
                        "--size",
                        "1080x1920",
                        "--fps",
                        "30",
                    ]
                ),
            )
            state["project_id"] = created["id"]
            save()
        if not state.get("adopted_from"):
            media = unique_media(preflight["items"])
            tool(
                "imports",
                "import_assets",
                {"paths": [m["path"] for m in media] + [audio["audio"]["path"]]},
            )
            pack_id = "mixcut-" + digest({"task": task, "director": record["packet_sha256"]})[:20]
            item = {
                "id": "approved-title",
                "type": "mg-template",
                "name": f"{task}_director_typography",
                "code": template["code"],
                "width": 1080,
                "height": 1920,
                "durationInFrames": record["packet"]["total_frames"],
                "props": {p["key"]: p["defaultValue"] for p in template["properties"]},
                "propSchema": template["properties"],
            }
            pack = {
                "format": "openchatcut-plugin@1",
                "id": pack_id,
                "name": "Mixcut approved title",
                "version": "1",
                "installedAt": 0,
                "enabled": True,
                "items": [item],
            }

            def install():
                plugin_root = root / "jobs/cli_title_templates"
                manifest = plugin_root / pack_id / "MQ" / "manifest.json"
                index = plugin_root / "index.json"
                entries = load(index) if index.exists() else []
                existing = [e for e in entries if e.get("id") == pack_id]
                if existing and (not manifest.exists() or load(manifest) != pack):
                    raise Blocked("CLI_TITLE_INSTALL", "标题模板已有冲突，先对账", 6)
                manifest.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(manifest, pack)
                if not existing:
                    entries.append(
                        {"id": pack_id, "version": "1", "enabled": True, "installedAt": 0}
                    )
                    atomic_write(index, entries)
                return {"ok": True, "path": str(manifest)}

            once("title_template", pack, install)
            tool(
                "title",
                "add_motion_graphic",
                {"templateName": item["name"], "track": "V1", "startFrame": 0},
            )
            doc = document()
            if "placement" not in state["results"]:
                assets = doc["assets"]

                def aid(name):
                    found = [a["id"] for a in assets if a["name"] == name]
                    if len(found) != 1:
                        raise Blocked("CLI_ASSET", "素材名称匹配不唯一", 6)
                    return found[0]

                tl = next(t for t in doc["timelines"] if t["id"] == doc["activeTimelineId"])
                if len(tl["items"]) != 1 or tl["items"][0]["kind"] != "motion-graphic":
                    raise Blocked("CLI_START", "CLI起始时间线非唯一标题", 6)
                adds = [
                    {
                        "type": "video",
                        "assetId": aid(s["source_id"] + ".mp4"),
                        "track": "V1",
                        "fromFrame": s["timeline_start_frame"],
                        "sourceStartFrame": s["source_in_frame"],
                        "sourceDurationInFrames": s["source_out_frame"] - s["source_in_frame"],
                    }
                    for s in record["packet"]["shots"]
                ]
                adds.append(
                    {
                        "type": "audio",
                        "assetId": aid(Path(audio["audio"]["path"]).name),
                        "track": "A1",
                        "fromFrame": 6,
                        "durationInFrames": audio["audio"]["duration_frames"],
                    }
                )
                ops = [
                    {
                        "tool": "edit_track",
                        "args": {
                            "action": "create",
                            "json": json.dumps({"trackType": kind, "name": name}),
                        },
                    }
                    for kind, name in [("audio", "确认稿配音"), ("video", "常驻标题与同步字幕")]
                ]
                ops += [
                    {
                        "tool": "move_item",
                        "args": {"itemId": tl["items"][0]["id"], "track": "V2", "startFrame": 0},
                    },
                    {"tool": "edit_item", "args": {"adds": adds}},
                ]
                opfile = directory / "placement.ops.json"
                atomic_write(opfile, ops)
                once(
                    "placement",
                    ops,
                    lambda: cli(
                        [
                            "edit",
                            "--project",
                            state["project_id"],
                            "--ops",
                            "@" + str(opfile),
                            "--apply",
                        ]
                    ),
                )
            doc = document()
            tl = next(t for t in doc["timelines"] if t["id"] == doc["activeTimelineId"])
            videos = sorted(
                (i for i in tl["items"] if i["kind"] == "video"), key=lambda i: i["startFrame"]
            )
            shots = record["packet"]["shots"]
            if len(videos) != len(shots):
                raise Blocked("CLI_ITEMS", "视频片段数量不符", 6)
            tool(
                "speed",
                "edit_item",
                {
                    "updates": [
                        {"type": "video", "itemId": v["id"], "speed": s["speed"], "volume": 0}
                        for v, s in zip(videos, shots)
                    ]
                },
            )
            tool(
                "timing",
                "edit_item",
                {
                    "updates": [
                        {
                            "type": "video",
                            "itemId": v["id"],
                            "fromFrame": s["timeline_start_frame"],
                            "durationInFrames": s["duration_frames"],
                            "sourceStartFrame": s["source_in_frame"],
                            "volume": 0,
                            **({"transform": s["transform"]} if s.get("transform") else {}),
                        }
                        for v, s in zip(videos, shots)
                    ]
                },
            )
        state["current_step"] = "readback"
        save()
        profile = cli(["where"])
        doc = document()
        checked = verify_document(
            doc,
            record["packet"],
            audio,
            template,
            {m["source_id"]: m["working_sha256"] for m in preflight["items"]},
            Path(profile["mediaDir"]),
        )
        atomic_write(directory / "readback.json", checked)
        output = (
            root
            / "deliverables/director_cli_v1"
            / task
            / f"r{record['revision']:04d}"
            / "candidate.mp4"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        state["current_step"] = "render"
        save()

        def render():
            if output.exists():
                raise Blocked("CLI_OUTPUT_CONFLICT", "已有无回执成片，不覆盖", 6)
            result = cli(
                [
                    "render",
                    state["project_id"],
                    "--out",
                    str(output),
                    "--resolution",
                    "1080p",
                    "--fps",
                    "30",
                    "--codec",
                    "h264",
                    "--from",
                    "0",
                    "--to",
                    str(record["packet"]["total_frames"]),
                ],
                timeout=900,
            )
            return {**result, "sha256": file_hash(output)}

        candidate = once(
            "render",
            {"project": state["project_id"], "doc": digest(doc), "output": str(output)},
            render,
        )
        if file_hash(output) != candidate["sha256"] or digest(document()) != digest(doc):
            raise Blocked("CLI_OUTPUT_CHANGED", "导出文件或工程在渲染后变化", 6)
        state["current_step"] = "technical_qc"
        save()
        qc = technical_qc(
            output,
            {"total_frames": record["packet"]["total_frames"], "audio_tracks": [audio["audio"]]},
            config,
            checked,
        )
        state["candidate"] = {"path": str(output), "sha256": candidate["sha256"]}
        if qc["status"] != "passed" or qc.get("warnings"):
            raise Blocked("CLI_QC", "技术检查含失败或告警", 6)
        state["status"] = "awaiting_content_review"
        state["current_step"] = "content_review"
    except Blocked as error:
        state.update(status="blocked", blocked_code=error.code, message=str(error))
    finally:
        save()
    return {
        k: state[k]
        for k in [
            "task_id",
            "status",
            "current_step",
            "project_id",
            "candidate",
            "blocked_code",
            "message",
        ]
        if k in state
    }
