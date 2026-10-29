from mixcut.analyze import g1_accepted
from mixcut.core import atomic_write, file_hash


def accepted_fixture(root):
    job = root / "jobs/G1/r1/build"
    job.mkdir(parents=True)
    (job / "candidate.mp4").write_bytes(b"test evidence only, no real media claim")
    sha = file_hash(job / "candidate.mp4")
    atomic_write(root / "reports/g1/preparation.json", {"job_dir": str(job)})
    atomic_write(
        job / "state.json",
        {
            "render_status": "succeeded",
            "qc_status": "passed",
            "human_review": "approved",
            "build_id": "build",
        },
    )
    atomic_write(
        job / "G1_QC_REPORT.json",
        {"technical_status": "passed", "technical": {"output_sha256": sha}},
    )
    atomic_write(
        job / "human_review.json",
        {
            "action": "approve",
            "actor": "fixture-reviewer",
            "build_id": "build",
            "output_sha256": sha,
        },
    )
    atomic_write(job / "edit_plan.json", {"build_id": "build"})
    return job


def test_gate_requires_complete_evidence(tmp_path):
    assert not g1_accepted(tmp_path)
    job = accepted_fixture(tmp_path)
    assert g1_accepted(tmp_path)
    (job / "human_review.json").unlink()
    assert not g1_accepted(tmp_path)


def test_gate_rejects_changed_media(tmp_path):
    job = accepted_fixture(tmp_path)
    (job / "candidate.mp4").write_bytes(b"changed")
    assert not g1_accepted(tmp_path)


def test_gate_rejects_other_build_and_outside_path(tmp_path):
    job = accepted_fixture(tmp_path)
    atomic_write(job / "edit_plan.json", {"build_id": "other"})
    assert not g1_accepted(tmp_path)
    atomic_write(tmp_path / "reports/g1/preparation.json", {"job_dir": str(tmp_path.parent)})
    assert not g1_accepted(tmp_path)
