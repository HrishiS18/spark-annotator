"""Claude Agent SDK adapter with the same study-facing contract as Codex."""
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import tempfile
import uuid
from pathlib import Path

from codex_runtime import RpcClient, RuntimeManager, RuntimeUnavailable, privacy_command, stamp


def claude_privacy_command(data_dir, workspace):
    """One macOS boundary around the SDK and every tool subprocess.

    Seatbelt cannot be nested on macOS. Keep the study-data fence and restrict
    host writes here instead of stacking Claude's Bash-only sandbox inside it.
    Temporary directories remain writable for the native CLI and task tools.
    This is not full host/network isolation for a deployed study.
    """
    prefix = privacy_command(sys.executable, data_dir, workspace)[:3]
    writable = [str(Path(workspace).resolve()), str(Path(tempfile.gettempdir()).resolve()),
                "/private/tmp", "/dev/fd"]
    exclusions = " ".join(f"(require-not (subpath {json.dumps(p)}))" for p in writable)
    credentials = [Path.home()/p for p in (".codex", ".claude", ".ssh", ".aws")]
    prefix[2] += f'''\n(deny file-write* (require-all (subpath "/") {exclusions}
        (require-not (literal "/dev/null"))))\n'''
    prefix[2] += "\n".join(f"(deny file-read-data (subpath {json.dumps(str(p))}))" for p in credentials)
    return prefix


class ClaudeRuntimeManager(RuntimeManager):
    def __init__(self, store, data_dir, rpc_factory=RpcClient, command=None):
        self.store, self.data_dir = store, Path(data_dir).resolve()
        self.rpc_factory, self.command_override = rpc_factory, command
        self.workers, self.lock = {}, threading.RLock()
        self.turn_timeout = int(os.environ.get("SPARK_TURN_TIMEOUT","600"))
        self.max_budget_usd = float(os.environ.get("SPARK_CLAUDE_MAX_BUDGET_USD","2"))
        spec = importlib.util.find_spec("claude_agent_sdk")
        self.binary = Path(spec.origin).parent / "_bundled" / "claude" if spec else None
        self.sdk_version = importlib.metadata.version("claude-agent-sdk") if spec else None
        self.version = None
        if self.binary and not command:
            try: self.version = subprocess.check_output([str(self.binary),"--version"],text=True,timeout=10).strip()
            except (OSError,subprocess.SubprocessError): self.binary = None
        if command: self.version = "TEST ONLY"

    def status(self):
        if self.command_override:
            return {"kind":"claude","installed":True,"configured":True,"version":"TEST ONLY","sdk_version":self.sdk_version,
                    "auth_mode":"synthetic","model":"TEST ONLY","local_only":True,"reason":None}
        installed = bool(self.binary and self.binary.is_file())
        key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        reason = None
        if not installed:
            reason = "Claude Agent SDK is not installed on the server. Install requirements.txt in the server's Python environment."
        elif not key:
            reason = "Claude is installed, but this server has no Anthropic API key. Configure ANTHROPIC_API_KEY on the Mac running the server, then restart it."
        return {"kind":"claude","installed":installed,"version":self.version,"sdk_version":self.sdk_version,
                "configured":installed and key,"auth_mode":"api_key","local_only":True,
                "model":os.environ.get("SPARK_CLAUDE_MODEL","sonnet"),
                "reason":reason}

    def command(self, workspace):
        if self.command_override: return self.command_override
        if not self.status()["configured"]:
            raise RuntimeUnavailable(self.status()["reason"])
        return claude_privacy_command(self.data_dir,workspace) + [
            sys.executable,"-u",str(Path(__file__).with_name("claude_sdk_worker.py"))]

    def environment(self):
        # Never inherit other providers' credentials or subscription tokens.
        env = {k:v for k,v in os.environ.items() if not any(w in k.upper() for w in ("KEY","TOKEN","SECRET","PASSWORD"))
               and not k.startswith(("ANTHROPIC_","CLAUDE_"))}
        if os.environ.get("ANTHROPIC_API_KEY"):
            env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def files(self,sid):
        return [f for f in super().files(sid) if not f["path"].startswith(".runtime/")]

    def file(self,sid,relative):
        path = super().file(sid,relative)
        if path.is_relative_to(self.workspace(sid).resolve()/".runtime"):
            raise KeyError(relative)
        return path

    def upload(self,sid,name,data):
        if name == ".runtime": raise ValueError("Internal runtime paths are reserved.")
        return super().upload(sid,name,data)

    def get(self,sid):
        with self.lock:
            if self.store.load(sid).get("runtime",{}).get("kind") != "claude":
                raise ValueError("This session does not use Claude")
            worker = self.workers.get(sid)
            if worker and worker.connected: return worker
            if worker: worker.close()
            try: worker = ClaudeWorker(self,sid)
            except Exception as exc:
                self.store.update(sid,lambda s:s["runtime"].update(status="error",error=str(exc)),
                                  {"method":"runtime/connect_failed"})
                raise
            self.workers[sid] = worker
            return worker


class ClaudeWorker:
    def __init__(self,manager,sid):
        self.manager,self.store,self.sid = manager,manager.store,sid
        self.lock = threading.RLock()
        self.connected,self.rpc,self.timer = False,None,None
        self.pending = {}
        self.stream_message_id = None
        self.blocks = {}
        self.replay_user_uuid = None
        session = self.store.load(sid)
        self.workspace = manager.workspace(sid)
        self.workspace.mkdir(parents=True,exist_ok=True)
        brief = self.workspace / "TASK.md"
        if not brief.exists(): brief.write_text(session["prompt"]["text"]+"\n",encoding="utf-8")
        runtime = session["runtime"]
        self.thread_id = runtime.get("thread_id") or str(uuid.uuid4())
        resuming = bool(runtime.get("thread_id") and any(e.get("meta",{}).get("native_user_uuid") for e in session["events"]))
        try:
            self.rpc = manager.rpc_factory(manager.command(self.workspace),str(self.workspace),self.notification,
                                           self.disconnected,env=manager.environment())
            result = self.rpc.request("initialize",{"cwd":str(self.workspace),"session_id":self.thread_id,"resume":resuming,
                "model":runtime.get("requested_model") or runtime.get("model") or "sonnet",
                "effort":runtime.get("effort","medium"),"max_budget_usd":manager.max_budget_usd,
                "persona":session["config_snapshot"]["persona"]["system"]},timeout=60)
            self.thread_id = result["session_id"]
            def ready(s):
                s["runtime"].update(status="ready",error=None,thread_id=self.thread_id,
                    auth_mode="api_key",version=manager.version,sdk_version=manager.sdk_version,
                    sandbox="study-data-fence+workspace-write",approval_policy="default+explicit-write-command-approval",
                    active_turn=None,approvals=[])
                for e in s["events"]:
                    if e.get("status") == "inProgress":
                        e["status"] = "interrupted"  # No unfinished tool is executed by reconnecting.
            self.store.update(sid,ready,{"method":"runtime/ready"})
            self.connected = True
        except BaseException:
            if self.rpc: self.rpc.close()
            raise

    def disconnected(self):
        self.connected = False
        self.pending.clear()
        if self.timer: self.timer.cancel()
        def fail(s):
            s["runtime"].update(status="error",active_turn=None,approvals=[],
                                error="Claude disconnected. Reconnect to resume the saved session.")
            for e in s["events"]:
                if e.get("status") == "inProgress": e["status"] = "interrupted"
        self.store.update(self.sid,fail,{"method":"runtime/disconnected"})

    def event(self,item_id,role,text,turn_id,status="inProgress",append=False,**metadata):
        def update(s):
            e = next((e for e in s["events"] if e.get("meta",{}).get("runtime_item_id") == item_id
                      and e["meta"].get("runtime_thread_id") == self.thread_id),None)
            if e is None:
                iso,t = stamp()
                e = {"id":"ev_"+uuid.uuid4().hex,"ts":iso,"t":t,"role":role,"text":"","tool":None,"meta":{}}
                s["events"].append(e)
            e["text"] = e["text"]+text if append else text
            e["status"] = status
            e["tool"] = metadata.get("tool")
            e["meta"].update(runtime_item_id=item_id,runtime_thread_id=self.thread_id,runtime_turn_id=turn_id,
                             runtime_provider="claude",**metadata)
        self.store.update(self.sid,update,{"method":"sdk/item","item_id":item_id})

    def block(self,block,message_id,index,turn_id,complete=False,**extra):
        kind = block.get("_type") or block.get("type")
        if kind in ("TextBlock","text"):
            self.event(message_id+":"+str(index),"assistant",block.get("text",""),turn_id,
                       status="completed" if complete else "inProgress",**extra)
        elif kind in ("ToolUseBlock","tool_use"):
            inputs = block.get("input") or {}
            text = inputs.get("command") if block["name"] == "Bash" else json.dumps(inputs,ensure_ascii=False)
            self.event(block["id"],"tool_call",text or block["name"],turn_id,tool=block["name"],input=inputs,**extra)
        elif kind in ("ToolResultBlock","tool_result"):
            content = block.get("content") or ""
            if not isinstance(content,str): content = json.dumps(content,ensure_ascii=False)
            self.event(block["tool_use_id"]+"_result","tool_result",content,turn_id,
                       status="failed" if block.get("is_error") else "completed",call_item_id=block["tool_use_id"],**extra)
            def finish_call(s):
                for e in s["events"]:
                    if e.get("meta",{}).get("runtime_item_id") == block["tool_use_id"]:
                        e["status"] = "failed" if block.get("is_error") else "completed"
            self.store.update(self.sid,finish_call)

    def notification(self,message):
        self.store.publish(self.sid,message)
        method,p = message["method"],message.get("params") or {}
        turn = p.get("turn_id")
        if method == "sdk/turn_started":
            self.store.update(self.sid,lambda s:s["runtime"].update(status="running",active_turn=turn,error=None))
        elif method == "sdk/permission":
            aid = "approval_"+uuid.uuid4().hex
            self.pending[aid] = p
            value = {"id":aid,"method":"claude/question" if p["tool"] == "AskUserQuestion" else "claude/tool",
                     "params":{"itemId":p["id"],"tool":p["tool"],"input":p["input"],
                               "command":p["input"].get("command") or json.dumps(p["input"],ensure_ascii=False),
                               "questions":p["input"].get("questions",[])}}
            def approval(s):
                s["runtime"].setdefault("approvals",[]).append(value)
                s["runtime"]["status"] = "awaiting_approval"
            self.store.update(self.sid,approval,{"method":"runtime/approval"})
        elif method == "sdk/message":
            m = p["message"]
            if m.get("parent_tool_use_id"): return  # Preserve raw subagent evidence, not main-chat text.
            kind = m["_type"]
            if kind == "SystemMessage" and m.get("subtype") == "init":
                self.store.update(self.sid,lambda s:s["runtime"].update(model=m["data"].get("model") or s["runtime"].get("model")))
            elif kind == "StreamEvent":
                event = m["event"]
                if event["type"] == "message_start":
                    self.stream_message_id = event["message"]["id"]; self.blocks = {}
                elif event["type"] == "content_block_start":
                    self.blocks[event["index"]] = event["content_block"]
                    self.block(event["content_block"],self.stream_message_id,event["index"],turn)
                elif event["type"] == "content_block_delta":
                    block = self.blocks.get(event["index"],{})
                    if event["delta"]["type"] == "text_delta":
                        self.event(self.stream_message_id+":"+str(event["index"]),"assistant",event["delta"]["text"],turn,append=True)
            elif kind == "AssistantMessage":
                mid = m.get("message_id") or self.stream_message_id or m.get("uuid")
                for i,block in enumerate(m["content"]):
                    self.block(block,mid,i,turn,complete=True,sdk_message_uuid=m.get("uuid"))
            elif kind == "UserMessage" and isinstance(m.get("content"),list):
                for i,block in enumerate(m["content"]): self.block(block,m.get("uuid") or "user",i,turn,complete=True)
        elif method == "sdk/replay":
            m = p["message"]
            raw = m["message"]
            content = raw.get("content",[])
            pure_user = m["type"] == "user" and (isinstance(content,str) or
                        any(b.get("type") == "text" for b in content))
            if pure_user:
                self.replay_user_uuid = m["uuid"]
                return
            s = self.store.load(self.sid)
            original = next((e for e in s["events"] if e.get("meta",{}).get("native_user_uuid") == self.replay_user_uuid),None)
            turn = original.get("meta",{}).get("runtime_turn_id") if original else None
            for i,block in enumerate(content if isinstance(content,list) else []):
                self.block(block,raw.get("id") or m["uuid"],i,turn,complete=True,recovered=True,sdk_message_uuid=m["uuid"])
        elif method == "sdk/turn_completed":
            if self.timer: self.timer.cancel()
            self.pending.clear()
            def finish(s):
                s["runtime"].update(status="ready" if p["status"] == "completed" else p["status"],
                    active_turn=None,approvals=[],last_turn_status=p["status"],error=p.get("error"),
                    last_usage=p.get("usage"),last_cost_usd=p.get("cost_usd"))
                for e in s["events"]:
                    if e.get("meta",{}).get("runtime_turn_id") == turn and e.get("status") == "inProgress":
                        e["status"] = p["status"]
            self.store.update(self.sid,finish,{"method":"runtime/turn_completed"})

    def message(self,text,client_id,compose_ms=None):
        with self.lock:
            s = self.store.load(self.sid)
            previous = next((e for e in s["events"] if e.get("meta",{}).get("client_message_id") == client_id),None)
            if previous: return previous
            if s["phase"] != "chat": raise RuntimeUnavailable("Conversation phase is over.")
            if s["runtime"].get("active_turn") or s["runtime"].get("status") in ("starting","running","awaiting_approval"):
                raise RuntimeUnavailable("The agent is working. Stop it or wait before sending another message.")
            iso,t = stamp()
            native_uuid,turn_id = str(uuid.uuid4()),"turn_"+uuid.uuid4().hex
            event = {"id":"ev_"+uuid.uuid4().hex,"ts":iso,"t":t,"role":"user","text":text,"tool":None,"status":"submitted",
                     "meta":{"client_message_id":client_id,"draft_ms":compose_ms,"native_user_uuid":native_uuid,
                             "runtime_turn_id":turn_id,"runtime_thread_id":self.thread_id,"runtime_provider":"claude"}}
            def submit(s):
                s["events"].append(event); s["runtime"].update(status="starting",active_turn=turn_id,error=None,last_turn_status=None)
            self.store.update(self.sid,submit,{"method":"turn/submitted"})
            self.timer = threading.Timer(self.manager.turn_timeout,self._timeout); self.timer.daemon=True; self.timer.start()
            try:
                self.rpc.request("turn/start",{"text":text,"turn_id":turn_id,"user_uuid":native_uuid})
            except Exception:
                self.timer.cancel(); self.disconnected(); raise
            return event

    def approve(self,aid,decision,answers=None):
        if decision not in ("accept","decline","cancel"): raise ValueError("Unsupported approval decision")
        with self.lock:
            p = self.pending.get(aid)
            if not p: raise RuntimeUnavailable("Approval expired. Reload the session.")
            if p["tool"] == "AskUserQuestion" and decision == "accept":
                questions = p["input"].get("questions",[])
                if not isinstance(answers,dict) or any(not isinstance(answers.get(q["question"]),str) or
                       not answers[q["question"]].strip() or len(answers[q["question"]]) > 4000 for q in questions):
                    raise ValueError("Answer each question before continuing.")
            answer_id = None
            if p["tool"] == "AskUserQuestion" and decision == "accept":
                iso,t = stamp(); answer_id = "ev_"+uuid.uuid4().hex
                answer_event = {"id":answer_id,"ts":iso,"t":t,"role":"user",
                    "text":"\n".join(q["question"]+"\n"+answers[q["question"]] for q in p["input"]["questions"]),
                    "tool":None,"status":"submitted","meta":{"runtime_provider":"claude","runtime_turn_id":p["turn_id"],
                    "source":"agent_question_answer","approval_id":aid,"delivery_status":"pending"}}
                self.store.update(self.sid,lambda s:s["events"].append(answer_event),{"method":"runtime/question_answer"})
            try:
                self.rpc.request("permission/respond",{"id":p["id"],"decision":decision,"answers":answers or {}})
            except Exception:
                if answer_id:
                    self.store.update(self.sid,lambda s:next(e for e in s["events"] if e["id"] == answer_id)["meta"].update(delivery_status="unknown"))
                raise
            self.pending.pop(aid,None)
            def decided(s):
                s["runtime"]["approvals"] = [a for a in s["runtime"].get("approvals",[]) if a["id"] != aid]
                if s["runtime"].get("active_turn"):
                    s["runtime"]["status"] = "awaiting_approval" if s["runtime"]["approvals"] else "running"
                s["runtime"].setdefault("approval_history",[]).append({"id":aid,"decision":decision,"t":time.time()})
                if answer_id:
                    next(e for e in s["events"] if e["id"] == answer_id)["meta"]["delivery_status"] = "delivered"
            self.store.update(self.sid,decided,{"method":"runtime/approval_decided","id":aid,"decision":decision})

    def interrupt(self):
        if self.store.load(self.sid)["runtime"].get("active_turn"):
            self.rpc.request("turn/interrupt",{})

    def _timeout(self):
        try: self.interrupt()
        except Exception: self.rpc.close(); self.disconnected()

    def close(self):
        if self.timer: self.timer.cancel()
        if self.rpc: self.rpc.close()
        self.disconnected()
