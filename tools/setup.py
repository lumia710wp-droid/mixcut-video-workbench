#!/usr/bin/env python3
"""Initialize a private local workspace and a user-selected Feishu Base."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mixcut.core import Blocked, atomic_write, load, now  # noqa: E402
from mixcut.feishu import invoke, schema_plan, unwrap  # noqa: E402
from mixcut.workbench import GROUP_FIELDS  # noqa: E402
from mixcut.title_style import FIELDS as TITLE_FIELDS  # noqa: E402
from mixcut.director_stage import FIELDS as DIRECTOR_FIELDS  # noqa: E402
from mixcut.director_runner import FIELDS as RUN_FIELDS  # noqa: E402
from mixcut.delivery_package import FIELDS as DELIVERY_FIELDS  # noqa: E402


def full_schema(root):
    plan = schema_plan(load(root / "config/taxonomy.json"))
    task = plan["tables"]["tasks"]
    for key, (label, kind) in {
        **GROUP_FIELDS,
        **TITLE_FIELDS,
        **DIRECTOR_FIELDS,
        **RUN_FIELDS,
        **DELIVERY_FIELDS,
    }.items():
        if key in task["labels"]:
            if task["labels"][key] != label:
                raise Blocked("SCHEMA_DUPLICATE", f"字段映射冲突: {key}", 6)
            continue
        if any(field["name"] == label for field in task["fields"]):
            raise Blocked("SCHEMA_DUPLICATE", f"字段名称重复: {label}", 6)
        task["labels"][key] = label
        task["fields"].append({"name": label, "type": kind, "description": f"{key} | program"})
    return plan


def init(root, source):
    path = root / "config/config.local.json"
    if path.exists():
        raise Blocked("CONFIG_EXISTS", "本地配置已存在，未覆盖", 6)
    if source and not Path(source).expanduser().is_dir():
        raise Blocked("SOURCE_MISSING", "素材目录不存在", 2)
    config = load(root / "config/config.example.json")
    config["workspace_root"] = str(root)
    if source:
        config["source_roots"] = [
            {"root_id": "materials", "path": str(Path(source).expanduser().resolve())}
        ]
    for key in ("ffmpeg", "ffprobe"):
        config["paths"][key] = shutil.which(key) or "discover"
    for directory in ("catalog", "jobs", "ledger", "reports", "audio", "media", "config/goods"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for name in ("sources.json", "clips.json"):
        target = root / "catalog" / name
        if not target.exists():
            atomic_write(target, [])
    atomic_write(path, config)
    return {
        "status": "created",
        "config": str(path),
        "source_configured": bool(source),
        "ffmpeg_available": all(config["paths"][x] != "discover" for x in ("ffmpeg", "ffprobe")),
    }


def invoke_once(root, journal, name, args, call):
    receipt = journal / (name + ".json")
    if receipt.exists():
        prior = load(receipt)
        if prior["status"] != "completed":
            raise Blocked(
                "SETUP_UNKNOWN", f"{name} 的结果未知，请在飞书确认后再处理；不会重复创建", 6
            )
        return prior["result"]
    atomic_write(receipt, {"status": "started", "at": now()})
    result = call(args, root)
    atomic_write(receipt, {"status": "completed", "at": now(), "result": result})
    return result


def list_tables(root, token, call):
    data = unwrap(call(["+table-list", "--base-token", token, "--limit", "100"], root))
    tables = data.get("tables")
    if not isinstance(tables, list):
        raise Blocked("SETUP_TABLE_LIST", "读取表结构失败", 6)
    if data.get("has_more") or (isinstance(data.get("total"), int) and data["total"] > len(tables)):
        raise Blocked("SETUP_TABLE_LIST", "表列表未读完整，不能安全确定目标", 6)
    return tables


def bind(root, token, url, call=invoke):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".feishu.cn")
        or not parsed.path.startswith("/base/")
    ):
        raise Blocked(
            "SETUP_URL", "请输入当前企业的飞书 Base 地址（https://...feishu.cn/base/...）", 2
        )
    if (root / "config/feishu_mapping.local.json").exists():
        raise Blocked("SETUP_EXISTS", "已有飞书绑定，未覆盖", 6)
    plan = full_schema(root)
    tables = list_tables(root, token, call)
    mapping = {"base_token": token, "url": url.split("?")[0], "tables": {}}
    for kind in ("clips", "tasks"):
        spec = plan["tables"][kind]
        matches = [x for x in tables if x.get("name") == spec["name"]]
        if len(matches) != 1:
            raise Blocked("SETUP_TABLE", f"未找到唯一表: {spec['name']}", 6)
        table_id = matches[0]["id"]
        data = unwrap(
            call(
                ["+field-list", "--base-token", token, "--table-id", table_id, "--limit", "200"],
                root,
            )
        )
        fields = data.get("fields", [])
        if data.get("has_more") or (
            isinstance(data.get("total"), int) and data["total"] > len(fields)
        ):
            raise Blocked("SETUP_FIELDS", "字段列表未读完整", 6)
        by_name = {}
        for f in fields:
            by_name.setdefault(f["name"], []).append(f)
        ids = {}
        by_type = {f["name"]: f["type"] for f in spec["fields"]}
        for key, label in spec["labels"].items():
            options = by_name.get(label, [])
            if len(options) != 1 or options[0].get("type") != by_type[label]:
                raise Blocked("SETUP_FIELDS", f"缺少唯一且类型正确的字段: {label}", 6)
            ids[key] = options[0]["id"]
        mapping["tables"][kind] = {"table_id": table_id, "field_ids": ids, "labels": spec["labels"]}
    config_path = root / "config/config.local.json"
    config = load(config_path)
    config["feishu"].update(
        enabled=True,
        base_app_token=token,
        clips_table_id=mapping["tables"]["clips"]["table_id"],
        tasks_table_id=mapping["tables"]["tasks"]["table_id"],
        transport="existing_lark_cli_user",
    )
    atomic_write(root / "config/feishu_mapping.local.json", mapping)
    atomic_write(config_path, config)
    return {
        "status": "bound",
        "url": mapping["url"],
        "field_counts": {k: len(v["field_ids"]) for k, v in mapping["tables"].items()},
    }


def create(root, name, call=invoke):
    if (root / "config/feishu_mapping.local.json").exists():
        raise Blocked("SETUP_EXISTS", "已有飞书绑定，未创建新表", 6)
    if not (root / "config/config.local.json").exists():
        raise Blocked("SETUP_INIT", "先运行 init", 2)
    plan = full_schema(root)
    journal = root / "ledger/setup_feishu"
    journal.mkdir(parents=True, exist_ok=True)
    clips = plan["tables"]["clips"]
    first = unwrap(
        invoke_once(
            root,
            journal,
            "base_create",
            [
                "+base-create",
                "--name",
                name,
                "--table-name",
                clips["name"],
                "--fields",
                json.dumps(clips["fields"], ensure_ascii=False),
            ],
            call,
        )
    )
    base = first.get("base", {})
    token, url = base.get("base_token"), base.get("url")
    if not token or not url:
        raise Blocked("SETUP_BASE_UNKNOWN", "建表回执缺少Base地址，先对账", 6)
    tasks = plan["tables"]["tasks"]
    invoke_once(
        root,
        journal,
        "tasks_create",
        [
            "+table-create",
            "--base-token",
            token,
            "--name",
            tasks["name"],
            "--fields",
            json.dumps(tasks["fields"], ensure_ascii=False),
        ],
        call,
    )
    return bind(root, token, url, call)


def main():
    parser = argparse.ArgumentParser(description="Mixcut 首次配置（不会覆盖已有数据）")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("init")
    p.add_argument("--source", help="本机素材目录，可稍后配置")
    p = sub.add_parser("create-base")
    p.add_argument("--name", default="Mixcut 视频生产")
    p = sub.add_parser("bind-base")
    p.add_argument("--url", required=True)
    args = parser.parse_args()
    try:
        if args.action == "init":
            result = init(ROOT, args.source)
        elif args.action == "create-base":
            result = create(ROOT, args.name)
        else:
            resolved = unwrap(invoke(["+url-resolve", "--url", args.url], ROOT))
            token = resolved.get("base_token")
            if not token:
                raise Blocked("SETUP_URL", "未解析出 Base，请检查地址", 6)
            result = bind(ROOT, token, args.url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (Blocked, OSError, ValueError) as error:
        print(
            json.dumps({"status": "blocked", "message": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
