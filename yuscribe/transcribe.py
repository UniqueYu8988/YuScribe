"""Local-media validation entry; no model or media is downloaded implicitly."""
import os
from pathlib import Path
import shutil
import subprocess
from .common import Problem, digest, read, write

CODE_ROOT = Path(__file__).resolve().parent.parent


def configuration(root):
    path = CODE_ROOT / "local.json"
    if not path.is_file():
        raise Problem("MODEL_NOT_CONFIGURED", "尚未配置独立转写进程；不会自动安装或下载模型")
    config = read(path)
    for key in ("python", "packages", "model"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute() or not Path(config[key]).exists():
            raise Problem("MODEL_NOT_INSTALLED", "转写解释器、推理包或本地模型不可用")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise Problem("FFMPEG_MISSING", "未找到本地音频转换工具")
    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=config["packages"])
    probe = subprocess.run([config["python"], "-c",
                            "import importlib.metadata as m,json,sys; print(json.dumps({**{n:m.version(n) for n in ['torch','transformers','qwen-asr']},'python':sys.version.split()[0]}))"],
                           env=env, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if probe.returncode:
        raise Problem("ASR_RUNTIME_ERROR", "本地转写依赖元数据不可用")
    import json
    config["versions"] = json.loads(probe.stdout)
    return config


def fingerprint(config):
    model = Path(config["model"])
    # Actual weight content, not a guessed model name or directory timestamp.
    weights = sorted(model.glob("*.safetensors"))
    if not weights or sum(p.stat().st_size for p in weights) > 8 * 1024**3:
        raise Problem("MODEL_NOT_INSTALLED", "本地模型缺少权重或超出首版规格上限")
    import hashlib
    checksums = {}
    metadata = sorted(p for p in model.iterdir() if p.suffix in {".json", ".txt"} and p.is_file())
    for path in weights + metadata:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024**2), b""):
                hasher.update(block)
        checksums[path.name] = hasher.hexdigest()
    return checksums


def file_hash(path):
    import hashlib
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b""):
            hasher.update(block)
    return hasher.hexdigest()


def local_request(root, path):
    config = configuration(root)
    path = path.resolve(strict=True)
    if path.suffix.lower() not in {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".mkv", ".webm"} or not path.is_file():
        raise Problem("UNSUPPORTED_MEDIA", "本地验证入口仅接受明确指定的音视频文件")
    if not 0 < path.stat().st_size <= 4 * 1024**3:
        raise Problem("MEDIA_LIMIT", "样本为空或超过 4GiB 上限")
    return {"kind": "local", "file": str(path), "sha256": file_hash(path), "language": "auto",
            "processor": "qwen-asr-chunks-v1", "model_files": fingerprint(config),
            "runtime": config}


def transcribe(root, folder, request):
    path = Path(request["file"])
    if file_hash(path) != request["sha256"] or fingerprint(request["runtime"]) != request["model_files"]:
        raise Problem("INPUT_CHANGED", "媒体或模型在提交后发生变化，请重新提交")
    cache = root / "cache" / folder.name
    cache.mkdir(parents=True, exist_ok=True)
    payload = {**request, "cache": str(cache), "result": str(folder / "asr.json"), "progress": str(folder / "progress.json")}
    write(folder / "progress.json", {"stage": "loading_runtime"})
    write(folder / "asr-request.json", payload)
    env = os.environ.copy()
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HOME=str(root / "cache" / "hf"),
               PYTHONDONTWRITEBYTECODE="1", NUMBA_CACHE_DIR=str(root / "cache" / "numba"),
               PYTHONPATH=os.pathsep.join([request["runtime"]["packages"], str(CODE_ROOT)]))
    result = subprocess.run([request["runtime"]["python"], "-m", "yuscribe.asr_worker", str(folder / "asr-request.json")],
                            cwd=CODE_ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=3 * 3600, creationflags=subprocess.CREATE_NO_WINDOW)
    if not (folder / "asr.json").is_file():
        raise Problem("ASR_RUNTIME_ERROR", "独立转写进程未交付结果；未启用在线下载")
    raw = read(folder / "asr.json")
    if raw.get("error"):
        raise Problem(raw["error"]["code"], raw["error"]["message"])
    if result.returncode or not raw.get("segments"):
        raise Problem("ASR_EMPTY", "未获得可交付文字；可能为静音或转写未完成")
    if file_hash(path) != request["sha256"] or fingerprint(request["runtime"]) != request["model_files"]:
        raise Problem("INPUT_CHANGED", "处理过程中媒体或模型发生变化，未发布完整成功")
    source = {"source_id": "local:sha256:" + request["sha256"], "title": path.stem,
              "author": None, "url": None, "part_title": None, "scope": "whole_file",
              "duration_seconds": raw["duration_seconds"], "sha256": request["sha256"]}
    record = {"origin": "local_asr", "language": "auto", "model": raw["model"],
              "warnings": ["仅有30秒音频切片边界，无词级对齐；固定切片接缝须人工核对",
                           "识别完整性与准确性未经过人工参考稿核验"], "metrics": raw["metrics"]}
    # Cache removal is deliberately explicit/manual for this validation phase.
    return source, raw, raw["segments"], record
