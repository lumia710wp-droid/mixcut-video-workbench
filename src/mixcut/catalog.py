from __future__ import annotations
import subprocess
from pathlib import Path
from .core import atomic_write, load, file_hash, resolve_media, Blocked
from .contracts import content_hash, validate_record
from .feishu import enqueue


def seed_candidates(workspace: Path, config: dict) -> dict:
    sources = load(workspace / "catalog/sources.json")
    dest = workspace / "catalog/clips.json"
    clips = load(dest) if dest.exists() else []
    existing = {c["clip_id"] for c in clips}
    for source in sources:
        cid = "c_" + source["file_sha256"][:24] + "_whole"
        if cid in existing:
            continue
        clip = {
            "schema_version": "1.0.0",
            "entity": "clip",
            "is_fixture": source["is_fixture"],
            "clip_id": cid,
            "revision": 1,
            "source_id": source["source_id"],
            "brand_id": None,
            "goods_id": None,
            "source_type": "other",
            "taxonomy_version": load(workspace / "config/taxonomy.json")["version"],
            "tags": {"content": [], "action": [], "shot_size": [], "risk": []},
            "usable_start_sec": 0,
            "usable_end_sec": source["duration_sec"],
            "description": "待人工标记：整文件候选，不代表单一动作或商品。",
            "review_status": "pending",
            "content_sha256": "",
            "approval": None,
        }
        clip["content_sha256"] = content_hash(clip)
        validate_record(clip, workspace, production=not clip["is_fixture"])
        clips.append(clip)
        loc = source["locations"][0]
        fields = {
            "clip_id": cid,
            "source_id": source["source_id"],
            "media_ref": loc["root_id"] + ":" + loc["relative_path"],
            "source_duration_sec": source["duration_sec"],
            "ai_proposal": "未调用模型。目录名称仅作为定位线索，商品/有效标签/区间须人工确认。",
            "content_sha256": clip["content_sha256"],
            "review_status": "pending",
            "taxonomy_version": clip["taxonomy_version"],
            "used_count": 0,
            "sync_info": "G1待联调；素材只读盘点，未批准。",
        }
        enqueue(workspace, "clips", cid, fields)
    atomic_write(dest, clips)
    return {
        "candidates": len(clips),
        "approved": sum(c["review_status"] == "approved" for c in clips),
    }


def previews(workspace: Path, config: dict) -> dict:
    sources = {s["source_id"]: s for s in load(workspace / "catalog/sources.json")}
    outputs = []
    for clip in load(workspace / "catalog/clips.json"):
        source = sources[clip["source_id"]]
        loc = source["locations"][0]
        path = resolve_media(config["source_roots"], loc["root_id"], loc["relative_path"])
        if file_hash(path) != source["file_sha256"]:
            raise Blocked("FILE_CHANGED", str(path), 6)
        directory = workspace / "previews" / clip["clip_id"]
        directory.mkdir(parents=True, exist_ok=True)
        times = [round(source["duration_sec"] * n / 6, 3) for n in [1, 3, 5]]
        frames = []
        for i, t in enumerate(times):
            target = directory / f"frame_{i}_{t:.3f}s.jpg"
            frames.append(target)
            if target.exists():
                continue
            vf = f"scale=270:-2,drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:text='SOURCE {t:.3f}s':fontcolor=white:fontsize=16:box=1:boxcolor=black@0.7:x=10:y=10"
            p = subprocess.run(
                [
                    config["paths"]["ffmpeg"],
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-n",
                    "-ss",
                    str(t),
                    "-i",
                    str(path),
                    "-vf",
                    vf,
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if p.returncode:
                raise Blocked("PREVIEW_FAILED", p.stderr[-500:], 4)
        contact = directory / "contact.jpg"
        if not contact.exists():
            argv = [config["paths"]["ffmpeg"], "-hide_banner", "-loglevel", "error", "-n"]
            for f in frames:
                argv += ["-i", str(f)]
            p = subprocess.run(
                argv + ["-filter_complex", "hstack=inputs=3", "-frames:v", "1", str(contact)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if p.returncode:
                raise Blocked("PREVIEW_FAILED", p.stderr[-500:], 4)
        outputs.append(
            {
                "clip_id": clip["clip_id"],
                "source_path": str(path),
                "evidence_times_sec": times,
                "preview": str(contact),
                "sha256": file_hash(contact),
            }
        )
        atomic_write(
            workspace / "reports/preview_manifest.json", {"outputs": outputs, "status": "partial"}
        )
    atomic_write(
        workspace / "reports/preview_manifest.json", {"outputs": outputs, "status": "complete"}
    )
    return {"count": len(outputs), "manifest": str(workspace / "reports/preview_manifest.json")}
