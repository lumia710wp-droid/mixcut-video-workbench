from __future__ import annotations
import subprocess
from pathlib import Path
from .core import Blocked, atomic_write, file_hash, load, now
from .contracts import plan_hash, validate_plan
from .ingest import probe

LETTERS = "ABCDE"
COLORS = ["0x263c80", "0x11795d", "0x933849", "0xa26a0b", "0x624094"]


def prepare(workspace: Path, config: dict) -> dict:
    receipt = workspace / "reports/g1/preparation.json"
    if receipt.exists():
        # Resuming preparation must never reset an existing render or review state.
        from .runner import preflight

        previous = load(receipt)
        previous_plan = load(Path(previous["plan"]))
        preflight(workspace, config, previous_plan, production=False)
        return {**previous, "reused_existing": True}
    root = workspace / "test_media/g1_v1"
    root.mkdir(parents=True, exist_ok=True)
    ffmpeg = config["paths"]["ffmpeg"]
    font = "/System/Library/Fonts/Helvetica.ttc"
    cmds = []
    for i, letter in enumerate(LETTERS):
        target = root / f"{letter}.mp4"
        if target.exists():
            info = probe(target, config["paths"]["ffprobe"])
            if int(info["streams"][0].get("nb_frames", 0)) != 300:
                raise Blocked("EXISTS", f"{target}; existing file fails 300-frame check")
            continue
        filt = f"drawtext=fontfile={font}:text='TEST ONLY  {letter}':fontsize=52:fontcolor=white:x=(w-tw)/2:y=180,drawtext=fontfile={font}:text='SOURCE FRAME %{{n}}':fontsize=38:fontcolor=white:x=(w-tw)/2:y=420,drawtext=fontfile={font}:timecode='00\\:00\\:00\\:00':r=30:fontsize=40:fontcolor=white:x=(w-tw)/2:y=490"
        # Machine-readable source frame bits, in addition to visible source timecode.
        for bit in range(9):
            filt += f",drawbox=x={25 + bit * 54}:y=90:w=40:h=40:color=white:t=fill:enable='mod(floor(n/{2**bit}),2)'"
        argv = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            f"color=c={COLORS[i]}:s=540x960:r=30:d=10",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={800 + 100 * i}:sample_rate=48000:duration=10",
            "-vf",
            filt,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-t",
            "10",
            "-movflags",
            "+faststart",
            str(target),
        ]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        cmds.append({"argv": argv, "exit_code": p.returncode, "stderr": p.stderr})
        atomic_write(workspace / "reports/g1/generation_commands.json", cmds)
        if p.returncode:
            raise Blocked("TEST_MEDIA_FAILED", p.stderr, 4)
        atomic_write(
            workspace / "reports/g1/preparation_checkpoint.json",
            {
                "at": now(),
                "completed_sources": [str(root / f"{x}.mp4") for x in LETTERS[: i + 1]],
                "next": "generate remaining sources, then exact plan",
                "candidate_exists": False,
            },
        )
    audio = root / "TEST_440Hz.wav"
    if not audio.exists():
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-n",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=24",
                "-c:a",
                "pcm_s16le",
                str(audio),
            ],
            check=True,
            timeout=60,
        )
    plan = load(workspace / "examples/edit_plan.fixture.json")
    plan["task_id"] = "G1_TEST"
    plan["batch_id"] = "G1_MECHANICAL"
    for i, m in enumerate(plan["media"]):
        m["root_id"] = "g1_test"
        m["relative_path"] = f"{LETTERS[i]}.mp4"
        m["source_sha256"] = m["working_sha256"] = file_hash(root / m["relative_path"])
    for s in plan["shots"]:
        s["selection_reason"] = "G1 人工指定 A→B→C→D→E；机械测试，不代表商品批准"
    plan["audio_tracks"] = [
        {
            "track_id": "TEST_AUDIO_440",
            "role": "bgm",
            "root_id": "g1_test",
            "relative_path": audio.name,
            "sha256": file_hash(audio),
            "start_frame": 0,
            "duration_frames": 720,
            "gain": 1,
        }
    ]
    plan["plan_sha256"] = plan_hash(plan)
    plan["build_id"] = "b_" + plan["plan_sha256"][:16]
    validate_plan(plan, workspace, production=False)
    job = workspace / "jobs" / plan["task_id"] / "r1" / plan["build_id"]
    job.mkdir(parents=True, exist_ok=True)
    atomic_write(job / "edit_plan.json", plan)
    atomic_write(
        job / "state.json",
        {
            "stage": "blocked",
            "reason": "EDITOR_REQUIRED",
            "render_status": "not_started",
            "qc_status": "not_checked",
            "human_review": "pending",
            "sync_status": "not_required",
            "is_fixture": True,
        },
    )
    srt = []

    def stamp(f):
        ms = f * 1000 // 30
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"

    for i, c in enumerate(plan["captions"], 1):
        srt.append(f"{i}\n{stamp(c['start_frame'])} --> {stamp(c['end_frame'])}\n{c['text']}\n")
    (job / "captions.srt").write_text("\n".join(srt))
    probes = {p.name: probe(p, config["paths"]["ffprobe"]) for p in root.glob("*.mp4")}
    atomic_write(workspace / "reports/g1/source_probes.json", probes)
    result = {
        "at": now(),
        "test_only": True,
        "source_root": str(root),
        "job_dir": str(job),
        "plan": str(job / "edit_plan.json"),
        "source_files": [str(root / f"{x}.mp4") for x in LETTERS],
        "audio": str(audio),
        "total_frames": 720,
        "candidate_exists": False,
        "human_review": "pending",
    }
    atomic_write(workspace / "reports/g1/preparation.json", result)
    return result
