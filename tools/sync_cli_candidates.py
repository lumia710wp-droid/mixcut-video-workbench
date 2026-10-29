"""Upload verified CLI candidates to existing Feishu attachment fields.

Run in the configured workspace after staging hash-verified files. A durable
intent prevents blindly replaying an upload with an unknown outcome.
"""

from pathlib import Path
import sys
import time
import argparse
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mixcut.core import Blocked, atomic_write, file_hash, load, now
from mixcut.director_runner import check_live
from mixcut.feishu import unwrap
from mixcut.workbench import Workbench


def sync_progress(bench, task, item):
    row = bench.task(task)
    if row["fields"].get("director_run_status") in {"approved_by_user_chat", "delivered_cli"}:
        return
    preview = json.dumps(
        {
            "task_id": task,
            "status": "awaiting_content_review",
            "candidate_sha256": item["sha256"],
            "note": "CLI成片已通过技术检查并上传，等待验收。",
        },
        ensure_ascii=False,
    )
    patch = {"director_run_status": "awaiting_content_review", "director_run_preview": preview}
    if any(row["fields"].get(k) != value for k, value in patch.items()):
        bench.patch(row["record_id"], patch)
    bench.verify_patch(task, patch)


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    manifest_path = (root / args.manifest).resolve()
    if not manifest_path.is_relative_to((root / "jobs").resolve()):
        raise Blocked("CANDIDATE_MANIFEST", "清单须位于任务目录", 6)
    job = manifest_path.parent
    manifest = load(manifest_path)
    bench = Workbench(root)
    table = bench.mapping["tables"]["tasks"]
    field_id = table["field_ids"]["candidate_preview"]
    field_list = unwrap(bench.call(["+field-list", *bench.common(), "--limit", "200"]))
    field = next((x for x in field_list["fields"] if x["id"] == field_id), None)
    if not field or field.get("type") != "attachment":
        raise Blocked("CANDIDATE_FIELD", "候选成片字段不是附件", 6)
    for item in manifest["items"]:
        task = item["task_id"]
        from mixcut.core import safe_id

        safe_id(task)
        if Path(item["remote_name"]).name != item["remote_name"]:
            raise Blocked("CANDIDATE_PATH", "候选文件名称无效", 6)
        check_live(root, task, bench)
        row = bench.task(task)
        staged = job / "media_uploads" / item["remote_name"]
        if (
            not staged.is_file()
            or staged.stat().st_size != item["size"]
            or file_hash(staged) != item["sha256"]
        ):
            raise Blocked("CANDIDATE_CHANGED", "待上传文件摘要不符", 6)
        receipt = job / "media_uploads" / (task + ".receipt.json")
        existing = row["fields"].get("candidate_preview")
        if receipt.exists():
            prior = load(receipt)
            if (
                prior.get("status") == "verified"
                and prior.get("sha256") == item["sha256"]
                and isinstance(existing, list)
                and len(existing) == 1
                and existing[0].get("name") == item["remote_name"]
                and existing[0].get("size") == item["size"]
                and existing[0].get("file_token")
                == prior.get("readback", [{}])[0].get("file_token")
            ):
                sync_progress(bench, task, item)
                print(task, "already verified", flush=True)
                continue
            raise Blocked("CANDIDATE_UPLOAD_UNKNOWN", "已有上传回执，先对账，不能重复上传", 6)
        if existing:
            raise Blocked("CANDIDATE_CONFLICT", "目标附件字段已有内容，不覆盖", 6)
        intent = {
            "status": "started",
            "task_id": task,
            "record_id": row["record_id"],
            "sha256": item["sha256"],
            "filename": item["remote_name"],
            "at": now(),
        }
        atomic_write(receipt, intent)
        bench.call(
            [
                "+record-upload-attachment",
                "--base-token",
                bench.mapping["base_token"],
                "--table-id",
                table["table_id"],
                "--record-id",
                row["record_id"],
                "--field-id",
                field_id,
                "--file",
                str(staged.relative_to(root)),
            ]
        )
        visible = None
        for attempt in range(5):
            visible = bench.task(task)["fields"].get("candidate_preview")
            if visible:
                break
            if attempt < 4:
                time.sleep(1)
        if (
            not isinstance(visible, list)
            or len(visible) != 1
            or visible[0].get("name") != item["remote_name"]
            or visible[0].get("size") != item["size"]
        ):
            raise Blocked("CANDIDATE_READBACK", "上传已发出但附件未读到，先对账", 6)
        atomic_write(
            receipt, {**intent, "status": "verified", "readback": visible, "verified_at": now()}
        )
        sync_progress(bench, task, item)
        print(task, "uploaded and read back", flush=True)


if __name__ == "__main__":
    main()
