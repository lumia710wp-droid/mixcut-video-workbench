"""Choose the local execution backend without switching an existing run."""

from pathlib import Path
from .core import Blocked, file_hash, load, safe_id
from .director_stage import folder, latest


def choose_backend(root: Path, task: str, requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    current = latest(root, safe_id(task))
    if current is None:
        return "cli" if (root / "tools/occ").is_file() else "mcp"
    revision = current["revision"]
    directory = folder(root, task)
    if (directory / "cli_runs" / f"r{revision:04d}" / "state.json").exists():
        return "cli"
    if (directory / "runs" / f"r{revision:04d}" / "state.json").exists():
        return "mcp"
    return "cli" if (root / "tools/occ").is_file() else "mcp"


def cli_status(root: Path, task: str) -> dict:
    current = latest(root, safe_id(task))
    if current is None:
        return {"task_id": task, "backend": "cli", "status": "not_started"}
    revision = current["revision"]
    path = folder(root, task) / "cli_runs" / f"r{revision:04d}" / "state.json"
    if not path.exists():
        return {"task_id": task, "backend": "cli", "status": "not_started"}
    state = load(path)
    result = {
        k: state[k]
        for k in ("task_id", "status", "current_step", "project_id", "candidate")
        if k in state
    }
    result["backend"] = "cli"
    sync_path = root / "jobs/cli_candidate_sync" / task / f"r{revision:04d}" / "sync_state.json"
    if sync_path.exists():
        result["candidate_sync"] = load(sync_path)
    if state.get("status") == "blocked":
        result.update({k: state[k] for k in ("blocked_code", "message") if k in state})
    if "candidate" in state:
        candidate = state["candidate"]
        p = Path(candidate["path"]).resolve()
        qc_path = p.parent / "qc.json"
        if (
            not p.is_relative_to(root.resolve())
            or not p.is_file()
            or file_hash(p) != candidate["sha256"]
        ):
            raise Blocked("CLI_OUTPUT_CHANGED", "候选成片缺失或摘要变化", 6)
        qc = load(qc_path) if qc_path.is_file() else {}
        if (
            qc.get("status") != "passed"
            or qc.get("warnings")
            or qc.get("output_sha256") != candidate["sha256"]
        ):
            raise Blocked("CLI_QC_BINDING", "成片技术检查未绑定当前文件", 6)
        review_path = p.parent / "human_review_chat.json"
        if review_path.exists():
            review = load(review_path)
            if (
                review.get("status") == "approved"
                and review.get("candidate_sha256") == candidate["sha256"]
            ):
                result["human_review"] = "approved_by_user_chat"
        archive_path = p.parent / "final_receipt.json"
        archive_sync = (
            root
            / "jobs/cli_candidate_sync"
            / task
            / f"r{revision:04d}"
            / "archive_sync_receipt.json"
        )
        if archive_path.exists():
            archive = load(archive_path)
            final = Path(archive.get("path", "")).resolve()
            if (
                result.get("human_review") != "approved_by_user_chat"
                or not final.is_relative_to(root.resolve())
                or not final.is_file()
                or file_hash(final) != candidate["sha256"]
                or archive.get("candidate_sha256") != candidate["sha256"]
            ):
                raise Blocked("CLI_ARCHIVE_CHANGED", "归档文件或审核绑定变化", 6)
            result["final"] = {"path": str(final), "sha256": candidate["sha256"]}
            result["status"] = "archive_sync_pending"
            if archive_sync.exists():
                synced = load(archive_sync)
                if (
                    synced.get("status") == "verified"
                    and synced.get("candidate_sha256") == candidate["sha256"]
                ):
                    result["status"] = "delivered"
                    result["current_step"] = "delivered"
    return result
