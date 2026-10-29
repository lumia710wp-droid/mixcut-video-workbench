#!/usr/bin/env python3
"""Mixcut artifact gate. Read only, except for one new local report."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


def audit(workspace: Path, root_id: str, proposal_path: Path) -> dict:
    # Existing project owns the data contract; this script does not duplicate it.
    sys.path.insert(0, str(workspace / "src"))
    from mixcut.analyze import validate_proposal
    from mixcut.contracts import content_hash
    from mixcut.core import Blocked, digest, file_hash, load, resolve_media

    issues: list[dict] = []
    warnings: list[dict] = []
    rows: list[dict] = []
    result = {
        "status": "blocked",
        "scope": "local_structure_and_evidence_only",
        "production_approved": False,
        "network_calls": 0,
        "rows": rows,
        "issues": issues,
        "warnings": warnings,
        "not_verified": [
            "actual visual inspection",
            "semantic truth",
            "human decision authenticity",
            "live Feishu readback",
        ],
    }

    def fail(code: str, clip: str | None = None):
        issues.append({"code": code, "clip_id": clip})

    try:
        path = proposal_path.resolve(strict=True)
        if not path.is_relative_to(workspace):
            raise Blocked("PROPOSAL_PATH", "Proposal must belong to this workspace")
        config = load(workspace / "config/config.local.json")
        roots = [r for r in config["source_roots"] if r["root_id"] == root_id]
        if len(roots) != 1:
            raise Blocked("ROOT_UNKNOWN", "Expected one authorized root")
        root = Path(roots[0]["path"]).resolve(strict=True)
        if not root.is_dir():
            raise Blocked("ROOT_UNAVAILABLE", "Source root is not a directory")
        # Directory enumeration establishes that the root is actually readable.
        next(root.iterdir(), None)
    except (Blocked, OSError, KeyError, ValueError) as exc:
        fail(getattr(exc, "code", "ROOT_OR_INPUT_UNAVAILABLE"))
        return result

    def index(records: list[dict], key: str) -> dict:
        out = {}
        for record in records:
            value = record[key]
            if value in out:
                raise Blocked("DUPLICATE_ID", key)
            out[value] = record
        return out

    try:
        sources = index(load(workspace / "catalog/sources.json"), "source_id")
        clips = index(load(workspace / "catalog/clips.json"), "clip_id")
        taxonomy = load(workspace / "config/taxonomy.json")
        proposal_set = load(path)
        records = proposal_set["records"]
        if not isinstance(records, list) or not records:
            raise Blocked("NO_ANALYSIS_RECORDS", "No visual records")
        index(records, "clip_id")
        bridges = []
        for filename, prefix in [
            ("ai_proposal_adoptions.json", "adopted"),
            ("material_confirmations.json", "confirmed"),
        ]:
            bridge_path = workspace / "catalog" / filename
            if bridge_path.exists():
                bridges.append((prefix, index(load(bridge_path)["records"], "clip_id")))
    except (Blocked, OSError, KeyError, ValueError, TypeError) as exc:
        fail(getattr(exc, "code", "CATALOG_INVALID"))
        return result

    source_hashes: dict[str, str] = {}
    for record in records:
        cid = record.get("clip_id")
        before = len(issues)
        try:
            clip = clips[cid]
            source = sources[clip["source_id"]]
            if record.get("is_fixture") is not False or source.get("is_fixture") is not False:
                raise Blocked("FIXTURE_BLOCKED", "Not real material")
            if record.get("analysis_status") != "visually_analyzed":
                raise Blocked("VISUAL_ANALYSIS_PENDING", "Only technical preparation is recorded")
            analysis_hash = digest({k: v for k, v in record.items() if k != "analysis_sha256"})
            if analysis_hash != record.get("analysis_sha256"):
                raise Blocked("ANALYSIS_HASH_CHANGED", "Analysis bytes changed")
            if clip.get("content_sha256") != content_hash(clip):
                raise Blocked("CATALOG_HASH_CHANGED", "Effective content changed")
            bound = (
                record["catalog_revision_at_analysis"] == clip["revision"]
                and record["catalog_content_sha256_at_analysis"] == clip["content_sha256"]
            )
            for prefix, bridge in bridges:
                receipt = bridge.get(cid, {})
                bound = bound or (
                    receipt.get("analysis_sha256") == analysis_hash
                    and receipt.get(prefix + "_catalog_revision") == clip["revision"]
                    and receipt.get(prefix + "_catalog_content_sha256") == clip["content_sha256"]
                )
            if not bound:
                raise Blocked("PROPOSAL_STALE", "No current revision or adoption binding")
            if record.get("production_approved") is not False:
                raise Blocked("PROPOSAL_APPROVAL_MIXED", "Keep analysis separate from approval")
            duration = source["duration_sec"]
            if not isinstance(duration, (int, float)) or not math.isfinite(duration):
                raise Blocked("SOURCE_DURATION_INVALID", "Nonfinite source duration")
            validate_proposal(record["proposal"], taxonomy, duration)
            if record["source_duration_sec"] != duration:
                raise Blocked("SOURCE_DURATION_CHANGED", "Analysis duration is stale")
            if record["source_sha256"] != source["file_sha256"]:
                raise Blocked("SOURCE_IDENTITY_CHANGED", "Analysis belongs to another source")
            if source["source_id"] not in source_hashes:
                locations = [loc for loc in source["locations"] if loc["root_id"] == root_id]
                if not locations:
                    raise Blocked("SOURCE_OUTSIDE_SCOPE", "No location within selected root")
                errors = []
                for loc in locations:
                    try:
                        original = resolve_media(
                            config["source_roots"], root_id, loc["relative_path"]
                        )
                        actual_hash = file_hash(original)
                        if actual_hash != source["file_sha256"]:
                            raise Blocked("FILE_CHANGED", "Source bytes differ")
                        source_hashes[source["source_id"]] = actual_hash
                        break
                    except (Blocked, OSError) as exc:
                        errors.append(getattr(exc, "code", "SOURCE_UNAVAILABLE"))
                else:
                    raise Blocked(errors[0], "No verified source location")
            evidence = record["evidence"]
            if evidence["clip_id"] != cid or evidence["source_sha256"] != record["source_sha256"]:
                raise Blocked("EVIDENCE_IDENTITY", "Evidence is for a different source")
            for key in ["proxy", "sheet", "candidate_sheet"]:
                raw = Path(evidence[key + "_path"])
                if not raw.is_absolute():
                    raise Blocked("EVIDENCE_PATH", "Absolute local path required")
                media = raw.resolve(strict=True)
                if not media.is_relative_to(workspace):
                    raise Blocked("EVIDENCE_PATH", "Evidence must belong to this workspace")
                if file_hash(media) != evidence[key + "_sha256"]:
                    raise Blocked("EVIDENCE_HASH_CHANGED", key)
            for key in ["sample_times_sec", "candidate_sample_times_sec"]:
                times = evidence[key]
                if (
                    not isinstance(times, list)
                    or not times
                    or any(
                        type(t) not in (int, float) or not math.isfinite(t) or not 0 <= t < duration
                        for t in times
                    )
                ):
                    raise Blocked("EVIDENCE_TIMES", "Missing or invalid source timestamps")
            if (
                record.get("semantic_tags", {}).get("actions")
                and not record["proposal"]["tags"]["action"]
            ):
                warnings.append({"code": "SEMANTIC_ACTIONS_NOT_MAPPED", "clip_id": cid})
            if record["proposal"]["product_uncertain"]:
                warnings.append({"code": "VISUAL_PRODUCT_UNCERTAIN", "clip_id": cid})
        except (Blocked, OSError, KeyError, ValueError, TypeError) as exc:
            fail(getattr(exc, "code", "MATERIAL_EVIDENCE_INVALID"), cid)
        rows.append(
            {"clip_id": cid, "local_evidence": "passed" if len(issues) == before else "blocked"}
        )
    if not issues:
        result["status"] = "local_checks_passed"
    result["checked_records"] = len(rows)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--proposals", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    target = args.report.resolve()
    if not target.is_relative_to(workspace) or target.exists():
        parser.error("Report must be a new local file within workspace")
    result = audit(workspace, args.root_id, args.proposals)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects reports from concurrent overwrite.
    with target.open("x") as report:
        json.dump(result, report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        report.write("\n")
    print(
        json.dumps(
            {
                "status": result["status"],
                "report": str(target),
                "issues": len(result["issues"]),
                "network_calls": 0,
            }
        )
    )
    return 0 if result["status"] == "local_checks_passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
