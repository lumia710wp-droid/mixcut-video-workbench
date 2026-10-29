"""Versioned post-edit review package; immutable files and explicit approval."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id

COVERS = {
    "douyin": (1080, 1440),
    "xiaohongshu": (1080, 1440),
    "channels_portrait": (1080, 1440),
    "channels_landscape": (1440, 1080),
}
PLATFORMS = ("douyin", "xiaohongshu", "channels")
FIELDS = {
    "delivery_manifest": ("成片验收包", "text"),
    "delivery_review_status": ("整包验收状态", "text"),
    "delivery_review_hash": ("整包验收摘要", "text"),
    "delivery_review_feedback": ("整包验收意见", "text"),
    "delivery_copy": ("平台发布文案", "text"),
    "delivery_cover_douyin": ("抖音封面", "attachment"),
    "delivery_cover_xiaohongshu": ("小红书封面", "attachment"),
    "delivery_cover_channels_portrait": ("视频号竖版封面", "attachment"),
    "delivery_cover_channels_landscape": ("视频号横版封面", "attachment"),
}


def image_size(path: Path) -> tuple[int, int]:
    """Read PNG/JPEG dimensions without trusting the filename or loading pixels."""
    with path.open("rb") as f:
        head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
        if head[:2] != b"\xff\xd8":
            raise Blocked("DELIVERY_IMAGE", "封面必须为 PNG 或 JPEG", 2)
        f.seek(2)
        while True:
            prefix = f.read(1)
            if not prefix:
                break
            if prefix != b"\xff":
                continue
            marker = f.read(1)
            while marker == b"\xff":
                marker = f.read(1)
            if not marker or marker in (b"\xda", b"\xd9"):
                break
            if marker in (b"\x01",) or 0xD0 <= marker[0] <= 0xD7:
                continue
            raw = f.read(2)
            if len(raw) != 2:
                break
            length = int.from_bytes(raw, "big")
            if length < 2:
                break
            if marker[0] in (
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            ):
                data = f.read(5)
                if len(data) == 5:
                    h, w = struct.unpack(">HH", data[1:])
                    return w, h
                break
            f.seek(length - 2, 1)
    raise Blocked("DELIVERY_IMAGE", "无法读取封面图片尺寸", 2)


def local_file(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise Blocked("DELIVERY_PATH", "缺少文件路径", 2)
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise Blocked("DELIVERY_PATH", "候选文件须位于工作区内且存在", 2)
    return path


def prepare(
    root: Path, task: str, candidate: dict, director_sha256: str, script_sha256: str, spec: dict
) -> dict:
    """Create a pending package from existing candidate and four local images.

    Called only after the surrounding workflow has checked the actual current
    director and text versions and candidate QC. This never grants approval.
    """
    task = safe_id(task)
    if not isinstance(spec, dict) or set(spec.get("covers", {})) != set(COVERS):
        raise Blocked("DELIVERY_COVERS", "需提供抖音、小红书及视频号横竖四张封面", 2)
    if set(spec.get("copy", {})) != set(PLATFORMS):
        raise Blocked("DELIVERY_COPY", "需提供三个平台的发布文案", 2)
    video = local_file(root, candidate.get("path"))
    if file_hash(video) != candidate.get("sha256"):
        raise Blocked("DELIVERY_VIDEO", "视频内容与候选摘要不符", 6)
    if not director_sha256 or not script_sha256:
        raise Blocked("DELIVERY_BINDING", "缺少已审核脚本或编导版本", 6)
    covers = {}
    for name, expected in COVERS.items():
        path = local_file(root, spec["covers"][name])
        if image_size(path) != expected or path.stat().st_size > 5_000_000:
            raise Blocked("DELIVERY_IMAGE_SIZE", f"{name} 尺寸或文件大小不符合要求", 2)
        covers[name] = {
            "path": str(path.relative_to(root)),
            "sha256": file_hash(path),
            "width": expected[0],
            "height": expected[1],
            "size": path.stat().st_size,
        }
    copies = {}
    for platform in PLATFORMS:
        item = spec["copy"][platform]
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("body"), str)
            or not item["body"].strip()
        ):
            raise Blocked("DELIVERY_COPY", f"{platform} 缺少发布正文", 2)
        if (
            not isinstance(item.get("titles"), list)
            or not item["titles"]
            or not all(isinstance(x, str) and x.strip() for x in item["titles"])
        ):
            raise Blocked("DELIVERY_COPY", f"{platform} 缺少短标题", 2)
        if not isinstance(item.get("hashtags", []), list) or not all(
            isinstance(x, str) for x in item.get("hashtags", [])
        ):
            raise Blocked("DELIVERY_COPY", f"{platform} 话题格式不正确", 2)
        if not isinstance(item.get("pinned_comment", ""), str):
            raise Blocked("DELIVERY_COPY", f"{platform} 置顶评论格式不正确", 2)
        copies[platform] = {
            "body": item["body"],
            "titles": item["titles"],
            "hashtags": item.get("hashtags", []),
            "pinned_comment": item.get("pinned_comment", ""),
        }
    content = {
        "task_id": task,
        "video": {"path": str(video.relative_to(root)), "sha256": candidate["sha256"]},
        "director_sha256": director_sha256,
        "script_sha256": script_sha256,
        "covers": covers,
        "copy": copies,
    }
    package_hash = digest(content)
    folder = root / "jobs/delivery_packages" / task
    record_path = folder / f"{package_hash}.json"
    if record_path.exists():
        prior = load(record_path)
        if prior.get("content") != content:
            raise Blocked("DELIVERY_CONFLICT", "已有不同的候选包，先对账", 6)
        record = prior
    else:
        record = {
            "schema_version": 1,
            "package_sha256": package_hash,
            "content": content,
            "created_at": now(),
        }
        atomic_write(record_path, record)
    atomic_write(folder / "current.json", {"package_sha256": package_hash})
    return record


def verify(root: Path, task: str, expected_hash: str | None = None) -> dict:
    task = safe_id(task)
    folder = root / "jobs/delivery_packages" / task
    if not (folder / "current.json").is_file():
        raise Blocked("DELIVERY_MISSING", "尚未准备完整验收包", 3)
    package_hash = load(folder / "current.json")["package_sha256"]
    if expected_hash and expected_hash != package_hash:
        raise Blocked("DELIVERY_STALE", "验收包已变化，请刷新后重新审核", 6)
    record = load(folder / f"{package_hash}.json")
    if (
        digest(record["content"]) != package_hash
        or record.get("package_sha256") != package_hash
        or record["content"]["task_id"] != task
    ):
        raise Blocked("DELIVERY_CHANGED", "验收包摘要不一致", 6)
    for entry in [record["content"]["video"], *record["content"]["covers"].values()]:
        if file_hash(local_file(root, entry["path"])) != entry["sha256"]:
            raise Blocked("DELIVERY_CHANGED", "验收包文件已变化，请重新生成候选", 6)
    return record


def verify_video_attachment(root: Path, task: str, candidate: dict, fields: dict) -> str:
    from .director_stage import latest

    record = latest(root, task)
    if not record:
        raise Blocked("DELIVERY_VIDEO_SYNC", "缺少当前编导版本", 6)
    directory = root / "jobs/cli_candidate_sync" / task / f"r{record['revision']:04d}"
    receipt = directory / "media_uploads" / (task + ".receipt.json")
    if not receipt.is_file():
        raise Blocked("DELIVERY_VIDEO_SYNC", "缺少视频同步回执", 6)
    saved = load(receipt)
    attached = fields.get("candidate_preview") or []
    readback = saved.get("readback") or []
    if (
        saved.get("status") != "verified"
        or saved.get("sha256") != candidate.get("sha256")
        or len(readback) != 1
        or len(attached) != 1
        or not readback[0].get("file_token")
        or attached[0].get("file_token") != readback[0].get("file_token")
    ):
        raise Blocked("DELIVERY_VIDEO_SYNC", "飞书视频附件与当前视频回执不一致", 6)
    return readback[0]["file_token"]


def publish(root: Path, task: str, bench, expected_hash: str | None = None) -> dict:
    """Upload each new cover with a durable intent and read the whole packet back."""
    from .director_runner import check_live, pull_authority

    task = safe_id(task)
    pull_authority(root, task)
    director, script = check_live(root, task, bench)
    package = verify(root, task, expected_hash)
    content, sha = package["content"], package["package_sha256"]
    candidate = content["video"]
    if (
        not candidate
        or candidate.get("sha256") != content["video"]["sha256"]
        or director["packet_sha256"] != content["director_sha256"]
        or script["content_sha256"] != content["script_sha256"]
    ):
        raise Blocked("DELIVERY_STALE", "成片、文字或编导版本已改变", 6)
    row = bench.task(task)
    video_token = verify_video_attachment(root, task, candidate, row["fields"])
    table = bench.mapping["tables"]["tasks"]
    if any(k not in table["field_ids"] for k in FIELDS):
        raise Blocked("DELIVERY_SCHEMA", "请先执行 director schema 创建并绑定验收字段", 6)
    directory = root / "jobs/delivery_packages" / task / sha
    directory.mkdir(parents=True, exist_ok=True)
    # A replacement is allowed only for the exact packet and attachments that
    # this program previously published and read back.
    previous = None
    existing_manifest = row["fields"].get("delivery_manifest")
    if existing_manifest:
        try:
            old_manifest = json.loads(existing_manifest)
            old_hash = old_manifest["package_sha256"]
        except (ValueError, KeyError, TypeError) as error:
            raise Blocked("DELIVERY_PUBLISH_CONFLICT", "旧验收包无法核对，不自动覆盖", 6) from error
        if old_hash != sha:
            old_path = directory.parent / (old_hash + ".json")
            old_publish = directory.parent / old_hash / "publish.json"
            if not old_path.is_file() or not old_publish.is_file():
                raise Blocked("DELIVERY_PUBLISH_CONFLICT", "旧验收包回执缺失，不自动覆盖", 6)
            old = load(old_path)
            if (
                digest(old.get("content")) != old_hash
                or load(old_publish).get("status") != "verified"
            ):
                raise Blocked("DELIVERY_PUBLISH_CONFLICT", "旧验收包未验证，不自动覆盖", 6)
            for name in COVERS:
                attached = row["fields"].get("delivery_cover_" + name) or []
                receipt = directory.parent / old_hash / (name + ".json")
                if not receipt.is_file():
                    raise Blocked("DELIVERY_PUBLISH_CONFLICT", "旧封面回执缺失，不自动覆盖", 6)
                saved = load(receipt)
                if (
                    saved.get("status") != "verified"
                    or saved.get("sha256") != old["content"]["covers"][name]["sha256"]
                    or len(attached) != 1
                    or attached[0].get("file_token") != saved.get("file_token")
                    or attached[0].get("file_token") != old_manifest.get("covers", {}).get(name)
                ):
                    raise Blocked("DELIVERY_PUBLISH_CONFLICT", "旧封面已被修改，不自动覆盖", 6)
            previous = old
            transition = directory / "transition.json"
            if transition.exists():
                if load(transition).get("status") != "verified":
                    raise Blocked("DELIVERY_PUBLISH_UNKNOWN", "旧审核失效状态未知，请先对账", 6)
            else:
                atomic_write(
                    transition, {"status": "started", "previous_sha256": old_hash, "at": now()}
                )
                patch = {"delivery_review_status": "updating", "delivery_review_hash": ""}
                bench.patch(row["record_id"], patch)
                bench.verify_patch(task, patch)
                atomic_write(
                    transition, {"status": "verified", "previous_sha256": old_hash, "at": now()}
                )
    # Detect conflicts for the whole packet before the first upload.
    for name, entry in content["covers"].items():
        existing = row["fields"].get("delivery_cover_" + name) or []
        receipt = directory / (name + ".json")
        if existing and not receipt.exists() and previous is None:
            raise Blocked(
                "DELIVERY_COVER_CONFLICT", f"{name} 附件已有内容，请先对账，不自动覆盖", 6
            )
    tokens = {}
    for name, entry in content["covers"].items():
        key = "delivery_cover_" + name
        path = local_file(root, entry["path"])
        receipt = directory / (name + ".json")
        existing = bench.task(task)["fields"].get(key) or []
        if receipt.exists():
            saved = load(receipt)
            if (
                saved.get("status") != "verified"
                or saved.get("sha256") != entry["sha256"]
                or len(existing) != 1
                or existing[0].get("file_token") != saved.get("file_token")
            ):
                raise Blocked("DELIVERY_UPLOAD_UNKNOWN", f"{name} 上传结果或附件变化，请先对账", 6)
            tokens[name] = saved["file_token"]
            continue
        if existing and previous:
            old_receipt = load(directory.parent / previous["package_sha256"] / (name + ".json"))
            if len(existing) != 1 or existing[0].get("file_token") != old_receipt.get("file_token"):
                raise Blocked("DELIVERY_PUBLISH_CONFLICT", f"{name} 已被修改，请先对账", 6)
            old_entry = previous["content"]["covers"][name]
            if old_entry["sha256"] == entry["sha256"]:
                token = existing[0]["file_token"]
                atomic_write(
                    receipt,
                    {
                        "status": "verified",
                        "sha256": entry["sha256"],
                        "file_token": token,
                        "at": now(),
                    },
                )
                tokens[name] = token
                continue
            clearing = directory / (name + ".clear.json")
            if clearing.exists():
                raise Blocked("DELIVERY_UPLOAD_UNKNOWN", f"{name} 旧附件替换结果未知，请先对账", 6)
            atomic_write(
                clearing,
                {"status": "started", "old_file_token": existing[0]["file_token"], "at": now()},
            )
            bench.patch(row["record_id"], {key: []})
            import time

            for attempt in range(4):
                if bench.task(task)["fields"].get(key) in (None, []):
                    break
                if attempt < 3:
                    time.sleep(0.35)
            else:
                raise Blocked("DELIVERY_UPLOAD_UNKNOWN", f"{name} 旧附件清空未读回，请先对账", 6)
            atomic_write(
                clearing,
                {"status": "verified", "old_file_token": existing[0]["file_token"], "at": now()},
            )
            existing = []
        if existing:
            raise Blocked(
                "DELIVERY_COVER_CONFLICT", f"{name} 附件已有内容，请先对账，不自动覆盖", 6
            )
        atomic_write(receipt, {"status": "started", "sha256": entry["sha256"], "at": now()})
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
                table["field_ids"][key],
                "--file",
                str(path.relative_to(root)),
            ]
        )
        import time

        for attempt in range(5):
            existing = bench.task(task)["fields"].get(key) or []
            if existing:
                break
            if attempt < 4:
                time.sleep(1)
        if (
            len(existing) != 1
            or existing[0].get("name") != path.name
            or existing[0].get("size") != entry["size"]
            or not existing[0].get("file_token")
        ):
            raise Blocked("DELIVERY_UPLOAD_UNKNOWN", f"{name} 上传后未正确读回，请先对账", 6)
        tokens[name] = existing[0]["file_token"]
        atomic_write(
            receipt,
            {
                "status": "verified",
                "sha256": entry["sha256"],
                "file_token": tokens[name],
                "at": now(),
            },
        )
    manifest = {
        "package_sha256": sha,
        "video_sha256": content["video"]["sha256"],
        "video_token": video_token,
        "director_sha256": content["director_sha256"],
        "script_sha256": content["script_sha256"],
        "covers": tokens,
    }
    patch = {
        "delivery_manifest": json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        "delivery_copy": json.dumps(content["copy"], ensure_ascii=False, sort_keys=True),
        "delivery_review_status": "pending",
        "delivery_review_hash": "",
        "delivery_review_feedback": "",
    }
    before = bench.task(task)["fields"]
    if previous and (
        before.get("delivery_manifest") != existing_manifest
        or before.get("delivery_review_status") != "updating"
    ):
        raise Blocked("DELIVERY_PUBLISH_CONFLICT", "旧验收包状态变化，请先对账", 6)
    for name, token in tokens.items():
        attached = before.get("delivery_cover_" + name) or []
        if len(attached) != 1 or attached[0].get("file_token") != token:
            raise Blocked("DELIVERY_PUBLISH_CONFLICT", "封面附件发生变化，请先对账", 6)
    if (
        before.get("delivery_manifest")
        and before.get("delivery_manifest") != patch["delivery_manifest"]
        and previous is None
    ):
        raise Blocked("DELIVERY_PUBLISH_CONFLICT", "飞书已有不同验收包，请先对账", 6)
    intent = directory / "publish.json"
    if intent.exists():
        saved = load(intent)
        if saved.get("status") != "verified":
            raise Blocked("DELIVERY_PUBLISH_UNKNOWN", "回填结果未知，请先对账", 6)
        bench.verify_patch(task, patch)
        return saved
    atomic_write(intent, {"status": "started", "package_sha256": sha, "at": now()})
    bench.patch(row["record_id"], patch)
    bench.verify_patch(task, patch)
    result = {"status": "verified", "package_sha256": sha, "at": now()}
    atomic_write(intent, result)
    return result


def review(
    root: Path, task: str, expected: str, action: str, bench, actor: dict, reason: str = ""
) -> dict:
    """Explicit user's decision, recorded only after checking the current full packet."""
    from .director_runner import check_live, pull_authority
    import json

    task = safe_id(task)
    if not isinstance(reason, str) or not isinstance(expected, str) or len(expected) != 64:
        raise Blocked("DELIVERY_REVIEW", "审核参数不正确", 2)
    if action not in ("approve", "reject") or (action == "reject" and not reason.strip()):
        raise Blocked("DELIVERY_REVIEW", "退回时请填写修改原因", 2)
    if not isinstance(actor, dict) or not actor.get("id"):
        raise Blocked("DELIVERY_REVIEW", "无法确认审核人", 4)
    pull_authority(root, task)
    director, script = check_live(root, task, bench)
    package = verify(root, task, expected)
    content = package["content"]
    candidate = content["video"]
    if (
        not candidate
        or candidate.get("sha256") != content["video"]["sha256"]
        or director["packet_sha256"] != content["director_sha256"]
        or script["content_sha256"] != content["script_sha256"]
    ):
        raise Blocked("DELIVERY_STALE", "视频、文字或编导版本已变化", 6)
    directory = root / "jobs/delivery_packages" / task / expected
    published = load(directory / "publish.json") if (directory / "publish.json").exists() else {}
    if published.get("status") != "verified":
        raise Blocked("DELIVERY_NOT_PUBLISHED", "验收包尚未完整回填并读回", 6)
    row = bench.task(task)
    fields = row["fields"]
    manifest = json.loads(fields.get("delivery_manifest") or "{}")
    video_token = verify_video_attachment(root, task, candidate, fields)
    if manifest.get("video_token") != video_token:
        raise Blocked("DELIVERY_STALE", "飞书视频附件已变化", 6)
    if (
        manifest.get("package_sha256") != expected
        or fields.get("delivery_review_status") != "pending"
        or fields.get("delivery_copy")
        != json.dumps(content["copy"], ensure_ascii=False, sort_keys=True)
        or not fields.get("candidate_preview")
    ):
        raise Blocked("DELIVERY_STALE", "飞书验收包内容或状态已变化", 6)
    for name in COVERS:
        attached = fields.get("delivery_cover_" + name) or []
        if len(attached) != 1 or attached[0].get("file_token") != manifest.get("covers", {}).get(
            name
        ):
            raise Blocked("DELIVERY_STALE", "飞书封面附件已变化", 6)
    receipt = directory / "review.json"
    if receipt.exists():
        raise Blocked("DELIVERY_REVIEW_UNKNOWN", "已有审核操作记录，请先对账", 6)
    patch = {
        "delivery_review_status": "approved" if action == "approve" else "rejected",
        "delivery_review_hash": expected,
        "delivery_review_feedback": reason,
    }
    atomic_write(
        receipt,
        {"status": "started", "action": action, "actor": actor, "reason": reason, "at": now()},
    )
    bench.patch(row["record_id"], patch)
    bench.verify_patch(task, patch)
    result = {
        "status": patch["delivery_review_status"],
        "package_sha256": expected,
        "actor": actor,
        "reason": reason,
        "at": now(),
    }
    atomic_write(receipt, result)
    return result
