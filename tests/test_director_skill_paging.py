"""Runtime skill paging and browser discovery without editor effects."""

import pytest
from mixcut.director_native import Native
from mixcut.core import Blocked


class Adapter:
    def __init__(self, *, browser=True, bad_cursor=False):
        self.bound = False
        self.browser = browser
        self.calls = []
        self.bad_cursor = bad_cursor

    def rpc(self, method, params):
        if method == "tools/list":
            tools = [{"name": "target_project", "inputSchema": {"type": "object"}}]
            if self.bound and self.browser:
                tools.append({"name": "load_skill", "inputSchema": {"type": "object"}})
            return {"tools": tools}
        name = params["name"]
        args = params["arguments"]
        self.calls.append((name, args.copy()))
        if name == "target_project":
            self.bound = True
            return {"structuredContent": {"projectId": args["projectId"]}}
        assert name == "load_skill" and args["file"] == "SKILL.md" and args["limit"] == 4000
        return {
            "structuredContent": {
                "file": "SKILL.md",
                "offset": args["offset"],
                "nextOffset": 0 if self.bad_cursor else 4000 if args["offset"] == 0 else None,
            }
        }


def native(tmp_path, **kwargs):
    n = Native(tmp_path, lambda: None)
    n.adapter = Adapter(**kwargs)
    n.schemas = {t["name"]: t["inputSchema"] for t in n.adapter.rpc("tools/list", {})["tools"]}
    return n


def test_bind_refreshes_after_target_and_follows_every_page(tmp_path):
    n = native(tmp_path)
    n.bind("actual-project")
    calls = [args for name, args in n.adapter.calls if name == "load_skill"]
    assert len(calls) == 8
    assert [a["offset"] for a in calls] == [0, 4000] * 4
    assert {a["name"] for a in calls} == {
        "asset-import",
        "create-motion-graphics",
        "verification",
        "export",
    }
    assert all(a["editorProjectId"] == "actual-project" for a in calls)


def test_missing_browser_stops_before_any_production_tool(tmp_path):
    n = native(tmp_path, browser=False)
    with pytest.raises(Blocked) as error:
        n.bind("actual-project")
    assert error.value.code == "RUNNER_EDITOR_BROWSER_REQUIRED"
    assert [name for name, _ in n.adapter.calls] == ["target_project"]


def test_nonadvancing_skill_cursor_stops_without_looping(tmp_path):
    n = native(tmp_path, bad_cursor=True)
    with pytest.raises(Blocked) as error:
        n.bind("actual-project")
    assert error.value.code == "RUNNER_SKILL_PAGING"
    assert len(n.adapter.calls) == 2


@pytest.mark.parametrize(
    "status,operations,allowed", [("stale", 0, True), ("stale", 1, False), ("drafting", 0, False)]
)
def test_only_empty_terminal_import_sessions_need_no_recovery_action(
    tmp_path, monkeypatch, status, operations, allowed
):
    n = Native(tmp_path, lambda: None)
    n.project = "p"
    sent = []
    monkeypatch.setattr(
        n,
        "read",
        lambda *a: {
            "sessions": [
                {
                    "editSessionId": "old",
                    "status": status,
                    "operationCount": operations,
                    "ownerOnline": False,
                    "recoveryActions": [],
                }
            ]
        },
    )
    monkeypatch.setattr(
        n, "effect", lambda label, name, args: sent.append(name) or {"editSessionId": "new"}
    )
    if allowed:
        assert (
            n.begin({"import_000_session": "old"}, "import_001_session")["editSessionId"] == "new"
        )
        assert sent == ["begin_edit_session"]
    else:
        with pytest.raises(Blocked) as error:
            n.begin({"import_000_session": "old"}, "import_001_session")
        assert error.value.code == "RUNNER_IMPORT_SESSION" and not sent


@pytest.mark.parametrize("edit_receipt_exists", [False, True])
def test_only_read_stale_before_edit_proposal_can_reconnect(
    tmp_path, monkeypatch, edit_receipt_exists
):
    from mixcut.core import atomic_write

    n = native(tmp_path)
    original = n.adapter.rpc
    stale = [True]
    reconnects = []
    delays = []
    monkeypatch.setattr("mixcut.director_native.time.sleep", lambda value: delays.append(value))

    def rpc(method, args):
        if method == "tools/call" and args["name"] == "load_skill" and stale[0]:
            stale[0] = False
            return {
                "isError": True,
                "structuredContent": {
                    "outcome": "stale",
                    "message": "Project changed; reinitialize",
                },
            }
        return original(method, args)

    n.adapter.rpc = rpc
    monkeypatch.setattr(n, "connect", lambda **kwargs: reconnects.append(kwargs))
    if edit_receipt_exists:
        atomic_write(n.directory / "begin_edit_session.json", {"status": "unknown"})
        with pytest.raises(Blocked) as error:
            n.bind("p")
        assert error.value.code == "RUNNER_READ_STALE" and not reconnects
    else:
        n.bind("p")
        assert reconnects == [{"fresh": True}]
        assert delays == [1]
