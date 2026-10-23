import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone


class Problem(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def task_path(root, task_id):
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise Problem("INVALID_TASK", "任务标识无效")
    path = root / "tasks" / task_id
    if path.is_symlink():
        raise Problem("INVALID_TASK", "任务目录不能是链接")
    return path
