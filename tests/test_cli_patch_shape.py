import json
import subprocess
import pytest
from mixcut.core import Blocked
from mixcut.feishu import invoke


def test_native_queue_patch_uses_current_cli_shape(tmp_path, monkeypatch):
    sent = []

    def run(argv, **kwargs):
        sent.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"ok":true,"data":{}}')

    monkeypatch.setattr("mixcut.feishu.subprocess.run", run)
    invoke(
        [
            "+record-batch-update",
            "--table-id",
            "tbl",
            "--json",
            json.dumps({"update_records": {"rec": {"status": "approved"}}}),
        ],
        tmp_path,
    )
    body = json.loads(sent[0][sent[0].index("--json") + 1])
    assert body == {"record_id_list": ["rec"], "patch": {"status": "approved"}}


def test_distinct_row_patches_never_broadcast(tmp_path, monkeypatch):
    monkeypatch.setattr("mixcut.feishu.subprocess.run", lambda *a, **k: pytest.fail("sent"))
    with pytest.raises(Blocked, match="单条串行"):
        invoke(
            [
                "+record-batch-update",
                "--json",
                json.dumps({"update_records": {"rec1": {"status": "a"}, "rec2": {"status": "b"}}}),
            ],
            tmp_path,
        )
