"""Freeze the complete typography recipe with the text script, before review."""

from copy import deepcopy
import hashlib
from .core import Blocked, atomic_write, digest, load, now

FIELDS = {
    "title_style_preview": ("标题样式与文字", "text"),
    "title_style_sha256": ("标题样式摘要", "text"),
    "title_style_review_status": ("标题样式审核结果", "text"),
}
APPROVED_CODE_SHA256 = "974d42bcfd9752b15086f9382bb801a25bf2509b3353f87db756c510579451f9"
TEXT_KEYS = ("hook", "product", "sellingPoint", "taste", "captionJson")


def snapshot(root):
    path = root / "config/director_typography_v1.json"
    if not path.is_file():
        raise Blocked("TITLE_STYLE_CONFIG", "缺少标题样式配置，不能生成待审脚本", 2)
    template = deepcopy(load(path))
    if hashlib.sha256(template["code"].encode()).hexdigest() != APPROVED_CODE_SHA256:
        raise Blocked("TITLE_STYLE_PROFILE", "标题模板代码变化，须先登记对应样式版本和预览规则", 6)
    props = {p["key"]: p["defaultValue"] for p in template["properties"]}
    template.pop("editSessionId", None)
    for p in template["properties"]:
        if p["key"] in TEXT_KEYS:
            p["defaultValue"] = "[]" if p["key"] == "captionJson" else ""
    result = {
        "version": 1,
        "style_id": "mixcut_v3_1",
        "name": "V3.1 四行常驻标题",
        "template": template,
        "spec": {
            "font_family": props["fontFamily"],
            "font_weight": 900,
            "font_sizes": [60, 76, 61, 63],
            "colors": [props[k] for k in ("yellow", "blue", "red", "cream")],
            "stroke_colors": [props[k] for k in ("blue", "cream", "white", "red")],
            "stroke_width": 4,
            "top": 108,
            "side": 48,
            "gap": 8,
            "line_height": 1.15,
            "align": "center",
            "display": "全片常驻，透明背景，四行顺序固定",
            "caption": "底部280px、72px白字深色3px描边；文字与时间按配音生成",
        },
    }
    result["sha256"] = digest(result)
    return result


def validate(script):
    style = script.get("title_style")
    if not style or style.get("sha256") != digest(
        {k: v for k, v in style.items() if k != "sha256"}
    ):
        raise Blocked("TITLE_STYLE_REVIEW_REQUIRED", "标题样式缺失或变化，须随文字稿重新审核", 3)
    if len(script["headline"]) != 4 or not all(
        isinstance(x, str) and x.strip() for x in script["headline"]
    ):
        raise Blocked("TITLE_STYLE_TEXT", "须有四行已确认标题文字", 2)
    return style


def preview(script):
    if not script.get("title_style"):
        return "标题样式尚未绑定，须先生成样式审核稿。"
    style = validate(script)
    spec = style["spec"]
    lines = [f"{style['name']}｜{style['style_id']} v{style['version']}｜{script['review_status']}"]
    lines.extend(
        f"第{i + 1}行：{text}｜{size}px｜字色{color}｜描边{stroke}"
        for i, (text, size, color, stroke) in enumerate(
            zip(script["headline"], spec["font_sizes"], spec["colors"], spec["stroke_colors"])
        )
    )
    lines += [
        f"字体{spec['font_family']}，字重{spec['font_weight']}；顶部{spec['top']}px、左右{spec['side']}px，居中，行间距{spec['gap']}px，描边{spec['stroke_width']}px。",
        spec["display"],
        spec["caption"],
        "审核包含原文、四行标题和完整模板；剪辑严格使用本版本快照。",
        "样式摘要：" + style["sha256"],
    ]
    return "\n".join(lines)


def bind_existing(root, group):
    """Create a review revision without rewriting or remeasuring existing text."""
    from . import script_stage

    prior = script_stage.latest(root, group)
    if not prior:
        raise Blocked("SCRIPT_DRAFT_MISSING", "先生成文字稿", 3)
    style = snapshot(root)
    if all(s.get("title_style") == style for s in prior["scripts"]):
        return prior
    record = deepcopy(prior)
    record.update(
        revision=prior["revision"] + 1,
        state="pending",
        created_at=now(),
        provenance={"type": "bind_title_style", "prior_revision": prior["revision"]},
    )
    for s in record["scripts"]:
        if s.get("title_style") == style:
            continue
        s.update(
            title_style=style, review_status="pending", approval=None, revision=record["revision"]
        )
        s["content_sha256"] = script_stage.script_hash(s)
    record["state"] = script_stage._state(record["scripts"])
    record["content_sha256"] = digest([s["content_sha256"] for s in record["scripts"]])
    out = script_stage.folder(root, group) / f"r{record['revision']:04d}.json"
    if out.exists():
        raise Blocked("SCRIPT_DRAFT_CONFLICT", "目标版本已存在，请对账", 6)
    atomic_write(out, record)
    script_stage._save(root, group, record)
    return record


def publish(bench, script):
    patch = {
        "title_style_preview": preview(script),
        "title_style_sha256": validate(script)["sha256"],
        "title_style_review_status": script["review_status"],
        "plan_preview": f"文字与标题样式 r{script['revision']} · {script['review_status']}\n{script['title']}｜目标{script['target_seconds']:g}秒\n{script['narration']}\n"
        + preview(script),
    }
    # Reconcile an earlier write before sending another identical patch.
    row = bench.task(script["task_id"])
    path = (
        bench.root
        / "ledger/title_style_publish"
        / (
            digest({"task": script["task_id"], "script": script["content_sha256"], "patch": patch})
            + ".json"
        )
    )
    matches = all(row["fields"].get(k) == v for k, v in patch.items())
    if path.exists() and load(path)["status"] == "unknown" and not matches:
        raise Blocked("TITLE_STYLE_SYNC_UNKNOWN", "标题样式写入结果未知，先对账，不重复写", 6)
    if not matches:
        atomic_write(path, {"status": "unknown", "patch_sha256": digest(patch)})
        bench.patch(row["record_id"], patch)
    bench.verify_patch(script["task_id"], patch)
    atomic_write(path, {"status": "synced", "patch_sha256": digest(patch)})
    return patch
