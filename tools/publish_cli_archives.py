"""Stage and publish reviewed CLI archives without altering native approvals."""

import argparse
from contextlib import nullcontext
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mixcut.core import Blocked, atomic_write, file_hash, load, now, safe_id, writer_lock
from mixcut.director_runner import check_live
from mixcut.workbench import Workbench


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--phase", choices=["prepare", "sync"], required=True)
    parser.add_argument("--caller-holds-lock", action="store_true")
    args = parser.parse_args()
    manifest_path = (root / args.manifest).resolve()
    if not manifest_path.is_relative_to((root / "jobs").resolve()):
        raise Blocked("CLI_ARCHIVE_MANIFEST", "归档清单路径无效", 6)
    bench = Workbench(root)
    with nullcontext() if args.caller_holds_lock else writer_lock(root, wait_seconds=20):
        for entry in load(manifest_path)["items"]:
            task = safe_id(entry["task_id"])
            current, _ = check_live(root, task, bench)
            if current["packet_sha256"] != entry["director_sha256"]:
                raise Blocked("CLI_ARCHIVE_STALE", "当前编导审核版本变化", 6)
            upload_path = (root / entry["upload_receipt"]).resolve()
            if not upload_path.is_relative_to((root / "jobs").resolve()):
                raise Blocked("CLI_ARCHIVE_PATH", "上传回执路径无效", 6)
            upload = load(upload_path)
            if (
                upload.get("status") != "verified"
                or upload.get("sha256") != entry["candidate_sha256"]
            ):
                raise Blocked("CLI_ARCHIVE_UPLOAD", "附件上传与成片摘要不符", 6)
            actual = bench.task(task)
            attached = actual["fields"].get("candidate_preview") or []
            token = upload["readback"][0]["file_token"]
            if not any(x.get("file_token") == token for x in attached if isinstance(x, dict)):
                raise Blocked("CLI_ARCHIVE_ATTACHMENT", "飞书当前附件已变化", 6)
            source = upload_path.parent / upload["filename"]
            if not source.is_file() or file_hash(source) != entry["candidate_sha256"]:
                raise Blocked("CLI_ARCHIVE_SOURCE", "远端成片文件变化", 6)
            if entry.get("status") != "archived" or entry.get("review_source") != "user_chat":
                raise Blocked("CLI_ARCHIVE_REVIEW", "没有人工确认的归档回执", 6)
            directory = root / "jobs/cli_delivery" / task / f"r{int(entry['revision']):04d}"
            target, receipt = directory / "final.mp4", directory / "receipt.json"
            directory.mkdir(parents=True, exist_ok=True)
            if receipt.exists():
                saved = load(receipt)
                if (
                    saved["candidate_sha256"] != entry["candidate_sha256"]
                    or not target.is_file()
                    or file_hash(target) != entry["candidate_sha256"]
                    or saved["attachment_token"] != token
                ):
                    raise Blocked("CLI_ARCHIVE_CONFLICT", "已有不同归档，先对账", 6)
            else:
                if target.exists():
                    raise Blocked("CLI_ARCHIVE_UNKNOWN", "已有无回执成片，先对账", 6)
                fd, temp = tempfile.mkstemp(prefix=".final-", suffix=".mp4", dir=directory)
                os.close(fd)
                try:
                    shutil.copyfile(source, temp)
                    if file_hash(Path(temp)) != entry["candidate_sha256"]:
                        raise Blocked("CLI_ARCHIVE_COPY", "归档复制摘要不符", 6)
                    os.replace(temp, target)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
                saved = {
                    **entry,
                    "path": str(target),
                    "attachment_token": token,
                    "qc_status": "passed",
                    "remote_archived_at": now(),
                }
                atomic_write(receipt, saved)
            if args.phase == "sync":
                import json

                preview = json.dumps(
                    {
                        "task_id": task,
                        "status": "delivered_cli",
                        "candidate_sha256": entry["candidate_sha256"],
                        "director_sha256": entry["director_sha256"],
                        "review_source": "user_chat",
                        "approved_at": entry["reviewed_at"],
                        "note": "聊天验收通过，成片已归档；附件在候选成片字段。",
                    },
                    ensure_ascii=False,
                )
                patch = {"director_run_status": "delivered_cli", "director_run_preview": preview}
                if any(actual["fields"].get(k) != value for k, value in patch.items()):
                    bench.patch(actual["record_id"], patch)
                bench.verify_patch(task, patch)
            print(task, args.phase, "verified", flush=True)


if __name__ == "__main__":
    main()
