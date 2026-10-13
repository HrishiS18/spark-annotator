import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_runtime import RuntimeManager, RuntimeUnavailable, privacy_command
from storage import Store


class FakeRpc:
    account = {"type": "chatgpt"}
    history = []

    def __init__(self, command, cwd, callback, disconnected):
        self.callback, self.on_disconnect = callback, disconnected
        self.sent, self.closed = [], False
        self.turns = 0
        self.cwd = cwd

    def request(self, method, params=None, timeout=30):
        params = params or {}
        self.sent.append({"method": method, "params": params})
        if method == "account/read":
            return {"account": self.account}
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": params.get("threadId", "thr_test"), "turns": self.history if method == "thread/resume" else []},
                    "model": "mock-model", "modelProvider": "test"}
        if method == "turn/start":
            self.turns += 1
            self.callback({"method": "turn/started", "params": {"threadId": "thr_test", "turn": {"id": "turn_test"}}})
            self.callback({"method": "item/started", "params": {"threadId": "thr_test", "turnId": "turn_test",
                          "item": {"id": "msg_test", "type": "agentMessage", "text": ""}}})
            self.callback({"method": "item/agentMessage/delta", "params": {"threadId": "thr_test", "turnId": "turn_test",
                          "itemId": "msg_test", "delta": "First partial text"}})
            return {"turn": {"id": "turn_test"}}
        if method == "turn/interrupt":
            self.callback({"method": "turn/completed", "params": {"turn": {"id": "turn_test", "status": "interrupted"}}})
        return {}

    def send(self, message):
        self.sent.append(message)

    def close(self):
        self.closed = True


def runtime_session(sid="s_12345678"):
    return {"id": sid, "phase": "chat", "events": [], "marks": [], "codes": {}, "candidates": [],
            "prompt": {"text": "A public task"}, "writing": {"pre": {"text": "PRIVATE BASELINE"}},
            "config_snapshot": {"persona": {"system": "A public persona"}},
            "runtime": {"kind": "codex", "status": "disconnected", "approvals": [], "effort": "medium"}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "study.sqlite3")
        self.sid = "s_12345678"
        self.store.save(runtime_session())
        self.manager = RuntimeManager(self.store, self.root, rpc_factory=FakeRpc, command=["test"])

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def test_streaming_identity_idempotency_and_status(self):
        worker = self.manager.get(self.sid)
        event = worker.message("Test", "message-0001")
        self.assertEqual(worker.message("Test", "message-0001")["id"], event["id"])
        self.assertEqual(worker.rpc.turns, 1)
        s = self.store.load(self.sid)
        assistant = next(e for e in s["events"] if e["role"] == "assistant")
        aid = assistant["id"]
        self.assertEqual(assistant["text"], "First partial text")
        self.assertEqual(s["runtime"]["status"], "running")
        with self.assertRaises(RuntimeUnavailable):
            worker.message("Another", "message-0002")
        worker.notification({"method": "item/completed", "params": {"threadId": worker.thread_id, "turnId": "turn_test",
            "item": {"id": "msg_test", "type": "agentMessage", "text": "Final text"}}})
        worker.notification({"method": "turn/completed", "params": {"turn": {"id": "turn_test", "status": "completed"}}})
        final = self.store.load(self.sid)
        self.assertEqual(final["runtime"]["status"], "ready")
        self.assertEqual(next(e for e in final["events"] if e["id"] == aid)["text"], "Final text")
        self.assertGreater(len(self.store.versions(self.sid, "event", aid)), 1)

    def test_resume_does_not_start_inference_or_include_private_baseline(self):
        worker = self.manager.get(self.sid)
        start = next(m for m in worker.rpc.sent if m.get("method") == "thread/start")
        self.assertNotIn("PRIVATE BASELINE", str(start))
        self.assertEqual(start["params"]["approvalPolicy"], "untrusted")
        self.assertEqual(start["params"]["sandbox"], "workspace-write")
        self.assertEqual(self.store.load(self.sid)["runtime"]["model"], "mock-model")
        worker.connected = False
        resumed = self.manager.get(self.sid)
        self.assertTrue(any(m.get("method") == "thread/resume" for m in resumed.rpc.sent))
        self.assertFalse(any(m.get("method") == "turn/start" for m in resumed.rpc.sent))

    def test_approvals_are_explicit_scoped_and_recorded(self):
        worker = self.manager.get(self.sid)
        worker.message("Test", "message-0001")
        worker.notification({"id": 101, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": worker.thread_id, "turnId": "turn_test", "itemId": "cmd", "command": "ls"}})
        approval = self.store.load(self.sid)["runtime"]["approvals"][0]
        self.assertEqual(self.store.load(self.sid)["runtime"]["status"], "awaiting_approval")
        worker.approve(approval["id"], "decline")
        self.assertIn({"id": 101, "result": {"decision": "decline"}}, worker.rpc.sent)
        self.assertEqual(self.store.load(self.sid)["runtime"]["approvals"], [])
        with self.assertRaises(RuntimeUnavailable):
            worker.approve(approval["id"], "accept")

    def test_tool_results_are_distinct_from_agent_messages(self):
        worker = self.manager.get(self.sid)
        worker.notification({"method": "item/completed", "params": {"threadId": worker.thread_id, "turnId": "turn_test",
            "item": {"type": "commandExecution", "id": "command-1", "command": "ls", "status": "completed",
                     "aggregatedOutput": "TASK.md", "exitCode": 0}}})
        events = self.store.load(self.sid)["events"]
        self.assertEqual([e["role"] for e in events], ["tool_call", "tool_result"])
        self.assertEqual(events[1]["text"], "TASK.md")
        worker.notification({"method": "item/completed", "params": {"threadId": worker.thread_id, "turnId": "turn_test",
            "item": {"type": "commandExecution", "id": "command-1", "command": "ls", "status": "completed",
                     "aggregatedOutput": "TASK.md", "exitCode": 0}}})
        self.assertEqual(len(self.store.load(self.sid)["events"]), 2)

    def test_interrupt_and_disconnect_do_not_claim_success(self):
        worker = self.manager.get(self.sid)
        worker.message("Test", "message-0001")
        worker.interrupt()
        self.assertEqual(self.store.load(self.sid)["runtime"]["last_turn_status"], "interrupted")
        worker.disconnected()
        self.assertEqual(self.store.load(self.sid)["runtime"]["status"], "error")

    def test_missing_auth_is_error_not_demo(self):
        with patch.object(FakeRpc, "account", None), patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaises(RuntimeUnavailable):
                self.manager.get(self.sid)
        s = self.store.load(self.sid)
        self.assertEqual(s["runtime"]["kind"], "codex")
        self.assertEqual(s["runtime"]["status"], "error")
        self.assertEqual(s["events"], [])

    def test_workspace_paths_and_overwrite_guard(self):
        self.manager.get(self.sid)
        self.manager.upload(self.sid, "input.txt", b"hello")
        with self.assertRaises(FileExistsError):
            self.manager.upload(self.sid, "input.txt", b"bad")
        with self.assertRaises(ValueError):
            self.manager.upload(self.sid, "../study.sqlite3", b"bad")
        with self.assertRaises(KeyError):
            self.manager.file(self.sid, "../../study.sqlite3")
        (self.manager.workspace(self.sid) / "secret.txt").symlink_to(self.root / "study.sqlite3")
        self.assertNotIn("secret.txt", {f["path"] for f in self.manager.files(self.sid)})
        with self.assertRaises(KeyError):
            self.manager.file(self.sid, "secret.txt")

    def test_resume_attaches_previously_unstreamed_user_item_without_duplication(self):
        worker = self.manager.get(self.sid)
        user = worker.message("Test", "message-0001")
        worker.interrupt()
        worker.connected = False
        history = [{"id": "turn_test", "status": "interrupted", "items": [
            {"id": "previously-unstreamed-user", "type": "userMessage", "content": [{"type": "text", "text": "Test"}]},
            {"id": "msg_test", "type": "agentMessage", "text": "First partial text"}]}]
        with patch.object(FakeRpc, "history", history):
            self.manager.get(self.sid)
        s = self.store.load(self.sid)
        self.assertEqual(len(s["events"]), 2)
        self.assertEqual(s["events"][0]["id"], user["id"])
        self.assertEqual(s["events"][1]["status"], "interrupted")

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), "macOS privacy fence")
    def test_privacy_fence_blocks_records_and_peer_workspace(self):
        workspace = self.manager.workspace(self.sid)
        workspace.mkdir(parents=True)
        peer = self.manager.workspace("s_87654321")
        peer.mkdir(parents=True)
        own = workspace / "own.txt"
        own.write_text("own")
        other = peer / "peer.txt"
        other.write_text("private")
        prefix = privacy_command(sys.executable, self.root, workspace)[:3]
        allowed = subprocess.run(prefix + [sys.executable, "-c", "import sys; print(open(sys.argv[1]).read())", str(own)],
                                 capture_output=True, text=True)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        for protected in (other, self.store.path):
            denied = subprocess.run(prefix + [sys.executable, "-c", "import sys; open(sys.argv[1]).read()", str(protected)],
                                    capture_output=True, text=True)
            self.assertNotEqual(denied.returncode, 0)


if __name__ == '__main__':
    unittest.main()
