import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from yuscribe import bilibili, tasks
from yuscribe.artifacts import publish, verify, markdown
from yuscribe.common import Problem, read, write
from yuscribe.processes import birth

BV = "BV15NJi65E2S"
SOURCE = {"bvid": BV, "page": 2, "cid": 222, "source_id": f"bilibili:{BV}:222",
          "title": "<script>中文</script>", "part_title": "第二 P", "url": f"https://www.bilibili.com/video/{BV}/?p=2"}
RECORD = {"origin": "platform_subtitle_unknown_authorship", "language": "zh-CN", "model": None, "warnings": []}
CUES = [{"from": 1, "to": 2, "content": "你好。"}, {"from": 2.1, "to": 3, "content": "World!"}]


class Core(unittest.TestCase):
    def test_asr_generation_output_and_truncation(self):
        from types import SimpleNamespace
        from yuscribe.asr_worker import check_generation
        output = SimpleNamespace(sequences=SimpleNamespace(shape=(1, 110)))
        self.assertIs(check_generation(output, 100, 1024), output)
        with self.assertRaises(Problem):
            check_generation(SimpleNamespace(shape=(1, 1124)), 100, 1024)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_source_security_and_explicit_part(self):
        for value in ["https://127.0.0.1/video/" + BV, "https://www.bilibili.com.evil/video/" + BV,
                      "http://www.bilibili.com/video/" + BV, "https://u@www.bilibili.com/video/" + BV,
                      "https://b23.tv/abcd", "https://www.bilibili.com:443/video/" + BV]:
            with self.assertRaises(Problem):
                bilibili.parse_source(value, 1)
        with self.assertRaises(Problem):
            bilibili.parse_source(f"https://www.bilibili.com/video/{BV}?p=2", 1)
        self.assertEqual(bilibili.parse_source(BV, 2)["page"], 2)

    def test_selected_cid_and_language_no_first_part_guess(self):
        def fake(url, kind="api"):
            if "/view?" in url:
                return {"code": 0, "data": {"bvid": BV, "pages": [{"page": 1, "cid": 111}, {"page": 2, "cid": 222}]}}
            if "/v2?" in url:
                self.assertIn("cid=222", url)
                return {"code": 0, "data": {"bvid": BV, "cid": 222, "subtitle": {"subtitles": [
                    {"lan": "zh-CN", "id": 5, "subtitle_url": "//aisubtitle.hdslb.com/bfs/subtitle/x.json?secret=abc"}]}}}
            return {"body": CUES}
        source = bilibili.inspect(bilibili.parse_source(BV, 2), fake)
        raw, segments, record = bilibili.extract(source, "zh-CN", fake)
        self.assertEqual(source["cid"], 222)
        self.assertEqual(segments[1]["text"], "World!")
        self.assertNotIn("secret", json.dumps(record))
        self.assertEqual(record["binding"]["verification"], "response_ids")

    def test_distinct_errors_and_identity_mismatch(self):
        for payload, code in [({"need_login_subtitle": True}, "LOGIN_REQUIRED"), ({}, "NO_SUBTITLES"),
                              ({"cid": 111}, "IDENTITY_MISMATCH")]:
            with self.assertRaises(Problem) as caught:
                bilibili.tracks(SOURCE, lambda *_: {"code": 0, "data": payload})
            self.assertEqual(caught.exception.code, code)

    def test_ssrf_paths_and_private_dns(self):
        for url in ["https://127.0.0.1/bfs/subtitle/x.json", "https://aisubtitle.hdslb.com.evil/bfs/subtitle/x.json",
                    "https://aisubtitle.hdslb.com/bfs/subtitle/../x.json", "https://aisubtitle.hdslb.com/bfs/subtitle/%2e/x.json"]:
            with self.assertRaises(Problem):
                bilibili.safe_url(url, "subtitle")
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.1", 443))]):
            with patch.object(bilibili, "ALLOW_PROXY_FAKE_IP", True):
                with self.assertRaises(Problem):
                    bilibili.PinnedHTTPS("api.bilibili.com").connect()

    def test_bad_timestamps_and_long_material_no_loss(self):
        for cue in [{"from": float("nan"), "to": 2, "content": "x"}, {"from": 3, "to": 2, "content": "x"}]:
            with self.assertRaises(Problem):
                bilibili.validate_cues([cue])
        cues = [{"from": i * 2, "to": i * 2 + 1, "content": f"合成{i:05}。"} for i in range(10000)]
        segments = bilibili.validate_cues(cues)
        text = markdown(SOURCE, segments, RECORD)
        self.assertEqual(text.count("合成"), 10000)
        self.assertIn("[05:33:", text)
        self.assertNotIn("<script>", text)
        no_punctuation = [{**segment, "text": "连续识别"} for segment in segments]
        paragraphs = markdown(SOURCE, no_punctuation, RECORD).split("\n\n")
        self.assertGreater(len(paragraphs), 30)
        self.assertEqual("".join(paragraphs).count("连续识别"), 10000)

    def test_immutable_artifacts_corrupt_reuse_and_notes(self):
        segments = bilibili.validate_cues(CUES)
        first = publish(self.root, "a" * 32, SOURCE, {"body": CUES}, segments, RECORD)
        note = self.root / "personal-note.md"
        note.write_text("个人笔记", encoding="utf-8")
        second = publish(self.root, "b" * 32, SOURCE, {"body": CUES}, segments, RECORD)
        self.assertNotEqual(first, second)
        self.assertEqual(verify(self.root, first)["source_id"], SOURCE["source_id"])
        (self.root / "artifacts" / first / "transcript.md").write_text("损坏", encoding="utf-8")
        with self.assertRaises(Problem):
            verify(self.root, first)
        self.assertEqual(note.read_text(encoding="utf-8"), "个人笔记")

    def test_readonly_status_detects_interruption_pid_reuse(self):
        folder = self.root / "tasks" / ("a" * 32)
        write(folder / "state.json", {"state": "running", "pid": os.getpid(), "birth": 1})
        before = (folder / "state.json").read_bytes()
        self.assertEqual(tasks.status(self.root, "a" * 32)["state"], "interrupted")
        self.assertEqual(before, (folder / "state.json").read_bytes())
        with self.assertRaises(Problem):
            tasks.status(self.root, "../escape")

    def test_duplicate_and_result_reuse(self):
        request = {"kind": "bilibili", "source": bilibili.parse_source(BV, 1), "language": "zh-CN"}
        key = tasks.digest(json.dumps(request, sort_keys=True).encode())
        task_id = "a" * 32
        state = {"state": "running", "task_id": task_id, "pid": os.getpid(), "birth": birth(os.getpid()), "request_key": key}
        write(self.root / "tasks" / task_id / "state.json", state)
        write(self.root / "active.json", {"task_id": task_id})
        self.assertTrue(tasks.submit(self.root, request)["reused_task"])
        with self.assertRaises(Problem):
            tasks.submit(self.root, {**request, "language": "en"})
        state["state"] = "failed"
        write(self.root / "tasks" / task_id / "state.json", state)
        artifact = publish(self.root, task_id, SOURCE, {"body": CUES}, bilibili.validate_cues(CUES), RECORD)
        write(self.root / "results" / (key + ".json"), {"task_id": task_id, "artifact_id": artifact})
        with patch.object(bilibili, "inspect", return_value=SOURCE):
            self.assertTrue(tasks.submit(self.root, request)["reused_result"])

    def test_actual_cancel_reaps_grandchild(self):
        task_id = "c" * 32
        folder = self.root / "tasks" / task_id
        write(folder / "state.json", {"task_id": task_id, "state": "starting", "request_key": "test"})
        (folder / "supervisor.go").touch()
        # Test-only launcher replaces the worker. No synthetic input flag exists in production CLI.
        code = ("import subprocess,sys,time; from pathlib import Path; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"Path({str(folder / 'child.pid')!r}).write_text(str(p.pid)); time.sleep(60)")
        original = subprocess.Popen
        def launch(*args, **kwargs):
            return original([sys.executable, "-c", code], **kwargs)
        def request_cancel():
            deadline = time.monotonic() + 10
            while not (folder / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(.05)
            (folder / "cancel.request").touch()
        thread = threading.Thread(target=request_cancel)
        thread.start()
        with patch.object(tasks.subprocess, "Popen", side_effect=launch):
            tasks.supervise(self.root, task_id)
        thread.join()
        self.assertEqual(read(folder / "state.json")["state"], "cancelled")
        self.assertIsNone(birth(int((folder / "child.pid").read_text())))

    def test_normal_completion_and_failure_reap_leftover_children(self):
        for succeeded in (True, False):
            task_id = ("d" if succeeded else "e") * 32
            folder = self.root / "tasks" / task_id
            write(folder / "state.json", {"task_id": task_id, "state": "starting", "request_key": task_id})
            (folder / "supervisor.go").touch()
            if succeeded:
                artifact = publish(self.root, task_id, SOURCE, {"body": CUES}, bilibili.validate_cues(CUES), RECORD)
                outcome = {"artifact_id": artifact}
            else:
                outcome = {"error": {"code": "TEST_FAILURE", "message": "合成失败"}}
            write(folder / "outcome.json", outcome)
            code = ("import subprocess,sys,time; from pathlib import Path; "
                    f"gate=Path({str(folder / 'worker.go')!r}); "
                    "exec('while not gate.exists(): time.sleep(.02)'); "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                    f"Path({str(folder / 'child.pid')!r}).write_text(str(p.pid))")
            original = subprocess.Popen
            with patch.object(tasks.subprocess, "Popen", side_effect=lambda *a, **kw: original([sys.executable, "-c", code], **kw)):
                tasks.supervise(self.root, task_id)
            self.assertEqual(read(folder / "state.json")["state"], "succeeded" if succeeded else "failed")
            self.assertIsNone(birth(int((folder / "child.pid").read_text())))

    def test_supervisor_crash_closes_job_and_status_is_interrupted(self):
        task_id = "f" * 32
        folder = self.root / "tasks" / task_id
        folder.mkdir(parents=True)
        worker = ("import subprocess,sys,time; from pathlib import Path; "
                  f"gate=Path({str(folder / 'worker.go')!r}); "
                  "exec('while not gate.exists(): time.sleep(.02)'); "
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                  f"Path({str(folder / 'child.pid')!r}).write_text(str(p.pid)); time.sleep(60)")
        helper = ("import subprocess,sys\nfrom pathlib import Path\nfrom unittest.mock import patch\n"
                  "from yuscribe import tasks\noriginal=subprocess.Popen\n"
                  f"worker={worker!r}\n"
                  "launch=lambda *a,**kw: original([sys.executable,'-c',worker],**kw)\n"
                  "with patch.object(tasks.subprocess,'Popen',side_effect=launch):\n "
                  f"tasks.supervise(Path({str(self.root)!r}),{task_id!r})\n")
        process = subprocess.Popen([sys.executable, "-c", helper], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            write(folder / "state.json", {"task_id": task_id, "state": "starting", "pid": process.pid,
                                          "birth": birth(process.pid), "request_key": "test"})
            (folder / "supervisor.go").touch()
            deadline = time.monotonic() + 10
            while not (folder / "child.pid").exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((folder / "child.pid").exists(), "合成监督器未启动")
            child_pid = int((folder / "child.pid").read_text())
            process.kill()
            process.wait(timeout=5)
            deadline = time.monotonic() + 3
            while birth(child_pid) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIsNone(birth(child_pid))
            self.assertEqual(tasks.status(self.root, task_id)["state"], "interrupted")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
