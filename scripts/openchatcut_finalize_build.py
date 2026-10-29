#!/usr/bin/env python3
"""Verify an approved OpenChatCut build and drive its guarded export.

The script keeps every MCP response as a redacted local receipt. Mutating live
project tools still require the one-shot confirmation shown by OpenChatCut.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Iterator

from mixcut.engine import OpenChatCutAdapter


def iter_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_dicts(nested)


def parse_text_content(result: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    for block in result.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        try:
            values.append(json.loads(text))
        except (TypeError, json.JSONDecodeError):
            values.append(text)
    return values


def first_string(values: Any, *keys: str) -> str | None:
    for item in iter_dicts(values):
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def verified_proposal_status(listed_sessions: Any, proposal_session: str) -> str:
    entry = next(
        (item for item in iter_dicts(listed_sessions)
         if item.get("editSessionId") == proposal_session),
        None,
    )
    if not entry or entry.get("status") != "applied":
        raise RuntimeError("Proposal is not confirmed applied; no export is allowed")
    return "applied_verified_in_editor_session_list"


def sanitize(value: Any) -> Any:
    secret_keys = {"authorization", "confirmationId", "receipt", "uploadUrl"}
    if isinstance(value, dict):
        if isinstance(value.get("type"), str) and value.get("type") in {"image", "audio"} and "data" in value:
            return {"type": value["type"], "mimeType": value.get("mimeType"),
                    "media_content": "omitted; use a local file"}
        return {
            key: "[redacted]" if key in secret_keys else sanitize(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [sanitize(nested) for nested in value]
    if isinstance(value, str):
        if "data:image/" in value or "data:audio/" in value or "data:video/" in value:
            return "[encoded media omitted; local file required]"
        if value.startswith(("{", "[")):
            try:
                return json.dumps(sanitize(json.loads(value)), ensure_ascii=False)
            except json.JSONDecodeError:
                pass
    return value


def inspection_available(search: Any) -> bool:
    return any(
        item.get("name") == "inspect_render"
        or "inspect_render" in (item.get("activatedTools") or [])
        for item in iter_dicts(search)
    )


def resolve_token() -> str:
    token = os.environ.get("OPENCHATCUT_MCP_TOKEN", "").strip()
    if token:
        return token
    token_file = Path.home() / ".openchatcut" / "mcp-token"
    if token_file.is_file():
        token = token_file.read_text().strip()
    if not token:
        raise RuntimeError("OpenChatCut MCP token is unavailable")
    return token


def result_has_error(result: dict[str, Any], values: list[Any]) -> bool:
    structured = result.get("structuredContent")
    candidates: list[Any] = [structured, *values]
    return bool(result.get("isError")) or any(
        isinstance(item, dict)
        and (bool(item.get("error")) or item.get("outcome") in {"rejected", "failed"})
        for item in candidates
    )


def needs_confirmation(result: dict[str, Any], values: list[Any]) -> bool:
    structured = result.get("structuredContent")
    return (
        isinstance(structured, dict)
        and structured.get("needs_confirmation") is True
    ) or any(
        isinstance(value, dict) and value.get("needs_confirmation") is True
        for value in values
    )


def validate_timeline(plan: dict[str, Any], timeline: dict[str, Any]) -> dict[str, Any]:
    items = timeline.get("items", [])
    videos = sorted(
        [item for item in items if item.get("kind") == "video"],
        key=lambda item: item.get("startFrame", -1),
    )
    audio = sorted(
        [item for item in items if item.get("kind") == "audio"],
        key=lambda item: item.get("startFrame", -1),
    )
    text = sorted(
        [item for item in items if item.get("kind") == "text"],
        key=lambda item: item.get("startFrame", -1),
    )
    expected_shots = plan["shots"]
    expected_captions = plan["captions"]
    expected_audio = plan["audio_tracks"]
    checks = {
        "fps_30": timeline.get("fps") == 30,
        "video_count": len(videos) == len(expected_shots),
        "audio_count": len(audio) == len(expected_audio),
        "text_count": len(text) == len(expected_captions),
        "video_start_frames": [item.get("startFrame") for item in videos]
        == [item["timeline_start_frame"] for item in expected_shots],
        "video_durations": [item.get("durationInFrames") for item in videos]
        == [item["duration_frames"] for item in expected_shots],
        "source_in_frames": [item.get("sourceStartFrame") for item in videos]
        == [item["source_in_frame"] for item in expected_shots],
        "nonzero_source_in_frames": bool(videos)
        and all((item.get("sourceStartFrame") or 0) > 0 for item in videos),
        "video_links": bool(videos)
        and all(item.get("linkStatus") == "linked" for item in videos),
        "audio_720_frames": len(audio) == 1
        and audio[0].get("startFrame") == 0
        and audio[0].get("durationInFrames") == plan["total_frames"]
        and audio[0].get("linkStatus") == "linked",
        "caption_text": [item.get("props", {}).get("text") for item in text]
        == [item["text"] for item in expected_captions],
        "caption_start_frames": [item.get("startFrame") for item in text]
        == [item["start_frame"] for item in expected_captions],
        "caption_durations": [item.get("durationInFrames") for item in text]
        == [item["end_frame"] - item["start_frame"] for item in expected_captions],
        "caption_lower_third": bool(text)
        and all(item.get("transform") == {"x": 0, "y": 35} for item in text),
        "timeline_end_frame": max(
            (item.get("startFrame", 0) + item.get("durationInFrames", 0) for item in items),
            default=0,
        )
        == plan["total_frames"],
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "evidence_kind": "OpenChatCut live timeline readback after manual approval",
        "counts": {
            "video": len(videos),
            "audio": len(audio),
            "text": len(text),
            "total": len(items),
        },
        "source_in_frames": [item.get("sourceStartFrame") for item in videos],
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=["readback", "submit", "track", "inspect", "full"]
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--render-id")
    args = parser.parse_args()

    root = args.workspace.resolve()
    build_dir = root / "jobs" / args.task / args.revision / args.build
    plan = json.loads((build_dir / "edit_plan.json").read_text())
    final_dir = build_dir / "openchatcut" / "finalize"
    final_dir.mkdir(parents=True, exist_ok=True)
    run_dir = final_dir / datetime.now().astimezone().strftime("run_%Y%m%dT%H%M%S%z")
    run_dir.mkdir(parents=True, exist_ok=True)
    session_file = final_dir / "export_session.json"

    os.environ["OPENCHATCUT_MCP_TOKEN"] = resolve_token()
    adapter = OpenChatCutAdapter()
    sequence = 0

    def tool(name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
        nonlocal sequence
        for _ in range(3):
            sequence += 1
            result = adapter.rpc("tools/call", {"name": name, "arguments": arguments})
            receipt = run_dir / f"{sequence:02d}_{name}.json"
            receipt.write_text(json.dumps(sanitize(result), ensure_ascii=False, indent=2) + "\n")
            values = parse_text_content(result)
            if needs_confirmation(result, values):
                print(f"CONFIRM_REQUIRED {name}", flush=True)
                input()
                continue
            if result_has_error(result, values):
                raise RuntimeError(f"{name} failed; see {receipt}")
            return result, values
        raise RuntimeError(f"{name} confirmation was not accepted")

    try:
        tool("target_project", {"projectId": args.project_id})
        for skill in ("verification", "export"):
            tool("load_skill", {"name": skill, "editorProjectId": args.project_id})

        # Every phase owns a fresh draft. Export-session ids from a preceding
        # CLI process belong to its closed MCP transport and cannot be reused.
        if args.mode in {"readback", "submit", "track", "inspect", "full"}:
            proposal_session = json.loads(
                (build_dir / "openchatcut" / "current_session.json").read_text()
            )["edit_session_id"]
            # Edit-session ids are intentionally scoped to the MCP transport
            # that created them, so a fresh verification transport cannot ask
            # get_edit_session about the earlier proposal. The new session
            # below reads the live project revision after the approval card has
            # disappeared from the connected editor.
            _, listed_sessions = tool(
                "list_edit_sessions", {"editorProjectId": args.project_id}
            )
            proposal_status = verified_proposal_status(listed_sessions, proposal_session)
            for item in iter_dicts(listed_sessions):
                recovery_actions = item.get("recoveryActions")
                orphan_id = item.get("editSessionId")
                if (
                    isinstance(orphan_id, str)
                    and item.get("status") == "drafting"
                    and item.get("ownerOnline") is False
                    and item.get("operationCount") == 0
                    and isinstance(recovery_actions, list)
                    and "discard" in recovery_actions
                ):
                    tool(
                        "recover_edit_session",
                        {
                            "editSessionId": orphan_id,
                            "action": "discard",
                            "editorProjectId": args.project_id,
                        },
                    )

            _, begin_values = tool(
                "begin_edit_session",
                {
                    "clientName": "Codex Mixcut G3 Final Export",
                    "approvalMode": "manual",
                    "reuseExisting": False,
                    "editorProjectId": args.project_id,
                },
            )
            edit_session_id = first_string(begin_values, "editSessionId", "sessionId")
            if not edit_session_id:
                raise RuntimeError("begin_edit_session response has no session id")
            session_file.write_text(
                json.dumps(
                    {
                        "project_id": args.project_id,
                        "edit_session_id": edit_session_id,
                        "proposal_session_id": proposal_session,
                        "proposal_status": proposal_status,
                        "status": "ready_for_export",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            timeline_result, _ = tool(
                "read_timeline",
                {"editSessionId": edit_session_id, "editorProjectId": args.project_id},
            )
            timeline = timeline_result.get("structuredContent", {})
            validation = validate_timeline(plan, timeline)
            validation.update(
                {
                    "project_id": args.project_id,
                    "edit_session_id": edit_session_id,
                    "proposal_session_id": proposal_session,
                    "proposal_status": proposal_status,
                    "build_id": args.build,
                    "receipt_dir": str(run_dir),
                }
            )
            (run_dir / "final_timeline.json").write_text(
                json.dumps(timeline, ensure_ascii=False, indent=2) + "\n"
            )
            (final_dir / "final_readback_validation.json").write_text(
                json.dumps(validation, ensure_ascii=False, indent=2) + "\n"
            )
            if validation["status"] != "passed":
                raise RuntimeError("Final timeline validation failed")
            print(json.dumps(validation, ensure_ascii=False), flush=True)
            if args.mode == "readback":
                return
            common = {
                "editSessionId": edit_session_id,
                "editorProjectId": args.project_id,
            }

        if args.mode != "full":
            if not session_file.is_file():
                raise RuntimeError("Run readback before export")
            session = json.loads(session_file.read_text())
            edit_session_id = session["edit_session_id"]
            common = {
                "editSessionId": edit_session_id,
                "editorProjectId": args.project_id,
            }

        if args.mode in {"submit", "full"}:
            result, values = tool(
                "submit_render_job",
                {
                    "format": "video",
                    "codec": "h264",
                    "resolution": "1080p",
                    "fps": 30,
                    "name": f"{args.task}_{args.revision}_{args.build}",
                    "startFrame": 0,
                    "endFrameExclusive": plan["total_frames"],
                    "saveToMediaPool": False,
                    **common,
                },
            )
            render_id = first_string(
                [result.get("structuredContent"), *values], "renderId", "render_id"
            )
            if not render_id:
                raise RuntimeError("submit_render_job response has no render id")
            job = {"render_id": render_id, "status": "submitted", "receipt_dir": str(run_dir)}
            (final_dir / "render_job.json").write_text(
                json.dumps(job, ensure_ascii=False, indent=2) + "\n"
            )
            print(json.dumps(job, ensure_ascii=False), flush=True)
            if args.mode == "submit":
                return

        if args.mode != "full":
            render_id = args.render_id
            if not render_id:
                render_id = json.loads((final_dir / "render_job.json").read_text())[
                    "render_id"
                ]

        if args.mode in {"track", "full"}:
            result, values = tool(
                "track_export",
                {
                    "action": "wait",
                    "renderIds": render_id,
                    "timeoutSeconds": 300 if args.mode == "full" else 20,
                    **common,
                },
            )
            payload = result.get("structuredContent") or values
            (final_dir / "render_status.json").write_text(
                json.dumps(sanitize(payload), ensure_ascii=False, indent=2) + "\n"
            )
            print(json.dumps(sanitize(payload), ensure_ascii=False), flush=True)
            if args.mode == "track":
                return
            render_status = first_string(payload, "status")
            if render_status != "completed":
                print(
                    json.dumps(
                        {"render_id": render_id, "status": render_status or "unknown"},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return

        search, search_values = tool(
            "ToolSearch", {"query": "inspect_render", "editorProjectId": args.project_id}
        )
        if not inspection_available([search.get("structuredContent"), *search_values]):
            inspection = {
                "render_id": render_id,
                "status": "unavailable",
                "reason": "inspect_render is absent from the live tool catalog",
                "local_qc_required": True,
                "engine_inspection_passed": False,
            }
            (final_dir / "render_inspection.json").write_text(
                json.dumps(inspection, ensure_ascii=False, indent=2) + "\n"
            )
            print(json.dumps(inspection, ensure_ascii=False), flush=True)
            return
        result, values = tool("inspect_render", {"renderId": render_id, **common})
        payload = result.get("structuredContent") or values
        (final_dir / "render_inspection.json").write_text(
            json.dumps(sanitize(payload), ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(sanitize(payload), ensure_ascii=False), flush=True)
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
