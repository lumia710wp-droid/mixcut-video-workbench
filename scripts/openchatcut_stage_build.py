#!/usr/bin/env python3
"""Stage one exact Mixcut build as a manual OpenChatCut proposal.

The script never stores or prints the bearer token. All OpenChatCut mutations
remain inside one manual edit-session draft until review_edit_session creates
the proposal card in the connected editor.
"""

from __future__ import annotations

import argparse
import atexit
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import time
from typing import Any, Iterator
from urllib.parse import urlparse

from mixcut.engine import OpenChatCutAdapter
from mixcut.runner import preflight


def iter_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_dicts(nested)


def first_string(values: Any, *keys: str) -> str | None:
    for item in iter_dicts(values):
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def wait_for_review_application(tool: Any, project_id: str, session_id: str,
                                state_files: tuple[Path, ...], wait: Any = input) -> None:
    """Keep the caller's transport alive until the real UI application is verified."""
    print("PROPOSAL_READY_WAITING_FOR_UI_APPLICATION", flush=True)
    wait()
    values = tool("get_edit_session", {
        "editSessionId": session_id, "editorProjectId": project_id,
    })
    status = first_string(values, "status")
    if status != "applied":
        raise RuntimeError(f"Proposal remains {status}; no export is allowed")
    for state_file in state_files:
        saved = json.loads(state_file.read_text())
        saved["status"] = "applied"
        state_file.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "applied", "edit_session_id": session_id}), flush=True)


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


def sanitize_receipt(value: Any) -> Any:
    secret_keys = {"authorization", "confirmationId", "receipt", "uploadUrl"}
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key in secret_keys else sanitize_receipt(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [sanitize_receipt(nested) for nested in value]
    return value


def upload_slot(path: Path, slot: dict[str, Any]) -> dict[str, Any]:
    url = urlparse(slot["uploadUrl"])
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("External upload slot is not localhost HTTP")
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=120)
    headers = {str(key): str(value) for key, value in slot.get("headers", {}).items()}
    with path.open("rb") as source:
        connection.request("POST", url.path + ("?" + url.query if url.query else ""), source, headers)
        response = connection.getresponse()
        body = response.read()
    connection.close()
    if response.status < 200 or response.status >= 300:
        raise RuntimeError(f"External upload failed with HTTP {response.status}")
    return json.loads(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--hold-for-review", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    root = args.workspace.resolve()
    build_dir = root / "jobs" / args.task / args.revision / args.build
    run_id = datetime.now().astimezone().strftime("run_%Y%m%dT%H%M%S%z")
    output_dir = build_dir / "openchatcut" / "runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = json.loads((build_dir / "edit_plan.json").read_text())
    config = json.loads((root / "config" / "config.local.json").read_text())
    preflight(root, config, plan)

    roots = {entry["root_id"]: Path(entry["path"]) for entry in config["source_roots"]}
    media_paths = [
        str((roots[media["root_id"]] / media["relative_path"]).resolve())
        for media in plan["media"]
    ]
    audio_paths = [
        str((roots[audio["root_id"]] / audio["relative_path"]).resolve())
        for audio in plan["audio_tracks"]
    ]
    paths = media_paths + audio_paths
    missing_paths = [path for path in paths if not Path(path).is_file()]
    if missing_paths:
        raise RuntimeError("Missing input files: " + ", ".join(missing_paths))

    os.environ["OPENCHATCUT_MCP_TOKEN"] = resolve_token()
    adapter = OpenChatCutAdapter()
    # A stopped confirmation wait must release its transport ownership too.
    # Otherwise the next run sees an online owner after this process has exited.
    def close_transport() -> None:
        try:
            adapter.close()
        except Exception:
            # Preserve the original failure/interrupt; recovery will inspect
            # ownership rather than replaying the pending mutation.
            pass

    atexit.register(close_transport)
    sequence = 0

    def tool(name: str, arguments: dict[str, Any]) -> list[Any]:
        nonlocal sequence
        for confirmation_attempt in range(3):
            sequence += 1
            result = adapter.rpc("tools/call", {"name": name, "arguments": arguments})
            receipt = output_dir / f"{sequence:02d}_{name}.json"
            receipt.write_text(
                json.dumps(sanitize_receipt(result), ensure_ascii=False, indent=2) + "\n"
            )
            values = parse_text_content(result)
            structured = result.get("structuredContent")
            needs_confirmation = (
                isinstance(structured, dict) and structured.get("needs_confirmation") is True
            ) or any(
                isinstance(value, dict) and value.get("needs_confirmation") is True
                for value in values
            )
            if needs_confirmation:
                print(f"CONFIRM_REQUIRED {name}", flush=True)
                input()
                continue
            structured_error = isinstance(structured, dict) and (
                bool(structured.get("error"))
                or structured.get("outcome") in {"rejected", "failed"}
            )
            text_error = any(
                isinstance(value, dict)
                and (
                    bool(value.get("error"))
                    or value.get("outcome") in {"rejected", "failed"}
                )
                for value in values
            )
            if result.get("isError") or structured_error or text_error:
                raise RuntimeError(f"{name} failed; see {receipt}")
            return values
        raise RuntimeError(f"{name} confirmation was not accepted")

    project_id = args.project_id

    def bind_transport(*, reinitialize: bool = False) -> None:
        nonlocal adapter
        attempts = 3 if reinitialize else 1
        if reinitialize:
            time.sleep(1)
        for attempt in range(attempts):
            if reinitialize:
                adapter.close()
                adapter = OpenChatCutAdapter()
            try:
                tool("target_project", {"projectId": project_id})
                for skill in [
                    "asset-import",
                    "ecommerce-product-montage",
                    "verification",
                    "export",
                ]:
                    tool("load_skill", {"name": skill, "editorProjectId": project_id})
                return
            except RuntimeError:
                if attempt == attempts - 1:
                    raise
                time.sleep(1)

    def discard_orphaned_empty_import_drafts() -> None:
        sessions = tool(
            "list_edit_sessions", {"editorProjectId": project_id}
        )
        for item in iter_dicts(sessions):
            session = item.get("editSessionId")
            actions = item.get("recoveryActions")
            if (
                isinstance(session, str)
                and item.get("status") == "drafting"
                and item.get("ownerOnline") is False
                and item.get("operationCount") == 0
                and isinstance(actions, list)
                and "discard" in actions
            ):
                tool(
                    "recover_edit_session",
                    {
                        "editSessionId": session,
                        "action": "discard",
                        "editorProjectId": project_id,
                    },
                )

    bind_transport()
    discard_orphaned_empty_import_drafts()

    def begin_session(client_name: str) -> str:
        begin = tool(
            "begin_edit_session",
            {
                "clientName": client_name,
                "approvalMode": "manual",
                "reuseExisting": False,
                "editorProjectId": project_id,
            },
        )
        new_session_id = first_string(begin, "editSessionId", "sessionId")
        if not new_session_id:
            raise RuntimeError("begin_edit_session response has no session id")
        return new_session_id

    # Asset finalization changes the live project revision. Therefore every
    # finalized asset ends its short import draft, and the actual edit proposal
    # starts only after the full media pool is stable.
    session_id = begin_session("Codex Mixcut G3 Asset Import")
    common = {"editSessionId": session_id, "editorProjectId": project_id}
    assets_view = tool(
        "read_project",
        {
            "view": "assets",
            "editSessionId": session_id,
            "editorProjectId": project_id,
        },
    )
    basenames = [Path(path).name for path in paths]
    def map_assets(values: list[Any]) -> dict[str, str]:
        mapped: dict[str, str] = {}
        for item in iter_dicts(values):
            asset_id = item.get("id") or item.get("assetId")
            if not isinstance(asset_id, str):
                continue
            serialized = json.dumps(item, ensure_ascii=False)
            for basename in basenames:
                if basename in serialized and basename not in mapped:
                    mapped[basename] = asset_id
        return mapped

    asset_map = map_assets(assets_view)
    missing_assets = [name for name in basenames if name not in asset_map]
    if missing_assets:
        path_by_name = {Path(path).name: Path(path) for path in paths}
        media_meta = {media["relative_path"]: media for media in plan["media"]}
        audio_meta = {audio["relative_path"]: audio for audio in plan["audio_tracks"]}
        for asset_index, basename in enumerate(missing_assets):
            path = path_by_name[basename]
            if path.suffix.lower() == ".wav":
                asset_type = "audio"
                content_type = "audio/wav"
                relative = next(key for key in audio_meta if Path(key).name == basename)
                audio = audio_meta[relative]
                finalize_metadata = {
                    "durationInSeconds": audio["duration_frames"] / plan["canvas"]["fps_num"],
                    "hasAudioTrack": True,
                }
            else:
                asset_type = "video"
                content_type = "video/mp4"
                relative = next(key for key in media_meta if Path(key).name == basename)
                media = media_meta[relative]
                finalize_metadata = {
                    "durationInSeconds": media["total_frames"] / media["fps_num"],
                    "width": plan["canvas"]["width"],
                    "height": plan["canvas"]["height"],
                    "fps": media["fps_num"] / media["fps_den"],
                    "hasAudioTrack": False,
                }
            slot_values = tool(
                "import_media",
                {
                    "action": "create_session",
                    "assetType": asset_type,
                    "filename": basename,
                    "contentType": content_type,
                    "size": path.stat().st_size,
                    **common,
                },
            )
            slot = next(
                (
                    item
                    for item in iter_dicts(slot_values)
                    if isinstance(item.get("uploadUrl"), str)
                ),
                None,
            )
            if not slot:
                raise RuntimeError(f"No upload slot returned for {basename}")
            uploaded = upload_slot(path, slot)
            (output_dir / f"upload_{basename}.json").write_text(
                json.dumps(sanitize_receipt(uploaded), ensure_ascii=False, indent=2) + "\n"
            )
            receipt_value = uploaded.get("receipt")
            echoed_type = uploaded.get("assetType")
            if not isinstance(receipt_value, str) or echoed_type != asset_type:
                raise RuntimeError(f"Invalid upload receipt for {basename}")
            finalized = tool(
                "finalize_uploaded_asset",
                {
                    "receipt": receipt_value,
                    "assetType": asset_type,
                    **finalize_metadata,
                    **common,
                },
            )
            asset_id = first_string(finalized, "assetId")
            if not asset_id:
                raise RuntimeError(f"Finalize returned no asset id for {basename}")
            asset_map[basename] = asset_id
            # A real-project asset commit invalidates the entire MCP transport,
            # not only the edit draft. Reinitialize before the next operation.
            adapter.close()
            bind_transport(reinitialize=True)
            discard_orphaned_empty_import_drafts()
            if asset_index < len(missing_assets) - 1:
                session_id = begin_session("Codex Mixcut G3 Asset Import")
                common = {
                    "editSessionId": session_id,
                    "editorProjectId": project_id,
                }
    if missing_assets:
        unresolved = [name for name in missing_assets if name not in asset_map]
        if unresolved:
            (output_dir / "asset_map_partial.json").write_text(
                json.dumps(asset_map, ensure_ascii=False, indent=2) + "\n"
            )
            raise RuntimeError("Imported asset mapping missing: " + ", ".join(unresolved))

        # The final imported asset also changed the revision, so start the
        # proposal against the new stable base before any timeline mutation.
        session_id = begin_session("Codex Mixcut G3")
        common = {"editSessionId": session_id, "editorProjectId": project_id}
    else:
        # No live mutation occurred; the read-only discovery draft is still a
        # valid base for the proposal.
        common = {"editSessionId": session_id, "editorProjectId": project_id}

    final_assets_view = tool("read_project", {"view": "assets", **common})
    asset_map.update(map_assets(final_assets_view))
    unresolved = [name for name in basenames if name not in asset_map]
    if unresolved:
        (output_dir / "asset_map_partial.json").write_text(
            json.dumps(asset_map, ensure_ascii=False, indent=2) + "\n"
        )
        raise RuntimeError("Imported asset mapping missing: " + ", ".join(unresolved))
    (output_dir / "asset_map.json").write_text(
        json.dumps(asset_map, ensure_ascii=False, indent=2) + "\n"
    )

    session_file = output_dir / "session.json"
    session_file.write_text(
        json.dumps(
            {
                "task_id": args.task,
                "revision": args.revision,
                "build_id": args.build,
                "project_id": project_id,
                "edit_session_id": session_id,
                "approval_mode": "manual",
                "status": "drafting",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    (build_dir / "openchatcut" / "current_session.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "receipt_dir": str(output_dir),
                "edit_session_id": session_id,
                "status": "drafting",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    tool("read_project", common)

    tool(
        "edit_track",
        {
            "action": "create",
            "json": json.dumps(
                {"trackType": "video", "count": 1, "name": "中文字幕"},
                ensure_ascii=False,
            ),
            **common,
        },
    )
    tool(
        "edit_track",
        {
            "action": "create",
            "json": json.dumps(
                {
                    "trackType": "audio",
                    "count": 1,
                    "name": "Tingting 配音",
                    "role": "dialogue",
                },
                ensure_ascii=False,
            ),
            **common,
        },
    )

    media_by_id = {media["media_id"]: media for media in plan["media"]}
    additions: list[dict[str, Any]] = []
    for shot in plan["shots"]:
        media = media_by_id[shot["media_id"]]
        basename = (roots[media["root_id"]] / media["relative_path"]).name
        additions.append(
            {
                "type": "video",
                "assetId": asset_map[basename],
                "track": "V1",
                "fromFrame": shot["timeline_start_frame"],
                "sourceStartFrame": shot["source_in_frame"],
                "sourceDurationInFrames": shot["duration_frames"],
            }
        )
    for audio in plan["audio_tracks"]:
        basename = (roots[audio["root_id"]] / audio["relative_path"]).name
        additions.append(
            {
                "type": "audio",
                "assetId": asset_map[basename],
                "track": "A1",
                "fromFrame": audio["start_frame"],
                "sourceStartFrame": 0,
                "sourceDurationInFrames": audio["duration_frames"],
            }
        )
    for caption in plan["captions"]:
        additions.append(
            {
                "type": "text",
                "text": caption["text"],
                "name": f"字幕 {caption['shot_id']}",
                "track": "V2",
                "fromFrame": caption["start_frame"],
                "durationInFrames": caption["end_frame"] - caption["start_frame"],
                "fontSize": 64,
                "color": "#FFFFFF",
                "fontWeight": 800,
                "align": "center",
            }
        )

    tool("edit_item", {"adds": additions, "validateOnly": True, **common})
    tool("edit_item", {"adds": additions, **common})
    timeline = tool("read_project", {"view": "timeline", **common})
    caption_texts = {caption["text"] for caption in plan["captions"]}
    text_items: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for item in iter_dicts(timeline):
        item_id = item.get("id") or item.get("itemId")
        if not isinstance(item_id, str) or item_id in seen_ids:
            continue
        text = item.get("text")
        props = item.get("props")
        if not isinstance(text, str) and isinstance(props, dict):
            text = props.get("text")
        if text in caption_texts:
            text_items.append((item_id, text))
            seen_ids.add(item_id)
    if len(text_items) != len(plan["captions"]):
        raise RuntimeError(
            f"Expected {len(plan['captions'])} text items, found {len(text_items)}"
        )
    updates = [
        {"type": "text", "itemId": item_id, "transform": {"x": 0, "y": 35}}
        for item_id, _ in text_items
    ]
    tool("edit_item", {"updates": updates, "validateOnly": True, **common})
    tool("edit_item", {"updates": updates, **common})
    final_timeline = tool("read_timeline", common)

    timeline_items = list(iter_dicts(final_timeline))
    typed_ids: dict[str, set[str]] = {"video": set(), "audio": set(), "text": set()}
    for item in timeline_items:
        item_type = item.get("type") or item.get("kind")
        item_id = item.get("id") or item.get("itemId")
        if item_type in typed_ids and isinstance(item_id, str):
            typed_ids[item_type].add(item_id)
    summary = {
        "task_id": args.task,
        "revision": args.revision,
        "build_id": args.build,
        "project_id": project_id,
        "edit_session_id": session_id,
        "imported_assets": len(asset_map),
        "planned_adds": len(additions),
        "live_video_items": len(typed_ids["video"]),
        "live_audio_items": len(typed_ids["audio"]),
        "live_text_items": len(typed_ids["text"]),
        "expected_total_frames": plan["total_frames"],
        "source_in_frames": [shot["source_in_frame"] for shot in plan["shots"]],
        "status": "awaiting_review",
    }
    (output_dir / "draft_structural_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    tool(
        "review_edit_session",
        {
            "editSessionId": session_id,
            "editorProjectId": project_id,
            "summary": (
                f"{args.task} {args.revision} / {args.build}：{plan['total_frames'] / 30:g}秒，"
                f"{len(plan['shots'])}个非零源入点镜头，{len(plan['captions'])}段中文字幕，Tingting配音；"
                "已按本地固定计划搭建并完成草稿回读。"
            ),
        },
    )
    session = json.loads(session_file.read_text())
    session["status"] = "awaiting_review"
    session_file.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n")
    current_session_file = build_dir / "openchatcut" / "current_session.json"
    current_session = json.loads(current_session_file.read_text())
    current_session["status"] = "awaiting_review"
    current_session_file.write_text(
        json.dumps(current_session, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if args.hold_for_review:
        wait_for_review_application(
            tool, project_id, session_id, (session_file, current_session_file)
        )


if __name__ == "__main__":
    main()
