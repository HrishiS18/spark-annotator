"""Opt-in real Claude SDK integration test, synthetic temporary data only.

API key may be read privately from the terminal; never saved to a file or argv.
"""
import argparse
import getpass
import json
import os
import tempfile
import threading
import time
import sys
from pathlib import Path
from urllib.request import Request,urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    args=argparse.ArgumentParser()
    args.add_argument("--run-live",action="store_true")
    args.add_argument("--key-stdin",action="store_true")
    options=args.parse_args()
    if not options.run_live: args.error("This uses paid API inference; explicitly pass --run-live.")
    if options.key_stdin:
        os.environ["ANTHROPIC_API_KEY"]=getpass.getpass("Anthropic API key (not saved): ").strip()
    if not os.environ.get("ANTHROPIC_API_KEY"): args.error("Configure ANTHROPIC_API_KEY.")
    os.environ["SPARK_AUTO_JUDGE"]="0"
    os.environ["SPARK_CLAUDE_MAX_BUDGET_USD"]="2"
    with tempfile.TemporaryDirectory(prefix="spark-claude-live-") as data:
        os.environ["SPARK_DATA_DIR"]=data
        import server
        httpd=server.StudyHTTPServer(("127.0.0.1",0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True); thread.start()
        base=f"http://127.0.0.1:{httpd.server_port}"
        def api(path,body=None):
            request=Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                            headers={"Content-Type":"application/json"},method="POST" if body is not None else "GET")
            with urlopen(request,timeout=60) as response: return json.load(response)
        try:
            s=api("/api/sessions",{"study":"dilemma","runtime":"claude","participant_id":"SYNTHETIC-LIVE-CLAUDE",
                "task":"Synthetic SDK test: create smoke.txt containing hello spark and a newline. Work only inside this task workspace."})
            path="/api/sessions/"+s["id"]
            api(path+"/advance",{"phase":"pre","text":"PRIVATE BASELINE — not sent to the agent"})
            runtime=api(path+"/runtime/connect",{})
            assert runtime["kind"]=="claude" and runtime["status"]=="ready"
            print(json.dumps({"connected":True,"sdk_version":runtime["sdk_version"],"runtime_version":runtime["version"]}),flush=True)
            api(path+"/message",{"text":"Use Read to read TASK.md, then Write to create smoke.txt containing exactly 'hello spark' and a newline. "
                "Remember the number 4327 for a later question. Do not use network or read outside the workspace. Give a short final reply.",
                "client_message_id":"live-claude-0001"})
            workspace=server.get_runtime().workspace(s["id"])
            def wait(approve=False,need_permission=False):
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    saved=api(path)
                    rt=saved["runtime"]
                    if need_permission and rt.get("approvals"): return saved
                    for a in rt.get("approvals",[]) if approve else []:
                        tool=a["params"].get("tool"); inputs=a["params"].get("input") or {}
                        target=(workspace/str(inputs.get("file_path",""))).resolve()
                        allowed=tool=="Write" and target==workspace/"smoke.txt" and inputs.get("content")=="hello spark\n"
                        if tool=="Bash":
                            allowed=inputs.get("command","").strip() in ("cat TASK.md","pwd","ls","cat smoke.txt")
                        api(path+"/runtime/approval",{"id":a["id"],"decision":"accept" if allowed else "decline"})
                        print(json.dumps({"tool":tool,"approval":"accept" if allowed else "decline"}),flush=True)
                    if rt.get("last_turn_status") and not rt.get("active_turn"): return saved
                    time.sleep(.2)
                api(path+"/runtime/interrupt",{})
                raise AssertionError("Claude did not finish within the live-test limit.")
            first=wait(approve=True)
            assert first["runtime"]["last_turn_status"]=="completed",first["runtime"].get("error")
            assert (workspace/"smoke.txt").read_text()=="hello spark\n"
            assert any(e["role"]=="tool_result" for e in first["events"])
            print(json.dumps({"file_tools_passed":True,"model":first["runtime"]["model"],
                              "cost_usd":first["runtime"].get("last_cost_usd")}),flush=True)
            api(path+"/message",{"text":"Use Bash to run exactly: cat smoke.txt. Do not use any other tools or commands. Give a short final reply.",
                                "client_message_id":"live-claude-shell-0001"})
            shell=wait(approve=True)
            assert shell["runtime"]["last_turn_status"]=="completed",shell["runtime"].get("error")
            new_events=[e for e in shell["events"] if e["id"] not in {x["id"] for x in first["events"]}]
            assert any(e["role"]=="tool_call" and e.get("tool")=="Bash" for e in new_events)
            assert any(e["role"]=="tool_result" and "hello spark" in e["text"] for e in new_events), [
                {"status":e.get("status"),"text":e["text"]} for e in new_events if e["role"]=="tool_result"]
            print(json.dumps({"shell_execution_passed":True}),flush=True)
            api(path+"/message",{"text":"Use Bash to run sleep 30. Wait for my approval before doing anything.",
                                "client_message_id":"live-claude-0002"})
            pending=wait(need_permission=True)
            assert pending["runtime"]["approvals"],"Expected a tool-approval pause"
            api(path+"/runtime/interrupt",{})
            stopped=wait()
            assert stopped["runtime"]["last_turn_status"]=="interrupted"
            ids=[e["id"] for e in stopped["events"]]
            session_id=stopped["runtime"]["thread_id"]
            server.get_runtime().adapter(s["id"]).get(s["id"]).close()
            resumed=api(path+"/runtime/connect",{})
            assert resumed["thread_id"]==session_id
            assert [e["id"] for e in api(path)["events"]]==ids,"Resume changed or duplicated transcript IDs"
            print(json.dumps({"interrupt_passed":True,"resume_passed":True}),flush=True)
            api(path+"/message",{"text":"What number did I ask you to remember? Reply with only that number. Do not use tools.",
                                "client_message_id":"live-claude-0003"})
            final=wait()
            assert final["runtime"]["last_turn_status"]=="completed",final["runtime"].get("error")
            assert any(e["role"]=="assistant" and "4327" in e["text"] for e in final["events"] if e["id"] not in ids)
            secret=os.environ["ANTHROPIC_API_KEY"]
            exported=json.dumps(server.get_store().export(s["id"]))
            assert secret not in exported
            for file in Path(data).rglob("*"):
                if file.is_file(): assert secret.encode() not in file.read_bytes(),"Credential was written to test data"
            print(json.dumps({"passed":True,"history_retained":True,"credentials_not_persisted":True,
                              "roles":[e["role"] for e in final["events"]]}),flush=True)
        finally:
            httpd.stream_shutdown=True; httpd.shutdown(); httpd.server_close()
            server.get_runtime().close(); thread.join(timeout=3)
            os.environ.pop("ANTHROPIC_API_KEY",None)


if __name__=="__main__": main()
