"""Narrow, journaled OpenChatCut transport for the fixed director runner."""

from __future__ import annotations

import http.client
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from jsonschema import Draft202012Validator
from .core import Blocked, atomic_write, digest, load
from .engine import OpenChatCutAdapter

MUTATIONS = {
    "create_project",
    "begin_edit_session",
    "recover_edit_session",
    "import_media",
    "finalize_uploaded_asset",
    "create_motion_graphic_from_code",
    "edit_track",
    "edit_item",
    "review_edit_session",
    "submit_render_job",
}
READS = {
    "target_project",
    "openchatcut_status",
    "load_skill",
    "read_project",
    "get_edit_session",
    "list_edit_sessions",
    "track_export",
}


def data(result):
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                value = json.loads(block["text"])
            except (ValueError, KeyError):
                continue
            if isinstance(value, dict):
                return value
    return result


def media_free(value):
    """No encoded media is persisted, including encodings nested in JSON text."""
    if isinstance(value, dict):
        if value.get("type") in {"image", "audio"} and "data" in value:
            return {"type": value["type"], "media": "omitted"}
        return {
            k: media_free(v)
            for k, v in value.items()
            if k.lower() not in {"base64", "imagebase64", "audiobase64"}
        }
    if isinstance(value, list):
        return [media_free(v) for v in value]
    if isinstance(value, str):
        if "data:image/" in value or "data:audio/" in value or "data:video/" in value:
            return "[encoded media omitted]"
        if value.startswith(("{", "[")):
            try:
                return json.dumps(media_free(json.loads(value)), ensure_ascii=False)
            except ValueError:
                pass
    return value


def upload_slot(packet, path):
    if "slots" in packet:
        slots = packet["slots"]
        if not isinstance(slots, list) or len(slots) != 1:
            raise Blocked("RUNNER_UPLOAD_SLOT", "单文件上传必须返回一个上传槽", 6)
        slot = slots[0]
    else:
        slot = packet
    if (
        not isinstance(slot, dict)
        or not slot.get("uploadUrl")
        or ("filename" in slot and slot["filename"] != path.name)
        or ("size" in slot and slot["size"] != path.stat().st_size)
        or slot.get("method", "POST") != "POST"
    ):
        raise Blocked("RUNNER_UPLOAD_SLOT", "上传槽与实际文件或上传方式不符", 6)
    return slot


def upload_slot_expired(slot):
    expiry = slot.get("expiresAt")
    return (
        isinstance(expiry, (int, float))
        and not isinstance(expiry, bool)
        and expiry <= time.time() * 1000
    )


class Native:
    def __init__(self, directory, guard, *, factory=OpenChatCutAdapter):
        self.directory = directory / "private_calls"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.guard, self.factory = guard, factory
        self.adapter = None
        self.schemas = {}
        self.project = None

    def connect(self, *, fresh=False):
        transport = self.directory / "transport.json"
        if self.adapter and fresh:
            self.adapter.close()
        if not os.environ.get("OPENCHATCUT_MCP_TOKEN"):
            p = Path.home() / ".openchatcut/mcp-token"
            if not p.is_file() or p.stat().st_mode & 0o077:
                raise Blocked("AUTH_MISSING", "编辑器凭据缺失或权限不符", 3)
            os.environ["OPENCHATCUT_MCP_TOKEN"] = p.read_text().strip()
        sid = load(transport).get("session_id") if transport.exists() and not fresh else None
        self.adapter = self.factory(session_id=sid)
        self.schemas = {
            t["name"]: t["inputSchema"] for t in self.adapter.rpc("tools/list", {}).get("tools", [])
        }
        if self.project:
            self.read("target_project", {"projectId": self.project})

    def close(self, *, terminal=False):
        if self.adapter:
            if terminal:
                self.adapter.close()
            else:
                atomic_write(
                    self.directory / "transport.json",
                    {"session_id": self.adapter.headers.get("Mcp-Session-Id")},
                )

    def read(self, name, args):
        if name not in READS:
            raise Blocked("RUNNER_TOOL", "只允许固定读取工具", 2)
        return self._call(name, args)

    def _prepare_call(self, name, args):
        if not self.adapter:
            self.connect()
        assert self.adapter is not None
        if name not in self.schemas or list(
            Draft202012Validator(self.schemas[name]).iter_errors(args)
        ):
            raise Blocked("RUNNER_TOOL_SCHEMA", f"编辑器{name}契约变化，停止执行", 6)

    def _send_call(self, name, args):
        assert self.adapter is not None
        result = self.adapter.rpc("tools/call", {"name": name, "arguments": args})
        value = data(result)
        if name in READS and value.get("outcome") == "stale":
            raise Blocked("RUNNER_READ_STALE", f"编辑器{name}连接版本已变化，须重新连接核对", 3)
        if value.get("needs_confirmation"):
            raise Blocked(
                "RUNNER_CONFIRMATION", f"OpenChatCut要求确认{name}；确认后重跑同一命令", 3
            )
        if (
            result.get("isError")
            or value.get("error")
            or value.get("outcome") in {"failed", "rejected"}
        ):
            raise Blocked("RUNNER_TOOL_FAILED", f"编辑器{name}返回失败，未自动重试", 6)
        return media_free(value)

    def _call(self, name, args):
        self._prepare_call(name, args)
        return self._send_call(name, args)

    def effect(self, label, name, args):
        if name not in MUTATIONS:
            raise Blocked("RUNNER_TOOL", "只允许固定制作工具", 2)
        return self.once(
            label,
            {"tool": name, "args": args},
            lambda: self._send_call(name, args),
            preflight=lambda: self._prepare_call(name, args),
        )

    def once(self, label, parameters, fn, *, preflight=None):
        self.guard()
        path = self.directory / (label + ".json")
        key = digest(parameters)
        prior = load(path) if path.exists() else None
        if prior:
            if prior["key"] != key:
                raise Blocked(
                    "RUNNER_PARAMETERS_CHANGED", "既有步骤参数发生变化，须对账或新版本", 6
                )
            if prior["status"] == "completed":
                if digest(prior["result"]) != prior.get("result_sha256"):
                    raise Blocked("RUNNER_RECEIPT_CHANGED", "执行回执内容变化，停止", 6)
                return prior["result"]
            if prior["status"] not in {"confirmation_required", "not_sent"}:
                raise Blocked("RUNNER_RESULT_UNKNOWN", f"{label}结果未知或失败，禁止自动重发", 6)
        # Only read/validate here. A failure is known not to have sent a mutation.
        if preflight is not None:
            try:
                preflight()
            except Exception:
                atomic_write(path, {"key": key, "status": "not_sent"})
                raise
            # Recheck human review after potentially slow connection/schema discovery.
            self.guard()
        atomic_write(path, {"key": key, "status": "unknown"})
        try:
            result = fn()
        except Blocked as e:
            if e.code == "RUNNER_CONFIRMATION":
                atomic_write(path, {"key": key, "status": "confirmation_required"})
            raise
        # Keep opaque upload receipts only in mode-0600 files, never in CLI output.
        result = media_free(result)
        atomic_write(
            path,
            {"key": key, "status": "completed", "result": result, "result_sha256": digest(result)},
        )
        return result

    def bind(self, project):
        self.project = project
        for attempt in range(3):
            try:
                self.read("target_project", {"projectId": project})
                assert self.adapter is not None
                self.schemas = {
                    t["name"]: t["inputSchema"]
                    for t in self.adapter.rpc("tools/list", {}).get("tools", [])
                }
                if "load_skill" not in self.schemas:
                    raise Blocked(
                        "RUNNER_EDITOR_BROWSER_REQUIRED",
                        "请打开本任务的工程编辑器后恢复，上传/包装/导出需要浏览器连接",
                        3,
                    )
                for skill in ["asset-import", "create-motion-graphics", "verification", "export"]:
                    self.load_editor_skill(skill)
                break
            except Blocked as error:
                if (
                    error.code != "RUNNER_READ_STALE"
                    or attempt == 2
                    or self.receipt_status("begin_edit_session") is not None
                ):
                    raise
                # Only import/template discovery may reconnect automatically. An
                # active or unknown edit proposal must preserve transport ownership.
                time.sleep(attempt + 1)
                self.project = None
                try:
                    self.connect(fresh=True)
                finally:
                    self.project = project
        assert self.adapter is not None
        self.schemas = {
            t["name"]: t["inputSchema"] for t in self.adapter.rpc("tools/list", {}).get("tools", [])
        }

    def load_editor_skill(self, skill):
        # Explicit file paging also bounds SKILL.md; omitting file can return the
        # entire entry even when limit is supplied by an external MCP caller.
        offset = 0
        for _ in range(128):
            value = self.read(
                "load_skill",
                {
                    "name": skill,
                    "file": "SKILL.md",
                    "offset": offset,
                    "limit": 4000,
                    "editorProjectId": self.project,
                },
            )
            following = value.get("nextOffset")
            if following is None:
                return
            if isinstance(following, bool) or not isinstance(following, int) or following <= offset:
                raise Blocked("RUNNER_SKILL_PAGING", "包装工具说明分页游标异常，停止", 6)
            offset = following
        raise Blocked("RUNNER_SKILL_PAGING", "工具说明超过固定分页预算，停止", 6)

    def begin(self, state, key):
        # Reclaim only this run's recorded draft; never adopt an unrelated draft.
        prior = state.get(key)
        args = {
            "clientName": "Mixcut fixed runner",
            "approvalMode": "auto",
            "reuseExisting": False,
            "editorProjectId": self.project,
        }

        def walk(v):
            if isinstance(v, dict):
                yield v
                for x in v.values():
                    yield from walk(x)
            elif isinstance(v, list):
                for x in v:
                    yield from walk(x)

        if prior:
            listed = self.read("list_edit_sessions", {"editorProjectId": self.project})
            found = next((x for x in walk(listed) if x.get("editSessionId") == prior), None)
            if not found or found.get("status") not in {
                "drafting",
                "review_pending",
                "ready_for_review",
            }:
                raise Blocked("RUNNER_SESSION_CHANGED", "已记录编辑会话状态变化，须对账", 6)
            if found.get("ownerOnline") is False:
                return self._call(
                    "recover_edit_session",
                    {"action": "resume", "editSessionId": prior, "editorProjectId": self.project},
                )
            return {"editSessionId": prior}
        # A live asset commit invalidates its empty import draft. Retire only
        # empty drafts recorded by this same run, never another user's work.
        owned = {
            v
            for k, v in state.items()
            if k.endswith("_session") and k not in {"edit_session", "export_session"}
        }
        if owned:
            listed = self.read("list_edit_sessions", {"editorProjectId": self.project})
            for entry in walk(listed):
                sid = entry.get("editSessionId")
                if sid in owned and entry.get("status") in {"drafting", "stale"}:
                    if (
                        entry.get("status") == "stale"
                        and entry.get("operationCount") == 0
                        and not entry.get("recoveryActions")
                    ):
                        # A finalized live upload can leave an empty terminal draft.
                        # There is nothing left to discard or recover in this session.
                        continue
                    if entry.get("operationCount") != 0 or "discard" not in entry.get(
                        "recoveryActions", []
                    ):
                        raise Blocked("RUNNER_IMPORT_SESSION", "导入会话含未预期操作，先对账", 6)
                    self.effect(
                        "retire_" + sid,
                        "recover_edit_session",
                        {
                            "action": "discard",
                            "editSessionId": sid,
                            "editorProjectId": self.project,
                        },
                    )
        return self.effect("begin_" + key, "begin_edit_session", args)

    def completed(self, label):
        path = self.directory / (label + ".json")
        value = load(path) if path.exists() else None
        if value and value["status"] == "completed":
            if digest(value["result"]) != value.get("result_sha256"):
                raise Blocked("RUNNER_RECEIPT_CHANGED", "执行回执内容变化，停止", 6)
            return value["result"]
        return None

    def receipt_status(self, label):
        path = self.directory / (label + ".json")
        return load(path).get("status") if path.exists() else None

    def upload(self, label, path, slot):
        original_slot = slot
        slot = upload_slot(slot, path)
        if upload_slot_expired(slot) and self.receipt_status(label) in {None, "not_sent"}:
            raise Blocked("RUNNER_UPLOAD_SLOT_EXPIRED", "未发送的上传槽已过期，请先续槽", 3)
        parsed = urlparse(slot["uploadUrl"])
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port != 5199
        ):
            raise Blocked("RUNNER_UPLOAD_HOST", "上传地址必须是本机编辑器", 6)

        def send():
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=45)
            try:
                with path.open("rb") as f:
                    headers = {
                        **slot.get("headers", {}),
                        "Content-Length": str(path.stat().st_size),
                    }
                    conn.request(
                        "POST",
                        parsed.path + ("?" + parsed.query if parsed.query else ""),
                        f,
                        headers,
                    )
                    response = conn.getresponse()
                    body = response.read()
                if not 200 <= response.status < 300:
                    raise Blocked("RUNNER_UPLOAD_FAILED", "上传失败，保留回执待对账", 6)
                return json.loads(body)
            finally:
                conn.close()

        from .core import file_hash

        return self.once(
            label, {"file_sha256": file_hash(path), "slot_sha256": digest(original_slot)}, send
        )
