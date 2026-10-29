import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from mixcut.core import atomic_write, digest, file_hash, load
from mixcut.contracts import content_hash

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_handoff.py"
spec = importlib.util.spec_from_file_location("curator_handoff", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(tmp_path):
    root = tmp_path / "originals"
    root.mkdir()
    original = root / "test.mov"
    original.write_bytes(b"fixture-original-not-real-video")
    atomic_write(
        tmp_path / "config/config.local.json",
        {"source_roots": [{"root_id": "test-root", "path": str(root)}]},
    )
    atomic_write(
        tmp_path / "config/taxonomy.json",
        {
            "axes": {
                "content": [{"id": "detail"}],
                "action": [{"id": "show"}],
                "shot_size": [{"id": "closeup"}],
                "risk": [{"id": "uncertain_product"}],
            }
        },
    )
    source = {
        "source_id": "S1",
        "is_fixture": False,
        "file_sha256": file_hash(original),
        "duration_sec": 10,
        "locations": [{"root_id": "test-root", "relative_path": original.name}],
    }
    atomic_write(tmp_path / "catalog/sources.json", [source])
    clip = {
        "clip_id": "C1",
        "source_id": "S1",
        "revision": 1,
        "review_status": "pending",
        "rights_confirmed": False,
        "tags": {"risk": []},
    }
    clip["content_sha256"] = content_hash(clip)
    atomic_write(tmp_path / "catalog/clips.json", [clip])
    evidence = {
        "clip_id": "C1",
        "source_sha256": source["file_sha256"],
        "sample_times_sec": [0, 3, 6],
        "candidate_sample_times_sec": [2, 3, 4],
    }
    for name, ext in [("proxy", "mp4"), ("sheet", "jpg"), ("candidate_sheet", "jpg")]:
        path = tmp_path / "analysis" / f"{name}.{ext}"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(("fixture-" + name).encode())
        evidence[name + "_path"] = str(path)
        evidence[name + "_sha256"] = file_hash(path)
    record = {
        "clip_id": "C1",
        "is_fixture": False,
        "analysis_status": "visually_analyzed",
        "source_sha256": source["file_sha256"],
        "source_duration_sec": 10,
        "catalog_revision_at_analysis": 1,
        "catalog_content_sha256_at_analysis": clip["content_sha256"],
        "production_approved": False,
        "proposal": {
            "description": "Fixture observation, not a real analysis",
            "tags": {
                "content": ["detail"],
                "action": [],
                "shot_size": ["closeup"],
                "risk": ["uncertain_product"],
            },
            "start_sec": 2,
            "end_sec": 5,
            "evidence_times": [2, 3, 4],
            "product_uncertain": True,
        },
        "semantic_tags": {"actions": ["closing"]},
        "evidence": evidence,
    }
    path = tmp_path / "analysis/proposals.json"
    save(path, record)
    return path, record, original


def save(path, record):
    record["analysis_sha256"] = digest({k: v for k, v in record.items() if k != "analysis_sha256"})
    atomic_write(path, {"records": [record]})


def codes(report):
    return {row["code"] for row in report["issues"]}


def test_readonly_gate_does_not_approve_or_mutate_catalog(tmp_path):
    path, record, _ = fixture(tmp_path)
    prior = (tmp_path / "catalog/clips.json").read_bytes()
    report = module.audit(tmp_path, "test-root", path)
    assert report["status"] == "local_checks_passed"
    assert report["production_approved"] is False and report["network_calls"] == 0
    assert (tmp_path / "catalog/clips.json").read_bytes() == prior
    assert "SEMANTIC_ACTIONS_NOT_MAPPED" in {x["code"] for x in report["warnings"]}
    assert record["proposal"]["product_uncertain"] is True


def test_missing_mount_is_blocked_not_empty_success(tmp_path):
    path, _, original = fixture(tmp_path)
    original.unlink()
    original.parent.rmdir()
    report = module.audit(tmp_path, "test-root", path)
    assert report["status"] == "blocked"
    assert "ROOT_OR_INPUT_UNAVAILABLE" in codes(report)
    assert load(tmp_path / "catalog/clips.json")[0]["clip_id"] == "C1"


def test_changed_original_invalidates_cached_analysis(tmp_path):
    path, _, original = fixture(tmp_path)
    original.write_bytes(b"changed")
    assert "FILE_CHANGED" in codes(module.audit(tmp_path, "test-root", path))


def test_evidence_cannot_be_replaced_under_the_same_name(tmp_path):
    path, record, _ = fixture(tmp_path)
    Path(record["evidence"]["sheet_path"]).write_bytes(b"wrong sheet")
    assert "EVIDENCE_HASH_CHANGED" in codes(module.audit(tmp_path, "test-root", path))


def test_stale_catalog_requires_exact_adoption_chain(tmp_path):
    path, record, _ = fixture(tmp_path)
    cpath = tmp_path / "catalog/clips.json"
    clip = load(cpath)[0]
    clip.update(revision=3, rights_confirmed=True, review_status="approved")
    clip["content_sha256"] = content_hash(clip)
    atomic_write(cpath, [clip])
    assert "PROPOSAL_STALE" in codes(module.audit(tmp_path, "test-root", path))
    atomic_write(
        tmp_path / "catalog/material_confirmations.json",
        {
            "records": [
                {
                    "clip_id": "C1",
                    "analysis_sha256": record["analysis_sha256"],
                    "confirmed_catalog_revision": 3,
                    "confirmed_catalog_content_sha256": clip["content_sha256"],
                }
            ]
        },
    )
    assert module.audit(tmp_path, "test-root", path)["status"] == "local_checks_passed"
    assert load(cpath)[0]["tags"]["risk"] == []


def test_unknown_labels_cannot_enter_fixed_taxonomy(tmp_path):
    path, record, _ = fixture(tmp_path)
    record["proposal"]["tags"]["action"] = ["closing"]
    save(path, record)
    assert "TAG_OUT_OF_VOCAB" in codes(module.audit(tmp_path, "test-root", path))


def test_ffprobe_only_is_not_visual_completion(tmp_path):
    path, record, _ = fixture(tmp_path)
    record["analysis_status"] = "probe_complete"
    save(path, record)
    assert "VISUAL_ANALYSIS_PENDING" in codes(module.audit(tmp_path, "test-root", path))


def test_symlink_cannot_escape_authorized_root(tmp_path):
    path, _, original = fixture(tmp_path)
    outside = tmp_path / "outside.mov"
    original.rename(outside)
    original.symlink_to(outside)
    assert "INPUT_OUTSIDE_ROOT" in codes(module.audit(tmp_path, "test-root", path))


def test_existing_report_is_not_overwritten(tmp_path):
    path, _, _ = fixture(tmp_path)
    report = tmp_path / "existing.json"
    report.write_bytes(b"keep-existing-evidence")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workspace",
            str(tmp_path),
            "--root-id",
            "test-root",
            "--proposals",
            str(path),
            "--report",
            str(report),
        ],
        capture_output=True,
    )
    assert result.returncode == 2
    assert report.read_bytes() == b"keep-existing-evidence"


@pytest.mark.parametrize("field", ["sample_times_sec", "candidate_sample_times_sec"])
def test_invalid_evidence_times_block_handoff(tmp_path, field):
    path, record, _ = fixture(tmp_path)
    record["evidence"][field] = [11]
    save(path, record)
    assert "EVIDENCE_TIMES" in codes(module.audit(tmp_path, "test-root", path))
