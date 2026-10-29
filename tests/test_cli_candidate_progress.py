import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "sync_cli_candidates", Path(__file__).resolve().parents[1] / "tools/sync_cli_candidates.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sync_progress = module.sync_progress


class Bench:
    def __init__(self, status):
        self.fields = {"director_run_status": status}
        self.calls = []

    def task(self, task):
        return {"record_id": "record", "fields": self.fields}

    def patch(self, record_id, patch):
        self.calls.append(patch)
        self.fields.update(patch)

    def verify_patch(self, task, patch):
        assert all(self.fields.get(k) == v for k, v in patch.items())


def test_candidate_sync_does_not_revoke_existing_approval():
    for status in ("approved_by_user_chat", "delivered_cli"):
        bench = Bench(status)
        sync_progress(bench, "VIDEO_A", {"sha256": "hash"})
        assert not bench.calls
        assert bench.fields["director_run_status"] == status


def test_new_candidate_updates_waiting_status_idempotently():
    bench = Bench("blocked")
    sync_progress(bench, "VIDEO_A", {"sha256": "hash"})
    sync_progress(bench, "VIDEO_A", {"sha256": "hash"})
    assert len(bench.calls) == 1
    assert bench.fields["director_run_status"] == "awaiting_content_review"
