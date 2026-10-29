from __future__ import annotations
import os
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from .core import Blocked, atomic_write, file_hash, load, now, resolve_media, writer_lock
from .contracts import validate_plan
from .feishu import enqueue
from .ledger import commit_usage, counts
from .workflow import current_build, scalar


def _iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_dicts(nested)


def _tool_values(result: dict) -> list:
    values = []
    for block in result.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            values.append(json.loads(block.get("text", "")))
        except (TypeError, json.JSONDecodeError):
            values.append(block.get("text", ""))
    return values


def _safe_receipt(value):
    secrets = {"authorization", "confirmationId", "receipt", "uploadUrl"}
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key in secrets else _safe_receipt(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_safe_receipt(nested) for nested in value]
    return value


def _ensure_engine_token() -> None:
    if os.environ.get("OPENCHATCUT_MCP_TOKEN"):
        return
    path = Path.home() / ".openchatcut/mcp-token"
    if path.is_file() and path.read_text().strip():
        os.environ["OPENCHATCUT_MCP_TOKEN"] = path.read_text().strip()


def _connected_project_ids(result: dict) -> set[str]:
    connected: set[str] = set()
    values = [result.get("structuredContent"), *_tool_values(result)]
    for item in _iter_dicts(values):
        ids = item.get("connectedProjectIds")
        if isinstance(ids, list):
            connected.update(value for value in ids if isinstance(value, str))
        project_id = item.get("projectId")
        if isinstance(project_id, str) and item.get("connected") is True:
            connected.add(project_id)
    return connected


def _require_connected_editor(build_dir: Path, project_id: str) -> dict:
    """Fail before any edit when the intended project is not the visible editor."""
    _ensure_engine_token()
    from .engine import OpenChatCutAdapter

    adapter = OpenChatCutAdapter()
    try:
        result = adapter.call("openchatcut_status")
    finally:
        adapter.close()
    connected = sorted(_connected_project_ids(result))
    receipt = {
        "at": now(),
        "project_id": project_id,
        "connected": project_id in connected,
        "connected_project_ids": connected,
    }
    atomic_write(build_dir / "openchatcut/runtime_preflight.json", receipt)
    if not receipt["connected"]:
        raise Blocked(
            "EDITOR_PROJECT_REQUIRED",
            f"Open the exact OpenChatCut project before this phase: {project_id}",
            3,
        )
    return receipt


def ensure_project(workspace: Path, build_dir: Path, plan: dict) -> dict:
    """Create once or reconcile by the exact deterministic project name."""
    project_path = build_dir / "openchatcut/project.json"
    if project_path.exists():
        return load(project_path)
    _ensure_engine_token()
    from .engine import OpenChatCutAdapter

    name = f"Mixcut｜{plan['task_id']}｜{plan['build_id']}"
    receipt_dir = build_dir / "openchatcut/create"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    adapter = OpenChatCutAdapter()
    sequence = 0

    def tool(tool_name: str, arguments: dict) -> tuple[dict, list]:
        nonlocal sequence
        for _ in range(3):
            sequence += 1
            result = adapter.rpc(
                "tools/call", {"name": tool_name, "arguments": arguments}
            )
            atomic_write(
                receipt_dir / f"{sequence:02d}_{tool_name}.json", _safe_receipt(result)
            )
            values = _tool_values(result)
            candidates = [result.get("structuredContent"), *values]
            if any(
                isinstance(item, dict) and item.get("needs_confirmation") is True
                for item in candidates
            ):
                print(f"CONFIRM_REQUIRED {tool_name}", flush=True)
                input()
                continue
            if result.get("isError") or any(
                isinstance(item, dict)
                and (item.get("error") or item.get("outcome") in {"failed", "rejected"})
                for item in candidates
            ):
                raise Blocked(
                    "ENGINE_TOOL_FAILED",
                    f"{tool_name} failed; inspect {receipt_dir}",
                    4,
                )
            return result, values
        raise Blocked("ENGINE_CONFIRMATION", f"{tool_name} was not confirmed", 3)

    try:
        _, projects_values = tool("list_projects", {})
        exact = []
        for item in _iter_dicts(projects_values):
            if item.get("name") == name:
                project_id = item.get("projectId") or item.get("id")
                if isinstance(project_id, str):
                    exact.append(project_id)
        if len(set(exact)) > 1:
            raise Blocked("PROJECT_CONFLICT", f"Multiple projects named {name}", 6)
        created_now = False
        if exact:
            project_id = exact[0]
        else:
            result, values = tool(
                "create_project",
                {
                    "name": name,
                    "description": (
                        f"Mixcut {plan['task_id']} r{plan['script_revision']} "
                        f"{plan['build_id']}"
                    ),
                    "compositionWidth": plan["canvas"]["width"],
                    "compositionHeight": plan["canvas"]["height"],
                    "fps": plan["canvas"]["fps_num"] / plan["canvas"]["fps_den"],
                },
            )
            project_id = None
            for item in _iter_dicts([result.get("structuredContent"), *values]):
                value = item.get("projectId") or item.get("id")
                if isinstance(value, str) and value:
                    project_id = value
                    break
            if not project_id:
                raise Blocked("PROJECT_CREATE_UNKNOWN", "No project id returned; reconcile", 5)
            created_now = True
        project = {
            "task_id": plan["task_id"],
            "script_revision": f"r{plan['script_revision']}",
            "build_id": plan["build_id"],
            "project_id": project_id,
            "project_name": name,
            "editor_url": f"http://127.0.0.1:5199/#/editor/{project_id}",
            "created_now": created_now,
        }
        atomic_write(project_path, project)
        return project
    finally:
        adapter.close()


def _persist_engine_phase(workspace: Path, build_dir: Path, plan: dict, phase: str) -> dict:
    project = load(build_dir / "openchatcut/project.json")
    state_path = build_dir / "state.json"
    state = load(state_path) if state_path.exists() else {}
    stage, render_status = {
        "create": ("ready_for_openchatcut_import", "not_started"),
        "stage": ("waiting_openchatcut_apply", "not_started"),
        "readback": ("timeline_verified", "not_started"),
        "submit": ("rendering", "running"),
        "track": ("rendering", "running"),
        "inspect": ("ready_for_candidate_registration", "unknown"),
        "full": ("ready_for_candidate_registration", "unknown"),
    }[phase]
    applied_stage = (
        phase == "stage"
        and (build_dir / "openchatcut/current_session.json").exists()
        and load(build_dir / "openchatcut/current_session.json").get("status") == "applied"
    )
    if applied_stage:
        stage = "editing"
    state.update(
        schema_version="1.0.0",
        entity="job_status",
        task_id=plan["task_id"],
        script_revision=plan["script_revision"],
        build_id=plan["build_id"],
        stage=stage,
        render_status=render_status,
        qc_status="not_checked",
        human_review="pending",
        sync_status="pending",
        project_ref=f"openchatcut://project/{project['project_id']}",
        candidate_path=None,
        candidate_sha256=None,
        final_path=None,
        evidence_paths=state.get("evidence_paths", []),
        updated_at=now(),
    )
    atomic_write(state_path, state)
    enqueue(
        workspace,
        "tasks",
        plan["task_id"],
        {
            "stage": stage,
            "build_id": plan["build_id"],
            "render_status": render_status,
            "qc_status": "not_checked",
            "human_review": "pending",
            "sync_status": "pending",
            "project_ref": state["project_ref"],
            "final_path": None,
            "error_summary": {
                "create": "OpenChatCut独立工程已创建或对账；等待导入。",
                "stage": (
                    "完整剪辑提案已应用；等待正式时间线回读。"
                    if applied_stage else "完整剪辑提案已生成；等待在OpenChatCut应用。"
                ),
                "readback": "应用后的OpenChatCut时间线已回读通过；等待导出。",
                "submit": "OpenChatCut渲染任务已提交；等待查询结果。",
                "track": "渲染状态已查询；继续按render receipt对账。",
                "inspect": "OpenChatCut导出已检查；等待登记本地候选MP4。",
                "full": "OpenChatCut导出流程已完成；等待登记本地候选MP4。",
            }[phase],
            "updated_at": now(),
        },
        supersede_group="production_state",
        supersede_groups={"material_index", "edit_plan", "production_state"},
    )
    return state


def preflight(workspace: Path, config: dict, plan: dict, production: bool = True) -> dict:
    validate_plan(plan, workspace, production)
    for m in plan["media"]:
        path = resolve_media(config["source_roots"], m["root_id"], m["relative_path"])
        if file_hash(path) != m["working_sha256"]:
            raise Blocked("FILE_CHANGED", m["media_id"], 6)
    for a in plan["audio_tracks"]:
        path = resolve_media(config["source_roots"], a["root_id"], a["relative_path"])
        if file_hash(path) != a["sha256"]:
            raise Blocked("FILE_CHANGED", a["track_id"], 6)
    return {
        "status": "passed",
        "scope": "local_media_integrity_only",
        "production_authorized": False,
    }


def _openchatcut_command(workspace: Path, task: str, phase: str, build_id: str | None) -> list[str]:
    build_dir, plan = current_build(workspace, task, build_id)
    preflight(workspace, load(workspace / "config/config.local.json"), plan)
    project_path = build_dir / "openchatcut/project.json"
    if not project_path.exists():
        raise Blocked("PROJECT_REQUIRED", "OpenChatCut project receipt is missing", 3)
    project = load(project_path)
    common = [
        "--workspace",
        str(workspace),
        "--task",
        task,
        "--revision",
        f"r{plan['script_revision']}",
        "--build",
        plan["build_id"],
        "--project-id",
        project["project_id"],
    ]
    if phase == "stage":
        script = workspace / "scripts/openchatcut_stage_build.py"
        return [sys.executable, str(script), *common, "--hold-for-review"]
    script = workspace / "scripts/openchatcut_finalize_build.py"
    return [sys.executable, str(script), phase, *common]


def run_task(
    workspace: Path,
    config: dict,
    task: str,
    phase: str = "stage",
    build_id: str | None = None,
    execute: bool = True,
) -> dict:
    if Path(task).name != task or task in {".", ".."}:
        raise Blocked("TASK_ID", "Invalid task id", 2)
    with writer_lock(workspace):
        build_dir, plan = current_build(workspace, task, build_id)
        preflight(workspace, config, plan)
        if phase == "create":
            project = ensure_project(workspace, build_dir, plan)
            state_path = build_dir / "state.json"
            if state_path.exists():
                existing = load(state_path)
                if existing.get("stage") not in {None, "ready_for_openchatcut_import"}:
                    return {
                        "status": existing["stage"],
                        "task_id": task,
                        "build_id": plan["build_id"],
                        "project_id": project["project_id"],
                        "editor_url": project["editor_url"],
                        "created_now": False,
                        "idempotent": True,
                    }
            _persist_engine_phase(workspace, build_dir, plan, phase)
            return {
                "status": "ready_for_openchatcut_import",
                "task_id": task,
                "build_id": plan["build_id"],
                "project_id": project["project_id"],
                "editor_url": project["editor_url"],
                "created_now": project["created_now"],
            }
        if phase == "stage":
            current_session = build_dir / "openchatcut/current_session.json"
            if (build_dir / "candidate.mp4").exists() or (
                build_dir / "openchatcut/finalize/final_readback_validation.json"
            ).exists():
                raise Blocked(
                    "STAGE_ALREADY_PASSED",
                    "This build already has readback or a candidate; no second proposal was sent",
                    6,
                )
            if current_session.exists() and load(current_session).get("status") == "awaiting_review":
                return {
                    "status": "waiting_openchatcut_apply",
                    "task_id": task,
                    "build_id": plan["build_id"],
                    "phase": phase,
                    "idempotent": True,
                    "receipt_root": str(build_dir / "openchatcut"),
                }
        if phase in {"submit", "full"} and (
            build_dir / "openchatcut/finalize/render_job.json"
        ).exists():
            raise Blocked(
                "RENDER_RECONCILE_REQUIRED",
                "A render receipt already exists; use track/inspect instead of resubmitting",
                5,
            )
        command = _openchatcut_command(workspace, task, phase, plan["build_id"])
        if not execute:
            return {
                "status": "ready",
                "task_id": task,
                "build_id": plan["build_id"],
                "phase": phase,
                "argv": command,
            }
        _require_connected_editor(
            build_dir,
            load(build_dir / "openchatcut/project.json")["project_id"],
        )
        completed = subprocess.run(command, cwd=workspace)
        if completed.returncode:
            raise Blocked(
                "ENGINE_PHASE_FAILED",
                f"OpenChatCut phase {phase} exited {completed.returncode}; inspect build receipts",
                4,
            )
        _persist_engine_phase(workspace, build_dir, plan, phase)
        stage = {
            "stage": "waiting_openchatcut_apply",
            "readback": "timeline_verified",
            "submit": "rendering",
            "track": "rendering",
            "inspect": "ready_for_candidate_registration",
            "full": "ready_for_candidate_registration",
        }[phase]
        if phase == "stage" and load(
            build_dir / "openchatcut/current_session.json"
        ).get("status") == "applied":
            stage = "ready_for_readback"
        return {
            "status": stage,
            "task_id": task,
            "build_id": plan["build_id"],
            "phase": phase,
            "receipt_root": str(build_dir / "openchatcut"),
        }


def finalize(workspace: Path, job: Path, expected_build: str, freshness: dict) -> Path:
    state = load(job / "state.json")
    review = load(job / "human_review.json")
    qc = load(job / "qc.json")
    plan = load(job / "edit_plan.json")
    if plan["is_fixture"]:
        raise Blocked("FIXTURE_BLOCKED", "Test output cannot be delivered as production")
    candidate = job / "candidate.mp4"
    if state["render_status"] != "succeeded" or qc["status"] != "passed" or not candidate.exists():
        raise Blocked("QC_REQUIRED", "No technically successful candidate")
    h = file_hash(candidate)
    if (
        qc["output_sha256"] != h
        or review.get("output_sha256") != h
        or review.get("build_id") != expected_build
        or plan["build_id"] != expected_build
        or review.get("action") != "approve"
        or not review.get("actor")
    ):
        raise Blocked("APPROVAL_STALE", "Human approval does not match this build and file", 6)
    fields = freshness.get("fields", {})
    snapshot_at = freshness.get("snapshot_at")
    if (
        review.get("channel") != "feishu"
        or review.get("snapshot_at") != snapshot_at
        or fields.get("build_id") != expected_build
        or fields.get("final_review_build_id") != expected_build
        or scalar(fields.get("final_review_action")) != "approve"
        or fields.get("final_review_actor") != review.get("actor")
        or fields.get("script_revision") != plan.get("script_revision")
        or fields.get("script_sha256") != plan.get("script_sha256")
    ):
        raise Blocked("FACT_EXPIRED", "Latest Feishu snapshot does not match the approval", 6)
    final = job / "final.mp4"
    if (
        final.is_file()
        and file_hash(final) == h
        and state.get("stage") == "delivered"
        and state.get("final_path") == str(final)
    ):
        return final
    if final.exists():
        if file_hash(final) != h:
            raise Blocked("FINAL_CONFLICT", f"Preserving different final: {final}", 6)
    else:
        fd, temp_name = tempfile.mkstemp(prefix=".final-", suffix=".mp4", dir=job)
        os.close(fd)
        temp = Path(temp_name)
        try:
            shutil.copy2(candidate, temp)
            if file_hash(temp) != h:
                raise Blocked("COPY_VERIFY_FAILED", str(candidate), 4)
            os.replace(temp, final)
            dir_fd = os.open(job, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            temp.unlink(missing_ok=True)
    task_id = plan["task_id"]
    clips = [shot["clip_id"] for shot in plan["shots"]]
    commit_usage(
        workspace,
        task_id,
        plan["script_revision"],
        expected_build,
        clips,
        candidate,
        qc,
    )
    usage = counts(workspace)
    for clip_id in sorted(set(clips)):
        enqueue(workspace, "clips", clip_id, {"used_count": usage[clip_id]})
    state.update(
        stage="delivered",
        human_review="approved",
        sync_status="pending",
        final_path=str(final),
        final_sha256=h,
        updated_at=now(),
    )
    atomic_write(job / "state.json", state)
    qc["human_review"] = "approved"
    atomic_write(job / "qc.json", qc)
    enqueue(
        workspace,
        "tasks",
        task_id,
        {
            "stage": "delivered",
            "build_id": expected_build,
            "render_status": "succeeded",
            "qc_status": "passed",
            "human_review": "approved",
            "sync_status": "pending",
            "project_ref": state.get("project_ref"),
            "final_path": str(final),
            "error_summary": None,
            "updated_at": now(),
        },
        supersede_group="production_state",
        supersede_groups={"material_index", "edit_plan", "production_state"},
    )
    return final
