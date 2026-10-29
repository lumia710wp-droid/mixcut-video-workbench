from __future__ import annotations
import subprocess
import json
import time
from fractions import Fraction
from pathlib import Path
from .core import Blocked, atomic_write, file_hash, load, now, resolve_media, writer_lock
from .contracts import validate_record

EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def probe(path: Path, ffprobe: str) -> dict:
    p = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if p.returncode:
        raise Blocked("CORRUPT_MEDIA", p.stderr[-600:], 4)
    d = json.loads(p.stdout)
    if not any(s["codec_type"] == "video" for s in d["streams"]):
        raise Blocked("CORRUPT_MEDIA", "No video stream", 4)
    return d


def scan(workspace: Path, config: dict, root_id: str, limit: int, fixture: bool = False) -> dict:
    roots = config["source_roots"]
    roots_by_id = {r["root_id"]: Path(r["path"]).resolve() for r in roots}
    if root_id not in roots_by_id:
        raise Blocked("ROOT_UNKNOWN", "Root is not authorized in config")
    root = roots_by_id[root_id]
    if workspace.is_relative_to(root) or root.is_relative_to(workspace / "output"):
        raise Blocked("INPUT_OUTPUT_OVERLAP", "Input root includes project outputs")
    files = sorted(
        p for p in root.rglob("*") if p.suffix.lower() in EXTENSIONS and not p.name.startswith("._")
    )
    if limit < 1 or limit > config["ingest"]["initial_file_limit"]:
        raise Blocked("LIMIT", "Scan limit exceeds configured scope", 2)
    files = files[:limit]
    safe = []
    errors = []
    for path in files:
        try:
            safe.append(resolve_media(roots, root_id, str(path.relative_to(root))))
        except (Blocked, OSError) as e:
            errors.append({"path": str(path), "error": str(e)})
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in safe}
    stable = set(safe)
    for _ in range(max(2, config["ingest"]["stable_samples"]) - 1):
        time.sleep(max(0.01, config["ingest"]["stable_interval_sec"]))
        for p in list(stable):
            try:
                if (p.stat().st_size, p.stat().st_mtime_ns) != before[p]:
                    stable.remove(p)
            except OSError:
                stable.remove(p)
    with writer_lock(workspace):
        target = workspace / "catalog/sources.json"
        records = load(target) if target.exists() else []
        by_hash = {r["file_sha256"]: r for r in records}
        added = []
        for p in safe:
            if p not in stable:
                errors.append({"path": str(p), "code": "FILE_UNSTABLE"})
                continue
            try:
                info = probe(p, config["paths"]["ffprobe"])
                sha = file_hash(p)
                if (p.stat().st_size, p.stat().st_mtime_ns) != before[p]:
                    raise Blocked("FILE_CHANGED", "File changed while probing/hashing")
                v = next(s for s in info["streams"] if s["codec_type"] == "video")
                fps = Fraction(v.get("avg_frame_rate", "0/1"))
                loc = {"root_id": root_id, "relative_path": str(p.relative_to(root))}
                if sha in by_hash:
                    if loc not in by_hash[sha]["locations"]:
                        by_hash[sha]["locations"].append(loc)
                    continue
                r = {
                    "schema_version": "1.0.0",
                    "entity": "source",
                    "is_fixture": fixture,
                    "source_id": "s_" + sha,
                    "file_sha256": sha,
                    "locations": [loc],
                    "duration_sec": float(v.get("duration") or info["format"]["duration"]),
                    "width": v["width"],
                    "height": v["height"],
                    "fps_num": fps.numerator,
                    "fps_den": fps.denominator,
                    "rotation_deg": int(
                        next(
                            (
                                x.get("rotation", 0)
                                for x in v.get("side_data_list", [])
                                if "rotation" in x
                            ),
                            0,
                        )
                    )
                    % 360,
                    "vfr_status": "unknown",
                    "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
                    "probe_status": "ok",
                }
                validate_record(r, workspace, production=not fixture)
                records.append(r)
                by_hash[sha] = r
                added.append(r["source_id"])
                atomic_write(workspace / "catalog/probes" / f"{r['source_id']}.json", info)
            except (Blocked, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as e:
                errors.append(
                    {"path": str(p), "code": getattr(e, "code", "PROBE_FAILED"), "error": str(e)}
                )
        atomic_write(target, records)
        report = {
            "at": now(),
            "root_id": root_id,
            "discovered_bounded": len(files),
            "added": added,
            "catalog_total": len(records),
            "errors": errors,
            "is_fixture": fixture,
        }
        atomic_write(workspace / "reports/scan.json", report)
        return report
