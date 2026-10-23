import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from . import PROTOCOL
from .common import Problem, digest, now, read, write, task_path
from .processes import birth, control_lock, Job
from .artifacts import verify

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
CODE_ROOT = Path(__file__).resolve().parent.parent


def status(root, task_id):
    folder = task_path(root, task_id)
    try:
        state = read(folder / "state.json")
    except FileNotFoundError:
        raise Problem("TASK_NOT_FOUND", "未找到此任务") from None
    live_birth = birth(state.get("pid", 0)) if state["state"] not in TERMINAL else None
    if state["state"] not in TERMINAL and (not live_birth or live_birth != state.get("birth")):
        # Read-only projection: never leave a dead task spinning and never write during queries.
        state = {**state, "state": "interrupted", "error": {"code": "INTERRUPTED", "message": "任务进程已结束，可手动重试"}}
    # Internal process handles and configuration are never part of the public response.
    state["partial_result_available"] = (root / "cache" / task_id / "partial.json").is_file()
    if state["state"] not in TERMINAL and (folder / "progress.json").exists():
        progress = read(folder / "progress.json")
        state["stage"] = progress.get("stage", state.get("stage"))
        state["progress"] = {k: progress[k] for k in ("completed_chunks", "total_chunks") if k in progress}
    return {k: v for k, v in state.items() if k not in {"pid", "birth"}}


def submit(root, request, reprocess=False):
    key = digest(json.dumps(request, sort_keys=True).encode())
    with control_lock(root):
        active_file = root / "active.json"
        if active_file.exists():
            active = status(root, read(active_file)["task_id"])
            if active["state"] not in TERMINAL:
                if active["request_key"] == key and not reprocess:
                    return {**active, "reused_task": True}
                raise Problem("BUSY", "已有任务运行中；首版一次处理一个任务")
        index_path = root / "results" / (key + ".json")
        if not reprocess and index_path.exists():
            old = read(index_path)
            try:
                manifest = verify(root, old["artifact_id"])
            except (Problem, OSError, ValueError, KeyError):
                pass  # A corrupt delivery is not success and cannot prevent manual reprocessing.
            else:
                matching_source = True
                if request.get("kind") == "bilibili":
                    from . import bilibili
                    bilibili.ALLOW_PROXY_FAKE_IP = request.get("proxy_fake_ip", False)
                    current_source = bilibili.inspect(request["source"])
                    matching_source = current_source["source_id"] == manifest["source_id"]
                if matching_source:
                    return {"protocol": PROTOCOL, "state": "succeeded", **old, "reused_result": True}
        task_id = uuid.uuid4().hex
        folder = task_path(root, task_id)
        folder.mkdir(parents=True)
        write(folder / "request.json", request)
        state = {"protocol": PROTOCOL, "task_id": task_id, "request_key": key,
                 "state": "starting", "stage": "starting", "created_at": now(), "updated_at": now(),
                 "artifact_id": None, "error": None, "pid": os.getpid(), "birth": birth(os.getpid())}
        write(folder / "state.json", state)
        process = subprocess.Popen([sys.executable, "-m", "yuscribe", "--data-dir", str(root), "_supervise", task_id],
                                   cwd=CODE_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                                   close_fds=True)
        state.update(pid=process.pid, birth=birth(process.pid))
        write(folder / "state.json", state)
        write(active_file, {"task_id": task_id})
        (folder / "supervisor.go").touch()
        return {k: v for k, v in state.items() if k not in {"pid", "birth"}}


def cancel(root, task_id):
    with control_lock(root):
        state = status(root, task_id)
        if state["state"] in TERMINAL:
            return state
        task_path(root, task_id).joinpath("cancel.request").touch()
    return {**state, "cancel_requested": True}  # Not yet cancelled until actual process exit.


def supervise(root, task_id):
    folder = task_path(root, task_id)
    for _ in range(100):
        if (folder / "supervisor.go").exists():
            break
        time.sleep(.1)
    else:
        return
    state = read(folder / "state.json")
    process = None
    def save(**change):
        state.update(change, updated_at=now())
        write(folder / "state.json", state)
    try:
        with Job() as job:
            process = subprocess.Popen([sys.executable, "-m", "yuscribe", "--data-dir", str(root), "_work", task_id],
                                       cwd=CODE_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                                       close_fds=True)
            job.attach(process)
            save(state="running", stage="extracting")
            (folder / "worker.go").touch()
            started = time.monotonic()
            while process.poll() is None:
                if (folder / "cancel.request").exists():
                    job.kill()
                    process.wait(timeout=10)
                    save(state="cancelled", stage="finished", error={"code": "CANCELLED", "message": "任务处理进程树已回收"})
                    return
                if time.monotonic() - started > 4 * 3600:
                    job.kill()
                    process.wait(timeout=10)
                    raise Problem("TIME_LIMIT", "任务超过四小时执行上限")
                time.sleep(.1)
        # Closing job also reaps any descendants left by a worker that already exited.
        process.wait(timeout=10)
        if (folder / "cancel.request").exists():
            save(state="cancelled", stage="finished", error={"code": "CANCELLED", "message": "任务已取消，未发布为成功"})
            return
        outcome = read(folder / "outcome.json")
        if process.returncode != 0 or outcome.get("error"):
            save(state="failed", stage="finished", error=outcome.get("error") or {"code": "WORKER_EXIT", "message": "工作进程异常退出"})
            return
        artifact_id = outcome["artifact_id"]
        verify(root, artifact_id)
        result = {"task_id": task_id, "artifact_id": artifact_id,
                  "manifest": f"artifacts/{artifact_id}/manifest.json"}
        write(root / "results" / (state["request_key"] + ".json"), result)
        save(state="succeeded", stage="finished", artifact_id=artifact_id, manifest=result["manifest"])
    except BaseException as error:
        if process and process.poll() is None:
            process.kill()  # Gated worker fallback if job assignment itself failed; it cannot spawn yet.
            process.wait(timeout=10)
        code = error.code if isinstance(error, Problem) else "WORKER_ERROR"
        message = error.message if isinstance(error, Problem) else "处理未完整交付；请检查环境后手动重试"
        save(state="failed", stage="finished", error={"code": code, "message": message})


def work(root, task_id):
    from . import bilibili
    from .artifacts import publish
    folder = task_path(root, task_id)
    for _ in range(100):
        if (folder / "worker.go").exists():
            break
        time.sleep(.1)
    else:
        return 2
    try:
        request = read(folder / "request.json")
        if request["kind"] == "bilibili":
            bilibili.ALLOW_PROXY_FAKE_IP = request.get("proxy_fake_ip", False)
            source = bilibili.inspect(request["source"])
            write(folder / "source.json", source)
            try:
                raw, segments, record = bilibili.extract(source, request["language"])
            except Problem as error:
                if not request.get("fallback_asr") or error.code not in {"NO_SUBTITLES", "LOGIN_REQUIRED"}:
                    raise
                from .transcribe import transcribe, file_hash
                cache = root / "cache" / task_id
                cache.mkdir(parents=True, exist_ok=True)
                audio = cache / "audio.m4s"
                write(folder / "progress.json", {"stage": "downloading_audio"})
                audio_record = bilibili.download_audio(source, audio)
                _, raw, segments, record = transcribe(root, folder, {**request, "file": str(audio), "sha256": file_hash(audio)})
                record.update(subtitle_attempt={"code": error.code, "message": error.message}, audio=audio_record)
                record["warnings"].append("音轨归属通过所选BV/CID的playurl请求绑定；需对照原视频核验")
        else:
            from .transcribe import transcribe
            source, raw, segments, record = transcribe(root, folder, request)
        artifact_id = publish(root, task_id, source, raw, segments, record)
        write(folder / "outcome.json", {"artifact_id": artifact_id})
        return 0
    except Exception as error:
        write(folder / "outcome.json", {"error": {
            "code": error.code if isinstance(error, Problem) else "INVALID_RESPONSE",
            "message": error.message if isinstance(error, Problem) else "返回数据或运行环境异常，未交付成功结果"}})
        return 1
