"""HTTP checks for opt-in iframe sessions; no platform or model calls."""

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from mixcut import workbench


@contextmanager
def running(tmp_path, monkeypatch, embedded):
    public = tmp_path / "web/feishu-workbench"
    public.mkdir(parents=True, exist_ok=True)
    (public / "index.html").write_text("test")
    media = tmp_path / "final.mp4"
    media.write_bytes(b"test-video-bytes")

    class App:
        mapping = {"url": "https://example.feishu.cn/base/test"}

        def __init__(self, root):
            pass

        def state(self):
            return {"tasks": [{"finalUrl": "/api/media/task/build/final"}], "materials": []}

        def task(self, task_id):
            return {"fields": {"build_id": "build"}}

        def media_path(self, fields, kind):
            return media

    monkeypatch.setattr(workbench, "Workbench", App)
    server = workbench.create_server(tmp_path, 0, embedded=embedded)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, headers=None, body=None):
        req = urllib.request.Request(
            base + path,
            headers=headers or {},
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as response:
            return response.code, response.headers, response.read()

    try:
        yield base, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_regular_entry_keeps_cookie_and_frame_policy(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch, False) as (_, get):
        status, headers, body = get("/api/session")
        session = json.loads(body)
        assert status == 200 and "sessionToken" not in session
        assert "SameSite=Strict" in headers["Set-Cookie"]
        assert "frame-ancestors 'self'" in headers["Content-Security-Policy"]
        assert "feishu.cn" not in headers["Content-Security-Policy"]
        assert get("/api/state", {"X-Mixcut-Session": session["csrf"]})[0] == 403


def test_embed_header_session_needs_no_third_party_cookie(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch, True) as (_, get):
        _, headers, body = get("/api/session")
        session = json.loads(body)
        assert session["embedded"] is True
        assert (
            "https://example.feishu.cn https://ext.baseopendev.com"
            in headers["Content-Security-Policy"]
        )
        assert "Access-Control-Allow-Origin" not in headers
        assert get("/api/state")[0] == 403
        assert get("/api/state", {"X-Mixcut-Session": "invalid"})[0] == 403
        assert get("/api/state", {"X-Mixcut-Session": session["sessionToken"]})[0] == 200
        assert get("/api/state", {"Host": "other.invalid"})[0] == 403


def test_embed_post_keeps_origin_and_csrf_gate(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch, True) as (base, get):
        session = json.loads(get("/api/session")[2])
        token_header = {"X-Mixcut-Session": session["sessionToken"]}
        for headers in (
            token_header,
            {**token_header, "Origin": base},
            {
                **token_header,
                "Origin": "https://example.feishu.cn",
                "X-Mixcut-CSRF": session["csrf"],
            },
            {"Origin": base, "X-Mixcut-CSRF": session["csrf"]},
        ):
            assert get("/api/projects", headers, {})[0] == 403


def test_media_grant_cannot_authorize_state_other_paths_or_stale_build(tmp_path, monkeypatch):
    with running(tmp_path, monkeypatch, True) as (_, get):
        session = json.loads(get("/api/session")[2])
        state = json.loads(get("/api/state", {"X-Mixcut-Session": session["sessionToken"]})[2])
        url = state["tasks"][0]["finalUrl"]
        query = url.split("?", 1)[1]
        assert get("/api/media/task/build/final")[0] == 403
        status, _, body = get(url, {"Range": "bytes=0-3"})
        assert status == 206 and body == b"test"
        assert get("/api/state?" + query)[0] == 403
        assert get("/api/media/task/old/final?" + query)[0] == 403
        # Even a valid header session must still pass the current-build check.
        assert (
            get("/api/media/task/old/final", {"X-Mixcut-Session": session["sessionToken"]})[0]
            == 409
        )


def test_embedded_mode_rejects_non_feishu_host(tmp_path, monkeypatch):
    class App:
        mapping = {"url": "https://example.invalid/base/test"}

        def __init__(self, root):
            pass

    monkeypatch.setattr(workbench, "Workbench", App)
    with pytest.raises(ValueError):
        workbench.create_server(tmp_path, 0, embedded=True)
