import json
from pathlib import Path
import pytest

from mixcut.core import Blocked, atomic_write, load
from mixcut.deepseek import generate_outline
from mixcut.feishu import TASK_FIELDS
from mixcut.script_button import default_task_id, process_once
from test_deepseek import model_config, provider_response


class Remote:
    def __init__(self, fields):
        self.fields = fields
        self.pending = None
        self.writes = 0

    def __call__(self, argv):
        if argv[0] == "+record-batch-update":
            self.pending = json.loads(argv[argv.index("--json") + 1])["update_records"]["rec1"]
            self.writes += 1
            return {"data": {}}
        return {
            "data": {
                "record_id_list": ["rec1"],
                "field_id_list": list(self.fields),
                "data": [list(self.fields.values())],
                "has_more": False,
            }
        }

    def visible(self):
        self.fields.update(self.pending or {})
        self.pending = None


def setup(tmp_path: Path, **changes):
    keys = {k: k for k, _, _, _ in TASK_FIELDS}
    atomic_write(
        tmp_path / "config/feishu_mapping.local.json",
        {"base_token": "test", "tables": {"tasks": {"table_id": "tbltest", "field_ids": keys}}},
    )
    atomic_write(tmp_path / "config/deepseek.local.json", model_config())
    return Remote(
        {
            "task_id": None,
            "creative_keywords": "手工现包",
            "seed_sentences": None,
            "target_duration_sec": None,
            "generation_request": "queued",
            **changes,
        }
    )


def test_button_defaults_single_call_readback_and_double_click(tmp_path):
    remote = setup(tmp_path)
    calls = []

    def generate(w, config, task_id, request_loader):
        def transport(*args):
            calls.append(args[0])
            return provider_response(request_loader())

        return generate_outline(
            w,
            config,
            task_id,
            request_loader=request_loader,
            transport=transport,
            environ={"TEST_DEEPSEEK_KEY": "test"},
        )

    report = process_once(tmp_path, call=remote, generator=generate)
    assert len(calls) == 1 and len(report["handled"]) == 1
    assert remote.pending["task_id"] == default_task_id("rec1")
    assert "旁白" in remote.pending["script_preview"]
    assert remote.pending["script_review_status"] == "pending"
    assert remote.pending["generation_request"] == "completed"
    assert "target_duration_sec" not in remote.pending  # Optional human input preserved.
    # Acknowledgement is not enough, and old reads must never repeat the write/model call.
    assert process_once(tmp_path, call=remote, generator=generate)["pending_sync"]
    assert len(calls) == 1 and remote.writes == 1
    remote.visible()
    process_once(tmp_path, call=remote, generator=generate)
    assert all(load(p)["status"] == "synced" for p in (tmp_path / "ledger/outbox").glob("*.json"))
    # A second click on identical inputs reuses the saved response.
    remote.fields["generation_request"] = "queued"
    process_once(tmp_path, call=remote, generator=generate)
    assert len(calls) == 1


def test_text_without_click_never_generates(tmp_path):
    remote = setup(tmp_path, generation_request=None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Model called without a click")

    assert process_once(tmp_path, call=remote, generator=forbidden)["queued"] == 0
    assert remote.writes == 0


def test_empty_input_has_readable_error_and_no_call(tmp_path):
    remote = setup(tmp_path, creative_keywords=" ", seed_sentences=None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Empty input called model")

    report = process_once(tmp_path, call=remote, generator=forbidden)
    assert report["errors"][0]["code"] == "SCRIPT_INPUT_MISSING"
    assert remote.pending["generation_request"] == "blocked"
    assert "至少填写一项" in remote.pending["error_summary"]


def test_model_timeout_is_not_retried_on_later_tick(tmp_path):
    remote = setup(tmp_path)
    calls = []

    def generate(w, config, task_id, request_loader):
        def transport(*args):
            calls.append(True)
            raise TimeoutError()

        return generate_outline(
            w,
            config,
            task_id,
            request_loader=request_loader,
            transport=transport,
            environ={"TEST_DEEPSEEK_KEY": "test"},
        )

    process_once(tmp_path, call=remote, generator=generate)
    assert len(calls) == 1
    assert "MODEL_OUTCOME_UNKNOWN" in remote.pending["error_summary"]
    process_once(tmp_path, call=remote, generator=generate)
    assert len(calls) == 1 and remote.writes == 1


def test_changed_input_during_call_cannot_be_saved_or_approved(tmp_path):
    remote = setup(tmp_path)

    def generate(w, config, task_id, request_loader):
        def transport(*args):
            response = provider_response(request_loader())
            remote.fields["creative_keywords"] = "已改主题"
            return response

        return generate_outline(
            w,
            config,
            task_id,
            request_loader=request_loader,
            transport=transport,
            environ={"TEST_DEEPSEEK_KEY": "test"},
        )

    report = process_once(tmp_path, call=remote, generator=generate)
    assert report["errors"][0]["code"] == "SCRIPT_INPUT_STALE"
    assert not list((tmp_path / "jobs").glob("*/r*/script_outline.json"))
    assert "script_review_action" not in remote.pending


def test_crash_after_model_saved_reuses_outbox_and_result(tmp_path):
    remote = setup(tmp_path)

    def generate(w, config, task_id, request_loader):
        result = generate_outline(
            w,
            config,
            task_id,
            request_loader=request_loader,
            transport=lambda *a: provider_response(request_loader()),
            environ={"TEST_DEEPSEEK_KEY": "test"},
        )
        raise Blocked("TEST_INTERRUPTION", result["status"], 5)

    process_once(tmp_path, call=remote, generator=generate)
    remote.visible()
    process_once(tmp_path, call=remote, generator=generate)  # reconcile blocked patch
    remote.fields["generation_request"] = "queued"

    def recover(w, config, task_id, request_loader):
        return generate_outline(
            w, config, task_id, request_loader=request_loader, environ={"TEST_DEEPSEEK_KEY": "test"}
        )

    process_once(tmp_path, call=remote, generator=recover)
    assert "script_preview" in remote.pending
    assert remote.pending["generation_request"] == "completed"


def test_explicit_web_request_does_not_falsify_remote_readback(tmp_path):
    remote = setup(tmp_path, task_id="TASK_001", generation_request=None)

    def generate(w, config, task_id, request_loader):
        return generate_outline(
            w,
            config,
            task_id,
            request_loader=request_loader,
            transport=lambda *args: provider_response(request_loader()),
            environ={"TEST_DEEPSEEK_KEY": "test"},
        )

    result = process_once(tmp_path, call=remote, generator=generate, requested_task_id="TASK_001")
    assert len(result["handled"]) == 1 and remote.fields["generation_request"] is None
    remote.visible()
    assert remote.fields["generation_request"] == "completed"
    process_once(
        tmp_path, call=remote, generator=lambda *args, **kwargs: pytest.fail("duplicate call")
    )
    assert all(
        load(p)["status"] == "synced" for p in (tmp_path / "ledger/script_button").glob("*.json")
    )
