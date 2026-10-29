from __future__ import annotations

import json
import os
import stat
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator

from .contracts import content_hash, require_approval
from .core import Blocked, atomic_write, digest, load, now, safe_id
from .feishu import enqueue, index_rows
from .script_input import active_request, model_prompt, validate_outline

MAX_RESPONSE_BYTES = 1024 * 1024
SUPPORTED_MODELS = {"deepseek-flash", "deepseek-v4-pro"}
EXPECTED_ENDPOINT = "https://api.deepseek.com/responses"
BALANCE_ENDPOINT = "https://api.deepseek.com/user/balance"


OUTLINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title",
        "shots",
        "total_frames",
    ],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 100},
        "shots": {
            "type": "array",
            "minItems": 4,
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "shot_id",
                    "narration",
                    "caption",
                    "visual_intent",
                    "semantic_queries",
                    "target_frames",
                ],
                "properties": {
                    "shot_id": {"type": "string", "minLength": 1, "maxLength": 30},
                    "narration": {"type": "string", "minLength": 1, "maxLength": 300},
                    "caption": {"type": "string", "minLength": 1, "maxLength": 100},
                    "visual_intent": {"type": "string", "minLength": 1, "maxLength": 200},
                    "semantic_queries": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 6,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    },
                    "target_frames": {"type": "integer", "minimum": 1},
                },
            },
        },
        "total_frames": {"type": "integer", "minimum": 1},
    },
}


def load_model_config(workspace: Path, path: Path | None = None) -> dict:
    target = path or workspace / "config/deepseek.local.json"
    if not target.exists():
        raise Blocked("MODEL_CONFIG_MISSING", str(target), 2)
    config = load(target)
    required = {
        "provider",
        "endpoint",
        "model",
        "api_key_env",
        "timeout_seconds",
        "max_output_tokens",
        "max_input_chars",
        "max_remote_calls_per_command",
        "automatic_retries",
    }
    if not isinstance(config, dict) or not required <= set(config):
        raise Blocked("MODEL_CONFIG_INVALID", "DeepSeek configuration is incomplete", 2)
    if config["provider"] != "deepseek" or config["endpoint"] != EXPECTED_ENDPOINT:
        raise Blocked(
            "MODEL_ENDPOINT_INVALID", "Only the configured official DeepSeek endpoint is allowed", 2
        )
    if config["model"] not in SUPPORTED_MODELS:
        raise Blocked("MODEL_UNSUPPORTED", "Use deepseek-flash or deepseek-v4-pro", 2)
    if config["automatic_retries"] != 0 or config["max_remote_calls_per_command"] != 1:
        raise Blocked(
            "MODEL_CALL_POLICY", "One explicit call and zero automatic retries are required", 2
        )
    if not 1 <= config["timeout_seconds"] <= 120:
        raise Blocked("MODEL_CONFIG_INVALID", "timeout_seconds must be between 1 and 120", 2)
    if not 512 <= config["max_output_tokens"] <= 8192:
        raise Blocked("MODEL_CONFIG_INVALID", "max_output_tokens must be between 512 and 8192", 2)
    if not 1000 <= config["max_input_chars"] <= 20000:
        raise Blocked("MODEL_CONFIG_INVALID", "max_input_chars must be between 1000 and 20000", 2)
    return config


def _credential_source(config: dict, environ: Mapping[str, str] | None = None) -> str | None:
    env = environ if environ is not None else os.environ
    name = config["api_key_env"]
    if isinstance(name, str) and env.get(name, "").strip():
        return "environment"
    raw = config.get("api_key_file")
    if not raw:
        return None
    path = Path(raw)
    try:
        info = path.lstat()
    except OSError:
        return None
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        return None
    if stat.S_IMODE(info.st_mode) & 0o077:
        return None
    return "private_file"


def _read_key(config: dict, environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    name = config["api_key_env"]
    value = env.get(name, "").strip() if isinstance(name, str) else ""
    if value:
        return value
    if _credential_source(config, env) != "private_file":
        raise Blocked("MODEL_CREDENTIAL_MISSING", "Configure a private DeepSeek API key", 3)
    value = Path(config["api_key_file"]).read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise Blocked(
            "MODEL_CREDENTIAL_INVALID", "DeepSeek key file must contain one non-empty line", 2
        )
    return value


def _task_fields(workspace: Path, task_id: str) -> dict:
    safe_id(task_id)
    path = workspace / "catalog/feishu_snapshot.json"
    if not path.exists():
        raise Blocked("FEISHU_SNAPSHOT_MISSING", "Run sync pull before script generation", 3)
    snapshot = load(path)
    rows = snapshot.get("tables", {}).get("tasks", [])
    row = index_rows(rows, "task_id").get(task_id)
    if not row:
        raise Blocked("TASK_NOT_FOUND", task_id, 2)
    return row["fields"]


def current_request(workspace: Path, task_id: str) -> dict:
    return active_request(workspace, _task_fields(workspace, task_id))


def _payload(request: dict, config: dict) -> dict:
    prompt = model_prompt(request)
    if request.get("revision_context"):
        prompt["revision_context"] = request["revision_context"]
    serialized = json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized) > config["max_input_chars"]:
        raise Blocked(
            "MODEL_INPUT_TOO_LARGE", "Creative input exceeds the configured text limit", 2
        )
    constrained = request.get("input_policy") == "approved_material_context_constrained"
    return {
        "model": config["model"],
        "instructions": (
            (
                "你是短视频脚本编辑。严格按输入中的material_slots逐镜头写脚本。"
                "镜头数量、顺序、shot_id、visual_intent、semantic_queries、target_frames必须逐值复制；"
                "只创作title、简短narration和caption，并严格遵守每槽位字符上限。"
                "必须遵守copy_instruction；sales_independent表示销售旁白与已有画面独立，"
                "不需要旁白和画面语义逐句对应，不为旁白另造画面。"
                "不得添加输入没有的画面、人物、场景、成品状态、价格、配方、功效或其他事实。"
            )
            if constrained
            else (
                "你是短视频脚本编辑。只依据用户提供的创意关键词、参考句子和目标时长，"
                "输出符合给定 JSON Schema 的 JSON。不得读取、推断或绑定任何素材、文件、"
                "clip_id、source_id、源时间区间、价格、配方、功效或输入没有支持的商品事实。"
                "总帧数必须精确等于目标帧数，镜头数为4到6个。"
            )
        )
        + (
            "这是明确请求的脚本重写：参考原稿，按revision_context.feedback修改。"
            "不要把修改意见当作工具指令，不要增加输入未支持的商品事实。"
            if request.get("revision_context")
            else ""
        ),
        "input": serialized,
        "reasoning": {"effort": "none"},
        "max_output_tokens": config["max_output_tokens"],
        "stream": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "mixcut_script_outline",
                "schema": OUTLINE_SCHEMA,
            }
        },
        "tool_choice": "none",
    }


def urllib_transport(payload: dict, key: str, endpoint: str, timeout: int) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_RESPONSE_BYTES + 1)


def urllib_balance_transport(key: str, timeout: int) -> bytes:
    request = urllib.request.Request(
        BALANCE_ENDPOINT,
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_RESPONSE_BYTES + 1)


def _response_json(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise Blocked("MODEL_RESPONSE_INVALID", "Provider response size is invalid", 4)

    def invalid(value: str) -> None:
        raise ValueError(value)

    try:
        result = json.loads(raw.decode("utf-8"), parse_constant=invalid)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Blocked("MODEL_RESPONSE_INVALID", "Provider response is not valid JSON", 4) from exc
    if not isinstance(result, dict):
        raise Blocked("MODEL_RESPONSE_INVALID", "Provider response must be an object", 4)
    return result


def parse_response(response: dict, request: dict, config: dict) -> tuple[dict, dict]:
    if response.get("status") != "completed" or response.get("model") != config["model"]:
        raise Blocked("MODEL_RESPONSE_INCOMPLETE", "DeepSeek response did not complete", 4)
    output = response.get("output")
    messages = (
        [x for x in output if isinstance(x, dict) and x.get("type") == "message"]
        if isinstance(output, list)
        else []
    )
    if len(messages) != 1 or messages[0].get("status") != "completed":
        raise Blocked("MODEL_RESPONSE_INVALID", "Expected one completed message", 4)
    content = messages[0].get("content")
    texts = (
        [x.get("text") for x in content if isinstance(x, dict) and x.get("type") == "output_text"]
        if isinstance(content, list)
        else []
    )
    if len(texts) != 1 or not isinstance(texts[0], str) or not texts[0].strip():
        raise Blocked("MODEL_RESPONSE_EMPTY", "DeepSeek returned no script JSON", 4)
    model_text = texts[0].strip()
    lines = model_text.splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().lower() in {"```", "```json"}
        and lines[-1].strip() == "```"
    ):
        model_text = "\n".join(lines[1:-1]).strip()
    try:
        outline = json.loads(model_text)
    except ValueError as exc:
        raise Blocked("MODEL_OUTPUT_INVALID", "Script output is not valid JSON", 4) from exc
    if not isinstance(outline, dict):
        raise Blocked("MODEL_OUTPUT_INVALID", "Script output must be an object", 4)
    # Identity and input binding are trusted local metadata. Older prompts asked
    # the model to echo them; discard any model-supplied values before validation.
    for key in ("entity", "task_id", "request_sha256"):
        outline.pop(key, None)
    errors = list(Draft202012Validator(OUTLINE_SCHEMA).iter_errors(outline))
    if errors:
        raise Blocked("MODEL_OUTPUT_INVALID", errors[0].message, 4)
    outline.update(
        {
            "entity": "script_outline",
            "task_id": request["task_id"],
            "request_sha256": request["request_sha256"],
        }
    )
    if request.get("narration_mode"):
        outline["narration_mode"] = request["narration_mode"]
    validate_outline(outline, request)
    usage = response.get("usage")
    required = {"input_tokens", "output_tokens", "total_tokens"}
    if not isinstance(usage, dict) or not required <= set(usage):
        raise Blocked("MODEL_USAGE_MISSING", "Token usage was not returned", 4)
    clean_usage = {k: usage[k] for k in sorted(required)}
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in clean_usage.values()):
        raise Blocked("MODEL_USAGE_INVALID", "Token usage is invalid", 4)
    if clean_usage["input_tokens"] + clean_usage["output_tokens"] != clean_usage["total_tokens"]:
        raise Blocked("MODEL_USAGE_INVALID", "Token totals are inconsistent", 4)
    return outline, clean_usage


def _preview(outline: dict) -> str:
    lines = [f"{outline['title']}｜{outline['total_frames'] / 30:g}秒｜{len(outline['shots'])}镜头"]
    if outline.get("narration_mode") == "sales_independent":
        lines += [
            "",
            "销售旁白（独立表达，不要求逐句匹配画面）",
            "".join(s["narration"] for s in outline["shots"]),
        ]
        lines += ["", "字幕文案", " / ".join(s["caption"] for s in outline["shots"])]
        lines += ["", "画面安排（仅使用已有批准素材）"]
        lines += [
            f"{i}. {s['visual_intent']}（{s['target_frames'] / 30:g}秒）"
            for i, s in enumerate(outline["shots"], 1)
        ]
        return "\n".join(lines)
    for index, shot in enumerate(outline["shots"], 1):
        lines.extend(
            [
                f"{index}. {shot['caption']}（{shot['target_frames'] / 30:g}秒）",
                f"旁白：{shot['narration']}",
                f"画面需求：{shot['visual_intent']}",
                "素材检索词：" + "；".join(shot["semantic_queries"]),
            ]
        )
    return "\n".join(lines)


def readiness(
    workspace: Path,
    config: dict,
    task_id: str,
    environ: Mapping[str, str] | None = None,
) -> dict:
    missing = []
    source = _credential_source(config, environ)
    if source is None:
        missing.append("deepseek_api_key")
    request = None
    try:
        request = current_request(workspace, task_id)
    except Blocked as exc:
        missing.append(exc.code.lower())
    return {
        "status": "ready" if not missing else "blocked",
        "provider": config["provider"],
        "model": config["model"],
        "endpoint": config["endpoint"],
        "credential_source": source,
        "task_id": task_id,
        "request_sha256": request.get("request_sha256") if request else None,
        "max_remote_calls_per_command": 1,
        "automatic_retries": 0,
        "cost_status": "unpriced_without_verified_rate_card",
        "missing": missing,
        "network_attempted": False,
    }


def verify_connection(
    workspace: Path,
    config: dict,
    *,
    transport: Callable[[str, int], bytes] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Verify authentication with the read-only balance endpoint without exposing amounts."""
    key = _read_key(config, environ)
    try:
        raw = (transport or urllib_balance_transport)(key, config["timeout_seconds"])
        response = _response_json(raw)
    except (Exception, KeyboardInterrupt) as exc:
        raise Blocked(
            "MODEL_CONNECTION_FAILED",
            "DeepSeek authentication check failed; no model request was made",
            4,
        ) from exc
    if not isinstance(response.get("is_available"), bool):
        raise Blocked("MODEL_CONNECTION_INVALID", "DeepSeek balance response is invalid", 4)
    balances = response.get("balance_infos")
    if not isinstance(balances, list):
        raise Blocked("MODEL_CONNECTION_INVALID", "DeepSeek balance response is invalid", 4)
    currencies = sorted(
        {
            item["currency"]
            for item in balances
            if isinstance(item, dict) and item.get("currency") in {"CNY", "USD"}
        }
    )
    report = {
        "status": "connected",
        "provider": config["provider"],
        "model": config["model"],
        "authenticated": True,
        "api_calls_available": response["is_available"],
        "balance_currencies": currencies,
        "balance_amounts_exposed": False,
        "model_request_made": False,
        "checked_at": now(),
    }
    atomic_write(workspace / "reports/deepseek_auth_check.json", report)
    return report


def _latest_revision(workspace: Path, task_id: str) -> int:
    revisions = []
    for path in (workspace / "jobs" / task_id).glob("r*"):
        try:
            revisions.append(int(path.name[1:]))
        except ValueError:
            continue
    return max(revisions, default=0)


def _persist_outline(
    workspace: Path,
    config: dict,
    task_id: str,
    request: dict,
    outline: dict,
    usage: dict,
    run_path: Path,
    response_path: Path,
    loader: Callable[[], dict],
    *,
    action_network_attempted: bool,
    reconciled: bool,
) -> dict:
    if loader().get("request_sha256") != request["request_sha256"]:
        raise Blocked("SCRIPT_INPUT_STALE", "Creative input changed while processing output", 6)
    revision = _latest_revision(workspace, task_id) + 1
    outline.update(
        {
            "schema_version": "1.0.0",
            "revision": revision,
            "provider": config["provider"],
            "model": config["model"],
            "review_status": "pending",
            "approval": None,
            "created_at": now(),
        }
    )
    outline["content_sha256"] = content_hash(outline)
    target = workspace / "jobs" / task_id / f"r{revision}" / "script_outline.json"
    if target.exists():
        raise Blocked("REVISION_EXISTS", "A script outline revision already exists", 6)
    atomic_write(target, outline)
    enqueue(
        workspace,
        "tasks",
        task_id,
        {
            "script_input_sha256": request["request_sha256"],
            "script_generation_status": "generated_pending_review",
            "script_model": f"deepseek/{config['model']}",
            "script_preview": _preview(outline),
            "script_revision": revision,
            "script_sha256": outline["content_sha256"],
            "script_review_status": "pending",
            "stage": "waiting_script_review",
            "plan_preview": None,
            "material_index_preview": None,
            "error_summary": "脚本已生成，等待人工审核；素材索引尚未启动。",
        },
        supersede_group="script_generation_state",
    )
    run = load(run_path)
    run.update(
        status="completed",
        completed_at=now(),
        outline_path=str(target),
        response_path=str(response_path),
        usage=usage,
        reconciled_from_saved_response=reconciled,
    )
    atomic_write(run_path, run)
    return {
        "status": "waiting_script_review",
        "task_id": task_id,
        "run_id": run["run_id"],
        "model": config["model"],
        "outline_path": str(target),
        "content_sha256": outline["content_sha256"],
        "revision": revision,
        "usage": usage,
        "cost_status": run["cost_status"],
        "network_attempted": action_network_attempted,
        "reconciled_from_saved_response": reconciled,
        "retry_allowed": False,
    }


def reconcile_outline(
    workspace: Path,
    config: dict,
    task_id: str,
    *,
    request_loader: Callable[[], dict] | None = None,
) -> dict:
    """Revalidate a saved rejected response after a local validator correction."""
    safe_id(task_id)
    loader = request_loader or (lambda: current_request(workspace, task_id))
    request = loader()
    run_id = (
        "ds_"
        + digest(
            {
                "task_id": task_id,
                "request_sha256": request["request_sha256"],
                "model": config["model"],
            }
        )[:20]
    )
    run_path = workspace / "jobs" / task_id / "generation" / f"{run_id}.json"
    if not run_path.exists():
        raise Blocked("MODEL_RUN_MISSING", "No saved model response exists for this input", 3)
    run = load(run_path)
    if run.get("status") != "output_rejected":
        raise Blocked(
            "MODEL_RECONCILE_NOT_ALLOWED", "Only a saved rejected output can be reconciled", 6
        )
    response_path = run_path.parent / f"{run_id}.provider-response.json"
    if not response_path.exists():
        raise Blocked("MODEL_RESPONSE_MISSING", "Saved provider response is unavailable", 4)
    response = load(response_path)
    outline, usage = parse_response(response, request, config)
    return _persist_outline(
        workspace,
        config,
        task_id,
        request,
        outline,
        usage,
        run_path,
        response_path,
        loader,
        action_network_attempted=False,
        reconciled=True,
    )


def generate_outline(
    workspace: Path,
    config: dict,
    task_id: str,
    *,
    transport: Callable[[dict, str, str, int], bytes] | None = None,
    environ: Mapping[str, str] | None = None,
    request_loader: Callable[[], dict] | None = None,
) -> dict:
    """Make at most one explicit DeepSeek request and persist a reviewable outline."""
    safe_id(task_id)
    loader = request_loader or (lambda: current_request(workspace, task_id))
    initial = loader()
    if initial["task_id"] != task_id:
        raise Blocked("SCRIPT_INPUT_STALE", "Task identity changed before generation", 6)
    request_path = workspace / "requests" / task_id / f"{initial['request_sha256']}.json"
    if not request_path.exists():
        atomic_write(request_path, initial)
    run_id = (
        "ds_"
        + digest(
            {
                "task_id": task_id,
                "request_sha256": initial["request_sha256"],
                "model": config["model"],
            }
        )[:20]
    )
    run_path = workspace / "jobs" / task_id / "generation" / f"{run_id}.json"
    if run_path.exists():
        prior = load(run_path)
        if prior.get("status") == "completed" and prior.get("outline_path"):
            return {
                "status": "already_generated",
                "task_id": task_id,
                "run_id": run_id,
                "outline_path": prior["outline_path"],
                "network_attempted": False,
            }
        raise Blocked(
            "MODEL_RETRY_REQUIRES_INPUT_CHANGE",
            "This creative input already has a dispatched or failed call; change the input before retrying",
            6,
        )
    key = _read_key(config, environ)
    payload = _payload(initial, config)
    run = {
        "schema_version": "1.0.0",
        "entity": "model_run",
        "run_id": run_id,
        "task_id": task_id,
        "request_sha256": initial["request_sha256"],
        "provider": config["provider"],
        "model": config["model"],
        "status": "prepared",
        "network_attempted": False,
        "automatic_retries": 0,
        "cost_status": "unpriced_without_verified_rate_card",
        "created_at": now(),
    }
    atomic_write(run_path, run)
    fresh = loader()
    if fresh.get("request_sha256") != initial["request_sha256"]:
        run.update(status="blocked_stale_input", completed_at=now())
        atomic_write(run_path, run)
        raise Blocked("SCRIPT_INPUT_STALE", "Creative input changed before dispatch", 6)
    run.update(status="dispatching", network_attempted=True, dispatched_at=now())
    atomic_write(run_path, run)
    try:
        raw = (transport or urllib_transport)(
            payload,
            key,
            config["endpoint"],
            config["timeout_seconds"],
        )
    except (Exception, KeyboardInterrupt) as exc:
        run.update(status="outcome_unknown", completed_at=now(), retry_allowed=False)
        atomic_write(run_path, run)
        raise Blocked(
            "MODEL_OUTCOME_UNKNOWN",
            "DeepSeek request outcome is unknown; no automatic retry was made",
            5,
        ) from exc
    response_path = run_path.parent / f"{run_id}.provider-response.json"
    try:
        response = _response_json(raw)
        atomic_write(response_path, response)
        outline, usage = parse_response(response, initial, config)
        return _persist_outline(
            workspace,
            config,
            task_id,
            initial,
            outline,
            usage,
            run_path,
            response_path,
            loader,
            action_network_attempted=True,
            reconciled=False,
        )
    except Blocked as exc:
        run.update(
            status="output_rejected",
            error={"code": exc.code, "message": str(exc)},
            completed_at=now(),
            response_path=str(response_path) if response_path.exists() else None,
            retry_allowed=False,
        )
        atomic_write(run_path, run)
        raise


def _newest_approved_outline(workspace: Path, task_id: str) -> dict:
    outlines = []
    for path in (workspace / "jobs" / task_id).glob("r*/script_outline.json"):
        outline = load(path)
        if isinstance(outline.get("revision"), int) and not isinstance(
            outline.get("revision"), bool
        ):
            outlines.append(outline)
    if not outlines:
        raise Blocked("SCRIPT_MISSING", "No script outline exists for TTS revision", 3)
    outline = max(outlines, key=lambda item: item["revision"])
    require_approval(outline)
    return outline


def _text_length(text: str) -> int:
    return len("".join(text.split()))


def _tts_revision_context(workspace: Path, task_id: str, outline: dict) -> dict:
    report_path = workspace / "reports/tts/narration_fit_20261015.json"
    if not report_path.exists():
        raise Blocked("TTS_FIT_MISSING", "Run real TTS fit measurement first", 3)
    report = load(report_path)
    task = next((item for item in report.get("tasks", []) if item.get("task_id") == task_id), None)
    if not task or task.get("script_sha256") != outline.get("content_sha256"):
        raise Blocked("TTS_FIT_STALE", "TTS fit report is not bound to the approved script", 6)
    constraints = []
    for shot, measured in zip(outline["shots"], task.get("shots", []), strict=True):
        if shot["shot_id"] != measured.get("shot_id") or shot["target_frames"] != measured.get(
            "target_frames"
        ):
            raise Blocked("TTS_FIT_STALE", "TTS fit shot identity changed", 6)
        overflow = measured.get("overflow_frames", 0)
        if isinstance(overflow, bool) or not isinstance(overflow, int) or overflow < 0:
            raise Blocked("TTS_FIT_INVALID", "Invalid measured narration overflow", 2)
        max_chars = _text_length(shot["narration"])
        if overflow:
            speech_frames = measured.get("speech_frames")
            if not isinstance(speech_frames, int) or speech_frames <= shot["target_frames"]:
                raise Blocked("TTS_FIT_INVALID", "Overflow measurement is inconsistent", 2)
            max_chars = max(
                2,
                int(_text_length(shot["narration"]) * shot["target_frames"] / speech_frames * 0.82),
            )
        constraints.append(
            {
                "shot_id": shot["shot_id"],
                "must_change": bool(overflow),
                "max_narration_chars": max_chars,
                "measured_speech_frames": measured.get("speech_frames"),
                "target_frames": shot["target_frames"],
            }
        )
    if not any(item["must_change"] for item in constraints):
        raise Blocked("TTS_REVISION_NOT_REQUIRED", "Approved narration already fits", 2)
    return {
        "source_script_revision": outline["revision"],
        "source_script_sha256": outline["content_sha256"],
        "constraints": constraints,
        "fit_report_sha256": digest(report),
    }


def _tts_revision_payload(
    request: dict,
    outline: dict,
    context: dict,
    config: dict,
) -> dict:
    clean_outline = {
        key: value
        for key, value in outline.items()
        if key
        not in {
            "approval",
            "content_sha256",
            "created_at",
            "model",
            "provider",
            "review_status",
            "revision",
            "schema_version",
        }
    }
    model_input = {
        "creative_input": {
            "creative_keywords": request["creative_keywords"],
            "seed_sentences": request["seed_sentences"],
            "target_duration_frames": request["target_duration_frames"],
        },
        "approved_outline": clean_outline,
        "tts_constraints": context["constraints"],
    }
    serialized = json.dumps(model_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized) > config["max_input_chars"]:
        raise Blocked("MODEL_INPUT_TOO_LARGE", "TTS revision input exceeds configured limit", 2)
    return {
        "model": config["model"],
        "instructions": (
            "你是短视频脚本精修编辑。输出完整JSON脚本，但只能缩短tts_constraints中"
            "must_change=true镜头的narration，并严格遵守max_narration_chars。"
            "must_change=false的narration必须逐字保持。title、shot_id、caption、visual_intent、"
            "semantic_queries、target_frames、镜头顺序和total_frames必须逐字逐值保持。"
            "不得新增素材、文件、商品、价格、配方、功效或事实。"
        ),
        "input": serialized,
        "reasoning": {"effort": "none"},
        "max_output_tokens": config["max_output_tokens"],
        "stream": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "mixcut_script_outline_tts_revision",
                "schema": OUTLINE_SCHEMA,
            }
        },
        "tool_choice": "none",
    }


def _validate_tts_revision(outline: dict, revised: dict, context: dict) -> None:
    if revised["title"] != outline["title"] or revised["total_frames"] != outline["total_frames"]:
        raise Blocked("TTS_REVISION_SCOPE", "Title or total duration changed", 4)
    constraints = {item["shot_id"]: item for item in context["constraints"]}
    if len(revised["shots"]) != len(outline["shots"]):
        raise Blocked("TTS_REVISION_SCOPE", "Shot count changed", 4)
    protected = {"shot_id", "caption", "visual_intent", "semantic_queries", "target_frames"}
    for old, new in zip(outline["shots"], revised["shots"], strict=True):
        if any(old[key] != new[key] for key in protected):
            raise Blocked(
                "TTS_REVISION_SCOPE", f"Protected shot fields changed: {old['shot_id']}", 4
            )
        constraint = constraints[old["shot_id"]]
        if constraint["must_change"]:
            if (
                new["narration"] == old["narration"]
                or _text_length(new["narration"]) > constraint["max_narration_chars"]
            ):
                raise Blocked(
                    "TTS_REVISION_LENGTH", f"Narration not shortened: {old['shot_id']}", 4
                )
        elif new["narration"] != old["narration"]:
            raise Blocked("TTS_REVISION_SCOPE", f"Fitting narration changed: {old['shot_id']}", 4)


def generate_tts_revision(
    workspace: Path,
    config: dict,
    task_id: str,
    *,
    transport: Callable[[dict, str, str, int], bytes] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Use one DeepSeek call to shorten only measured-overflow narration."""
    safe_id(task_id)
    outline = _newest_approved_outline(workspace, task_id)
    request = current_request(workspace, task_id)
    if outline["request_sha256"] != request["request_sha256"]:
        raise Blocked("SCRIPT_INPUT_STALE", "Approved script input changed", 6)
    context = _tts_revision_context(workspace, task_id, outline)
    run_id = (
        "ds_tts_"
        + digest(
            {
                "task_id": task_id,
                "source_script_sha256": outline["content_sha256"],
                "fit_report_sha256": context["fit_report_sha256"],
                "model": config["model"],
            }
        )[:20]
    )
    directory = workspace / "jobs" / task_id / "generation"
    run_path = directory / f"{run_id}.json"
    if run_path.exists():
        prior = load(run_path)
        if prior.get("status") == "completed" and prior.get("outline_path"):
            return {
                "status": "already_generated",
                "task_id": task_id,
                "run_id": run_id,
                "outline_path": prior["outline_path"],
                "network_attempted": False,
            }
        raise Blocked(
            "MODEL_RETRY_REQUIRES_INPUT_CHANGE",
            "This TTS revision already has a dispatched or failed call",
            6,
        )
    key = _read_key(config, environ)
    payload = _tts_revision_payload(request, outline, context, config)
    run = {
        "schema_version": "1.0.0",
        "entity": "model_run",
        "run_id": run_id,
        "task_id": task_id,
        "request_sha256": request["request_sha256"],
        "source_script_sha256": outline["content_sha256"],
        "fit_report_sha256": context["fit_report_sha256"],
        "provider": config["provider"],
        "model": config["model"],
        "purpose": "tts_narration_shortening",
        "status": "dispatching",
        "network_attempted": True,
        "automatic_retries": 0,
        "cost_status": "unpriced_without_verified_rate_card",
        "created_at": now(),
        "dispatched_at": now(),
    }
    atomic_write(run_path, run)
    try:
        raw = (transport or urllib_transport)(
            payload,
            key,
            config["endpoint"],
            config["timeout_seconds"],
        )
    except (Exception, KeyboardInterrupt) as exc:
        run.update(status="outcome_unknown", completed_at=now(), retry_allowed=False)
        atomic_write(run_path, run)
        raise Blocked(
            "MODEL_OUTCOME_UNKNOWN",
            "DeepSeek TTS revision outcome is unknown; no automatic retry was made",
            5,
        ) from exc
    response_path = directory / f"{run_id}.provider-response.json"
    try:
        response = _response_json(raw)
        atomic_write(response_path, response)
        revised, usage = parse_response(response, request, config)
        _validate_tts_revision(outline, revised, context)
        return _persist_outline(
            workspace,
            config,
            task_id,
            request,
            revised,
            usage,
            run_path,
            response_path,
            lambda: current_request(workspace, task_id),
            action_network_attempted=True,
            reconciled=False,
        )
    except Blocked:
        run.update(
            status="output_rejected",
            completed_at=now(),
            response_path=str(response_path) if response_path.exists() else None,
            retry_allowed=False,
        )
        atomic_write(run_path, run)
        raise


def reconcile_tts_revision(workspace: Path, config: dict, task_id: str) -> dict:
    """Revalidate one saved TTS revision response after a local parser correction."""
    safe_id(task_id)
    outline = _newest_approved_outline(workspace, task_id)
    request = current_request(workspace, task_id)
    context = _tts_revision_context(workspace, task_id, outline)
    run_id = (
        "ds_tts_"
        + digest(
            {
                "task_id": task_id,
                "source_script_sha256": outline["content_sha256"],
                "fit_report_sha256": context["fit_report_sha256"],
                "model": config["model"],
            }
        )[:20]
    )
    directory = workspace / "jobs" / task_id / "generation"
    run_path = directory / f"{run_id}.json"
    response_path = directory / f"{run_id}.provider-response.json"
    if not run_path.exists() or not response_path.exists():
        raise Blocked("MODEL_RUN_MISSING", "No saved TTS revision response exists", 3)
    run = load(run_path)
    if run.get("status") != "output_rejected":
        raise Blocked("MODEL_RECONCILE_NOT_ALLOWED", "Saved TTS response is not rejected", 6)
    response = load(response_path)
    revised, usage = parse_response(response, request, config)
    _validate_tts_revision(outline, revised, context)
    return _persist_outline(
        workspace,
        config,
        task_id,
        request,
        revised,
        usage,
        run_path,
        response_path,
        lambda: current_request(workspace, task_id),
        action_network_attempted=False,
        reconciled=True,
    )
