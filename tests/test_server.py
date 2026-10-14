import json
import tempfile
import threading
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server
from codex_runtime import RuntimeManager
from storage import Store
from tests.test_runtime import FakeRpc


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for directory in ("uploads", "recordings"):
            (root / directory).mkdir()
        self.store = Store(root / "study.sqlite3", write_lock=server.LOCK)
        self.runtime = RuntimeManager(self.store, root, rpc_factory=FakeRpc, command=["test"])
        self.patches = [patch.object(server, "_STORE", self.store), patch.object(server, "_RUNTIME", self.runtime),
                        patch.object(server, "UPLOADS", str(root / "uploads")),
                        patch.object(server, "RECORDINGS", str(root / "recordings"))]
        for p in self.patches:
            p.start()
        self.httpd = server.StudyHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.stream_shutdown = True
        self.runtime.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def request(self, path, body=None, method="GET", headers=None, raw=False):
        payload = body if raw else json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=payload, method=method,
                          headers={"Content-Type": "application/octet-stream" if raw else "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def create(self, **extra):
        code, s = self.request("/api/sessions", {"study": "dilemma", "participant_id": "TEST", **extra}, "POST")
        self.assertEqual(code, 201)
        self.sid = s["id"]
        self.prefix = f"/api/sessions/{self.sid}"
        self.request(self.prefix + "/advance", {"phase": "pre", "text": "Private baseline"}, "POST")
        return s

    def test_codex_session_message_stream_review_and_export(self):
        s = self.create()
        self.assertEqual(s["runtime"]["kind"], "codex")
        self.assertIn("taxonomy", s["config_snapshot"])
        self.assertEqual(self.request(self.prefix + "/runtime/connect", {}, "POST")[0], 200)
        code, result = self.request(self.prefix + "/message", {"text": "Test", "client_message_id": "message-0001"}, "POST")
        self.assertEqual(code, 202)
        with urlopen(self.base + self.prefix + "/stream", timeout=3) as stream:
            while True:
                line = stream.readline().decode()
                if line.startswith("data: "):
                    streamed = json.loads(line[6:])
                    break
        self.assertEqual(streamed["runtime"]["status"], "running")
        assistant = next(e for e in streamed["events"] if e["role"] == "assistant")
        code, mark = self.request(self.prefix + "/marks", {"event_id": assistant["id"], "anchor": "message"}, "POST")
        self.assertEqual(code, 201)
        review = {"types": ["additive"], "after": "Test explanation"}
        self.assertEqual(self.request(self.prefix + "/marks/" + mark["id"], {"review": review}, "PATCH")[0], 200)
        self.assertEqual(self.request(self.prefix + "/advance", {"phase": "chat"}, "POST")[0], 400)
        self.assertEqual(self.request(self.prefix + "/runtime/interrupt", {}, "POST")[0], 200)
        code, exported = self.request(self.prefix + "/export.json")
        self.assertEqual(code, 200)
        self.assertEqual(exported["marks"][0]["review"]["after"], "Test explanation")
        self.assertEqual(exported["marks"][0]["event_id"], assistant["id"])
        self.assertEqual(exported["runtime"]["last_turn_status"], "interrupted")

    def test_cross_origin_and_rebinding_requests_are_rejected(self):
        self.assertEqual(self.request("/api/config", headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/config", headers={"Host": "attacker.example"})[0], 403)

    def test_workspace_upload_preview_and_path_security(self):
        self.create()
        self.assertEqual(self.request(self.prefix + "/workspace/files", b"hello", "POST", {"X-Filename": "input.txt"}, raw=True)[0], 201)
        self.assertEqual(self.request(self.prefix + "/workspace/files", b"bad", "POST", {"X-Filename": "input.txt"}, raw=True)[0], 409)
        self.assertEqual(self.request(self.prefix + "/workspace/files", b"bad", "POST", {"X-Filename": "../study.sqlite3"}, raw=True)[0], 400)
        self.assertEqual(self.request(self.prefix + "/workspace/file?path=../../study.sqlite3")[0], 404)
        with urlopen(self.base + self.prefix + "/workspace/file?path=input.txt") as response:
            self.assertEqual(response.read(), b"hello")

    def test_import_does_not_rewrite_anchors_or_reviews(self):
        self.create(study="agentic")
        # The external condition starts in chat rather than pre.
        raw = json.dumps({"messages": [{"role": "assistant", "content": "Imported evidence", "timestamp": "2026-10-13T00:00:01Z"}]}).encode()
        code, upload = self.request("/api/upload", raw, "POST", {"X-Filename": "fixture.json"}, raw=True)
        self.assertEqual(code, 200)
        body = {"upload_id": upload["upload_id"]}
        code, _ = self.request(self.prefix + "/import", body, "POST")
        self.assertEqual(code, 200)
        s = self.store.load(self.sid)
        eid = s["events"][0]["id"]
        _, mark = self.request(self.prefix + "/marks", {"event_id": eid, "anchor": "message"}, "POST")
        self.request(self.prefix + "/marks/" + mark["id"], {"review": {"after": "Keep this"}}, "PATCH")
        self.request(self.prefix + "/import", body, "POST")
        s = self.store.load(self.sid)
        self.assertEqual(len(s["events"]), 1)
        self.assertEqual(s["marks"][0]["event_id"], eid)
        self.assertEqual(s["marks"][0]["review"]["after"], "Keep this")

    def test_plain_chat_never_silently_falls_back_to_demo(self):
        self.create(runtime="plain")
        with patch.object(server.llm, "status", return_value={"live": False}):
            code, _ = self.request(self.prefix + "/message", {"text": "Hi"}, "POST")
        self.assertEqual(code, 503)
        self.assertFalse(any(e["role"] == "assistant" for e in self.store.load(self.sid)["events"]))

    def test_live_deltas_and_bookmarks_do_not_conflict_or_lose_records(self):
        self.create()
        worker = self.runtime.get(self.sid)
        worker.message("Test", "message-0001")
        def stream():
            for _ in range(25):
                worker.notification({"method": "item/agentMessage/delta", "params": {
                    "threadId": worker.thread_id, "turnId": "turn_test", "itemId": "msg_test", "delta": " more"}})
        def mark(i):
            code, _ = self.request(self.prefix + "/marks", {"id": f"m_{i:08x}", "anchor": "latest"}, "POST")
            return code
        with ThreadPoolExecutor(max_workers=8) as pool:
            live = pool.submit(stream)
            codes = list(pool.map(mark, range(20)))
            live.result()
        self.assertEqual(codes, [201] * 20)
        self.assertEqual(len(self.store.load(self.sid)["marks"]), 20)

    def test_bookmark_retries_keep_click_and_original_exposure(self):
        self.create(runtime="demo")
        body = {"id":"m_12345678","client_version":2,"t":100,"quick_type":"additive"}
        _, mark = self.request(self.prefix+"/marks",body,"POST")
        self.request(self.prefix+"/marks",dict(body,client_version=1,t=200,quick_type=None),"POST")
        saved = self.store.load(self.sid)["marks"][0]
        self.assertIsNone(saved["event_id"])
        self.assertEqual(saved["t"],100)
        self.assertEqual(saved["quick_type"],"additive")

    def test_independent_pass_gates_suggestions_and_cannot_be_bypassed(self):
        self.create(runtime="demo")
        with server.LOCK:
            s = self.store.load(self.sid)
            ev = server.add_event(s,"assistant","Synthetic suggestion")
            s["candidates"] = [{"id":"c_test","source":"judge","trigger_event_id":ev["id"],"type":"additive","rationale":"PRIVATE_MODEL_SENTINEL"}]
            s["judge"] = {"status":"done","summary":"PRIVATE_MODEL_SENTINEL"}
            self.store.save(s)
        self.request(self.prefix+"/advance",{"phase":"chat"},"POST")
        self.request(self.prefix+"/advance",{"phase":"post","text":"Final position"},"POST")
        _, m = self.request(self.prefix+"/marks",{},"POST")
        self.assertEqual(self.request(self.prefix+"/advance",{"phase":"review"},"POST")[0],400)
        self.assertEqual(self.request(self.prefix+"/review/complete-independent",{},"POST")[0],400)
        _, hidden = self.request(self.prefix)
        self.assertEqual(hidden["candidates"],[])
        self.assertNotIn("summary",hidden["judge"])
        _, poll = self.request(self.prefix+"/poll")
        self.assertNotIn("summary",poll["judge"])
        self.assertEqual(len(self.request(self.prefix+"?view=researcher")[1]["candidates"]),1)
        self.request(self.prefix+"/marks/"+m["id"],{"review":{"decision":"unsure"}},"PATCH")
        self.assertEqual(self.request(self.prefix+"/review/complete-independent",{},"POST")[0],200)
        self.assertEqual(self.request(self.prefix+"/marks/"+m["id"],{"review":{"decision":"rejected"}},"PATCH")[0],409)
        self.assertEqual(self.request(self.prefix+"/advance",{"phase":"review"},"POST")[0],200)
        self.assertEqual(self.request(self.prefix+"/candidates/c_test",{"participant_response":{"answer":"yes"}},"PATCH")[0],400)

    def test_judge_explicit_run_only_and_no_regex_endpoints(self):
        self.create(runtime="demo")
        with patch.object(server.judge,"status",return_value={"live":True,"model":"test","provider":"codex"}):
            self.assertEqual(self.request(self.prefix+"/judge",{},"POST")[0],400)
            self.assertEqual(self.request(self.prefix+"/judge",{"hints":True},"POST")[0],400)
        self.assertEqual(self.request(self.prefix+"/detect",{},"POST")[0],404)
        with server.LOCK:
            s = self.store.load(self.sid); server.add_event(s,"assistant","Synthetic."); self.store.save(s)
        self.request(self.prefix+"/advance",{"phase":"chat"},"POST")
        self.assertIsNone(self.store.load(self.sid).get("judge"))
        started, release = threading.Event(), threading.Event()
        def infer(s):
            started.set(); release.wait(3); return [], {"summary":"No candidates","version":"test"}
        with patch.object(server.judge,"status",return_value={"live":True,"model":"test","provider":"codex"}), patch.object(server.judge,"run",side_effect=infer):
            self.assertEqual(self.request(self.prefix+"/judge",{},"POST")[0],202)
            self.assertTrue(started.wait(1))
            self.assertEqual(self.request(self.prefix+"/judge",{},"POST")[0],400)
            release.set()
            for _ in range(50):
                if self.store.load(self.sid).get("judge",{}).get("status") == "done": break
                time.sleep(.02)
        saved = self.store.load(self.sid)
        self.assertEqual(saved["judge"]["status"],"done")
        self.assertEqual(saved["candidates"],[])
        self.assertEqual(saved["analysis_runs"][0]["source"],"judge")

    def test_failed_judge_run_preserves_history_without_old_active_predictions(self):
        self.create(runtime="demo")
        with server.LOCK:
            s = self.store.load(self.sid); ev = server.add_event(s,"assistant","Synthetic.")
            server.advance(s,"chat")
            server._merge_candidates(s,[{"trigger_event_id":ev["id"],"type":"additive"}],"judge",{"status":"done"})
            self.store.save(s)
        with patch.object(server.judge,"run",side_effect=server.llm.LLMError("Unavailable")):
            server.run_judge(self.sid)
        saved = self.store.load(self.sid)
        self.assertEqual(saved["judge"]["status"],"error")
        self.assertEqual(saved["candidates"],[])
        self.assertEqual(len(saved["analysis_runs"]),2)
        self.assertEqual(saved["analysis_runs"][0]["metadata"]["status"],"done")
        self.assertEqual(saved["analysis_runs"][1]["metadata"]["status"],"error")

    def test_startup_judge_recovery_does_not_start_inference(self):
        self.create(runtime="demo")
        with server.LOCK:
            s = self.store.load(self.sid); s["judge"] = {"status":"running"}; self.store.save(s)
        with patch.object(server.judge,"run") as inference:
            server.recover_judges()
        inference.assert_not_called()
        self.assertEqual(self.store.load(self.sid)["judge"]["status"],"error")


if __name__ == "__main__":
    unittest.main()
