import importlib.util
import json
from pathlib import Path

import pytest


def script_module(name):
    source = Path(__file__).parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage = script_module("openchatcut_stage_build")
export = script_module("openchatcut_finalize_build")


def test_review_wait_keeps_pending_state_until_live_application(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"status": "awaiting_review"}))
    events = []

    def wait():
        assert json.loads(path.read_text())["status"] == "awaiting_review"
        events.append("human_ui_application")

    def tool(name, args):
        assert events == ["human_ui_application"]
        assert name == "get_edit_session" and args["editSessionId"] == "S1"
        return [{"status": "applied"}]

    stage.wait_for_review_application(tool, "P1", "S1", (path,), wait)
    assert json.loads(path.read_text())["status"] == "applied"


@pytest.mark.parametrize("status", ["awaiting_review", "stale", "rejected", None])
def test_review_wait_does_not_record_unapplied_proposal(tmp_path, status):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"status": "awaiting_review"}))
    with pytest.raises(RuntimeError, match="no export is allowed"):
        stage.wait_for_review_application(
            lambda *_: [{"status": status}], "P1", "S1", (path,), lambda: None,
        )
    assert json.loads(path.read_text())["status"] == "awaiting_review"


@pytest.mark.parametrize("status", ["awaiting_review", "stale", None])
def test_export_requires_matching_live_applied_proposal(status):
    rows = [{"editSessionId": "OTHER", "status": "applied"},
            {"editSessionId": "S1", "status": status}]
    with pytest.raises(RuntimeError, match="no export is allowed"):
        export.verified_proposal_status(rows, "S1")


def test_export_accepts_exact_applied_session():
    assert export.verified_proposal_status(
        [{"result": [{"editSessionId": "S1", "status": "applied"}]}], "S1"
    ) == "applied_verified_in_editor_session_list"


def test_unsupported_inspection_is_not_a_success_or_an_implicit_retry():
    assert not export.inspection_available({"count": 0, "results": [], "activatedTools": []})
    assert not export.inspection_available({"activatedTools": ["submit_export"]})
    assert export.inspection_available({"activatedTools": ["inspect_render"]})


def test_binary_media_is_removed_before_receipts_or_output():
    receipt = {"type": "image", "mimeType": "image/png", "data": "synthetic-media-content"}
    cleaned = export.sanitize(receipt)
    assert "data" not in cleaned
    assert "synthetic-media-content" not in json.dumps(cleaned)
    nested = export.sanitize({"content": [{"type": "text", "text": json.dumps({
        "receipt": "synthetic-secret", "frame": receipt,
    })}]})
    assert "synthetic-secret" not in json.dumps(nested)
    assert "synthetic-media-content" not in json.dumps(nested)


def test_schema_type_object_does_not_break_receipt_sanitization():
    schema = {"type": {"enum": ["video", "audio"]}, "nested": {"receipt": "private"}}
    assert export.sanitize(schema) == {
        "type": {"enum": ["video", "audio"]}, "nested": {"receipt": "[redacted]"}
    }
