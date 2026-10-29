from __future__ import annotations
import json
import os
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from .core import Blocked, atomic_write, now


def local_runtime_status(token_file: Path | None = None) -> dict:
    """Inspect local readiness without returning or logging the credential value."""
    token_file = token_file or Path.home() / ".openchatcut/mcp-token"
    try:
        file_nonempty = token_file.is_file() and bool(token_file.read_text().strip())
        file_secure = file_nonempty and token_file.stat().st_mode & 0o077 == 0
    except OSError:
        file_nonempty = file_secure = False
    credential_source = (
        "environment"
        if bool(os.environ.get("OPENCHATCUT_MCP_TOKEN"))
        else "local_0600_file"
        if file_secure
        else None
    )
    process = subprocess.run(["pgrep", "-x", "OpenChatCut"], capture_output=True, text=True)
    running = True if process.returncode == 0 else False if process.returncode == 1 else None
    blockers = []
    unknowns = []
    if running is False:
        blockers.append("OpenChatCut is not running")
    elif running is None:
        unknowns.append("OpenChatCut process visibility is unavailable")
    if credential_source is None:
        blockers.append("OpenChatCut MCP credential is unavailable or not mode 0600")
    ready = False if blockers else True if running is True else None
    return {
        "openchatcut_running": running,
        "process_check_exit_code": process.returncode,
        "credential_available": credential_source is not None,
        "credential_source": credential_source,
        "token_file_nonempty": file_nonempty,
        "token_file_secure": file_secure,
        "ready": ready,
        "blockers": blockers,
        "unknowns": unknowns,
    }


class OpenChatCutAdapter:
    """MCP transport. Mutations are intentionally unavailable until G1 adapter validation.

    Token is environment-only. No editor-bootstrap fallback, hidden project writes,
    default/recent project selection, or automatic mutation retries.
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:5199/api/external-mcp/mcp",
        *,
        session_id: str | None = None,
    ):
        from urllib.parse import urlparse

        if urlparse(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise Blocked("LOCAL_ENGINE_ONLY", "P0 engine must use localhost")
        self.endpoint = endpoint
        self.counter = 0
        self.session = None
        token = os.environ.get("OPENCHATCUT_MCP_TOKEN")
        if not token:
            raise Blocked(
                "AUTH_MISSING", "Set OPENCHATCUT_MCP_TOKEN locally; never send it to logs"
            )
        self.headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id
            return
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mixcut", "version": "0.1.0"},
            },
        )
        self.rpc("notifications/initialized", {}, notification=True)

    def rpc(self, method: str, params: dict, notification: bool = False) -> dict:
        self.counter += 1
        body: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.counter
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode(), headers=self.headers
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                sid = response.headers.get("Mcp-Session-Id")
                if sid:
                    self.headers["Mcp-Session-Id"] = sid
                raw = response.read().decode()
        except (TimeoutError, urllib.error.URLError) as e:
            raise Blocked(
                "ENGINE_UNAVAILABLE", "Local engine request failed; no mutation was retried", 5
            ) from e
        if not raw:
            return {}
        if raw.startswith(("event:", "data:")):
            messages = [
                json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")
            ]
            value = messages[-1]
        else:
            value = json.loads(raw)
        if "error" in value:
            raise Blocked("ENGINE_RPC", str(value["error"]), 4)
        return value.get("result", {})

    def close(self) -> None:
        """Terminate the Streamable HTTP MCP session without exposing its id."""
        if "Mcp-Session-Id" not in self.headers:
            return
        request = urllib.request.Request(
            self.endpoint,
            headers={
                "Authorization": self.headers["Authorization"],
                "Accept": self.headers["Accept"],
                "Mcp-Session-Id": self.headers["Mcp-Session-Id"],
            },
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            # Some older MCP servers return 404 after already expiring a
            # session. Treat that as closed; other responses remain errors.
            if exc.code != 404:
                raise Blocked("ENGINE_RPC", "Failed to close local MCP session", 4) from exc
        finally:
            self.headers.pop("Mcp-Session-Id", None)

    def call(self, name: str, args: dict | None = None) -> dict:
        if name not in {
            "openchatcut_status",
            "list_projects",
            "target_project",
            "get_editor_url",
            "load_skill",
            "read_project",
            "read_timeline",
            "get_edit_session",
        }:
            raise Blocked(
                "CAPABILITY_UNVALIDATED",
                "Editing/import/export adapter is deferred until live G1 validation",
            )
        result = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        if result.get("isError"):
            raise Blocked("ENGINE_TOOL_FAILED", "See tool receipt; do not blindly repeat", 4)
        return result

    def probe(self, workspace: Path) -> dict:
        status = self.call("openchatcut_status")
        tools = self.rpc("tools/list", {})
        report = {
            "at": now(),
            "status": status,
            "tools": tools,
            "production_editing_validated": False,
        }
        atomic_write(workspace / "reports/engine_probe.json", report)
        return report


def verify_readback(plan: dict, actual: dict) -> dict:
    """Normalized DTO comparison; native normalization remains a live G1 requirement."""
    differences = []
    for key in ["canvas", "total_frames", "shots", "captions", "audio_tracks"]:
        if plan.get(key) != actual.get(key):
            differences.append(key)
    return {
        "status": "passed" if not differences else "failed",
        "differences": differences,
        "kind": "normalized_dto_only",
    }
