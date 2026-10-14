"""Browser smoke-test server. Uses temporary data and an explicitly fake runtime."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from codex_runtime import RuntimeManager
from agent_runtime import RuntimeManager as AgentManager
from claude_runtime import ClaudeRuntimeManager

manager = RuntimeManager(server.get_store(), server.DATA,
                         command=[sys.executable, str(Path(__file__).with_name("mock_app_server.py"))])
manager.version = "mock-app-server (TEST ONLY)"
claude=ClaudeRuntimeManager(server.get_store(),server.DATA,
                          command=[sys.executable,str(Path(__file__).with_name("mock_claude_worker.py"))])
server._RUNTIME = AgentManager(server.get_store(),server.DATA,codex=manager,claude=claude)

def fake_judge(session):
    """Explicitly synthetic predictions; never calls a model in browser QA."""
    if session["participant_id"] == "TEST-JUDGE-ERROR" and session["judge"]["requested_by"] == "automatic":
        raise server.llm.LLMError("Synthetic generation failure")
    if session["participant_id"] in ("TEST-JUDGE-EMPTY","TEST-JUDGE-ERROR"):
        return [], {"summary":"SYNTHETIC_EMPTY_RESULT","model":"TEST ONLY"}
    trigger = next(e for e in session["events"] if e["role"] == "assistant")
    return [{"source":"judge","start_event_id":trigger["id"],"end_event_id":trigger["id"],
             "trigger_event_id":trigger["id"],"uptake_event_id":"","type":"additive",
             "secondary_type":"none","insight_evidence":"possible","persuasion":"unclear",
             "copying":"not_observed","uptake":"unclear","strength":"low",
             "trigger_quote":trigger["text"],"uptake_quote":"","before":"","after":"",
             "rationale":"SYNTHETIC_TEST_SUGGESTION","quotes_verified":True}], {"summary":"SYNTHETIC_TEST_SUMMARY","model":"TEST ONLY"}

server.judge.run = fake_judge
server.judge.status = lambda: {"live":True,"provider":"test","model":"TEST ONLY","version":server.judge.VERSION,
                               "effort":"medium","cost_note":"Synthetic test fixture — no live inference."}
server.main()
