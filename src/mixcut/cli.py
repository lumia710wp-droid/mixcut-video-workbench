from __future__ import annotations
import argparse
import json
from pathlib import Path
from .core import Blocked, atomic_write, load, writer_lock, safe_id


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="本地短视频混剪：真实证据、待审核门禁、无隐式云调用")
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    p.add_argument("--config", type=Path)
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("director", help="固定编导交接、飞书审核和剪辑门禁；无隐式模型调用")
    s.add_argument(
        "action",
        choices=[
            "schema",
            "delivery-prepare",
            "delivery-status",
            "delivery-publish",
            "build-packet",
            "submit",
            "publish",
            "status",
            "gate",
            "preflight",
            "prepare-audio",
            "step-intent",
            "step-receipt",
            "template-command",
            "compile-adds",
            "compile-updates",
            "run",
            "run-status",
            "sync-candidate",
            "archive-candidate",
            "approve-candidate",
            "export-approved",
            "check-approved",
            "bind-title-style",
            "publish-title-style",
        ],
    )
    s.add_argument("--task")
    s.add_argument("--group")
    s.add_argument("--plan", type=Path)
    s.add_argument("--file", type=Path)
    s.add_argument("--output", type=Path)
    s.add_argument("--step")
    s.add_argument("--session")
    s.add_argument("--parameters", type=Path)
    s.add_argument("--backend", choices=["auto", "mcp", "cli"], default="auto")
    s.add_argument("--candidate-sha256")
    s.add_argument("--review-message")
    s = sub.add_parser("material-search", help="Codex编导的本地事实检索；无模型调用")
    s.add_argument("action", choices=["build", "query", "show", "validate-plan"])
    s.add_argument("--request", type=Path)
    s.add_argument("--result", type=Path)
    s.add_argument("--candidate")
    s.add_argument("--plan", type=Path)
    s.add_argument("--output", type=Path)
    s = sub.add_parser("montage-v3")
    s.add_argument("action", choices=["import-suggestions", "add-proposals", "preflight", "packet"])
    s.add_argument("--group")
    s.add_argument("--task")
    s.add_argument("--file", type=Path)
    sub.add_parser("doctor")
    s = sub.add_parser("scan")
    s.add_argument("--root", required=True)
    s.add_argument("--limit", type=int, default=50)
    sub.add_parser("candidates")
    sub.add_parser("previews")
    sub.add_parser("review-pack")
    s = sub.add_parser("audio")
    s.add_argument("action", choices=["prepare"])
    s.add_argument("--script", required=True, type=Path)
    s = sub.add_parser("validate")
    s.add_argument("file", type=Path)
    s.add_argument("--test-only", action="store_true")
    s = sub.add_parser("g1")
    s.add_argument("action", choices=["prepare", "preflight"])
    s = sub.add_parser("feishu")
    s.add_argument("action", choices=["plan-schema"])
    s = sub.add_parser("sync")
    s.add_argument("action", choices=["pull", "push"])
    s = sub.add_parser("script")
    s.add_argument(
        "action",
        choices=[
            "auth-check",
            "readiness",
            "generate",
            "reconcile",
            "revise-for-tts",
            "reconcile-tts",
        ],
    )
    s.add_argument("--task")
    s.add_argument("--model-config", type=Path)
    s = sub.add_parser("script-button")
    s.add_argument("action", choices=["once", "watch", "start", "stop", "status"])
    s.add_argument("--interval", type=int, default=20)
    s = sub.add_parser("material-index")
    s.add_argument("--task", required=True)
    s.add_argument("--proposals", type=Path)
    s = sub.add_parser("edit-plan")
    s.add_argument("--task", required=True)
    s = sub.add_parser("material-prefill")
    s.add_argument("action", choices=["prepare", "apply", "record", "confirm"])
    s.add_argument("--actor")
    s.add_argument("--proposals", type=Path)
    s.add_argument("--plan", type=Path)
    s.add_argument("--confirmation-plan", type=Path)
    s.add_argument("--receipt", type=Path)
    s.add_argument("--report", type=Path)
    s = sub.add_parser("analyze")
    s.add_argument("--source", required=True)
    s = sub.add_parser("match")
    s.add_argument("--script", required=True, type=Path)
    s.add_argument("--product", required=True, type=Path)
    s = sub.add_parser("plan")
    s.add_argument("--script", required=True, type=Path)
    s = sub.add_parser("run")
    s.add_argument("--task", required=True)
    s.add_argument("--build")
    s.add_argument(
        "--phase",
        choices=["create", "stage", "readback", "submit", "track", "inspect", "full"],
        default="stage",
    )
    s.add_argument("--prepare-only", action="store_true")
    s = sub.add_parser("candidate")
    s.add_argument("action", choices=["register"])
    s.add_argument("--task", required=True)
    s.add_argument("--build")
    s.add_argument("--file", required=True, type=Path)
    s.add_argument("--readback", type=Path)
    s = sub.add_parser("finalize")
    s.add_argument("--task", required=True)
    s.add_argument("--build")
    s = sub.add_parser("workflow")
    s.add_argument("action", choices=["status"], default="status")
    s.add_argument("--task")
    s.add_argument("--batch")
    s = sub.add_parser("batch")
    s.add_argument("action", choices=["preflight"])
    s.add_argument("--manifest", type=Path, required=True)
    s.add_argument("--report", type=Path, required=True)
    s = sub.add_parser("retry")
    s.add_argument("--task", required=True)
    s.add_argument("--stage", choices=["sync"], required=True)
    s = sub.add_parser("report")
    s.add_argument("--batch")
    return p


def execute(args) -> dict:
    w = args.workspace.resolve()
    c = load(args.config or w / "config/config.local.json")
    if args.cmd == "director":
        from .director_stage import (
            FIELDS,
            build_packet,
            submit,
            publish,
            latest,
            gate,
            claim_step,
            complete_step,
        )
        from .director_execution import media_preflight, prepare_audio
        from .workbench import Workbench

        with writer_lock(w, wait_seconds=20):
            if args.action == "schema":
                from .title_style import FIELDS as TITLE_FIELDS
                from .director_runner import FIELDS as RUN_FIELDS
                from .delivery_package import FIELDS as DELIVERY_FIELDS

                return Workbench(w).ensure_schema(
                    {**FIELDS, **TITLE_FIELDS, **RUN_FIELDS, **DELIVERY_FIELDS}
                )
            if args.action == "delivery-prepare" and args.task and args.file:
                from .delivery_package import prepare
                from .director_backend import cli_status
                from .director_runner import check_live, pull_authority

                pull_authority(w, args.task)
                director, script = check_live(w, args.task, Workbench(w))
                candidate = cli_status(w, args.task).get("candidate")
                if not candidate:
                    raise Blocked("DELIVERY_VIDEO", "请先完成并通过技术检查的视频候选", 3)
                return prepare(
                    w,
                    args.task,
                    candidate,
                    director["packet_sha256"],
                    script["content_sha256"],
                    load(args.file),
                )
            if args.action == "delivery-publish" and args.task:
                from .delivery_package import publish as publish_delivery

                if (w / "config/director_authority.local.json").is_file():
                    from .delivery_remote import publish_remote

                    return publish_remote(w, args.task)
                return publish_delivery(w, args.task, Workbench(w))
            if args.action == "delivery-status" and args.task:
                from .delivery_package import verify

                return verify(w, args.task)
            if args.action == "run" and args.task:
                from .director_backend import choose_backend

                if choose_backend(w, args.task, args.backend) == "cli":
                    from .director_cli_runner import run as run_cli
                    from .director_backend import cli_status
                    from .director_candidate_sync import record_sync_status, sync_candidate

                    before = cli_status(w, args.task)
                    result = run_cli(w, args.task, c, Workbench(w))
                    if result.get("status") == "awaiting_content_review" and (
                        before.get("status") != "awaiting_content_review"
                        or before.get("candidate_sync", {}).get("status") == "pending"
                    ):
                        try:
                            result["candidate_sync"] = sync_candidate(w, args.task)
                        except Blocked as error:
                            result["candidate_sync"] = {
                                "status": "pending",
                                "code": error.code,
                                "message": str(error),
                            }
                        record_sync_status(w, args.task, result["candidate_sync"])
                    return result
                from .director_runner import run_until_pause

                return run_until_pause(w, args.task, c, Workbench(w))
            if args.action == "run-status" and args.task:
                from .director_backend import choose_backend, cli_status

                if choose_backend(w, args.task, args.backend) == "cli":
                    return cli_status(w, args.task)
                from .director_runner import run_status

                return run_status(w, args.task)
            if args.action == "sync-candidate" and args.task:
                from .director_candidate_sync import record_sync_status, sync_candidate

                try:
                    result = sync_candidate(w, args.task)
                except Blocked as error:
                    record_sync_status(
                        w,
                        args.task,
                        {"status": "pending", "code": error.code, "message": str(error)},
                    )
                    raise
                record_sync_status(w, args.task, result)
                return result
            if args.action == "archive-candidate" and args.task:
                from .director_archive import sync_cli_archive
                from .director_runner import check_live, pull_authority

                pull_authority(w, args.task)
                check_live(w, args.task, Workbench(w))
                return sync_cli_archive(w, args.task)
            if args.action == "approve-candidate" and args.task:
                from .director_archive import record_chat_approval, sync_cli_archive
                from .director_runner import check_live, pull_authority

                if not args.candidate_sha256 or not args.review_message:
                    raise Blocked(
                        "CLI_APPROVAL_EVIDENCE", "需要候选视频摘要和用户明确的验收原话", 3
                    )
                pull_authority(w, args.task)
                check_live(w, args.task, Workbench(w))
                record_chat_approval(w, args.task, args.candidate_sha256, args.review_message)
                return sync_cli_archive(w, args.task)
            if args.action == "export-approved" and args.task:
                from .director_runner import export_approved

                return export_approved(w, args.task, Workbench(w))
            if args.action == "check-approved" and args.task:
                from .director_runner import check_live, approval_binding

                record, script = check_live(w, args.task, Workbench(w))
                return {
                    "entity": "director_approval_binding",
                    "binding": approval_binding(record, script),
                }
            if args.action in {"bind-title-style", "publish-title-style"} and args.group:
                from .title_style import bind_existing, publish as publish_style
                from .script_stage import latest as latest_text

                record = (
                    bind_existing(w, args.group)
                    if args.action == "bind-title-style"
                    else latest_text(w, args.group)
                )
                if not record:
                    raise Blocked("SCRIPT_DRAFT_MISSING", "先生成文字稿", 3)
                bench = Workbench(w)
                for script in record["scripts"]:
                    publish_style(bench, script)
                return {
                    "status": "title_style_published",
                    "revision": record["revision"],
                    "tasks": [s["task_id"] for s in record["scripts"]],
                    "human_approval_created": False,
                }
            if args.action == "build-packet" and args.plan and args.task and args.output:
                packet = build_packet(w, args.plan, args.task)
                path = args.output.resolve()
                if not path.is_relative_to(w) or (path.exists() and load(path) != packet):
                    raise Blocked("DIRECTOR_OUTPUT", "输出须在工作区且不覆盖不同交接文件", 2)
                atomic_write(path, packet)
                return {"status": "packet_saved", "path": str(path)}
            if args.action == "submit" and args.group and args.file:
                return submit(w, args.group, load(args.file))
            if args.action == "publish" and args.task:
                return publish(Workbench(w), args.task)
            if args.action == "status" and args.task:
                return latest(w, args.task) or {"status": "not_submitted"}
            if args.action == "gate" and args.task:
                return gate(w, args.task)
            if args.action == "preflight" and args.task:
                record = latest(w, args.task)
                if not record:
                    raise Blocked("DIRECTOR_MISSING", "先提交编导脚本", 3)
                return media_preflight(w, record["packet"], c)
            if args.action == "prepare-audio" and args.task:
                from .director_runner import check_live

                check_live(w, args.task, Workbench(w))
                return prepare_audio(w, args.task, c)
            if (
                args.action in {"template-command", "compile-adds", "compile-updates"}
                and args.task
                and args.session
            ):
                from .director_editor import template_command, compile_adds, compile_updates

                if args.action == "template-command":
                    result = template_command(w, args.task, args.session)
                elif args.file:
                    result = (
                        compile_adds(w, args.task, args.session, load(args.file), c)
                        if args.action == "compile-adds"
                        else compile_updates(w, args.task, args.session, load(args.file))
                    )
                else:
                    raise Blocked("DIRECTOR_ARGS", "缺少实际编辑器回读/新增结果文件", 2)
                if not args.output or not args.output.resolve().is_relative_to(w):
                    raise Blocked("DIRECTOR_OUTPUT", "剪辑指令须保存到工作区文件", 2)
                if args.output.exists() and load(args.output) != result:
                    raise Blocked("DIRECTOR_OUTPUT", "不覆盖不同版本指令", 2)
                atomic_write(args.output, result)
                return {"status": "commands_saved", "path": str(args.output.resolve())}
            if args.action == "step-intent" and args.task and args.step and args.parameters:
                return claim_step(w, args.task, args.step, load(args.parameters))
            if (
                args.action == "step-receipt"
                and args.task
                and args.step
                and args.parameters
                and args.file
            ):
                return complete_step(
                    w, args.task, args.step, load(args.parameters), load(args.file)
                )
        raise Blocked("DIRECTOR_ARGS", "编导命令缺少参数", 2)
    if args.cmd == "material-search":
        from .material_search import (
            build_index,
            query_index,
            show_candidate,
            validate_director_plan,
        )

        if args.action == "build":
            with writer_lock(w, wait_seconds=20):
                result = build_index(w)
        elif args.action == "query" and args.request:
            if args.request.stat().st_size > 65536:
                raise Blocked("SEARCH_QUERY", "查询文件过大", 2)
            result = query_index(w, load(args.request))
        elif args.action == "show" and args.result and args.candidate:
            result = show_candidate(w, args.result, args.candidate)
        elif args.action == "validate-plan" and args.plan:
            result = validate_director_plan(w, load(args.plan))
        else:
            raise Blocked(
                "INPUT_REQUIRED", "query需request；show需result/candidate；validate-plan需plan", 2
            )
        if args.output:
            target = args.output.resolve()
            if not target.is_relative_to(w) or target.exists():
                raise Blocked("OUTPUT_PATH", "输出须为工作区内尚不存在的新路径", 2)
            atomic_write(target, result)
        return result
    if args.cmd == "montage-v3":
        from .segments import import_suggestions, add_proposals
        from .executable import policy, preflight as v3_preflight
        from .montage_engine import packet

        with writer_lock(w, wait_seconds=20):
            if args.action == "import-suggestions":
                return import_suggestions(w)
            if args.action == "add-proposals":
                if not args.file:
                    raise Blocked("INPUT_REQUIRED", "请指定本地分段建议JSON", 2)
                return add_proposals(w, load(args.file))
            if not args.group:
                raise Blocked("INPUT_REQUIRED", "请指定需求编号group", 2)
            if args.action == "packet":
                if not args.task:
                    raise Blocked("INPUT_REQUIRED", "请指定任务编号task", 2)
                return packet(w, c, args.group, args.task)
            from .workbench import Workbench

            app = Workbench(w)
            settings = policy(w, args.group)
            if settings.get("generation_policy_version") != 3:
                raise Blocked("V3_POLICY_REQUIRED", "项目未启用执行脚本流程", 3)
            app.v3_material_gate(settings)
            result = v3_preflight(w, settings, app.v3_rows(args.group))
            return {
                "status": "passed",
                "eligible_segments": len(result["segments"]),
                "diversity": result["diversity"],
            }
    if args.cmd == "script-button":
        import fcntl
        import time
        from .script_button import process_once, service_control, ensure_worker_host

        ensure_worker_host(w, args.action)

        if args.action in {"start", "stop", "status"}:
            return service_control(w, args.action)
        if not 10 <= args.interval <= 300:
            raise Blocked("BUTTON_INTERVAL_INVALID", "interval must be 10 to 300 seconds", 2)
        with (w / "ledger/script_button_worker.lock").open("a+") as worker_lock:
            try:
                fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Blocked(
                    "BUTTON_ALREADY_RUNNING", "One script worker is already running", 3
                ) from exc
            while True:
                if args.action == "watch" and (w / "ledger/script_button.stop").exists():
                    return {"status": "stopped"}
                try:
                    with writer_lock(w):
                        result = process_once(w)
                except Blocked as exc:
                    if args.action == "once":
                        raise
                    from .core import now

                    result = {
                        "at": now(),
                        "status": "blocked",
                        "code": exc.code,
                        "automatic_model_retry": False,
                    }
                    atomic_write(w / "reports/script_button_status.json", result)
                if args.action == "once":
                    return result
                print(json.dumps(result, ensure_ascii=False), flush=True)
                time.sleep(args.interval)
    if args.cmd == "batch":
        from .batch_preflight import preflight_batch

        result = preflight_batch(w, c, load(args.manifest))
        # Reports are immutable evidence; choose a new name for a new observation.
        with writer_lock(w):
            if args.report.exists():
                raise Blocked("REPORT_CONFLICT", str(args.report), 6)
            atomic_write(args.report, result)
        return result
    if args.cmd == "review-pack":
        from .review_pack import prepare_review_pack

        with writer_lock(w):
            return prepare_review_pack(w, c)
    if args.cmd == "audio":
        from .audio import prepare_narration, prepare_outline_narration
        from .contracts import validate_record
        from .script_input import validate_reviewable_outline

        script = load(args.script)
        if script.get("entity") == "script_outline":
            request = load(w / "requests" / script["task_id"] / f"{script['request_sha256']}.json")
            validate_reviewable_outline(script, request)
            with writer_lock(w):
                return prepare_outline_narration(w, c, script)
        validate_record(script, w)
        with writer_lock(w):
            return prepare_narration(w, c, script)
    if args.cmd == "doctor":
        from .doctor import doctor

        return doctor(w, c)
    if args.cmd == "scan":
        from .ingest import scan

        result = scan(w, c, args.root, args.limit)
        if result["errors"]:
            raise Blocked("SCAN_PARTIAL", "See reports/scan.json; not all files accepted", 4)
        return result
    if args.cmd in {"candidates", "previews"}:
        from .catalog import seed_candidates, previews

        with writer_lock(w):
            return (seed_candidates if args.cmd == "candidates" else previews)(w, c)
    if args.cmd == "validate":
        from .contracts import validate_record, validate_plan

        d = load(args.file)
        (validate_plan if d.get("entity") == "edit_plan" else validate_record)(
            d, w, production=not args.test_only
        )
        return {"status": "passed", "scope": "contract_only"}
    if args.cmd == "g1":
        from .g1 import prepare
        from .runner import preflight

        if args.action == "prepare":
            return prepare(w, c)
        plan = load(Path(load(w / "reports/g1/preparation.json")["plan"]))
        return preflight(w, c, plan, production=False)
    if args.cmd == "feishu":
        from .feishu import schema_plan

        result = schema_plan(load(w / "config/taxonomy.json"))
        atomic_write(w / "reports/feishu_schema_plan.json", result)
        return result
    if args.cmd in {"sync", "retry"}:
        from .feishu import invoke, push_outbox
        from .sync import sync_pull

        m = load(w / "config/feishu_mapping.local.json")
        with writer_lock(w):
            if args.cmd == "sync" and args.action == "pull":
                return sync_pull(w, m)
            return push_outbox(w, m, lambda argv: invoke(argv, w))
    if args.cmd == "script":
        from .deepseek import (
            generate_outline,
            generate_tts_revision,
            load_model_config,
            readiness,
            reconcile_outline,
            reconcile_tts_revision,
            verify_connection,
        )

        model_config = load_model_config(w, args.model_config)
        if args.action == "auth-check":
            return verify_connection(w, model_config)
        if not args.task:
            raise Blocked("TASK_REQUIRED", "--task is required for this script action", 2)
        if args.action == "readiness":
            return readiness(w, model_config, args.task)
        with writer_lock(w):
            if args.action == "reconcile":
                return reconcile_outline(w, model_config, args.task)
            if args.action == "revise-for-tts":
                return generate_tts_revision(w, model_config, args.task)
            if args.action == "reconcile-tts":
                return reconcile_tts_revision(w, model_config, args.task)
            return generate_outline(w, model_config, args.task)
    if args.cmd == "material-index":
        from .material_index import index_materials

        with writer_lock(w):
            return index_materials(w, args.task, args.proposals)
    if args.cmd == "edit-plan":
        from .production_plan import compile_edit_plan

        with writer_lock(w):
            return compile_edit_plan(w, c, args.task)
    if args.cmd == "material-prefill":
        from .proposal_adoption import (
            apply_prefill,
            prepare_prefill,
            record_adoptions,
            record_confirmations,
        )

        with writer_lock(w):
            if args.action == "prepare":
                return prepare_prefill(w, args.proposals, report_path=args.report)
            if args.action == "apply":
                return apply_prefill(w, args.plan, report_path=args.report)
            if args.action == "confirm":
                if not args.actor:
                    raise Blocked("CONFIRMATION_ACTOR_REQUIRED", "--actor is required", 2)
                return record_confirmations(
                    w,
                    args.actor,
                    confirmation_plan_path=args.confirmation_plan,
                    prefill_plan_path=args.plan,
                    receipt_path=args.receipt,
                )
            return record_adoptions(w, args.plan, receipt_path=args.receipt)
    if args.cmd == "analyze":
        from .analyze import cloud_gate, g1_accepted

        cloud_gate(c, g1_passed=g1_accepted(w), calls_used=0, frames=3)
        raise Blocked("PROVIDER_NOT_VALIDATED", "Real model adapter not accepted yet")
    if args.cmd == "plan":
        from .contracts import validate_record, content_hash

        script = load(args.script)
        validate_record(script, w)
        script["approval"] = None
        script["review_status"] = "pending"
        script["content_sha256"] = content_hash(script)
        safe_id(script["task_id"])
        target = w / "jobs" / script["task_id"] / f"r{script['revision']}" / "script.json"
        if target.exists():
            raise Blocked("REVISION_EXISTS", "Use a new revision; no old script overwritten", 6)
        atomic_write(target, script)
        return {"status": "waiting_script_review", "path": str(target)}
    if args.cmd == "match":
        from .matcher import match, validate_script
        from .ledger import counts

        script = load(args.script)
        product = load(args.product)
        taxonomy = load(w / "config/taxonomy.json")
        validate_script(script, product, taxonomy)
        result = match(
            script,
            load(w / "catalog/clips.json"),
            taxonomy,
            {s["source_id"]: s for s in load(w / "catalog/sources.json")},
            counts(w, True),
        )
        atomic_write(w / "reports/matching.json", result)
        return result
    if args.cmd == "run":
        from .runner import run_task

        return run_task(
            w,
            c,
            args.task,
            phase=args.phase,
            build_id=args.build,
            execute=not args.prepare_only,
        )
    if args.cmd == "candidate":
        from .candidate import register_candidate

        with writer_lock(w):
            return register_candidate(
                w,
                c,
                args.task,
                args.file,
                build_id=args.build,
                readback_path=args.readback,
            )
    if args.cmd == "finalize":
        from .runner import finalize
        from .sync import sync_pull
        from .workflow import current_build, task_fields

        mapping = load(w / "config/feishu_mapping.local.json")
        with writer_lock(w):
            sync_pull(w, mapping)
            snapshot_at, fields = task_fields(w, args.task)
            build_dir, plan = current_build(w, args.task, args.build)
            final = finalize(
                w,
                build_dir,
                plan["build_id"],
                {"snapshot_at": snapshot_at, "fields": fields},
            )
        return {
            "status": "delivered_sync_pending",
            "task_id": args.task,
            "build_id": plan["build_id"],
            "final_path": str(final),
            "next": "./mixcut --json sync push",
        }
    if args.cmd == "workflow":
        from .workflow import workflow_status

        return workflow_status(w, args.task, args.batch)
    states = []
    for p in (w / "jobs").glob("*/r*/*/state.json"):
        states.append({"path": str(p), **load(p)})
    return {
        "scope": "local_job_states",
        "jobs": states,
        "cloud_calls": 0,
        "human_approved": sum(s.get("human_review") == "approved" for s in states),
        "engine_integration": (
            load(w / "config/workflow.local.json").get("engine_integration", "unknown")
            if (w / "config/workflow.local.json").exists()
            else "unknown"
        ),
    }


def main() -> int:
    args = parser().parse_args()
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.cmd == "director" and args.action == "run":
            if result.get("status") == "ready_to_resume":
                return 3
            if result.get("status") == "blocked":
                return (
                    3
                    if result.get("blocked_code")
                    in {
                        "RUNNER_RENDER_PENDING",
                        "RUNNER_CONFIRMATION",
                        "RUNNER_AUDIO_TIMING",
                        "RUNNER_APPLY_PENDING",
                        "RUNNER_TECHNICAL_QC",
                    }
                    else 6
                )
        return 0
    except Blocked as e:
        print(
            json.dumps(
                {
                    "status": "blocked" if e.exit_code == 3 else "failed",
                    "code": e.code,
                    "message": str(e),
                },
                ensure_ascii=False,
            )
        )
        return e.exit_code
    except (ValueError, KeyError, OSError) as e:
        print(
            json.dumps(
                {"status": "failed", "code": "INPUT_ERROR", "message": str(e)}, ensure_ascii=False
            )
        )
        return 2
