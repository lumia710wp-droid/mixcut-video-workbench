"""Actual one-file slots envelope and expired-slot resume semantics; no network."""

import json
import time
from types import SimpleNamespace
import pytest
from mixcut.core import Blocked
from mixcut.director_native import Native, upload_slot
from mixcut.director_runner import active_upload_slot


def packet(path, expiry=None):
    return {
        "projectId": "p",
        "slots": [
            {
                "filename": path.name,
                "size": path.stat().st_size,
                "assetType": "video",
                "uploadUrl": "http://127.0.0.1:5199/upload/test",
                "headers": {},
                "method": "POST",
                "expiresAt": expiry or time.time() * 1000 + 300000,
            }
        ],
    }


def test_upload_one_slot_envelope_is_sent_once_and_completed_receipt_survives_expiry(
    tmp_path, monkeypatch
):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    calls = []

    class Connection:
        def __init__(self, *args, **kw):
            pass

        def request(self, method, route, body, headers):
            calls.append((method, route, headers["Content-Length"], body.read()))

        def getresponse(self):
            return SimpleNamespace(
                status=200,
                read=lambda: json.dumps({"receipt": "known", "assetType": "video"}).encode(),
            )

        def close(self):
            pass

    monkeypatch.setattr("mixcut.director_native.http.client.HTTPConnection", Connection)
    n = Native(tmp_path / "run", lambda: None)
    slot = packet(path)
    assert n.upload("import_000_upload", path, slot)["receipt"] == "known"
    monkeypatch.setattr("mixcut.director_native.time.time", lambda: 10**15)
    assert n.upload("import_000_upload", path, slot)["receipt"] == "known"
    assert len(calls) == 1 and calls[0][0] == "POST" and calls[0][2] == "7"


@pytest.mark.parametrize("damage", ["name", "size", "multiple"])
def test_wrong_slot_binding_rejects_before_upload(tmp_path, damage):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    value = packet(path)
    if damage == "name":
        value["slots"][0]["filename"] = "other.mp4"
    if damage == "size":
        value["slots"][0]["size"] = 8
    if damage == "multiple":
        value["slots"].append(dict(value["slots"][0]))
    with pytest.raises(Blocked) as error:
        upload_slot(value, path)
    assert error.value.code == "RUNNER_UPLOAD_SLOT"


class Journal:
    def __init__(self, new, status=None):
        self.new = new
        self.status = status
        self.saved = {}
        self.sent = []

    def completed(self, label):
        return self.saved.get(label)

    def receipt_status(self, label):
        return self.status

    def effect(self, label, name, args):
        self.sent.append(label)
        self.saved[label] = self.new
        return self.new


def test_acknowledged_renewal_is_reused_after_upload_completes(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    old = packet(path, 1)
    new = packet(path)
    native = Journal(new)
    assert active_upload_slot(native, "import_000", path, old, {"known": "same"}) == new
    native.status = "completed"
    assert active_upload_slot(native, "import_000", path, old, {"known": "same"}) == new
    assert len(native.sent) == 1


def test_unknown_upload_never_allocates_a_replacement_slot(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    old = packet(path, 1)
    native = Journal(packet(path), "unknown")
    assert active_upload_slot(native, "import_000", path, old, {}) == old
    assert not native.sent
