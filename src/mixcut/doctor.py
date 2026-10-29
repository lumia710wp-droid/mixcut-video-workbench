from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path

from .core import Blocked, atomic_write, file_hash, load, now
from .engine import OpenChatCutAdapter, local_runtime_status


CAPABILITIES = (
    "create_project",
    "import_local",
    "source_in",
    "duration",
    "placement",
    "mute",
    "captions",
    "audio",
    "readback",
    "save",
    "render",
)

INTERACTIVE_CAPABILITIES = {"import_local", "save", "render"}


def _install_artifact(contents: Path) -> Path | None:
    """Support both unpacked development builds and packaged Electron apps."""
    candidates = (
        contents / "Resources/app/desktop-dist/main.mjs",
        contents / "Resources/app.asar",
    )
    return next((path for path in candidates if path.is_file()), None)


def _validated_capabilities(workspace: Path) -> dict:
    acceptance = workspace / "reports/g1/G1_ACCEPTANCE.md"
    accepted_video = (
        workspace
        / "jobs/g1/accepted_test.mp4"
    )
    g1_validated = acceptance.is_file() and accepted_video.is_file()
    capabilities = {
        name: {
            "supported": g1_validated,
            "status": "live_validated_g1" if g1_validated else "not_validated",
            "requires_interaction": name in INTERACTIVE_CAPABILITIES,
            "evidence": str(acceptance) if g1_validated else None,
        }
        for name in CAPABILITIES
    }
    create_receipt = workspace / "reports/g1/create_project.json"
    if create_receipt.is_file() and not g1_validated:
        capabilities["create_project"].update(
            supported=True,
            status="live_receipt",
            evidence=str(create_receipt),
        )
    return capabilities


def _source_root_status(config: dict) -> list[dict]:
    roots = []
    for item in config.get("source_roots", []):
        path = Path(item["path"])
        roots.append(
            {
                "root_id": item["root_id"],
                "path": str(path),
                "exists": path.is_dir(),
                "readable": path.is_dir() and os.access(path, os.R_OK),
            }
        )
    return roots


def _version_line(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "-version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else None


def doctor(workspace: Path, config: dict) -> dict:
    bundle = Path(config.get("openchatcut", {}).get("app_path", "/Applications/OpenChatCut.app"))
    contents = bundle / "Contents"
    info_path = contents / "Info.plist"
    meta = plistlib.loads(info_path.read_bytes()) if info_path.is_file() else {}
    artifact = _install_artifact(contents)
    installed_schema = contents / "Resources/dist/agent/openchatcut-tool-schemas.json"
    settings_path = workspace / "reports/g0/settings_summary.json"
    tools_path = workspace / "reports/g0/tools.json"
    settings = load(settings_path) if settings_path.exists() else {}
    historical_tools = load(tools_path) if tools_path.exists() else {}
    installed_tools = load(installed_schema) if installed_schema.exists() else {}
    token_file = Path(config.get("openchatcut", {}).get("token_file", Path.home() / ".openchatcut/mcp-token"))
    token_in_process_initial = bool(os.environ.get("OPENCHATCUT_MCP_TOKEN"))
    runtime = local_runtime_status(token_file)
    capabilities = _validated_capabilities(workspace)
    source_roots = _source_root_status(config)
    ffmpeg_version = _version_line(config["paths"]["ffmpeg"])
    ffprobe_version = _version_line(config["paths"]["ffprobe"])
    mcp_probe: dict[str, object] = {
        "performed": False,
        "ok": None,
        "connected_project_ids": [],
        "available_tool_tier": None,
        "error_code": None,
    }
    if runtime["ready"] is True:
        mcp_probe["performed"] = True
        if not os.environ.get("OPENCHATCUT_MCP_TOKEN") and runtime["token_file_secure"]:
            os.environ["OPENCHATCUT_MCP_TOKEN"] = token_file.read_text().strip()
        adapter = None
        try:
            adapter = OpenChatCutAdapter()
            status_result = adapter.call("openchatcut_status")
            status = status_result.get("structuredContent") or {}
            mcp_probe["ok"] = True
            mcp_probe["connected_project_ids"] = status.get("connectedProjectIds") or []
            mcp_probe["available_tool_tier"] = status.get("availableToolTier")
        except (Blocked, OSError) as exc:
            mcp_probe["ok"] = False
            mcp_probe["error_code"] = (
                exc.code if isinstance(exc, Blocked) else "MCP_PROBE_FAILED"
            )
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Blocked:
                    mcp_probe["ok"] = False
                    mcp_probe["error_code"] = "MCP_SESSION_CLOSE_FAILED"
    g1_validated = all(value["supported"] for value in capabilities.values())
    historical_tool_count = len(historical_tools.get("tools", []))
    installed_tool_count = sum(
        len(installed_tools.get(tier, [])) for tier in ("edit", "ask")
    )
    report = {
        "checked_at": now(),
        "status": (
            "ready"
            if artifact
            and ffmpeg_version
            and ffprobe_version
            and mcp_probe["ok"] is not False
            else "needs_attention"
        ),
        "os": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "disk_free_bytes": shutil.disk_usage(workspace).free,
        "app_present": bundle.is_dir(),
        "app_version": meta.get("CFBundleShortVersionString"),
        "app_build": meta.get("CFBundleVersion"),
        "app_path": str(bundle),
        "app_artifact_path": str(artifact) if artifact else None,
        "app_artifact_sha256": file_hash(artifact) if artifact else None,
        "cli_path": shutil.which("occ"),
        "openchatcut_running": runtime["openchatcut_running"],
        "openchatcut_process_check_exit_code": runtime["process_check_exit_code"],
        "mcp_token_in_current_process": token_in_process_initial,
        "mcp_token_file_available": runtime["token_file_nonempty"],
        "mcp_token_file_secure": runtime["token_file_secure"],
        "mcp_live_probe_performed": mcp_probe["performed"],
        "mcp_live_probe_ok": mcp_probe["ok"],
        "mcp_connected_project_ids": mcp_probe["connected_project_ids"],
        "mcp_available_tool_tier": mcp_probe["available_tool_tier"],
        "mcp_probe_error_code": mcp_probe["error_code"],
        "prior_authenticated_mcp_verified": (
            workspace / "reports/g0/openchatcut_status.json"
        ).exists(),
        "project_dir": settings.get("dataDir"),
        "media_dir": settings.get("mediaDir"),
        "import_roots": settings.get("import_roots"),
        "historical_mcp_tools_count": historical_tool_count,
        "installed_tool_schema_count": installed_tool_count,
        "tools_schema_path": str(installed_schema) if installed_schema.exists() else None,
        "tools_schema_sha256": file_hash(installed_schema) if installed_schema.exists() else None,
        "capabilities": capabilities,
        "render_status": "live_validated_g1" if g1_validated else "not_validated",
        "source_roots": source_roots,
        "ffmpeg": config["paths"]["ffmpeg"],
        "ffmpeg_version": ffmpeg_version,
        "ffprobe": config["paths"]["ffprobe"],
        "ffprobe_version": ffprobe_version,
        "cloud_enabled": config["cloud"]["cloud_analysis_enabled"],
        "max_remote_calls": config["cloud"]["max_remote_calls"],
        "feishu_configured": config["feishu"]["enabled"],
        "credentials_saved_by_project": False,
    }
    audit = {
        "OpenChatCut": {
            "version": report["app_version"],
            "path": str(bundle),
            "artifact_path": report["app_artifact_path"],
            "artifact_sha256": report["app_artifact_sha256"],
            "license": "AGPL-3.0-or-later (installed distribution metadata)",
            "mode": "local MCP invocation; no copied/modified upstream code",
        },
        "FFmpeg": {
            "path": report["ffmpeg"],
            "version": report["ffmpeg_version"],
            "mode": "probe, normalization, preview and technical QC",
        },
        "python": {"version": platform.python_version(), "lock": "requirements.lock"},
        "feishu": {
            "transport": "existing lark-cli user auth",
            "mode": "reuse existing secure credentials; no extracted secret",
        },
    }

    # Finish probes and hashes before replacing rolling reports. A layout error cannot
    # leave doctor.json and capabilities.json describing different runs.
    atomic_write(workspace / "reports/doctor.json", report)
    atomic_write(workspace / "reports/capabilities.json", capabilities)
    atomic_write(workspace / "reports/dependency_audit.json", audit)
    root_summary = ", ".join(
        f"{item['root_id']}={'readable' if item['readable'] else 'unavailable'}"
        for item in source_roots
    )
    (workspace / "reports/doctor.md").write_text(
        "# G0 实机检查\n\n"
        f"OpenChatCut {report['app_version']}（build {report['app_build']}），"
        f"运行中={runtime['openchatcut_running']}，CLI={report['cli_path']}，"
        f"工程库={report['project_dir']}。\n\n"
        f"当前进程MCP凭据={'可见' if report['mcp_token_in_current_process'] else '不可见'}，"
        f"本地0600凭据文件={'可用' if runtime['token_file_secure'] else '不可用'}；"
        f"本轮MCP探测={mcp_probe['ok'] if mcp_probe['performed'] else '未执行'}。"
        f"历史真实鉴权={'已验证' if report['prior_authenticated_mcp_verified'] else '无证据'}，"
        f"G1能力={'已真实验收' if g1_validated else '未完整验收'}。\n\n"
        f"素材根目录：{root_summary}。飞书已配置={report['feishu_configured']}；"
        f"收费视觉分析开关={report['cloud_enabled']}。\n\n"
        "详见 doctor.json、capabilities.json、g0 和 dependency_audit.json。\n"
    )
    return report
