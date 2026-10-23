"""Windows task ownership. A gated worker cannot spawn children before job assignment."""
import ctypes as C
from ctypes import wintypes as W
import os
from contextlib import contextmanager
from .common import Problem

if os.name == "nt":
    K = C.WinDLL("kernel32", use_last_error=True)
    for name, args, restype in [
        ("CreateJobObjectW", [C.c_void_p, W.LPCWSTR], W.HANDLE),
        ("SetInformationJobObject", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        ("AssignProcessToJobObject", [W.HANDLE, W.HANDLE], W.BOOL),
        ("CloseHandle", [W.HANDLE], W.BOOL),
        ("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        ("GetProcessTimes", [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
        ("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
        ("TerminateJobObject", [W.HANDLE, W.UINT], W.BOOL),
    ]:
        fn = getattr(K, name)
        fn.argtypes, fn.restype = args, restype

    class Basic(C.Structure):
        _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", W.DWORD),
                    ("min_ws", C.c_size_t), ("max_ws", C.c_size_t), ("active", W.DWORD),
                    ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]

    class IO(C.Structure):
        _fields_ = [(name, C.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class Extended(C.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", C.c_size_t),
                    ("job_memory", C.c_size_t), ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


def birth(pid):
    if os.name != "nt":
        raise Problem("UNSUPPORTED_OS", "首版进程管理仅在 Windows 验证")
    handle = K.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return None
    try:
        if K.WaitForSingleObject(handle, 0) != 258:
            return None
        times = [W.FILETIME() for _ in range(4)]
        if not K.GetProcessTimes(handle, *(C.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        K.CloseHandle(handle)


class Job:
    def __init__(self):
        self.handle = K.CreateJobObjectW(None, None)
        if not self.handle:
            raise Problem("PROCESS_ISOLATION", "无法创建任务进程容器")
        info = Extended()
        info.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE, applies even if supervisor crashes.
        if not K.SetInformationJobObject(self.handle, 9, C.byref(info), C.sizeof(info)):
            self.close()
            raise Problem("PROCESS_ISOLATION", "无法设置任务退出回收")

    def attach(self, process):
        if not K.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise Problem("PROCESS_ISOLATION", "无法隔离工作进程，任务未开始")

    def kill(self):
        if self.handle:
            K.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            K.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@contextmanager
def control_lock(root):
    import msvcrt
    root.mkdir(parents=True, exist_ok=True)
    with (root / "control.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise Problem("BUSY", "另一请求正在提交，请稍后查询") from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
