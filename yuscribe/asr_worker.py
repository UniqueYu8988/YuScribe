"""Optional existing qwen-asr backend, one model load for all chunks. Offline only."""
import json
from pathlib import Path
import subprocess
import sys
import time
import wave
from .common import read, write, Problem


def check_generation(ids, prompt_tokens, limit):
    sequences = ids.sequences if hasattr(ids, "sequences") else ids
    if sequences.shape[1] - prompt_tokens >= limit:
        raise Problem("ASR_TOKEN_LIMIT", "切片识别达到生成上限，可能末尾截断，未交付完整成功")
    return ids


def run(payload):
    started = time.monotonic()
    import importlib.metadata
    import numpy as np
    import torch
    from qwen_asr import Qwen3ASRModel
    if not torch.cuda.is_available():
        raise Problem("CUDA_UNAVAILABLE", "当前推理环境无法使用 CUDA；未自动切换到其他后端")
    torch.cuda.reset_peak_memory_stats()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", payload["file"]],
                           capture_output=True, timeout=30, check=True)
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    if not 0 < duration <= 4 * 3600:
        raise Problem("MEDIA_LIMIT", "媒体时长未知或超过四小时上限")
    load_start = time.monotonic()
    write(payload["progress"], {"stage": "loading_model"})
    model = Qwen3ASRModel.from_pretrained(payload["runtime"]["model"], dtype=torch.float16,
                                         device_map="cuda:0", max_inference_batch_size=1, max_new_tokens=1024)
    load_seconds = time.monotonic() - load_start
    generate = model.model.generate
    def checked_generate(**kwargs):
        ids = generate(**kwargs)
        return check_generation(ids, kwargs["input_ids"].shape[1], 1024)
    model.model.generate = checked_generate
    segments, chunks = [], []
    wav = Path(payload["cache"]) / "chunk.wav"
    import math
    for index in range(math.ceil(duration / 30)):
        write(payload["progress"], {"stage": "transcribing", "completed_chunks": index,
                                    "total_chunks": math.ceil(duration / 30)})
        start = index * 30
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
                        "-i", payload["file"], "-t", "30", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=120, check=True)
        with wave.open(str(wav), "rb") as stream:
            frames = stream.getnframes()
            audio = np.frombuffer(stream.readframes(frames), dtype=np.int16)
        if frames == 0:
            raise Problem("ASR_TRUNCATED", "音频切片意外为空，未交付完整成功")
        end = min(duration, start + frames / 16000)
        # Only exact digital silence is skipped. Quiet speech is never removed by a heuristic.
        if not np.any(audio):
            chunks.append({"start": start, "end": end, "text": "", "digital_silence": True})
            continue
        item = model.transcribe(audio=str(wav), language=None)[0]
        text = item.text
        if not text.strip():
            raise Problem("ASR_EMPTY_CHUNK", "非数字静音切片未识别出文字，需人工检查；未标完整成功")
        chunks.append({"start": start, "end": end, "text": text, "language": item.language})
        segments.append({"start": start, "end": end, "text": text, "timing": "audio_chunk_boundary"})
        write(Path(payload["cache"]) / "partial.json", {"complete": False, "chunks": chunks})
    if wav.exists():
        wav.unlink()
    import psutil
    memory = psutil.Process().memory_info()
    return {"duration_seconds": duration, "segments": segments, "chunks": chunks,
            "model": {"family": "Qwen3-ASR", "files": payload["model_files"],
                      "qwen_asr": importlib.metadata.version("qwen-asr"), "torch": torch.__version__,
                      "transformers": importlib.metadata.version("transformers"), "dtype": "float16"},
            "metrics": {"model_load_seconds": load_seconds, "worker_seconds": time.monotonic() - started,
                        "peak_torch_allocated_bytes": torch.cuda.max_memory_allocated(),
                        "peak_torch_reserved_bytes": torch.cuda.max_memory_reserved(),
                        "peak_worker_working_set_bytes": getattr(memory, "peak_wset", None),
                        "peak_worker_pagefile_bytes": getattr(memory, "peak_pagefile", None),
                        "note": "内存仅ASR进程；Torch统计不等于整机显存峰值；未测冷缓存启动"}}


if __name__ == "__main__":
    payload = read(sys.argv[1])
    try:
        write(payload["result"], run(payload))
    except Exception as error:
        import traceback
        write(payload["result"], {"error": {
            "code": error.code if isinstance(error, Problem) else "ASR_RUNTIME_ERROR",
            "message": error.message if isinstance(error, Problem) else "本地转写环境或推理异常；未启动在线下载"},
            "diagnostic": {"exception": type(error).__name__,
                           "frames": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                                      for f in traceback.extract_tb(error.__traceback__)[-8:]]}})
        sys.exit(1)
