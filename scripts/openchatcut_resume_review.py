"""Resume an unchanged orphaned proposal and hold ownership for UI approval."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mixcut.engine import OpenChatCutAdapter
from mixcut.runner import _ensure_engine_token


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-dir", required=True, type=Path)
    args = parser.parse_args()
    directory = args.build_dir / "openchatcut"
    project = json.loads((directory / "project.json").read_text())["project_id"]
    session = json.loads((directory / "current_session.json").read_text())
    session_id = session["edit_session_id"]
    _ensure_engine_token()
    adapter = OpenChatCutAdapter()

    def call(name: str, arguments: dict) -> dict:
        result = adapter.rpc("tools/call", {"name": name, "arguments": arguments})
        structured = result.get("structuredContent", {})
        (directory / f"recovery_{name}.json").write_text(
            json.dumps(structured, ensure_ascii=False, indent=2) + "\n"
        )
        if result.get("isError"):
            raise RuntimeError(f"{name} rejected; inspect recovery receipt")
        return structured

    try:
        call("target_project", {"projectId": project})
        common = {"editSessionId": session_id, "editorProjectId": project}
        call("recover_edit_session", {**common, "action": "resume"})
        print("PROPOSAL_RESUMED_WAITING_FOR_UI_APPLICATION", flush=True)
        input()
        actual = call("get_edit_session", common)
        status = actual.get("status")
        if status != "applied":
            raise RuntimeError(f"Proposal remains {status}; no export is allowed")
        session["status"] = "applied"
        (directory / "current_session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps({"status": status, "edit_session_id": session_id}), flush=True)
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
