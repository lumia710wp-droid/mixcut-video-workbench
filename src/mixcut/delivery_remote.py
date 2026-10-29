"""Stage a post-edit package on the configured Feishu authority, then publish."""

from __future__ import annotations

import json
import shlex
import subprocess
import uuid
from pathlib import Path

from .core import Blocked, file_hash, load, safe_id
from .delivery_package import local_file, verify


def _ssh(host, root, code, payload=None, timeout=60):
    command = "cd " + shlex.quote(root) + " && " + shlex.join([".venv/bin/python", "-c", code])
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, command],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Blocked("DELIVERY_REMOTE_UNKNOWN", "远端操作结果未知，请先对账", 6) from error
    if result.returncode:
        raise Blocked("DELIVERY_REMOTE_PENDING", "远端验收包同步未确认，请先对账", 6)
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise Blocked("DELIVERY_REMOTE_PENDING", "远端回执格式不正确，请先对账", 6) from error


def publish_remote(root: Path, task: str) -> dict:
    task = safe_id(task)
    cfg = load(root / "config/director_authority.local.json")
    host, remote_root = cfg.get("host"), cfg.get("workspace")
    if not host or host.startswith("-") or not Path(remote_root).is_absolute():
        raise Blocked("DELIVERY_REMOTE_CONFIG", "远端工作区配置无效", 6)
    package = verify(root, task)
    content = package["content"]
    entries = [content["video"], *content["covers"].values()]
    inspect = """import hashlib,json,pathlib,sys
x=json.load(sys.stdin);r=pathlib.Path.cwd().resolve();p=(r/x["path"]).resolve();j=(r/"jobs").resolve()
assert p.is_relative_to(j);p.parent.mkdir(parents=True,exist_ok=True)
if p.exists():
 assert p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==x["sha256"]
 print(json.dumps({"exists":True}))
else: print(json.dumps({"exists":False}))"""
    commit = """import hashlib,json,os,pathlib,sys
x=json.load(sys.stdin);r=pathlib.Path.cwd().resolve();j=(r/"jobs").resolve();p=(r/x["path"]).resolve();t=(r/x["temp"]).resolve()
assert p.is_relative_to(j) and t.is_relative_to(j) and t.is_file()
assert hashlib.sha256(t.read_bytes()).hexdigest()==x["sha256"]
if p.exists(): assert p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==x["sha256"]
else: os.link(t,p)
t.unlink();print(json.dumps({"verified":True}))"""
    for entry in entries:
        path = local_file(root, entry["path"])
        if not path.is_relative_to((root / "jobs").resolve()) or file_hash(path) != entry["sha256"]:
            raise Blocked("DELIVERY_REMOTE_PATH", "待同步媒体须位于任务目录且摘要一致", 6)
        rel = str(path.relative_to(root))
        item = {"path": rel, "sha256": entry["sha256"]}
        if _ssh(host, remote_root, inspect, item)["exists"]:
            continue
        temp = str(Path(rel).parent / (".mixcut-stage-" + uuid.uuid4().hex))
        remote_path = str(Path(remote_root) / temp)
        try:
            sent = subprocess.run(
                ["scp", "-q", str(path), host + ":" + shlex.quote(remote_path)],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Blocked("DELIVERY_REMOTE_UNKNOWN", "媒体传输结果未知，请先对账", 6) from error
        if sent.returncode:
            raise Blocked("DELIVERY_REMOTE_UNKNOWN", "媒体传输未确认，请先对账", 6)
        _ssh(host, remote_root, commit, {**item, "temp": temp})
    stage = """import json,pathlib,sys
sys.path.insert(0,str(pathlib.Path.cwd()/"src"))
from mixcut.core import atomic_write,digest
x=json.load(sys.stdin);r=pathlib.Path.cwd();p=x["package"]; assert digest(p["content"])==p["package_sha256"]
d=r/"jobs/delivery_packages"/x["task"];f=d/(p["package_sha256"]+".json")
if f.exists(): assert json.loads(f.read_text())==p
else: atomic_write(f,p)
atomic_write(d/"current.json",{"package_sha256":p["package_sha256"]});print(json.dumps({"staged":True}))"""
    _ssh(host, remote_root, stage, {"task": task, "package": package})
    publish_code = """import json,os,pathlib,plistlib,sys
r=pathlib.Path.cwd();sys.path.insert(0,str(r/"src"));x=json.load(sys.stdin)
p=pathlib.Path.home()/"Library/LaunchAgents/com.mixcut.workbench.8768.plist"
if p.is_file(): os.environ.update(plistlib.load(open(p,"rb")).get("EnvironmentVariables",{}))
from mixcut.core import writer_lock
from mixcut.delivery_package import publish
from mixcut.workbench import Workbench
with writer_lock(r,wait_seconds=20): out=publish(r,x["task"],Workbench(r),x["package_sha256"])
print(json.dumps(out,ensure_ascii=False))"""
    return _ssh(
        host,
        remote_root,
        publish_code,
        {"task": task, "package_sha256": package["package_sha256"]},
        timeout=240,
    )
