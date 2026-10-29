from __future__ import annotations
from pathlib import Path
from .core import Blocked, digest, load, file_hash
from .contracts import validate_tags


def proposal_set_path(workspace: Path, explicit: Path | None = None) -> Path:
    """Resolve the current visual proposal set without baking a dated run into code."""
    if explicit is not None:
        path = explicit.expanduser()
        path = path if path.is_absolute() else workspace / path
    else:
        workflow_path = workspace / "config/workflow.local.json"
        configured = (
            load(workflow_path).get("visual_analysis_proposals")
            if workflow_path.exists()
            else None
        )
        path = (
            Path(configured)
            if isinstance(configured, str) and configured
            else Path("analysis/codex_visual_20261014/proposals.json")
        )
        path = path if path.is_absolute() else workspace / path
    path = path.resolve()
    if not path.is_relative_to(workspace.resolve()) or not path.is_file():
        raise Blocked("MATERIAL_ANALYSIS_MISSING", f"Proposal set is unavailable: {path}", 3)
    return path


def g1_accepted(workspace: Path) -> bool:
    """Use actual file-bound G1 evidence, never a hard-coded pending flag."""
    try:
        job = Path(load(workspace / "reports/g1/preparation.json")["job_dir"]).resolve()
        if not job.is_relative_to((workspace / "jobs").resolve()):
            return False
        state, qc, review, plan = [
            load(job / name)
            for name in ["state.json", "G1_QC_REPORT.json", "human_review.json", "edit_plan.json"]
        ]
        if not (
            state["render_status"] == "succeeded"
            and state["qc_status"] == qc["technical_status"] == "passed"
            and state["human_review"] == "approved"
            and review["action"] == "approve"
            and review.get("actor")
            and review["build_id"] == plan["build_id"] == state["build_id"]
        ):
            return False
        actual_hash = file_hash(job / "candidate.mp4")
        return actual_hash == review["output_sha256"] == qc["technical"]["output_sha256"]
    except (OSError, KeyError, TypeError, ValueError):
        return False


def analysis_key(
    source_sha: str,
    interval: list,
    strategy: dict,
    model: str,
    prompt_version: str,
    taxonomy: dict,
    parameters: dict,
) -> str:
    return digest([source_sha, interval, strategy, model, prompt_version, taxonomy, parameters])


def cloud_gate(config: dict, g1_passed: bool, calls_used: int, frames: int) -> None:
    if not g1_passed:
        raise Blocked("G1_REQUIRED", "Real 24-second engine export and human review remain pending")
    cloud = config["cloud"]
    if not cloud["cloud_analysis_enabled"] or cloud["max_remote_calls"] <= calls_used:
        raise Blocked("BUDGET_BLOCKED", "Cloud permission or remaining calls not configured")
    if frames > cloud["max_frames_per_source"]:
        raise Blocked("BUDGET_BLOCKED", "Frame limit exceeded")
    if not config["vision"].get("model") or not config["vision"].get("endpoint"):
        raise Blocked("CONFIG_MISSING", "Select provider, endpoint and model before any request")


def validate_proposal(proposal: dict, taxonomy: dict, duration: float) -> None:
    if set(proposal) != {
        "description",
        "tags",
        "start_sec",
        "end_sec",
        "evidence_times",
        "product_uncertain",
    }:
        raise Blocked("MODEL_SCHEMA_INVALID", "Unexpected analysis fields", 2)
    digest(proposal)
    validate_tags(proposal["tags"], taxonomy)
    if not isinstance(proposal["description"], str) or not isinstance(
        proposal["product_uncertain"], bool
    ):
        raise Blocked("MODEL_SCHEMA_INVALID", "Invalid analysis types", 2)
    if not 0 <= proposal["start_sec"] < proposal["end_sec"] <= duration:
        raise Blocked("MODEL_SCHEMA_INVALID", "Out of source bounds", 2)
    if (
        not isinstance(proposal["evidence_times"], list)
        or len(proposal["evidence_times"]) > 12
        or any(not 0 <= t < duration for t in proposal["evidence_times"])
    ):
        raise Blocked("MODEL_SCHEMA_INVALID", "Invalid evidence times", 2)
