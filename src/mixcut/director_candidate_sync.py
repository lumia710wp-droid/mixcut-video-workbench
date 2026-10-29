"""Sync a verified local candidate to the configured Feishu Base."""
import shutil
import subprocess
import sys
from pathlib import Path
from .core import Blocked, atomic_write, file_hash, safe_id
from .director_backend import cli_status
from .director_stage import latest


def record_sync_status(root: Path, task: str, result: dict) -> None:
    rev = latest(root, safe_id(task))['revision']
    directory = root / 'jobs/cli_candidate_sync' / task / f'r{rev:04d}'
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / 'sync_state.json', result)


def sync_candidate(root: Path, task: str) -> dict:
    task = safe_id(task)
    status = cli_status(root, task)
    candidate = status.get('candidate')
    if not candidate:
        raise Blocked('CANDIDATE_MISSING', '没有通过检查的候选视频', 6)
    video = Path(candidate['path']).resolve()
    rev = latest(root, task)['revision']
    job = root / 'jobs/cli_candidate_sync' / task / f'r{rev:04d}'
    stage = job / 'media_uploads'
    stage.mkdir(parents=True, exist_ok=True)
    name = f"{task}_{candidate['sha256'][:16]}.mp4"
    path = stage / name
    if path.exists():
        if file_hash(path) != candidate['sha256']:
            raise Blocked('CANDIDATE_STAGE_CONFLICT', '暂存文件不一致，先对账', 6)
    else:
        # Exclusive creation prevents replacing another file or unknown prior output.
        with video.open('rb') as src, path.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        if file_hash(path) != candidate['sha256']:
            raise Blocked('CANDIDATE_STAGE_UNKNOWN', '暂存摘要不符，先对账', 6)
    manifest = {'items': [{'task_id': task, 'sha256': candidate['sha256'],
                           'size': video.stat().st_size, 'remote_name': name}]}
    dest = job / 'upload_manifest.json'
    atomic_write(dest, manifest)
    try:
        result = subprocess.run([sys.executable, str(root/'tools/sync_cli_candidates.py'),
                                 '--manifest', str(dest.relative_to(root))], cwd=root,
                                capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Blocked('CANDIDATE_SYNC_UNKNOWN', '上传结果未知，保留回执并先对账', 6) from error
    if result.returncode:
        raise Blocked('CANDIDATE_SYNC_PENDING', '上传未确认；保留视频和回执，先对账', 6)
    receipt = stage / (task + '.receipt.json')
    from .core import load
    saved = load(receipt)
    if saved.get('status') != 'verified' or saved.get('sha256') != candidate['sha256']:
        raise Blocked('CANDIDATE_SYNC_PENDING', '附件回执未验证，先对账', 6)
    result = {'task_id': task, 'status': 'verified', 'candidate_sha256': candidate['sha256']}
    atomic_write(job/'sync_receipt.json', result)
    return result
