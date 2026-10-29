"""File-backed fact retrieval for an interactive Codex director, never final selection."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from .core import Blocked, atomic_write, digest, file_hash, load, now, safe_id
from .contracts import inward_frames
from .fact_planning import planning_segments
from .product_scope import scope_values, scope_key, validate_scope
from .montage import reservations, overlap, needed_frames, diversity

VERSION = "fact-search-1.2"
INPUTS = [
    "catalog/material_facts_v1.json",
    "catalog/sources.json",
    "catalog/clips.json",
    "catalog/product_identity_confirmations.json",
    "catalog/material_workflow_policy.json",
]
# Visual synonyms only. Customer identity, recipe and taste are not inferred from actions.
CONCEPTS = {
    "wrapper": ["展皮", "展白皮", "拉伸白皮", "撑皮", "展开白皮"],
    "filling": ["放果肉", "放入果肉", "装入果肉", "入馅", "放馅", "装馅"],
    "closing": ["包拢", "收口", "捏合", "收边"],
    "shaping": ["整形", "揉圆", "团圆", "塑形"],
    "liner": ["装托", "纸托", "放入托"],
    "cutaway": ["切开", "切面", "断面", "剖面"],
    "fruit": ["果肉", "鲜果"],
    "finished": ["成品", "完成包制", "包好"],
    "packing": ["包装", "装盒", "封盒", "打包"],
    "closeup": ["特写", "近景"],
    "customer": ["顾客", "客人"],
}
TERM_ALIASES = {
    "放果肉": ["放果肉", "放入果肉", "装入果肉"],
    "放入果肉": ["放果肉", "放入果肉", "装入果肉"],
    "收口": ["收口", "收边", "捏口"],
    "包拢": ["包拢", "包起", "合拢"],
    "切面": ["切面", "断面", "剖面"],
    "切开": ["切开", "剖开"],
    "特写": ["特写", "近景"],
}
FILLING_PATTERNS = [
    r"果肉.{0,10}(?:放到|放向|放在|放入|移至).{0,8}(?:白皮|皮上|皮中央|白片)",
    r"(?:放入|装入|放到).{0,5}果肉",
]


def _supports(text: str, term: str) -> bool:
    aliases = CONCEPTS.get(term, TERM_ALIASES.get(term, [term]))
    patterns = [re.escape(alias.lower()) for alias in aliases]
    if term in {"filling", "放果肉", "放入果肉"}:
        patterns.extend(FILLING_PATTERNS)
    for pattern in patterns:
        for match in re.finditer(pattern, text.lower()):
            before = re.split(r"[，。；;,.]", text[: match.start()])[-1][-8:]
            after = text[match.end() : match.end() + 8]
            if re.search(r"未|没有|不见|无法|不能|疑似|不确定|无明显|未见", before):
                continue
            if re.search(r"(?:未|没有|不能|不曾).{0,3}(?:放|移至)", match.group()):
                continue
            if re.match(r".{0,2}(?:未出现|不清楚|无法确认|不确定)", after):
                continue
            return True
    return False


def _tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for word in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.update(word[n : n + 2] for n in range(max(1, len(word) - 1)))
    tokens.update("concept:" + k for k in CONCEPTS if _supports(text, k))
    return tokens


def _snapshot(root: Path) -> str:
    optional = {"catalog/product_identity_confirmations.json"}
    missing = [n for n in INPUTS if n not in optional and not (root / n).is_file()]
    if missing:
        raise Blocked("SEARCH_INPUT_MISSING", "缺少素材索引输入：" + ", ".join(missing), 3)
    return digest(
        {
            "version": VERSION,
            "vocabulary": CONCEPTS,
            "term_aliases": TERM_ALIASES,
            "action_patterns": FILLING_PATTERNS,
            "inputs": {n: file_hash(root / n) if (root / n).is_file() else None for n in INPUTS},
            "taxonomy": file_hash(root / "config/taxonomy.json")
            if (root / "config/taxonomy.json").is_file()
            else None,
        }
    )


def _index(root: Path) -> dict:
    pointer = root / "indexes/material_search/current.json"
    if not pointer.is_file():
        raise Blocked("SEARCH_INDEX_MISSING", "请先运行 material-search build", 3)
    key = safe_id(load(pointer)["snapshot_sha256"])
    path = root / "indexes/material_search" / (key + ".json")
    value = load(path)
    if value.get("content_sha256") != digest(
        {k: v for k, v in value.items() if k != "content_sha256"}
    ):
        raise Blocked("SEARCH_INDEX_HASH", "检索缓存摘要不符，请重新构建", 6)
    if value["snapshot_sha256"] != _snapshot(root):
        raise Blocked("SEARCH_INDEX_STALE", "素材或商品记录已变化，请先更新检索索引", 6)
    return value


def build_index(root: Path) -> dict:
    snapshot = _snapshot(root)
    analyzer = digest([VERSION, CONCEPTS, TERM_ALIASES, FILLING_PATTERNS])
    folder = root / "indexes/material_search"
    target = folder / (snapshot + ".json")
    if target.is_file():
        value = load(target)
        if value.get("content_sha256") != digest(
            {k: v for k, v in value.items() if k != "content_sha256"}
        ):
            raise Blocked("SEARCH_INDEX_HASH", "保留冲突缓存，需对账", 6)
        atomic_write(folder / "current.json", {"snapshot_sha256": snapshot})
        return {"status": "reused", "path": str(target), "documents": len(value["documents"])}
    old = {}
    pointer = folder / "current.json"
    if pointer.exists():
        p = folder / (safe_id(load(pointer)["snapshot_sha256"]) + ".json")
        if p.is_file():
            previous = load(p)
            if previous.get("analyzer_sha256") == analyzer and previous.get(
                "content_sha256"
            ) == digest({k: v for k, v in previous.items() if k != "content_sha256"}):
                old = previous["documents"]
    documents: dict[str, dict] = {}
    postings: dict[str, list[str]] = {}
    reused = 0
    for facts in load(root / INPUTS[0])["materials"]:
        sid = facts["source"]["source_id"]
        for n, phase in enumerate(facts["phases"]):
            key = "phase_" + digest([sid, facts["content_sha256"], n])[:24]
            if key in old:
                doc = old[key]
                reused += 1
            else:
                events = [
                    e
                    for e in facts.get("events", [])
                    if phase["start_sec"] <= e["time_sec"] < phase["end_sec"]
                ]
                text = " ".join(
                    [
                        phase.get("title", ""),
                        phase["observation"],
                        " ".join(phase.get("semantic_tags", [])),
                        " ".join(e["text"] for e in events),
                    ]
                )
                doc = {
                    "document_id": key,
                    "source_id": sid,
                    "facts_sha256": facts["content_sha256"],
                    "phase_index": n,
                    "start_sec": phase["start_sec"],
                    "end_sec": phase["end_sec"],
                    "observation": phase["observation"],
                    "quality": phase["quality"],
                    "text": text,
                    "tokens": sorted(_tokens(text)),
                }
            documents[key] = doc
            for token in doc["tokens"]:
                postings.setdefault(token, []).append(key)
    value = {
        "version": VERSION,
        "analyzer_sha256": analyzer,
        "created_at": now(),
        "snapshot_sha256": snapshot,
        "documents": documents,
        "postings": postings,
    }
    value["content_sha256"] = digest(value)
    atomic_write(target, value)
    atomic_write(folder / "current.json", {"snapshot_sha256": snapshot})
    return {
        "status": "built",
        "path": str(target),
        "documents": len(documents),
        "reused_documents": reused,
        "new_documents": len(documents) - reused,
        "provider_calls": 0,
    }


def _settings(root: Path, query: dict) -> dict:
    settings = scope_values(query)
    validate_scope(settings)
    if query.get("group_id"):
        from .executable import policy

        policy_value = policy(root, query["group_id"])
        if any(policy_value.get(k) != v for k, v in settings.items()):
            raise Blocked("SEARCH_SCOPE", "项目与商品/品牌不一致", 6)
        settings.update(policy_value)
        validate_scope(settings)
    return settings


def _free(lo: int, hi: int, source: str, used: list[dict]) -> list[tuple[int, int]]:
    pieces = [(lo, hi)]
    for shot in used:
        if shot["source_id"] != source:
            continue
        next_pieces = []
        for a, b in pieces:
            if a < shot["source_out_frame"] and shot["source_in_frame"] < b:
                if a < shot["source_in_frame"]:
                    next_pieces.append((a, min(b, shot["source_in_frame"])))
                if shot["source_out_frame"] < b:
                    next_pieces.append((max(a, shot["source_out_frame"]), b))
            else:
                next_pieces.append((a, b))
        pieces = next_pieces
    return pieces


def _validate_query(query: dict) -> None:
    allowed = {
        "brand_id",
        "goods_id",
        "goods_scope",
        "goods_ids",
        "group_id",
        "query",
        "must",
        "exclude",
        "min_duration_sec",
        "limit",
        "max_per_source",
        "max_output_chars",
        "cursor",
    }
    if set(query) - allowed or not query.get("brand_id") or not query.get("goods_id"):
        raise Blocked("SEARCH_QUERY", "请提供品牌、商品及允许的检索字段", 2)
    if not isinstance(query.get("query"), str) or not 1 <= len(query["query"]) <= 512:
        raise Blocked("SEARCH_QUERY", "画面检索文字须为1—512字符", 2)
    for field in ["must", "exclude"]:
        terms = query.get(field, [])
        if (
            not isinstance(terms, list)
            or len(terms) > 12
            or any(not isinstance(t, str) or not 1 <= len(t) <= 64 for t in terms)
        ):
            raise Blocked("SEARCH_QUERY", "must/exclude须为最多12条短词", 2)
    for field, default, low, high in [
        ("limit", 6, 1, 12),
        ("max_per_source", 2, 1, 4),
        ("max_output_chars", 6000, 1800, 12000),
    ]:
        v = query.get(field, default)
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise Blocked("SEARCH_QUERY", field + "超出允许范围", 2)
    duration = query.get("min_duration_sec", 0.5)
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not 0.1 <= duration <= 120
    ):
        raise Blocked("SEARCH_QUERY", "检索时长须为0.1—120秒", 2)


def query_index(root: Path, query: dict) -> dict:
    _validate_query(query)
    index = _index(root)
    settings = _settings(root, query)
    eligible = planning_segments(root, settings)
    scopes: dict[str, set] = {}
    for segment in eligible:
        scopes.setdefault(segment["source_id"], set()).add(
            (segment["source_in_frame"], segment["source_out_frame"])
        )
    used = [s for r in reservations(root) for s in r["shots"]]
    base = {k: v for k, v in query.items() if k != "cursor"}
    binding = digest([base, settings, index["content_sha256"], used])
    cursor = query.get("cursor")
    offset = 0
    if cursor:
        if (
            not isinstance(cursor, dict)
            or cursor.get("binding") != binding
            or isinstance(cursor.get("offset"), bool)
            or not isinstance(cursor.get("offset"), int)
            or cursor["offset"] < 0
        ):
            raise Blocked("SEARCH_CURSOR_STALE", "查询/版本/预占已变化，请从首页重新检索", 6)
        offset = cursor["offset"]
    terms = _tokens(query["query"])
    for term in query.get("must", []):
        terms.update(_tokens(term))
    ids = set().union(*(set(index["postings"].get(t, [])) for t in terms)) if terms else set()
    matches = []
    minimum = math.ceil(query.get("min_duration_sec", 0.5) * 30)
    requested_concepts = {k for k in CONCEPTS if _supports(query["query"], k)}
    for key in ids:
        doc = index["documents"][key]
        sid = doc["source_id"]
        if sid not in scopes:
            continue
        text = doc["text"]
        if any(not _supports(text, t) for t in query.get("must", [])):
            continue
        if any(_supports(text + " " + doc["quality"], t) for t in query.get("exclude", [])):
            continue
        found = sorted(k for k in requested_concepts if _supports(text, k))
        if requested_concepts and not found:
            continue
        hits = terms.intersection(doc["tokens"])
        score = round(2 * len(found) + len(hits) / max(1, math.sqrt(len(doc["tokens"]))), 4)
        score -= sum(
            _supports(doc["quality"], term) for term in ["失焦", "不清楚", "强反光", "遮挡"]
        )
        lo, hi = inward_frames(doc["start_sec"], doc["end_sec"])
        intervals = set()
        for a, b in scopes[sid]:
            a, b = max(a, lo), min(b, hi)
            if b > a:
                intervals.update(_free(a, b, sid, used))
        # Whole-product and phase windows may produce contained duplicates.
        intervals = {
            (a, b)
            for a, b in intervals
            if not any(c <= a and b <= d and (c, d) != (a, b) for c, d in intervals)
        }
        for a, b in intervals:
            if b - a < minimum:
                continue
            cid = (
                "candidate_"
                + digest([index["content_sha256"], key, scope_key(settings), a, b])[:24]
            )
            matches.append(
                {
                    "candidate_id": cid,
                    "document_id": key,
                    "source_id": sid,
                    **(
                        {
                            "goods_id": next(
                                s["goods_id"]
                                for s in eligible
                                if s["source_id"] == sid
                                and s["source_in_frame"] <= a < b <= s["source_out_frame"]
                            )
                        }
                        if "goods_scope" in settings
                        else {}
                    ),
                    "facts_sha256": doc["facts_sha256"],
                    "phase_index": doc["phase_index"],
                    "start_frame": a,
                    "end_frame": b,
                    "duration_sec": round((b - a) / 30, 3),
                    "observation": doc["observation"][:180],
                    "quality": doc["quality"][:100],
                    "score": score,
                    "matched_concepts": found,
                    "requires_visual_review": False,
                    "selection_basis": "reuse_ingest_facts",
                }
            )
    matches.sort(key=lambda c: (-c["score"], c["source_id"], c["start_frame"], c["candidate_id"]))
    ranked: list[dict] = []
    counts: dict[str, int] = {}
    for candidate in matches:
        sid = candidate["source_id"]
        if counts.get(sid, 0) >= query.get("max_per_source", 2):
            continue
        ranked.append(candidate)
        counts[sid] = counts.get(sid, 0) + 1
    if offset > len(ranked):
        raise Blocked("SEARCH_CURSOR_STALE", "翻页位置超出结果范围", 6)
    report = {
        "version": VERSION,
        "status": "candidates" if ranked else "no_match",
        "scope": {
            **scope_values(settings),
            **({"group_id": settings["group_id"]} if "group_id" in settings else {}),
        },
        "query": base,
        "binding": binding,
        "index_sha256": index["content_sha256"],
        "snapshot_sha256": index["snapshot_sha256"],
        "indexed_phases": len(index["documents"]),
        "eligible_sources": len(scopes),
        "matched_candidates": len(ranked),
        "offset": offset,
        "candidates": [],
        "next_cursor": None,
        "meaning": "采样事实定位候选，不是最终选材或批准切点",
        "provider_calls": 0,
    }
    budget = query.get("max_output_chars", 6000)
    for candidate in ranked[offset : offset + query.get("limit", 6)]:
        trial = {**report, "candidates": [*report["candidates"], candidate]}
        if len(json.dumps(trial, ensure_ascii=False, indent=2)) + 256 > budget:
            break
        report["candidates"].append(candidate)
    consumed = offset + len(report["candidates"])
    if consumed < len(ranked):
        if not report["candidates"]:
            raise Blocked("SEARCH_BUDGET", "返回预算不足一条候选；请调整预算或精简查询", 2)
        report["next_cursor"] = {"offset": consumed, "binding": binding}
    report["content_sha256"] = digest(report)
    if len(json.dumps(report, ensure_ascii=False, indent=2)) + 1 > budget:
        raise Blocked("SEARCH_BUDGET", "检索元数据超出返回预算", 2)
    return report


def _result(root: Path, path: Path, candidate_id: str) -> tuple[dict, dict, dict]:
    result = load(path)
    if result.get("content_sha256") != digest(
        {k: v for k, v in result.items() if k != "content_sha256"}
    ):
        raise Blocked("SEARCH_RESULT_HASH", "候选结果被修改，请重新检索", 6)
    index = _index(root)
    if result["index_sha256"] != index["content_sha256"]:
        raise Blocked("SEARCH_RESULT_STALE", "候选已过期，请重新检索", 6)
    candidate = next((c for c in result["candidates"] if c["candidate_id"] == candidate_id), None)
    if not candidate:
        raise Blocked("SEARCH_CANDIDATE", "候选不属于这份检索结果", 2)
    doc = index["documents"].get(candidate["document_id"])
    a, b = candidate["start_frame"], candidate["end_frame"]
    expected = (
        "candidate_"
        + digest(
            [index["content_sha256"], candidate["document_id"], scope_key(result["scope"]), a, b]
        )[:24]
    )
    if (
        not doc
        or candidate_id != expected
        or candidate["source_id"] != doc["source_id"]
        or candidate["facts_sha256"] != doc["facts_sha256"]
        or not inward_frames(doc["start_sec"], doc["end_sec"])[0]
        <= a
        < b
        <= inward_frames(doc["start_sec"], doc["end_sec"])[1]
    ):
        raise Blocked("SEARCH_CANDIDATE_BINDING", "候选身份、范围或事实绑定不符", 6)
    return result, candidate, doc


def _evidence_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    elif not path.is_relative_to(root):
        # Relocated analysis paths retain their workspace-relative suffix.
        parts = path.parts
        for marker in ("analysis", "reviews", "previews", "deliverables"):
            if marker in parts:
                path = root.joinpath(*parts[parts.index(marker) :])
                break
    path = path.resolve()
    if not path.is_relative_to(root.resolve()):
        raise Blocked("SEARCH_EVIDENCE_PATH", "证据路径超出工作区", 6)
    return path


def show_candidate(root: Path, path: Path, candidate_id: str) -> dict:
    result, candidate, doc = _result(root, path, candidate_id)
    facts = next(
        f
        for f in load(root / INPUTS[0])["materials"]
        if f["source"]["source_id"] == doc["source_id"]
    )
    frames = [
        f
        for f in facts["evidence"].get("frames", [])
        if candidate["start_frame"] <= f["time_sec"] * 30 < candidate["end_frame"]
    ]
    chosen = [frames[n] for n in sorted({0, len(frames) // 2, len(frames) - 1})] if frames else []
    evidence = []
    for frame in chosen:
        p = _evidence_path(root, frame["path"])
        if not p.is_file() or file_hash(p) != frame["sha256"]:
            raise Blocked("SEARCH_EVIDENCE_CHANGED", "候选证据缺失或变化", 6)
        evidence.append({"path": str(p), "time_sec": frame["time_sec"], "sha256": frame["sha256"]})
    proxy = facts["evidence"].get("review", {})
    review = None
    if proxy.get("path"):
        p = _evidence_path(root, proxy["path"])
        if p.is_file() and file_hash(p) == proxy.get("sha256"):
            review = {
                "path": str(p),
                "sha256": proxy["sha256"],
                "time_mapping": proxy.get("time_mapping"),
            }
    return {
        "candidate": candidate,
        "scope": result["scope"],
        "observation": doc["observation"],
        "quality": doc["quality"],
        "frames": evidence,
        "review_proxy": review,
        "seek_sec": candidate["start_frame"] / 30,
        "end_sec": candidate["end_frame"] / 30,
        "precision": "sampled_observation_not_edit_cut",
        "production_approved": False,
    }


def validate_director_plan(root: Path, plan: dict, *, check_media: bool = True) -> dict:
    """Validate a Codex-authored draft; this never creates human approval or reservations."""
    if plan.get("version") != 1 or plan.get("entity") != "codex_director_plan":
        raise Blocked("DIRECTOR_PLAN", "编导计划格式错误", 2)
    script = _evidence_path(root, plan["script_path"])
    if not script.is_file() or file_hash(script) != plan["script_sha256"]:
        raise Blocked("DIRECTOR_SCRIPT_STALE", "脚本已改变，须重新核对选材", 6)
    query = scope_values(plan)
    if plan.get("group_id"):
        query["group_id"] = plan["group_id"]
    settings = _settings(root, query)
    segments = planning_segments(root, settings)
    if "goods_scope" in settings and scope_values(plan) != scope_values(settings):
        raise Blocked("DIRECTOR_SCOPE", "编导计划须完整绑定项目素材范围和口味集合", 6)
    by_source: dict[str, list] = {}
    for segment in segments:
        by_source.setdefault(segment["source_id"], []).append(segment)
    used = [s for r in reservations(root) for s in r["shots"]]
    shots: list[dict] = []
    facts_reused = 0
    supplemental_reviews = 0
    resolved_tasks = []
    facts_by_source = {f["source"]["source_id"]: f for f in load(root / INPUTS[0])["materials"]}
    tasks = plan.get("tasks", [])
    if not tasks or len(tasks) > 30 or len({t["task_id"] for t in tasks}) != len(tasks):
        raise Blocked("DIRECTOR_TASKS", "须有1—30条独立任务", 2)
    for task in tasks:
        safe_id(task["task_id"])
        cursor = 0
        resolved = []
        for shot in task["shots"]:
            response, candidate, _ = _result(
                root, _evidence_path(root, shot["search_result_path"]), shot["candidate_id"]
            )
            if scope_values(response["scope"]) != scope_values(settings):
                raise Blocked("DIRECTOR_SCOPE", "候选与编导计划的商品/品牌不符", 6)
            sid = candidate["source_id"]
            a, b = shot["source_in_frame"], shot["source_out_frame"]
            if any(
                isinstance(v, bool) or not isinstance(v, int)
                for v in (a, b, shot["duration_frames"], shot["timeline_start_frame"])
            ):
                raise Blocked("DIRECTOR_FRAMES", "时间线必须使用整数帧", 2)
            # Observation stages locate content. A fact-grounded cut may cross stages,
            # but must stay inside one currently confirmed product range.
            if not any(
                s["source_in_frame"] <= a < b <= s["source_out_frame"]
                and (
                    "goods_scope" not in settings or s.get("goods_id") == candidate.get("goods_id")
                )
                for s in by_source.get(sid, [])
            ):
                raise Blocked("DIRECTOR_RANGE", "镜头越出当前确认的商品区间", 6)
            if not a < candidate["end_frame"] or not candidate["start_frame"] < b:
                raise Blocked("DIRECTOR_RANGE", "所选镜头与检索定位不相交", 6)
            if shot["timeline_start_frame"] != cursor or b - a != needed_frames(
                shot["duration_frames"], shot["speed"]
            ):
                raise Blocked("DIRECTOR_TIMING", "源区间、播放速度和成片时长不一致", 6)
            if (
                shot.get("transition_out") != {"type": "hard_cut", "frames": 0}
                or shot.get("source_audio_gain") != 0
            ):
                raise Blocked("DIRECTOR_EXECUTION", "当前适配仅支持硬切及原声静音", 3)
            review = shot.get("visual_review") or {}
            if not isinstance(review, dict):
                raise Blocked("DIRECTOR_REVIEW_REQUIRED", "补充核看记录格式错误", 2)
            basis = shot.get("selection_basis")
            if not shot.get("rationale"):
                raise Blocked("DIRECTOR_REASON_REQUIRED", "缺少按脚本选材的理由", 3)
            if basis:
                if (
                    not isinstance(basis, dict)
                    or basis.get("type") != "indexed_facts"
                    or basis.get("facts_sha256") != candidate["facts_sha256"]
                    or basis.get("document_id") != candidate["document_id"]
                    or basis.get("facts_sha256") != facts_by_source[sid]["content_sha256"]
                ):
                    raise Blocked("DIRECTOR_FACTS_STALE", "选材依据不是当前源的有效入库事实", 6)
                facts_reused += 1
            elif not review:
                raise Blocked("DIRECTOR_BASIS_REQUIRED", "缺少有效入库事实依据", 3)
            if review:
                if (
                    review.get("reviewer") != "codex"
                    or not review.get("note")
                    or not review.get("evidence")
                ):
                    raise Blocked("DIRECTOR_REVIEW_REQUIRED", "补充核看记录不完整", 3)
                supplemental_reviews += 1
            for evidence in review.get("evidence", []):
                p = _evidence_path(root, evidence["path"])
                original = facts_by_source[sid]["evidence"]
                refs = [*original.get("frames", []), *original.get("sheets", [])]
                if original.get("review"):
                    refs.append(original["review"])
                if not any(
                    ref.get("path")
                    and ref.get("sha256")
                    and _evidence_path(root, ref["path"]) == p
                    and ref["sha256"] == evidence["sha256"]
                    for ref in refs
                ):
                    raise Blocked("DIRECTOR_EVIDENCE_SOURCE", "核看证据不属于所选源的当前分析", 6)
                if not p.is_file() or file_hash(p) != evidence["sha256"]:
                    raise Blocked("DIRECTOR_EVIDENCE_CHANGED", "核看证据已变化", 6)
            current = {"source_id": sid, "source_in_frame": a, "source_out_frame": b}
            if any(overlap(current, u) for u in used + shots):
                raise Blocked("DIRECTOR_OVERLAP", "所选源区间与本批或已有预占重复", 6)
            shots.append(current)
            resolved.append({**current, "duration_frames": shot["duration_frames"]})
            cursor += shot["duration_frames"]
        if cursor != task["total_frames"] or not task["shots"]:
            raise Blocked("DIRECTOR_TIMING", "镜头总长不等于任务目标", 6)
        resolved_tasks.append({"task_id": task["task_id"], "shots": resolved})
    diversity_report = diversity(resolved_tasks, settings.get("minimum_unique", 0.7))
    if check_media:
        from .executable import verify_media_available

        verify_media_available(root, segments, {s["source_id"] for s in shots})
    return {
        "status": "validated_draft_not_approved",
        "plan_sha256": digest(plan),
        "tasks": len(tasks),
        "shots": len(shots),
        "script_sha256": plan["script_sha256"],
        "diversity": diversity_report,
        "visual_review": "ingest_facts_reused"
        if facts_reused and not supplemental_reviews
        else "agent_declared_not_machine_proof",
        "indexed_facts_shots": facts_reused,
        "supplementally_reviewed_shots": supplemental_reviews,
        "human_approval_created": False,
        "reservations_created": False,
        "provider_calls": 0,
    }
