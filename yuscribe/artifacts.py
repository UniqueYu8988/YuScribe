import json
import os
from pathlib import Path
import re
import uuid
from .common import digest, read, write, now, Problem
from . import PROTOCOL, VERSION


def clock(value):
    n = int(value)
    return f"{n // 3600:02}:{n // 60 % 60:02}:{n % 60:02}"


def prose(text):
    # Escape untrusted Markdown/HTML without altering the retained structured text.
    return re.sub(r"([\\`*_{}\[\]<>()#!|~+-])", r"\\\1", text.replace("\r", "").replace("\n", " "))


def markdown(source, segments, record):
    lines = [f"# {prose(source.get('title') or '原稿')}", "",
             f"来源：{source.get('url') or '本地样本'}", "",
             f"范围：{prose(source.get('part_title') or '完整文件')}（仅当前选定部分）", "",
             f"类型：{record['origin']}；语言：{record['language']}。时间仅对应字幕或音频切片边界。", "",
             "> 原稿未作总结；分段是保守排版，不代表已识别语义主题。请结合原视频核对。", ""]
    group, chars = [], 0
    def flush():
        if group:
            # One paragraph for a group of consecutive cues; text is never discarded.
            lines.extend([f"[{clock(group[0]['start'])}–{clock(group[-1]['end'])}] " +
                          " ".join(prose(s["text"]) for s in group), ""])
            group.clear()
    for segment in segments:
        if group and (segment["start"] - group[-1]["end"] >= 2.5 or
                      chars >= 1200 or
                      (chars >= 240 and
                       re.search(r"[。！？.!?][”’\"']?$", group[-1]["text"]))):
            flush()
            chars = 0
        group.append(segment)
        chars += len(segment["text"])
    flush()
    return "\n".join(lines)


def publish(root, task_id, source, raw, segments, record):
    artifact_id = uuid.uuid4().hex
    base = root / "artifacts"
    base.mkdir(parents=True, exist_ok=True)
    stage = base / (".partial-" + artifact_id)
    stage.mkdir()
    write(stage / "raw.json", raw)
    write(stage / "segments.json", segments)
    write(stage / "source.json", {"source": source, "processing": record})
    with (stage / "transcript.md").open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(markdown(source, segments, record))
        stream.flush()
        os.fsync(stream.fileno())
    manifest = {"protocol": PROTOCOL, "tool_version": VERSION, "artifact_id": artifact_id,
                "task_id": task_id, "source_id": source["source_id"], "source": source,
                "processing": record, "created_at": now(), "complete": True,
                "files": {name: {"sha256": digest((stage / name).read_bytes()), "bytes": (stage / name).stat().st_size}
                          for name in ("raw.json", "segments.json", "source.json", "transcript.md")}}
    write(stage / "manifest.json", manifest)
    verify_folder(stage)
    stage.rename(base / artifact_id)  # Unique immutable version; no overwrite.
    verify(root, artifact_id)
    return artifact_id


def verify_folder(folder):
    manifest = read(folder / "manifest.json")
    expected = {"raw.json", "segments.json", "source.json", "transcript.md"}
    if manifest.get("protocol") != PROTOCOL or manifest.get("complete") is not True or set(manifest.get("files", {})) != expected:
        raise Problem("INVALID_ARTIFACT", "结果清单不完整")
    for name, item in manifest["files"].items():
        file = folder / name
        if file.is_symlink() or file.stat().st_size != item["bytes"] or digest(file.read_bytes()) != item["sha256"]:
            raise Problem("CORRUPT_ARTIFACT", "原稿文件校验未通过，不能复用")
    if not read(folder / "segments.json") or not read(folder / "raw.json"):
        raise Problem("INVALID_ARTIFACT", "原始结果或分段为空")
    return manifest


def verify(root, artifact_id):
    if not isinstance(artifact_id, str) or not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
        raise Problem("INVALID_ARTIFACT", "原稿标识无效")
    folder = root / "artifacts" / artifact_id
    if folder.is_symlink():
        raise Problem("INVALID_ARTIFACT", "原稿目录不能是链接")
    return verify_folder(folder)
