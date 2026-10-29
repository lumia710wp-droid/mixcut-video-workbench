"""Archive a CLI candidate only after a hash-bound human review."""

import os
import shutil
import tempfile
import subprocess
from pathlib import Path

from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id
from .director_backend import cli_status
from .director_stage import latest


def record_chat_approval(root: Path, task: str, candidate_sha256: str, message: str) -> dict:
    """Record an explicit user's chat approval for one exact candidate."""
    task = safe_id(task)
    if (root / "jobs/delivery_packages" / task / "required.json").is_file():
        raise Blocked("DELIVERY_REVIEW_REQUIRED", "此任务需在飞书整体验收视频、封面和文案", 3)
    status = cli_status(root, task)
    candidate = status.get("candidate")
    if not candidate or candidate.get("sha256") != candidate_sha256:
        raise Blocked("CLI_APPROVAL_CHANGED", "待审核视频摘要不符", 6)
    if not isinstance(message, str) or not message.strip() or len(message) > 1000:
        raise Blocked("CLI_APPROVAL_EVIDENCE", "需要用户明确的聊天验收原话", 3)
    path = Path(candidate["path"]).parent / "human_review_chat.json"
    if path.exists():
        prior = load(path)
        if prior.get("status") != "approved" or prior.get("candidate_sha256") != candidate_sha256:
            raise Blocked("CLI_APPROVAL_CONFLICT", "已有不同验收记录，请先对账", 6)
        return prior
    receipt = {
        "task_id": task,
        "status": "approved",
        "source": "user_chat",
        "candidate_path": candidate["path"],
        "candidate_sha256": candidate_sha256,
        "approved_at": now(),
        "scope": "final_video",
        "technical_qc": "passed",
        "review_message": message.strip(),
        "note": "聊天验收，非飞书原生审批",
    }
    atomic_write(path, receipt)
    return receipt


def archive_cli_candidate(root: Path, task: str) -> dict:
    task = safe_id(task)
    status = cli_status(root, task)
    candidate = status.get("candidate")
    if not candidate or status.get("human_review") != "approved_by_user_chat":
        raise Blocked("CLI_APPROVAL_REQUIRED", "当前视频尚无绑定文件摘要的人工验收", 3)
    record = latest(root, task)
    source = Path(candidate["path"]).resolve()
    review = load(source.parent / "human_review_chat.json")
    if review.get("candidate_sha256") != candidate["sha256"]:
        raise Blocked("CLI_APPROVAL_CHANGED", "验收文件摘要与成片不符", 6)
    target = source.parent / "final.mp4"
    receipt_path = source.parent / "final_receipt.json"
    if receipt_path.exists():
        receipt = load(receipt_path)
        if (
            not target.is_file()
            or file_hash(target) != candidate["sha256"]
            or receipt.get("candidate_sha256") != candidate["sha256"]
        ):
            raise Blocked("CLI_ARCHIVE_CHANGED", "归档文件或回执变化，先对账", 6)
        commit_source_usage(root, task, record, candidate)
        return receipt
    if target.exists():
        raise Blocked("CLI_ARCHIVE_CONFLICT", "已有无回执归档，不覆盖", 6)
    fd, tmp_name = tempfile.mkstemp(prefix=".final-", suffix=".mp4", dir=source.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, tmp_name)
        if file_hash(Path(tmp_name)) != candidate["sha256"]:
            raise Blocked("CLI_ARCHIVE_COPY", "归档文件摘要不符", 6)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    receipt = {
        "task_id": task,
        "revision": record["revision"],
        "director_sha256": record["packet_sha256"],
        "candidate_sha256": candidate["sha256"],
        "path": str(target),
        "status": "archived",
        "review_source": "user_chat",
        "reviewed_at": review["approved_at"],
        "archived_at": now(),
    }
    atomic_write(receipt_path, receipt)
    commit_source_usage(root, task, record, candidate)
    return receipt


def commit_source_usage(root, task, record, candidate):
    # Director plans use source IDs, not the legacy clip IDs. Keep separate
    # idempotent source-level events instead of corrupting the old clip ledger.
    for source_id in sorted({shot["source_id"] for shot in record["packet"]["shots"]}):
        event_path = (
            root
            / "ledger/director_source_usage"
            / f"{digest([task, record['revision'], source_id])}.json"
        )
        event = {
            "task_id": task,
            "revision": record["revision"],
            "source_id": source_id,
            "candidate_sha256": candidate["sha256"],
            "director_sha256": record["packet_sha256"],
        }
        if event_path.exists() and load(event_path) != event:
            raise Blocked("CLI_USAGE_CONFLICT", "来源使用记录冲突，先对账", 6)
        if not event_path.exists():
            atomic_write(event_path, event)


def sync_cli_archive(root: Path, task: str) -> dict:
    """Publish the reviewed local archive to the configured Feishu Base."""
    from .director_candidate_sync import sync_candidate
    import sys

    task = safe_id(task)
    archived = archive_cli_candidate(root, task)
    sync_candidate(root, task)
    rev = archived["revision"]
    job = root / "jobs/cli_candidate_sync" / task / f"r{rev:04d}"
    upload_receipt = job / "media_uploads" / (task + ".receipt.json")
    manifest = {"items": [{**archived, "upload_receipt": str(upload_receipt.relative_to(root))}]}
    manifest_path = job / "archive_manifest.json"
    atomic_write(manifest_path, manifest)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(root / "tools/publish_cli_archives.py"),
                "--manifest",
                str(manifest_path.relative_to(root)),
                "--phase",
                "sync",
                "--caller-holds-lock",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Blocked("CLI_ARCHIVE_SYNC_PENDING", "归档同步结果未知，先对账", 6) from error
    if result.returncode:
        raise Blocked("CLI_ARCHIVE_SYNC_PENDING", "归档回填未确认，请先对账", 6)
    receipt = {
        "task_id": task,
        "status": "verified",
        "candidate_sha256": archived["candidate_sha256"],
    }
    atomic_write(job / "archive_sync_receipt.json", receipt)
    return receipt
