"""Fixed, resumable director execution. No model chooses the next operation."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
import urllib.request
from pathlib import Path
from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id
from .director_stage import gate, folder
from .director_execution import media_preflight, prepare_audio
from .director_editor import compile_adds, compile_updates, template_command, verify_placement
from .director_native import Native, upload_slot, upload_slot_expired
from .script_stage import latest as text_latest
from .title_style import validate as validate_style

FIELDS = {
    "director_run_status": ("固定执行状态", "text"),
    "director_run_preview": ("固定执行进度", "text"),
}
STEPS = (
    "preflight",
    "audio",
    "project",
    "imports",
    "typography",
    "add_items",
    "speed",
    "timing",
    "readback",
    "apply",
    "applied_readback",
    "render",
    "download",
    "technical_qc",
)


def export_approved(root, task, bench):
    record = gate(root, task)
    script = next(
        s for s in text_latest(root, record["group_id"])["scripts"] if s["task_id"] == task
    )
    check_live(root, task, bench)
    return {
        "entity": "director_authority_snapshot",
        "record": record,
        "text_batch": text_latest(root, record["group_id"]),
        "style_sha256": validate_style(script)["sha256"],
    }


def authority_request(root, task, action):
    cfg = root / "config/director_authority.local.json"
    if not cfg.exists():
        return None

    authority = load(cfg)
    command = (
        "cd "
        + shlex.quote(authority["workspace"])
        + " && "
        + shlex.join(
            [
                "/usr/bin/env",
                "PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONPATH=src",
                ".venv/bin/python",
                "-m",
                "mixcut",
                "--json",
                "director",
                action,
                "--task",
                safe_id(task),
            ]
        )
    )
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", authority["host"], command],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Blocked("RUNNER_AUTHORITY_UNAVAILABLE", "无法核对单写实例当前审核，已停止", 6) from e
    try:
        bundle = json.loads(result.stdout)
    except ValueError as e:
        raise Blocked("RUNNER_AUTHORITY", "无法读取单写实例的已审版本", 6) from e
    if result.returncode:
        raise Blocked(
            bundle.get("code", "RUNNER_AUTHORITY"), bundle.get("message", "尚无已审执行包"), 3
        )
    return bundle


def pull_authority(root, task):
    bundle = authority_request(root, task, "export-approved")
    if bundle is None:
        return
    if bundle.get("entity") != "director_authority_snapshot":
        raise Blocked("RUNNER_AUTHORITY", "单写实例没有返回有效审核快照", 6)
    record, batch = bundle["record"], bundle["text_batch"]
    if record["task_id"] != task or record["group_id"] != batch["group_id"]:
        raise Blocked("RUNNER_AUTHORITY", "单写实例快照任务不符", 6)
    from .script_stage import folder as text_folder, script_hash

    if (
        any(script_hash(s) != s["content_sha256"] for s in batch["scripts"])
        or digest([s["content_sha256"] for s in batch["scripts"]]) != batch["content_sha256"]
    ):
        raise Blocked("RUNNER_AUTHORITY", "文字快照校验不符", 6)
    for directory, obj in [
        (text_folder(root, batch["group_id"]), batch),
        (folder(root, task), record),
    ]:
        dest = directory / f"r{obj['revision']:04d}.json"
        if dest.exists() and load(dest) != obj:
            # Approval/sync fields may legitimately change; never overwrite different content.
            prior = load(dest)
            content_key = "packet_sha256" if "packet" in obj else "content_sha256"
            if prior[content_key] != obj[content_key]:
                raise Blocked("RUNNER_MIRROR_CONFLICT", "本地已有不同版本，先对账", 6)
        atomic_write(dest, obj)
        atomic_write(directory / "current.json", {"revision": obj["revision"]})


def approval_binding(record, script):
    return {
        "task_id": script["task_id"],
        "director_revision": record["revision"],
        "director_sha256": record["packet_sha256"],
        "text_sha256": script["content_sha256"],
        "title_style_sha256": validate_style(script)["sha256"],
        "input_sha256": script["input_sha256"],
    }


def check_live(root, task, bench):
    record = gate(root, task)
    script = next(
        s for s in text_latest(root, record["group_id"])["scripts"] if s["task_id"] == task
    )
    style = validate_style(script)
    authority = authority_request(root, task, "check-approved")
    if authority is not None:
        if authority.get("entity") != "director_approval_binding" or authority.get(
            "binding"
        ) != approval_binding(record, script):
            raise Blocked("RUNNER_AUTHORITY_CHANGED", "单写实例审核已变化，停止使用本机旧版本", 6)
        # The authority checks its current local approval and live Base requirements.
        # Do not duplicate Base reads on the execution computer.
        return record, script
    fields = bench.task(task)["fields"]
    expected = {
        "director_sha256": record["packet_sha256"],
        "director_review_status": "approved",
        "title_style_sha256": style["sha256"],
        "title_style_review_status": "approved",
    }
    if any(fields.get(k) != v for k, v in expected.items()):
        raise Blocked("RUNNER_LIVE_REVIEW_CHANGED", "飞书文案/标题/编导审核版本变化，已停止", 6)
    # The current human requirements must still match the reviewed text snapshot.
    if hasattr(bench, "v3_rows"):
        from .script_stage import snapshot

        rows = bench.v3_rows(record["group_id"])
        if digest(snapshot(root, record["group_id"], rows)) != script["input_sha256"]:
            raise Blocked("RUNNER_INPUT_CHANGED", "飞书要求已变化，须重新审核", 6)
    return record, script


def verify_native(root, task, native, commands, *, code_readback):
    from .director_editor import context

    record, audio = context(root, task)
    timeline = native.get("timeline", {})
    items = timeline.get("items", [])
    shots = record["packet"]["shots"]
    expected_adds = commands[-1]["arguments"]["adds"]
    videos = sorted([x for x in items if x.get("kind") == "video"], key=lambda x: x["startFrame"])
    differences = []
    if (timeline.get("width"), timeline.get("height"), timeline.get("fps")) != (1080, 1920, 30):
        differences.append("canvas")
    if len(videos) != len(shots) or len(items) != len(shots) + 2 or timeline.get("transitions"):
        differences.append("items_or_transitions")
    for i, (shot, item, addition) in enumerate(zip(shots, videos, expected_adds)):
        expected = {
            "sourceAssetId": addition["assetId"],
            "sourceStartFrame": shot["source_in_frame"],
            "sourceDurationInFrames": shot["source_out_frame"] - shot["source_in_frame"],
            "startFrame": shot["timeline_start_frame"],
            "durationInFrames": shot["duration_frames"],
            "volume": 0,
        }
        if any(item.get(k) != v for k, v in expected.items()):
            differences.append(f"shot_{i + 1}")
        source = shot["source_out_frame"] - shot["source_in_frame"]
        # Current read_project exposes speed through exact source and output windows.
        if abs(source - round(shot["duration_frames"] * shot["speed"])) > 1:
            differences.append(f"shot_{i + 1}.speed")
        transform = item.get("transform") or {}
        allowed = {
            "scale": 1,
            "x": 0,
            "y": 0,
            "rotation": 0,
            "opacity": 1,
            **shot.get("transform", {}),
        }
        if any(k not in allowed or allowed[k] != v for k, v in transform.items()) or any(
            transform.get(k, 1 if k == "scale" else 0) != v
            for k, v in shot.get("transform", {}).items()
        ):
            differences.append(f"shot_{i + 1}.transform")
    voice = [x for x in items if x.get("kind") == "audio"]
    expected_voice = {
        "sourceAssetId": expected_adds[-2]["assetId"],
        "startFrame": 6,
        "sourceStartFrame": 0,
        "durationInFrames": audio["audio"]["duration_frames"],
        "volume": 1,
    }
    if len(voice) != 1 or any(voice[0].get(k) != v for k, v in expected_voice.items()):
        differences.append("voice")
    graphics = [x for x in items if x.get("kind") == "motion-graphic"]
    recipe = template_command(root, task, "readback")["arguments"]
    expected_props = {p["key"]: p["defaultValue"] for p in recipe["properties"]}
    if len(graphics) != 1:
        differences.append("title_count")
    else:
        g = graphics[0]
        if (
            g.get("sourceAssetId") != expected_adds[-1]["assetId"]
            or g.get("startFrame") != 0
            or g.get("durationInFrames") != record["packet"]["total_frames"]
        ):
            differences.append("title_timing_or_asset")
        if any(g.get("props", {}).get(k) != v for k, v in expected_props.items()):
            differences.append("title_style_or_text")
        if any(k not in expected_props and k != "__description" for k in g.get("props", {})):
            differences.append("unplanned_title_property")
        if g.get("transform") not in (
            None,
            {},
            {"scale": 1, "x": 0, "y": 0, "rotation": 0, "opacity": 1},
        ):
            differences.append("title_transform")

        def codes(v):
            if isinstance(v, dict):
                for k, value in v.items():
                    if k == "code" and isinstance(value, str):
                        yield value
                    else:
                        yield from codes(value)
            elif isinstance(v, list):
                for value in v:
                    yield from codes(value)

        if recipe["code"] not in set(codes(code_readback)):
            differences.append("title_code")
    for item in items:
        if (
            item.get("offline")
            or item.get("linkStatus") != "linked"
            or any(
                item.get(k)
                for k in ["effects", "filters", "keyframes", "fadeInFrames", "fadeOutFrames"]
            )
        ):
            differences.append("unplanned_effect_or_offline")
    return {
        "status": "failed" if differences else "passed",
        "differences": differences,
        "kind": "native_editor_readback",
        "director_sha256": record["packet_sha256"],
    }


def unique_media(items):
    unique: dict[str, dict] = {}
    for item in items:
        sid = item["source_id"]
        if sid in unique and any(unique[sid][k] != item[k] for k in ("path", "working_sha256")):
            raise Blocked("RUNNER_MEDIA_CONFLICT", "同一源文件的工作副本绑定不一致", 6)
        unique.setdefault(sid, item)
    return list(unique.values())


def active_upload_slot(native, label, path, slot, arguments):
    # Follow only acknowledged renewals so a resumed upload keeps its original key.
    # Unknown uploads/renewals remain blocked by the durable Native.once journal.
    for _ in range(128):
        current = upload_slot(slot, path)
        expiry = current.get("expiresAt")
        if expiry is None:
            return slot
        renewed_label = f"{label}_slot_renew_{int(expiry)}"
        renewed = native.completed(renewed_label)
        if renewed is not None:
            slot = renewed
            continue
        if not upload_slot_expired(current) or native.receipt_status(label + "_upload") not in {
            None,
            "not_sent",
        }:
            return slot
        slot = native.effect(renewed_label, "import_media", arguments)
    raise Blocked("RUNNER_UPLOAD_SLOT_EXPIRED", "上传槽恢复链超过预算，先对账", 6)


def verify_candidate(root, directory, state):
    if "download" not in state["completed"]:
        return
    candidate = state["results"]["download"]
    output = Path(candidate["path"]).resolve()
    receipt_path = directory / "download.json"
    if (
        not output.is_relative_to(root.resolve())
        or not output.is_file()
        or file_hash(output) != candidate["sha256"]
        or not receipt_path.is_file()
        or load(receipt_path) != candidate
    ):
        raise Blocked("RUNNER_OUTPUT_CHANGED", "候选文件或下载回执变化，旧质检/审核就绪状态失效", 6)
    if "technical_qc" in state["completed"]:
        qc = state["results"]["technical_qc"]
        if qc.get("status") != "passed" or qc.get("output_sha256") != candidate["sha256"]:
            raise Blocked("RUNNER_QC_BINDING", "质检未绑定当前候选文件，不能报告审核就绪", 6)


def run_status(root, task):
    from .director_stage import latest

    record = latest(root, task)
    directory = folder(root, task) / "runs" / f"r{record['revision']:04d}" if record else None
    if directory is None or not (directory / "state.json").is_file():
        return {"status": "not_started"}
    state = load(directory / "state.json")
    if state.get("state_sha256") != digest({k: v for k, v in state.items() if k != "state_sha256"}):
        raise Blocked("RUNNER_STATE_CHANGED", "执行状态文件变化，先对账", 6)
    result = {
        k: state[k]
        for k in ("task_id", "status", "current_step", "completed", "blocked_code", "message")
        if k in state
    }
    try:
        verify_candidate(root, directory, state)
    except Blocked as e:
        result.update(
            status="blocked",
            current_step="artifact_check",
            blocked_code=e.code,
            message=str(e),
            artifact_valid=False,
        )
    return result


def verify_render_ready(root, task, native, state, common):
    proposal = native.read(
        "get_edit_session",
        {
            "editSessionId": state["edit_session"],
            "editorProjectId": state["project_id"],
        },
    )
    before = native.read("get_edit_session", common)
    if (
        proposal.get("status") != "applied"
        or before.get("stale")
        or before.get("status") != "drafting"
        or before.get("baseRevision") is None
    ):
        raise Blocked("RUNNER_RENDER_REVIEW", "已应用提案或导出会话已变化，禁止提交导出", 6)
    view = native.read("read_project", common)
    mg_id = state["add_commands"][-1]["arguments"]["adds"][-1]["assetId"]
    code = native.read("read_project", {"assetId": mg_id, "code": True, **common})
    checked = verify_native(root, task, view, state["add_commands"], code_readback=code)
    after = native.read("get_edit_session", common)
    if (
        checked["status"] != "passed"
        or after.get("stale")
        or after.get("status") != "drafting"
        or before.get("baseRevision") != after.get("baseRevision")
    ):
        raise Blocked("RUNNER_RENDER_READBACK", "导出前实际标题/镜头或工程版本不符，禁止提交", 6)
    return {**checked, "at": now(), "base_revision": after.get("baseRevision")}


def run(root, task, config, bench, *, native_factory=Native, budget_seconds=40):
    """Run in the fixed order. Same entry resumes; unknown mutations never repeat."""
    pull_authority(root, task)
    record, script = check_live(root, task, bench)
    directory = folder(root, task) / "runs" / f"r{record['revision']:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "state.json"
    program_sha256 = digest(
        [
            file_hash(Path(__file__).with_name(n))
            for n in ("director_runner.py", "director_native.py", "director_editor.py")
        ]
    )
    if state_path.exists():
        prior_state = load(state_path)
        if prior_state.get("state_sha256") != digest(
            {k: v for k, v in prior_state.items() if k != "state_sha256"}
        ):
            raise Blocked("RUNNER_STATE_CHANGED", "执行状态文件变化，先对账", 6)
    state = (
        load(state_path)
        if state_path.exists()
        else {
            "version": 1,
            "task_id": task,
            "director_sha256": record["packet_sha256"],
            "title_style_sha256": validate_style(script)["sha256"],
            "completed": [],
            "results": {},
            "status": "running",
            "program_sha256": program_sha256,
        }
    )
    if (
        state["director_sha256"] != record["packet_sha256"]
        or state["title_style_sha256"] != validate_style(script)["sha256"]
    ):
        raise Blocked("RUNNER_VERSION_CHANGED", "执行版本已变化，须新执行目录", 6)
    if state.get("program_sha256") != program_sha256:
        raise Blocked("RUNNER_PROGRAM_CHANGED", "执行程序版本已变化，须核对迁移，不能混用旧回执", 6)
    if state["completed"] != list(STEPS[: len(state["completed"])]):
        raise Blocked("RUNNER_SEQUENCE", "执行步骤不符合固定顺序，已停止", 6)
    state.pop("blocked_code", None)
    state.pop("message", None)
    native = native_factory(directory, lambda: check_live(root, task, bench))
    deadline = time.monotonic() + budget_seconds

    def save():
        state["updated_at"] = now()
        state["state_sha256"] = digest({k: v for k, v in state.items() if k != "state_sha256"})
        atomic_write(state_path, state)

    def session(key):
        value = native.begin(state, key)
        sid = value.get("editSessionId") or value.get("sessionId")
        if not sid:
            raise Blocked("RUNNER_SESSION", "编辑器没有返回会话编号", 6)
        state[key] = sid
        save()
        return {"editSessionId": sid, "editorProjectId": state["project_id"]}

    def rebind():
        native.connect(fresh=True)
        native.bind(state["project_id"])

    try:
        if "download" in state["completed"]:
            state["current_step"] = "artifact_check"
            verify_candidate(root, directory, state)
            state["artifact_valid"] = True
        for step in STEPS:
            if step in state["completed"]:
                continue
            state["current_step"] = step
            state["status"] = "running"
            save()
            if time.monotonic() >= deadline:
                state["status"] = "ready_to_resume"
                break
            check_live(root, task, bench)
            if step == "preflight":
                result = media_preflight(root, record["packet"], config)
                if result["status"] != "passed":
                    raise Blocked("RUNNER_PREFLIGHT", "素材预检未通过", 3)
            elif step == "audio":
                result = prepare_audio(root, task, config)
                if result["status"] != "ready_for_editing":
                    raise Blocked("RUNNER_AUDIO_TIMING", "实测配音时长不符，保留结果等待处理", 3)
            elif step == "project":
                result = native.effect(
                    "project",
                    "create_project",
                    {
                        "name": f"Mixcut固定执行｜{task}｜r{record['revision']}",
                        "compositionWidth": 1080,
                        "compositionHeight": 1920,
                        "fps": 30,
                    },
                )
                state["project_id"] = result.get("projectId") or result.get("id")
                if not state["project_id"]:
                    raise Blocked("RUNNER_PROJECT_UNKNOWN", "工程编号未知，先对账", 6)
            else:
                native.bind(state["project_id"])
                if step == "imports":
                    media = unique_media(state["results"]["preflight"]["items"])
                    audio = state["results"]["audio"]["audio"]
                    files = [(Path(x["path"]), "video", "video/mp4") for x in media]
                    files.append((Path(audio["path"]), "audio", "audio/mpeg"))
                    expected_hashes = {str(Path(x["path"])): x["working_sha256"] for x in media}
                    expected_hashes[audio["path"]] = audio["sha256"]
                    result = {}
                    for i, (path, kind, mime) in enumerate(files):
                        label = f"import_{i:03d}"
                        if file_hash(path) != expected_hashes[str(path)]:
                            raise Blocked(
                                "RUNNER_IMPORT_CHANGED", "待上传媒体与预检/配音回执不符", 6
                            )
                        finalized = native.completed(label + "_finalize")
                        if finalized:
                            state[label] = finalized
                            save()
                        if label in state:
                            result[path.name] = state[label]
                            continue
                        common = session(label + "_session")
                        current = native.read("read_project", {"view": "assets", **common})
                        matches = [
                            x for x in current["mediaPool"]["assets"] if x["name"] == path.name
                        ]
                        if matches:
                            raise Blocked(
                                "RUNNER_IMPORT_CONFLICT", "未记录的同名素材已存在，须对账", 6
                            )
                        slot_arguments = {
                            "action": "create_session",
                            "assetType": kind,
                            "filename": path.name,
                            "contentType": mime,
                            "size": path.stat().st_size,
                            **common,
                        }
                        slot = native.effect(label + "_slot", "import_media", slot_arguments)
                        slot = active_upload_slot(native, label, path, slot, slot_arguments)
                        uploaded = native.upload(label + "_upload", path, slot)
                        if uploaded.get("assetType") != kind or not uploaded.get("receipt"):
                            raise Blocked("RUNNER_IMPORT_RECEIPT", "上传回执不符", 6)
                        probe = json.loads(
                            subprocess.check_output(
                                [
                                    config["paths"]["ffprobe"],
                                    "-v",
                                    "error",
                                    "-show_streams",
                                    "-show_format",
                                    "-of",
                                    "json",
                                    str(path),
                                ],
                                text=True,
                            )
                        )
                        args = {
                            "receipt": uploaded["receipt"],
                            "assetType": kind,
                            "durationInSeconds": float(probe["format"]["duration"]),
                            **common,
                        }
                        if kind == "video":
                            args.update(
                                width=1080,
                                height=1920,
                                fps=30,
                                hasAudioTrack=any(
                                    x["codec_type"] == "audio" for x in probe["streams"]
                                ),
                            )
                        value = native.effect(label + "_finalize", "finalize_uploaded_asset", args)
                        state[label] = value
                        result[path.name] = value
                        save()
                        rebind()
                elif step == "typography":
                    if native.completed("typography"):
                        state["results"][step] = native.completed("typography")
                        state["completed"].append(step)
                        save()
                        rebind()
                        continue
                    common = session("typography_session")
                    command = template_command(root, task, common["editSessionId"])
                    result = native.effect(
                        "typography",
                        command["name"],
                        {**command["arguments"], "editorProjectId": state["project_id"]},
                    )
                    rebind()
                elif step in {"add_items", "speed", "timing", "readback", "apply"}:
                    if step == "apply" and native.completed("apply"):
                        status = native.read(
                            "get_edit_session",
                            {
                                "editSessionId": state["edit_session"],
                                "editorProjectId": state["project_id"],
                            },
                        )
                        if status.get("status") != "applied":
                            raise Blocked(
                                "RUNNER_APPLY_PENDING", "编辑器提案尚未applied，禁止导出", 3
                            )
                        state["results"][step] = native.completed("apply")
                        state["completed"].append(step)
                        save()
                        continue
                    common = session("edit_session")
                    if step == "add_items":
                        view = native.read("read_project", common)
                        saved_commands = state.get("add_commands")
                        commands = compile_adds(
                            root,
                            task,
                            common["editSessionId"],
                            view,
                            config,
                            recovering=bool(saved_commands),
                        )
                        if saved_commands is not None and commands != saved_commands:
                            raise Blocked(
                                "RUNNER_PLACEMENT_CHANGED", "恢复时媒体绑定或已保存调用发生变化", 6
                            )
                        state["add_commands"] = commands
                        save()
                        receipt = native.completed(f"add_{len(commands) - 1}")
                        if receipt:
                            result = verify_placement(view, commands, receipt)
                        else:
                            if view.get("timeline", {}).get("items"):
                                raise Blocked(
                                    "RUNNER_PLACEMENT_UNKNOWN",
                                    "已有片段但缺少成功回执，先对账，不重复添加",
                                    6,
                                )
                            result = {}
                            for i, cmd in enumerate(commands):
                                result = native.effect(
                                    f"add_{i}",
                                    cmd["name"],
                                    {**cmd["arguments"], "editorProjectId": state["project_id"]},
                                )
                    elif step in {"speed", "timing"}:
                        commands = compile_updates(
                            root, task, common["editSessionId"], state["results"]["add_items"]
                        )
                        cmd = commands[0 if step == "speed" else 1]
                        result = native.effect(
                            step,
                            cmd["name"],
                            {**cmd["arguments"], "editorProjectId": state["project_id"]},
                        )
                    elif step == "readback":
                        view = native.read("read_project", common)
                        mg_id = state["add_commands"][-1]["arguments"]["adds"][-1]["assetId"]
                        code = native.read(
                            "read_project", {"assetId": mg_id, "code": True, **common}
                        )
                        result = verify_native(
                            root, task, view, state["add_commands"], code_readback=code
                        )
                        if result["status"] != "passed":
                            raise Blocked(
                                "RUNNER_READBACK", "时间线或标题样式与审核稿不一致，禁止导出", 6
                            )
                    else:
                        result = native.effect(
                            "apply",
                            "review_edit_session",
                            {"summary": "按已审编导和标题样式固定执行", **common},
                        )
                        status = native.read("get_edit_session", common)
                        if status.get("status") != "applied":
                            raise Blocked(
                                "RUNNER_APPLY_PENDING", "编辑器提案尚未applied，禁止导出", 3
                            )
                else:
                    common = session("export_session")
                    if step == "applied_readback":
                        view = native.read("read_project", common)
                        mg_id = state["add_commands"][-1]["arguments"]["adds"][-1]["assetId"]
                        code = native.read(
                            "read_project", {"assetId": mg_id, "code": True, **common}
                        )
                        result = verify_native(
                            root, task, view, state["add_commands"], code_readback=code
                        )
                        if result["status"] != "passed":
                            raise Blocked(
                                "RUNNER_APPLIED_READBACK", "已应用时间线不符，禁止导出", 6
                            )
                    elif step == "render":
                        if native.completed("render") is None:
                            state["results"]["render_preflight"] = verify_render_ready(
                                root, task, native, state, common
                            )
                            save()
                        result = native.effect(
                            "render",
                            "submit_render_job",
                            {
                                "format": "video",
                                "codec": "h264",
                                "resolution": "1080p",
                                "fps": 30,
                                "name": f"{task}_director_r{record['revision']}.mp4",
                                "startFrame": 0,
                                "endFrameExclusive": record["packet"]["total_frames"],
                                "saveToMediaPool": False,
                                **common,
                            },
                        )
                        if not result.get("renderId"):
                            raise Blocked("RUNNER_RENDER_UNKNOWN", "导出编号未知，先对账", 6)
                    elif step == "download":
                        result = native.read(
                            "track_export",
                            {
                                "action": "status",
                                "renderIds": state["results"]["render"]["renderId"],
                                **common,
                            },
                        )
                        if result.get("status") in {"failed", "cancelled", "canceled"}:
                            raise Blocked(
                                "RUNNER_RENDER_FAILED",
                                "导出已失败或取消，保留renderId，不自动重做",
                                6,
                            )
                        if result.get("status") in {"pending", "queued", "rendering", "running"}:
                            raise Blocked("RUNNER_RENDER_PENDING", "导出尚未完成，不重复提交", 3)
                        if result.get("status") != "completed":
                            raise Blocked(
                                "RUNNER_RENDER_UNKNOWN", "导出状态未知，先对账，不重新提交", 6
                            )
                        relative = result.get("downloadUrl", "")
                        if (
                            not relative.startswith("/media/uploads/")
                            or ".." in relative
                            or "?" in relative
                        ):
                            raise Blocked("RUNNER_DOWNLOAD_URL", "导出地址不符合本机媒体路径", 6)
                        output = (
                            root
                            / "deliverables/director_fixed_v1"
                            / task
                            / f"r{record['revision']:04d}"
                            / "candidate.mp4"
                        )
                        output.parent.mkdir(parents=True, exist_ok=True)
                        receipt_file = directory / "download.json"
                        if receipt_file.exists():
                            result = load(receipt_file)
                            if not output.is_file() or file_hash(output) != result["sha256"]:
                                raise Blocked("RUNNER_OUTPUT_CHANGED", "已下载成片变化，停止", 6)
                        else:
                            if output.exists():
                                raise Blocked("RUNNER_OUTPUT_CONFLICT", "已有无回执成片，不覆盖", 6)
                            temp = output.with_suffix(".partial")
                            with (
                                urllib.request.urlopen(
                                    "http://127.0.0.1:5199" + relative, timeout=45
                                ) as response,
                                temp.open("wb") as out,
                            ):
                                while chunk := response.read(1024 * 1024):
                                    out.write(chunk)
                            if (
                                result.get("sizeBytes")
                                and temp.stat().st_size != result["sizeBytes"]
                            ):
                                raise Blocked("RUNNER_DOWNLOAD_SIZE", "成片下载大小不符", 6)
                            temp.replace(output)
                            result = {
                                "path": str(output),
                                "sha256": file_hash(output),
                                "render_id": state["results"]["render"]["renderId"],
                            }
                            atomic_write(receipt_file, result)
                    else:
                        from .qc import technical_qc

                        result = technical_qc(
                            Path(state["results"]["download"]["path"]),
                            {
                                "total_frames": record["packet"]["total_frames"],
                                "audio_tracks": [state["results"]["audio"]["audio"]],
                            },
                            config,
                            state["results"]["applied_readback"],
                        )
                        if result["status"] != "passed" or result.get("warnings"):
                            raise Blocked(
                                "RUNNER_TECHNICAL_QC", "技术检查存在失败或告警，等待处理", 3
                            )
            state["results"][step] = result
            state["completed"].append(step)
            save()
        else:
            verify_candidate(root, directory, state)
            state["artifact_valid"] = True
            state["status"] = "awaiting_content_review"
            state["current_step"] = "content_review"
    except Blocked as e:
        state.update(status="blocked", blocked_code=e.code, message=str(e))
        if e.code in {"RUNNER_OUTPUT_CHANGED", "RUNNER_QC_BINDING"}:
            state["artifact_valid"] = False
    except Exception as e:
        # Preserve the unknown mutation journal; never log opaque API payloads.
        state.update(
            status="blocked",
            blocked_code="RUNNER_EXCEPTION",
            message=f"{type(e).__name__}，已保留步骤和回执，先对账",
        )
    finally:
        save()
        native.close()
    summary = {
        k: state[k]
        for k in [
            "task_id",
            "status",
            "current_step",
            "completed",
            "director_sha256",
            "title_style_sha256",
        ]
    }
    summary.update({k: state[k] for k in ["blocked_code", "message", "project_id"] if k in state})
    if state.get("project_id"):
        summary["editor_url"] = "http://127.0.0.1:5199/#/editor/" + state["project_id"]
    if "download" in state["results"]:
        summary["candidate"] = state["results"]["download"]
    patch = {
        "director_run_status": state["status"],
        "director_run_preview": json.dumps(summary, ensure_ascii=False),
    }
    if all(k in bench.mapping["tables"]["tasks"]["field_ids"] for k in FIELDS):
        row = bench.task(task)
        if any(row["fields"].get(k) != v for k, v in patch.items()):
            bench.patch(row["record_id"], patch)
        bench.verify_patch(task, patch)
    return summary


def run_until_pause(root, task, config, bench, *, advance=run, maximum_seconds=300):
    """Continue normal work and bounded polling; stop on every actionable blocker."""
    deadline = time.monotonic() + maximum_seconds
    while True:
        result = advance(root, task, config, bench)
        if result["status"] == "ready_to_resume":
            if time.monotonic() < deadline:
                continue
            return result
        if result.get("blocked_code") == "RUNNER_RENDER_PENDING" and time.monotonic() < deadline:
            time.sleep(min(3, max(0, deadline - time.monotonic())))
            continue
        return result
