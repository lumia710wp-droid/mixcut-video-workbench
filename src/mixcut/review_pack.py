"""Local review copies, never production inputs or inferred approvals."""

from __future__ import annotations

import html
import subprocess
from pathlib import Path

from .contracts import inward_frames
from .core import Blocked, atomic_write, file_hash, load, now, resolve_media, safe_id
from .ingest import probe

PROFILE = {"version": 1, "width": 540, "height": 960, "fps": 30, "audio": "muted"}


def suggested_window(duration: float) -> dict:
    """Center a five-second review window; no semantic completeness claim."""
    _, end = inward_frames(0, duration)
    length = min(150, end)
    start = (end - length) // 2
    return {
        "start_sec": start / 30,
        "end_sec": (start + length) / 30,
        "duration_frames_at_30fps": length,
        "method": "center_window_not_semantic_analysis",
        "human_review": "pending",
    }


def prepare_review_pack(workspace: Path, config: dict) -> dict:
    sources = {s["source_id"]: s for s in load(workspace / "catalog/sources.json")}
    clips = [c for c in load(workspace / "catalog/clips.json") if not c["is_fixture"]]
    if len(clips) > config["ingest"]["initial_file_limit"]:
        raise Blocked("LIMIT", "Review pack exceeds the authorized sample limit")
    output = workspace / "reviews/G2_sample_v1"
    output.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "at": now(),
        "scope": "local_review_only",
        "profile": PROFILE,
        "semantic_analysis": "not_performed",
        "base_url": load(workspace / "config/feishu_mapping.local.json").get("url", ""),
        "outputs": [],
        "errors": [],
    }
    for clip in clips:
        safe_id(clip["clip_id"])
        source = sources[clip["source_id"]]
        loc = source["locations"][0]
        target = output / f"{clip['clip_id']}.mp4"
        receipt_path = target.with_suffix(".json")
        try:
            original = resolve_media(config["source_roots"], loc["root_id"], loc["relative_path"])
            stat = original.stat()
            if file_hash(original) != source["file_sha256"]:
                raise Blocked("FILE_CHANGED", "Original differs from source catalog")
            cached = load(receipt_path) if receipt_path.exists() else None
            if target.exists():
                if (
                    not cached
                    or cached.get("source_sha256") != source["file_sha256"]
                    or cached.get("profile") != PROFILE
                    or file_hash(target) != cached.get("output_sha256")
                ):
                    raise Blocked(
                        "REVIEW_COPY_CONFLICT", "Existing copy is unverified; preserve it"
                    )
            else:
                part = target.with_suffix(".partial.mp4")
                if part.exists():
                    raise Blocked("REVIEW_COPY_UNKNOWN", "Partial output exists; reconcile first")
                vf = "scale=540:960:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=540:960:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:text='REVIEW ONLY | SOURCE %{pts\\:hms}':fontcolor=white:fontsize=18:box=1:boxcolor=black@0.7:x=12:y=12"
                cmd = [
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
                    "ultrafast",
                    "-crf",
                    "28",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(part),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode:
                    raise Blocked("REVIEW_COPY_FAILED", result.stderr[-600:], 4)
                info = probe(part, config["paths"]["ffprobe"])
                stream = next(s for s in info["streams"] if s["codec_type"] == "video")
                if (stream["width"], stream["height"], stream["codec_name"]) != (
                    540,
                    960,
                    "h264",
                ) or abs(float(stream["duration"]) - source["duration_sec"]) > 0.1:
                    raise Blocked("REVIEW_COPY_QC", "Dimensions, codec or duration mismatch")
                decode = subprocess.run(
                    [config["paths"]["ffmpeg"], "-v", "error", "-i", str(part), "-f", "null", "-"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if decode.returncode or decode.stderr.strip():
                    raise Blocked("REVIEW_COPY_QC", "Full decode failed")
                after = original.stat()
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise Blocked("FILE_CHANGED", "Source changed during review copy generation")
                part.rename(target)
                cached = {
                    "source_sha256": source["file_sha256"],
                    "output_sha256": file_hash(target),
                    "profile": PROFILE,
                    "decode_exit": decode.returncode,
                    "output_duration_sec": float(stream["duration"]),
                    "source_path": str(original),
                    "output_path": str(target),
                }
                atomic_write(receipt_path, cached)
            report["outputs"].append(
                {
                    "clip_id": clip["clip_id"],
                    "revision": clip["revision"],
                    "content_sha256": clip["content_sha256"],
                    "relative_source_path": loc["relative_path"],
                    "duration_sec": source["duration_sec"],
                    "suggested_window": suggested_window(source["duration_sec"]),
                    **cached,
                }
            )
        except (Blocked, OSError, subprocess.TimeoutExpired) as exc:
            report["errors"].append(
                {
                    "clip_id": clip["clip_id"],
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": str(exc)[:600],
                }
            )
        report["status"] = "partial"
        atomic_write(output / "manifest.json", report)
    report["status"] = (
        "complete" if len(report["outputs"]) == len(clips) and not report["errors"] else "partial"
    )
    atomic_write(output / "manifest.json", report)
    (output / "index.html").write_text(render_review_html(report), encoding="utf-8")
    return {
        "status": report["status"],
        "copies": len(report["outputs"]),
        "errors": report["errors"],
        "manifest": str(output / "manifest.json"),
        "review_page": str(output / "index.html"),
    }


def render_review_html(report: dict) -> str:
    cards = []
    for i, row in enumerate(report["outputs"], 1):
        window = row["suggested_window"]
        label = html.escape(row["relative_source_path"])
        src = html.escape(Path(row["output_path"]).name, quote=True)
        cards.append(
            f'''<article><h2>{i:02d} · {label}</h2><video controls preload="metadata" src="{src}"></video><div class="info"><p>原片 {row["duration_sec"]:.3f} 秒 · 当前版本 {row["revision"]}</p><p>待审起点 <b>{window["start_sec"]:.3f}</b> 秒 → 终点 <b>{window["end_sec"]:.3f}</b> 秒</p><button onclick="this.closest('article').querySelector('video').currentTime={window["start_sec"]}">跳到建议起点</button><p class="muted">这是按时长居中取的候选区间，尚未判断商品、动作完整性或画面质量。可在下方记下更合适的源时间，然后填入飞书。</p><label>我的备注（只暂存在此页面）<textarea placeholder="商品、起止秒、动作、画面风险"></textarea></label><p class="id">{html.escape(row["clip_id"])}</p></div></article>'''
        )
    return (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>门店素材 · G2 审核包</title><style>body{margin:32px auto;max-width:1100px;padding:0 24px;font:16px/1.65 system-ui;color:#24302a;background:#f5f3ed}h1{font-size:32px}article{background:white;border:1px solid #ddd8ce;border-radius:12px;margin:24px 0;padding:24px;display:grid;grid-template-columns:260px 1fr;gap:24px}h2{grid-column:1/-1;font-size:20px;margin:0}video{width:260px;max-height:465px;background:#111}textarea{display:block;width:95%;min-height:80px}button,a{color:#18583d}button{padding:9px 16px;background:#e5efe7;border:0;border-radius:6px;cursor:pointer}.muted,.id{color:#68726d;font-size:14px}.id{word-break:break-all}header{padding-bottom:10px;border-bottom:2px solid #24302a}@media(max-width:650px){article{display:block}video{width:100%;margin:16px 0}}</style><header><h1>门店素材 · G2 审核包</h1><p>已入库原片的本地低清副本，原片保持不变。审核副本静音；片内时码对应源视频从零开始的时间。副本仅供审核，不能当作生产素材。</p><p><a href="https://example.invalid" id="base">打开飞书素材审核表</a>，按镜头编号确认品牌、商品、可用起止秒、有效标签。保存后同步内容版本，再批准对应版本。</p><p>页面不会上传视频、提交批准或保存备注到磁盘；备注请复制到飞书。目录名称只是定位线索，不是视觉识别结论。</p></header>""".replace(
            "https://example.invalid",
            html.escape(report.get("base_url", ""), quote=True),
        )
        + "".join(cards)
        + "</html>"
    )
