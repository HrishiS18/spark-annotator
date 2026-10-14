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
from agent_runtime import RuntimeManager as AgentManager
from claude_runtime import ClaudeRuntimeManager
import sys


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for directory in ("uploads", "recordings"):
            (root / directory).mkdir()
        self.store = Store(root / "study.sqlite3", write_lock=server.LOCK)
        self.runtime = RuntimeManager(self.store, root, rpc_factory=FakeRpc, command=["test"])
        self.claude=ClaudeRuntimeManager(self.store,root,command=[sys.executable,str(Path(__file__).with_name("mock_claude_worker.py"))])
        self.agents=AgentManager(self.store,root,codex=self.runtime,claude=self.claude)
        self.patches = [patch.object(server, "_STORE", self.store), patch.object(server, "_RUNTIME", self.agents),
                        patch.object(server, "UPLOADS", str(root / "uploads")),
                        patch.object(server, "RECORDINGS", str(root / "recordings")),
                        patch.object(server.judge, "status", return_value={"live":True,"provider":"test","model":"test","version":"test"}),
                        patch.object(server.judge, "run", return_value=([],{"summary":"Synthetic no candidates","model":"test"}))]
        for p in self.patches:
            p.start()
        self.httpd = server.StudyHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.stream_shutdown = True
        self.agents.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        for worker in list(server._JUDGE_JOBS.values()):
            worker.join(timeout=5)
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
        body = {"study":"dilemma","participant_id":"TEST",**extra}
        if extra.get("runtime") in ("plain","demo"):
            s,code = server.create_session(body,allow_legacy=True),201
        else:
            code,s = self.request("/api/sessions",body,"POST")
        self.assertEqual(code, 201)
        self.sid = s["id"]
        self.prefix = f"/api/sessions/{self.sid}"
        self.request(self.prefix + "/advance", {"phase": "pre", "text": "Private baseline"}, "POST")
        return s

    def wait_judge(self):
        for _ in range(150):
            s = self.store.load(self.sid)
            if (s.get("judge") or {}).get("status") not in ("queued","running") and self.sid not in server._JUDGE_JOBS:
                return s
            time.sleep(.02)
        self.fail("Judge job did not finish")

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

    def test_blinded_double_coded_judge_validation_api(self):
        self.create()
        with server.LOCK:
            s = self.store.load(self.sid)
            s["events"] = [
                {"id":"ev_v1","role":"user","text":"I favor option A.","t":1,"meta":{}},
                {"id":"ev_v2","role":"assistant","text":"What if the affected person cannot appeal?","t":2,"meta":{}},
                {"id":"ev_v3","role":"user","text":"That changes how I see the fairness issue.","t":3,"meta":{}},
                {"id":"ev_v4","role":"assistant","text":"What follows?","t":4,"meta":{}},
                {"id":"ev_v5","role":"user","text":"I would add a review path.","t":5,"meta":{}},
            ]
            s["candidates"] = [{"id":"c_private","source":"judge","start_event_id":"ev_v2","end_event_id":"ev_v3",
                "trigger_event_id":"ev_v2","type":"counterfactual","secondary_type":"none","insight_evidence":"expressed",
                "uptake":"observed","persuasion":"not_observed","copying":"not_observed","strength":"high",
                "rationale":"PRIVATE_MODEL_LABEL"}]
            self.store.save(s)
        code, created = self.request("/api/validations", {"name":"API pilot","session_ids":[self.sid],
            "candidate_n":1,"report_n":0,"control_n":0,"seed":"fixed","coder_a":"A","coder_b":"B"}, "POST")
        self.assertEqual(code, 201)
        self.assertEqual(created["items"], 1)
        vid, links = created["id"], created["coder_links"]
        code, blinded = self.request(f"/api/validations/{vid}?token={links[0]['token']}")
        self.assertEqual(code, 200)
        self.assertNotIn("PRIVATE_MODEL_LABEL", repr(blinded))
        self.assertNotIn("stratum", repr(blinded))
        payload = {"item_id":blinded["items"][0]["id"],"evidence":"expressed","primary_type":"counterfactual",
                   "uptake":"observed","span_quality":"adequate","confidence":"high","notes":"clear"}
        for link in links:
            self.assertEqual(self.request(f"/api/validations/{vid}/codes?token={link['token']}", payload, "POST")[0], 200)
        code, result = self.request(f"/api/validations/{vid}?view=researcher")
        self.assertEqual(code, 200)
        self.assertEqual(result["complete_items"], 1)
        self.assertEqual(result["agreement"]["evidence"]["cohen_kappa"], 1.0)
        self.assertIn("PRIVATE_MODEL_LABEL", repr(result))

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

    def test_manual_judge_override_for_demo_and_no_regex_endpoints(self):
        self.create(runtime="demo")
        with patch.object(server.judge,"status",return_value={"live":True,"model":"test","provider":"codex"}):
            self.assertEqual(self.request(self.prefix+"/judge",{},"POST")[0],400)
            self.assertEqual(self.request(self.prefix+"/judge",{"hints":True},"POST")[0],400)
        self.assertEqual(self.request(self.prefix+"/detect",{},"POST")[0],404)
        with server.LOCK:
            s = self.store.load(self.sid); server.add_event(s,"assistant","Synthetic."); self.store.save(s)
        self.request(self.prefix+"/advance",{"phase":"chat"},"POST")
        self.assertEqual(self.store.load(self.sid)["judge"]["status"],"disabled")
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

    def test_automatic_judge_after_conversation_deduplicates_refresh_and_waits_before_finish(self):
        self.create()
        with server.LOCK:
            s = self.store.load(self.sid); server.add_event(s,"assistant","Synthetic suggestion."); self.store.save(s)
        started, release = threading.Event(), threading.Event()
        def infer(s):
            started.set(); release.wait(3); return [], {"summary":"Empty is a valid result","version":"test"}
        with patch.object(server.judge,"run",side_effect=infer) as model:
            try:
                self.assertEqual(self.request(self.prefix+"/advance",{"phase":"chat"},"POST")[0],200)
                self.assertTrue(started.wait(1))
                with ThreadPoolExecutor(max_workers=8) as pool:
                    replies = list(pool.map(lambda _: self.request(self.prefix+"/judge/ensure",{},"POST"),range(12)))
                self.assertTrue(all(code == 202 for code,_ in replies))
                self.request(self.prefix+"/advance",{"phase":"post","text":"Final position"},"POST")
                self.assertEqual(self.request(self.prefix)[1]["candidates"],[])
                self.request(self.prefix+"/review/complete-independent",{},"POST")
                self.assertEqual(self.request(self.prefix+"/advance",{"phase":"review"},"POST")[0],400)
            finally:
                release.set()
            saved = self.wait_judge()
            for _ in range(3):
                self.assertEqual(self.request(self.prefix+"/judge/ensure",{},"POST")[0],200)
            self.assertEqual(model.call_count,1)
        self.assertEqual(saved["judge"]["status"],"done")
        self.assertEqual(saved["judge"]["n"],0)
        self.assertEqual(saved["judge"]["requested_by"],"automatic")
        self.assertEqual(saved["analysis_runs"][0]["metadata"]["requested_by"],"automatic")

    def test_automatic_judge_waits_for_external_transcript_then_starts_on_import(self):
        self.create(study="agentic")
        with patch.object(server.judge,"run",return_value=([],{"summary":"Synthetic"})) as model:
            self.request(self.prefix+"/advance",{"phase":"chat"},"POST")
            self.assertEqual(self.store.load(self.sid)["judge"]["status"],"waiting_for_transcript")
            self.request(self.prefix+"/judge/ensure",{},"POST")
            model.assert_not_called()
            raw = json.dumps({"messages":[{"role":"assistant","content":"Synthetic imported message.","timestamp":"2026-10-13T00:00:01Z"}]}).encode()
            _, upload = self.request("/api/upload",raw,"POST",{"X-Filename":"fixture.json"},raw=True)
            self.assertEqual(self.request(self.prefix+"/import",{"upload_id":upload["upload_id"]},"POST")[0],200)
            self.assertEqual(self.wait_judge()["judge"]["status"],"done")
            self.request(self.prefix+"/import",{"upload_id":upload["upload_id"]},"POST")
            self.assertEqual(model.call_count,1)

    def test_automatic_failure_is_distinct_and_retry_is_explicit(self):
        self.create()
        with server.LOCK:
            s = self.store.load(self.sid); server.add_event(s,"user","Synthetic question."); self.store.save(s)
        with patch.object(server.judge,"run",side_effect=server.llm.LLMError("Synthetic failure")) as model:
            self.request(self.prefix+"/advance",{"phase":"chat"},"POST")
            self.assertEqual(self.wait_judge()["judge"]["status"],"error")
            self.request(self.prefix+"/judge/ensure",{},"POST")
            self.assertEqual(model.call_count,1)
        self.assertEqual(self.request(self.prefix+"/judge",{},"POST")[0],202)
        self.assertEqual(self.wait_judge()["judge"]["status"],"done")
        self.assertEqual(len(self.store.load(self.sid)["analysis_runs"]),2)

    def test_no_live_calls_for_demo_disabled_or_unavailable_judges(self):
        self.create(runtime="demo")
        with server.LOCK:
            s = self.store.load(self.sid); server.add_event(s,"assistant","Synthetic demo."); self.store.save(s)
        with patch.object(server.judge,"run") as model:
            self.request(self.prefix+"/advance",{"phase":"chat"},"POST")
            self.assertEqual(self.store.load(self.sid)["judge"]["status"],"disabled")
            model.assert_not_called()
        self.create()
        with server.LOCK:
            s = self.store.load(self.sid); server.add_event(s,"assistant","Synthetic live."); self.store.save(s)
        with patch.object(server.judge,"status",return_value={"live":False,"reason":"Missing setup","provider":"test","model":"test"}), patch.object(server.judge,"run") as model:
            self.request(self.prefix+"/advance",{"phase":"chat"},"POST")
            self.assertEqual(self.store.load(self.sid)["judge"]["status"],"unavailable")
            model.assert_not_called()
        with patch.dict("os.environ",{"SPARK_AUTO_JUDGE":"0"}), patch.object(server.judge,"run") as model:
            self.request(self.prefix+"/judge/ensure",{},"POST")
            self.assertEqual(self.store.load(self.sid)["judge"]["status"],"disabled")
            model.assert_not_called()

    def test_claude_native_question_tools_and_private_workspace_routes(self):
        self.create(runtime="claude")
        self.assertEqual(self.request(self.prefix+"/runtime/connect",{},"POST")[0],200)
        self.assertEqual(self.request(self.prefix+"/workspace/files",b"Test","POST",{"X-Filename":"input.txt"},raw=True)[0],201)
        self.assertEqual(self.request(self.prefix+"/message",{"text":"[ASK] Make a file","client_message_id":"claude-api-test-0001"},"POST")[0],202)
        for _ in range(100):
            rt=self.store.load(self.sid)["runtime"]
            if rt.get("approvals"): break
            time.sleep(.02)
        a=rt["approvals"][0]
        self.assertEqual(a["method"],"claude/question")
        self.assertEqual(self.request(self.prefix+"/runtime/approval",{"id":a["id"],"decision":"accept"},"POST")[0],400)
        self.assertEqual(self.request(self.prefix+"/runtime/approval",{"id":a["id"],"decision":"accept",
            "answers":{"Which format should I use?":"Text"}},"POST")[0],200)
        for _ in range(100):
            if self.store.load(self.sid)["runtime"].get("last_turn_status")=="completed": break
            time.sleep(.02)
        self.assertEqual(self.request(self.prefix+"/workspace/file?path=.runtime/claude/fake-history.json")[0],404)
        self.assertEqual(self.request(self.prefix+"/import",{"upload_id":"000000000000"},"POST")[0],400)
        self.assertEqual(self.store.load(self.sid)["runtime"]["kind"],"claude")

    def test_only_two_new_ai_runtime_choices(self):
        for kind in ("plain","demo","unknown"):
            self.assertEqual(self.request("/api/sessions",{"study":"dilemma","runtime":kind},"POST")[0],400)
        config=self.request("/api/config")[1]
        self.assertEqual(set(config["runtimes"]),{"codex","claude"})


if __name__ == "__main__":
    unittest.main()
