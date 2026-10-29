from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from .contracts import (
    inward_frames,
    plan_hash,
    require_approval,
    validate_plan,
    validate_record,
)
from .core import Blocked, atomic_write, digest, file_hash, load, now, resolve_media, safe_id
from .feishu import enqueue, index_rows
from .ingest import probe
from .ledger import reserve
from .material_index import material_index_preview_text
from .script_input import validate_reviewable_outline


NORMALIZED_ROOT_ID = "normalized_cfr30_v1"
WORKSPACE_ROOT_ID = "mixcut_workspace"
NORMALIZATION_PROFILE = {
    "version": 1,
    "width": 1080,
    "height": 1920,
    "fps_num": 30,
    "fps_den": 1,
    "audio": "muted",
    "codec": "h264",
    "pixel_format": "yuv420p",
    "time_mapping": "full_timeline_cfr_zero_origin",
}


def _configured_root(config: dict, root_id: str) -> Path:
    roots = [
        Path(row["path"]).resolve() for row in config["source_roots"] if row["root_id"] == root_id
    ]
    if len(roots) != 1:
        raise Blocked("ROOT_UNKNOWN", f"Configure exactly one {root_id} source root", 2)
    return roots[0]


def normalize_source(workspace: Path, config: dict, source: dict) -> dict:
    """Create a cached, full-timeline 1080x1920 CFR30 production working file."""
    output_root = _configured_root(config, NORMALIZED_ROOT_ID)
    if not output_root.is_relative_to(workspace):
        raise Blocked("NORMALIZED_ROOT", "Normalized media must stay inside the workspace", 2)
    output_root.mkdir(parents=True, exist_ok=True)
    source_id = safe_id(source["source_id"])
    target = output_root / f"{source_id}.mp4"
    receipt_path = output_root / f"{source_id}.json"
    location = source["locations"][0]
    try:
        original = resolve_media(
            config["source_roots"], location["root_id"], location["relative_path"]
        )
    except FileNotFoundError:
        original = None
    if original is not None and file_hash(original) != source["file_sha256"]:
        raise Blocked("FILE_CHANGED", source_id, 6)
    if target.exists():
        if not receipt_path.exists():
            raise Blocked("NORMALIZE_RECONCILE", f"Unreceipted output exists: {target}", 5)
        receipt = load(receipt_path)
        if (
            receipt.get("source_sha256") != source["file_sha256"]
            or receipt.get("profile") != NORMALIZATION_PROFILE
            or file_hash(target) != receipt.get("working_sha256")
        ):
            raise Blocked("NORMALIZE_CONFLICT", f"Preserving unverified output: {target}", 6)
        return receipt

    if original is None:
        raise Blocked("SOURCE_UNAVAILABLE", f"原片未挂载且没有已验证制作副本：{source_id}", 3)
    before = original.stat()

    partial = target.with_suffix(".partial.mp4")
    if partial.exists():
        raise Blocked("NORMALIZE_OUTCOME_UNKNOWN", f"Reconcile partial output: {partial}", 5)
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30"
    )
    command = [
        config["paths"]["ffmpeg"],
        "-hide_banner",
        "-v",
        "error",
        "-n",
        "-i",
        str(original),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(partial),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise Blocked("NORMALIZE_OUTCOME_UNKNOWN", source_id, 5) from exc
    if completed.returncode:
        raise Blocked("NORMALIZE_FAILED", completed.stderr[-600:], 4)
    info = probe(partial, config["paths"]["ffprobe"])
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    fps = Fraction(video.get("avg_frame_rate", "0/1"))
    duration = float(video.get("duration") or info["format"]["duration"])
    total_frames = int(video.get("nb_frames") or round(duration * 30))
    if (
        (video.get("width"), video.get("height"), video.get("codec_name")) != (1080, 1920, "h264")
        or fps != 30
        or total_frames < 1
        or any(stream["codec_type"] == "audio" for stream in info["streams"])
        or abs(duration - source["duration_sec"]) > 0.12
    ):
        raise Blocked("NORMALIZE_QC", f"Invalid CFR output: {source_id}", 4)
    decode = subprocess.run(
        [config["paths"]["ffmpeg"], "-v", "error", "-i", str(partial), "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if decode.returncode or decode.stderr.strip():
        raise Blocked("NORMALIZE_QC", f"Full decode failed: {source_id}", 4)
    after = original.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise Blocked("FILE_CHANGED", f"Source changed while normalizing: {source_id}", 6)
    partial.rename(target)
    receipt = {
        "schema_version": "1.0.0",
        "entity": "normalized_media_receipt",
        "source_id": source_id,
        "source_sha256": source["file_sha256"],
        "working_sha256": file_hash(target),
        "root_id": NORMALIZED_ROOT_ID,
        "relative_path": target.name,
        "path": str(target),
        "total_frames": total_frames,
        "duration_sec": duration,
        "profile": NORMALIZATION_PROFILE,
        "decode_exit": decode.returncode,
        "created_at": now(),
    }
    atomic_write(receipt_path, receipt)
    return receipt


def _latest_approved_outline(workspace: Path, task_id: str) -> tuple[Path, dict, dict]:
    safe_id(task_id)
    found = []
    for path in (workspace / "jobs" / task_id).glob("r*/script_outline.json"):
        outline = load(path)
        if isinstance(outline.get("revision"), int):
            found.append((outline["revision"], path, outline))
    if not found:
        raise Blocked("SCRIPT_MISSING", task_id, 3)
    _, path, outline = max(found, key=lambda item: item[0])
    request_path = workspace / "requests" / task_id / f"{outline['request_sha256']}.json"
    if not request_path.exists():
        raise Blocked("SCRIPT_INPUT_MISSING", task_id, 4)
    request = load(request_path)
    validate_reviewable_outline(outline, request)
    require_approval(outline)
    return path, outline, request


def _task_fields(workspace: Path, task_id: str) -> dict:
    snapshot = load(workspace / "catalog/feishu_snapshot.json")
    rows = snapshot.get("tables", {}).get("tasks", [])
    task = index_rows(rows, "task_id").get(task_id)
    if not task:
        raise Blocked("TASK_NOT_FOUND", task_id, 2)
    return task["fields"]


def _product_scope(workspace: Path) -> dict:
    path = workspace / "config/goods/store_multi_product_v1.json"
    if not path.exists():
        raise Blocked("PRODUCT_SCOPE_MISSING", str(path), 3)
    product = load(path)
    validate_record(product, workspace)
    require_approval(product)
    return product


def compile_edit_plan(workspace: Path, config: dict, task_id: str) -> dict:
    from .executable import uses_v3

    fields = _task_fields(workspace, task_id)
    if uses_v3(workspace, fields):
        raise Blocked(
            "V3_EXECUTOR_REQUIRED", "执行脚本V3须使用montage-v3 packet，不能回退到旧版重选素材", 3
        )
    """Compile an approved outline, exact material bindings and TTS into an edit plan."""
    _, outline, request = _latest_approved_outline(workspace, task_id)
    if request.get("input_policy") != "approved_material_context_constrained":
        raise Blocked("PLAN_INPUT_POLICY", "Formal plan requires locked material slots", 3)
    job = workspace / "jobs" / task_id / f"r{outline['revision']}"
    material_index_path = job / "material_index_preview.json"
    manifest_path = job / "preproduction/audio_caption_manifest.json"
    if not material_index_path.exists() or not manifest_path.exists():
        raise Blocked("PREPRODUCTION_MISSING", task_id, 3)
    material_index = load(material_index_path)
    manifest = load(manifest_path)
    if (
        not material_index.get("production_ready")
        or material_index.get("script_sha256") != outline["content_sha256"]
        or manifest.get("status") != "ready_for_edit_plan"
        or manifest.get("script_sha256") != outline["content_sha256"]
    ):
        raise Blocked("PREPRODUCTION_STALE", task_id, 6)
    product = _product_scope(workspace)
    fields = _task_fields(workspace, task_id)
    clips = {clip["clip_id"]: clip for clip in load(workspace / "catalog/clips.json")}
    sources = {source["source_id"]: source for source in load(workspace / "catalog/sources.json")}
    index_shots = {shot["shot_id"]: shot for shot in material_index["shots"]}
    slots = {slot["slot_id"]: slot for slot in request["material_slots"]}

    media: list[dict[str, Any]] = []
    media_by_source: dict[str, dict] = {}
    normalized_by_source: dict[str, dict] = {}
    plan_shots = []
    cursor = 0
    for shot in outline["shots"]:
        slot = slots[shot["shot_id"]]
        clip = clips.get(slot["clip_id"])
        if not clip:
            raise Blocked("MATERIAL_CONTEXT_STALE", slot["clip_id"], 6)
        require_approval(clip)
        if (
            clip["revision"] != slot["clip_revision"]
            or clip["content_sha256"] != slot["approved_content_sha256"]
            or clip["source_id"] not in sources
        ):
            raise Blocked("MATERIAL_CONTEXT_STALE", shot["shot_id"], 6)
        indexed = index_shots.get(shot["shot_id"], {}).get("candidates", [])
        if len(indexed) != 1 or not indexed[0].get("production_eligible"):
            raise Blocked("MATERIAL_INDEX_STALE", shot["shot_id"], 6)
        candidate = indexed[0]
        if (
            candidate.get("clip_id") != clip["clip_id"]
            or candidate.get("start_sec") != slot["usable_start_sec"]
            or candidate.get("end_sec") != slot["usable_end_sec"]
        ):
            raise Blocked("MATERIAL_INDEX_STALE", shot["shot_id"], 6)
        source = sources[clip["source_id"]]
        receipt = normalized_by_source.get(source["source_id"])
        if receipt is None:
            receipt = normalize_source(workspace, config, source)
            normalized_by_source[source["source_id"]] = receipt
        media_row = media_by_source.get(source["source_id"])
        if media_row is None:
            media_row = {
                "media_id": "M_" + digest(source["source_id"])[:16],
                "source_id": source["source_id"],
                "source_sha256": source["file_sha256"],
                "working_sha256": receipt["working_sha256"],
                "root_id": receipt["root_id"],
                "relative_path": receipt["relative_path"],
                "total_frames": receipt["total_frames"],
                "fps_num": 30,
                "fps_den": 1,
                "time_mapping": "full_timeline_cfr_zero_origin",
            }
            media_by_source[source["source_id"]] = media_row
            media.append(media_row)
        approved_in, approved_out = inward_frames(slot["usable_start_sec"], slot["usable_end_sec"])
        duration = shot["target_frames"]
        available = approved_out - approved_in
        if available < duration:
            raise Blocked("TIMING_UNSATISFIABLE", shot["shot_id"], 3)
        source_in = approved_in + (available - duration) // 2
        source_out = source_in + duration
        if source_out > media_row["total_frames"]:
            raise Blocked("TIMING_UNSATISFIABLE", shot["shot_id"], 3)
        plan_shots.append(
            {
                "shot_id": shot["shot_id"],
                "clip_id": clip["clip_id"],
                "clip_revision": clip["revision"],
                "media_id": media_row["media_id"],
                "source_in_frame": source_in,
                "source_out_frame": source_out,
                "timeline_start_frame": cursor,
                "duration_frames": duration,
                "source_audio_gain": 0,
                "speed": 1,
                "fit_mode": config["output"]["fit_mode"],
                "crop": None,
                "selection_reason": ("用户批准脚本绑定的素材槽位；使用已批准可用区间的居中连续帧"),
            }
        )
        cursor += duration

    workspace_root = _configured_root(config, WORKSPACE_ROOT_ID)
    voiceover = Path(manifest["voiceover"]["path"]).resolve()
    if not voiceover.is_relative_to(workspace_root) or not voiceover.is_file():
        raise Blocked("AUDIO_OUTSIDE_ROOT", str(voiceover), 2)
    captions = [
        {
            "shot_id": row["shot_id"],
            "start_frame": row["start_frame"],
            "end_frame": row["end_frame"],
            "text": row["text"],
        }
        for row in manifest["captions"]
    ]
    snapshot = {
        "outline_sha256": outline["content_sha256"],
        "material_context_sha256": request["material_context_sha256"],
        "material_index_id": material_index["index_id"],
        "product_scope_sha256": product["content_sha256"],
        "catalog_clip_hashes": [
            clips[slot["clip_id"]]["content_sha256"] for slot in request["material_slots"]
        ],
        "working_media_hashes": [row["working_sha256"] for row in media],
        "voiceover_sha256": manifest["voiceover"]["sha256"],
        "style_sha256": manifest["style"]["style_sha256"],
    }
    plan = {
        "schema_version": "1.0.0",
        "entity": "edit_plan",
        "is_fixture": False,
        "task_id": task_id,
        "batch_id": fields.get("batch_id") or fields.get("request_group_id") or "BATCH_UNSPECIFIED",
        "brand_id": product["brand_id"],
        "goods_id": product["goods_id"],
        "script_revision": outline["revision"],
        "script_sha256": outline["content_sha256"],
        "product_revision": product["revision"],
        "taxonomy_version": load(workspace / "config/taxonomy.json")["version"],
        "snapshot_sha256": digest(snapshot),
        "build_id": "b_" + "0" * 16,
        "plan_sha256": "0" * 64,
        "audio_mode": "tts_uniform",
        "canvas": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "total_frames": outline["total_frames"],
        "style_sha256": manifest["style"]["style_sha256"],
        "media": media,
        "shots": plan_shots,
        "captions": captions,
        "audio_tracks": [
            {
                "track_id": "VOICEOVER_" + outline["content_sha256"][:12],
                "role": "voiceover",
                "root_id": WORKSPACE_ROOT_ID,
                "relative_path": str(voiceover.relative_to(workspace_root)),
                "sha256": manifest["voiceover"]["sha256"],
                "start_frame": 0,
                "duration_frames": outline["total_frames"],
                "gain": manifest["voiceover"]["gain"],
            }
        ],
    }
    plan["plan_sha256"] = plan_hash(plan)
    plan["build_id"] = "b_" + plan["plan_sha256"][:16]
    validate_plan(plan, workspace)
    reserve(
        workspace,
        plan["batch_id"],
        task_id,
        outline["revision"],
        plan["plan_sha256"],
        [shot["clip_id"] for shot in plan_shots],
    )
    build_dir = job / plan["build_id"]
    target = build_dir / "edit_plan.json"
    if target.exists() and load(target) != plan:
        raise Blocked("BUILD_CONFLICT", str(target), 6)
    atomic_write(target, plan)
    manifest.update(
        formal_edit_plan_created=True,
        edit_plan_path=str(target),
        build_id=plan["build_id"],
        plan_sha256=plan["plan_sha256"],
        plan_created_at=now(),
    )
    manifest["manifest_sha256"] = digest(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    atomic_write(manifest_path, manifest)
    enqueue(
        workspace,
        "tasks",
        task_id,
        {
            "stage": "ready_for_openchatcut_import",
            "build_id": plan["build_id"],
            "material_index_preview": material_index_preview_text(material_index),
            "plan_preview": (
                f"正式剪辑计划 {plan['build_id']}｜{plan['total_frames'] / 30:g}秒/{plan['total_frames']}帧｜"
                f"{len(plan_shots)}个已批准素材镜头｜Tingting配音｜{len(captions)}段字幕｜原声静音"
            ),
            "error_summary": "正式计划已生成并通过本地媒体完整性预检；待OpenChatCut导入。",
        },
        supersede_group="production_state",
        supersede_groups={"material_index", "edit_plan", "production_state"},
    )
    return {
        "status": "ready_for_openchatcut_import",
        "task_id": task_id,
        "script_revision": outline["revision"],
        "build_id": plan["build_id"],
        "plan_sha256": plan["plan_sha256"],
        "path": str(target),
        "normalized_media": len(media),
        "shots": len(plan_shots),
        "captions": len(captions),
        "total_frames": plan["total_frames"],
    }
