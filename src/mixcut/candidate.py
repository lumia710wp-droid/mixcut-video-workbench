from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .core import Blocked, atomic_write, file_hash, load, now
from .feishu import enqueue
from .qc import QC_PROFILE, technical_qc
from .workflow import current_build


def _copy_once(source: Path, target: Path) -> None:
    if target.exists():
        if file_hash(source) != file_hash(target):
            raise Blocked("CANDIDATE_CONFLICT", f"Preserving existing candidate: {target}", 6)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".candidate-", suffix=".mp4", dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(source, temp)
        if file_hash(temp) != file_hash(source):
            raise Blocked("COPY_VERIFY_FAILED", str(source), 4)
        os.replace(temp, target)
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        temp.unlink(missing_ok=True)


def register_candidate(
    workspace: Path,
    config: dict,
    task_id: str,
    source: Path,
    build_id: str | None = None,
    readback_path: Path | None = None,
) -> dict:
    """Copy one exported MP4 into its immutable build and run technical QC."""
    source = source.expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".mp4":
        raise Blocked("CANDIDATE_INPUT", "Candidate must be an existing MP4", 2)
    build_dir, plan = current_build(workspace, task_id, build_id)
    readback_path = readback_path or (
        build_dir / "openchatcut/finalize/final_readback_validation.json"
    )
    if not readback_path.is_file():
        raise Blocked("READBACK_REQUIRED", "OpenChatCut timeline readback is missing", 3)
    readback = load(readback_path)
    if readback.get("status") != "passed" or readback.get("build_id") != plan["build_id"]:
        raise Blocked("READBACK_FAILED", "Timeline readback does not match the build", 6)
    candidate = build_dir / "candidate.mp4"
    qc_path = build_dir / "qc.json"
    state_path = build_dir / "state.json"
    source_hash = file_hash(source)
    if candidate.is_file() and qc_path.exists() and state_path.exists():
        qc = load(qc_path)
        state = load(state_path)
        if (
            file_hash(candidate) == source_hash
            and qc.get("status") == "passed"
            and qc.get("qc_profile") == QC_PROFILE
            and qc.get("output_sha256") == source_hash
            and state.get("candidate_sha256") == source_hash
            and state.get("build_id") == plan["build_id"]
        ):
            return {
                "status": "already_registered",
                "task_id": task_id,
                "build_id": plan["build_id"],
                "candidate_path": str(candidate),
                "output_sha256": source_hash,
                "qc_status": "passed",
                "source_preserved": str(source),
            }
    _copy_once(source, candidate)
    qc = technical_qc(candidate, plan, config, readback)
    if qc["status"] != "passed":
        raise Blocked("QC_FAILED", ",".join(qc.get("errors", [])), 4)
    review = {
        "task_id": task_id,
        "build_id": plan["build_id"],
        "output_sha256": qc["output_sha256"],
        "status": "pending",
        "action": "none",
        "actor": None,
        "note": "Awaiting review of this exact candidate file.",
    }
    review_path = build_dir / "human_review.json"
    if review_path.exists():
        old = load(review_path)
        if old.get("output_sha256") == qc["output_sha256"] and old.get("action") in {
            "approve",
            "reject",
        }:
            review = old
    atomic_write(review_path, review)
    project_path = build_dir / "openchatcut/project.json"
    project = load(project_path) if project_path.exists() else {}
    prior_state = load(state_path) if state_path.exists() else {}
    evidence_paths = list(
        dict.fromkeys(
            [
                *prior_state.get("evidence_paths", []),
                str(readback_path),
                str(build_dir / "qc.json"),
            ]
        )
    )
    state = {
        "schema_version": "1.0.0",
        "entity": "job_status",
        "task_id": task_id,
        "script_revision": plan["script_revision"],
        "build_id": plan["build_id"],
        "stage": "waiting_final_review",
        "render_status": "succeeded",
        "qc_status": "passed",
        "human_review": review.get("status", "pending"),
        "sync_status": "pending",
        "project_ref": (
            f"openchatcut://project/{project['project_id']}" if project.get("project_id") else None
        )
        or prior_state.get("project_ref"),
        "candidate_path": str(candidate),
        "candidate_sha256": qc["output_sha256"],
        "final_path": None,
        "evidence_paths": evidence_paths,
        "updated_at": now(),
    }
    atomic_write(state_path, state)
    silence_count = sum(
        warning.get("code") == "LONG_AUDIO_SILENCE" for warning in qc.get("warnings", [])
    )
    black_count = sum(
        warning.get("code") == "LONG_BLACK_FRAME" for warning in qc.get("warnings", [])
    )
    warning_text = ""
    if silence_count or black_count:
        warning_text = (
            f" 自动告警：长静音{silence_count}处、长黑帧{black_count}处，需人工听审/观看。"
        )
    enqueue(
        workspace,
        "tasks",
        task_id,
        {
            "stage": "waiting_final_review",
            "build_id": plan["build_id"],
            "render_status": "succeeded",
            "qc_status": "passed",
            "human_review": review.get("status", "pending"),
            "sync_status": "pending",
            "project_ref": state["project_ref"],
            "final_path": None,
            "error_summary": (
                "候选MP4已登记并通过分辨率、帧率、帧数、音视频轨、完整解码和"
                "OpenChatCut时间线回读检查；等待人工审核当前build。" + warning_text
            ),
            "updated_at": now(),
        },
        supersede_group="production_state",
        supersede_groups={"material_index", "edit_plan", "production_state"},
    )
    return {
        "status": "waiting_final_review",
        "task_id": task_id,
        "build_id": plan["build_id"],
        "candidate_path": str(candidate),
        "output_sha256": qc["output_sha256"],
        "qc_status": qc["status"],
        "source_preserved": str(source),
    }
