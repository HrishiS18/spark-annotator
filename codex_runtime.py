"""A local, stdio Codex app-server bridge. No agent loop is reimplemented here.

Each study session owns a subprocess and workspace. Study records never enter the
model context. This is a localhost development integration, not a multi-tenant host.
"""

import json
import os
import queue
import signal
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path


# Match the low-friction interactive Codex profile: routine work inside the
# workspace stays sandboxed and does not require a click. Codex asks only when
# an action needs to cross that boundary.
APPROVAL_POLICY = "on-request"
PERMISSION_PROFILE = ":workspace"


class RuntimeUnavailable(ValueError):
    pass


class RpcClient:
    def __init__(self, command, cwd, callback, disconnected, env=None):
        self.callback, self.disconnected = callback, disconnected
        self.pending, self.serial = {}, 0
        self.lock = threading.RLock()
        self.closed = False
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                msg = json.loads(line)
                if "method" in msg:
                    self.callback(msg)
                else:
                    with self.lock:
                        waiter = self.pending.get(msg.get("id"))
                    if waiter:
                        waiter.put(msg)
        except Exception:
            pass
        finally:
            with self.lock:
                for waiter in self.pending.values():
                    waiter.put({"error": {"message": "Codex process disconnected."}})
            if not self.closed:
                self.disconnected()

    def send(self, message):
        with self.lock:
            if self.closed or self.proc.poll() is not None:
                raise RuntimeUnavailable("Codex process is not running. Reconnect to resume.")
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    def request(self, method, params=None, timeout=30):
        with self.lock:
            self.serial += 1
            rid = self.serial
            waiter = self.pending[rid] = queue.Queue()
        try:
            self.send({"id": rid, "method": method, "params": params or {}})
            reply = waiter.get(timeout=timeout)
            if "error" in reply:
                raise RuntimeUnavailable(reply["error"].get("message", "Codex request failed."))
            return reply.get("result", {})
        except queue.Empty:
            # A timed-out mutating request may have succeeded. Never retry it
            # automatically, which could create another thread or inference turn.
            self.close()
            raise RuntimeUnavailable("Codex request timed out. Reconnect before retrying.")
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def close(self):
        self.closed = True
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def privacy_command(binary, data_dir, workspace):
    """Build the legacy process privacy fence used by non-Codex workers.

    Claude and the read-only judge use the three-element sandbox-exec prefix.
    Codex app-server must not use it because Codex creates its own sandbox.
    """
    guard = "/usr/bin/sandbox-exec"
    if not Path(guard).is_file():
        raise RuntimeUnavailable("Local privacy fence requires macOS. Use an isolated execution host before enabling this worker on another OS.")
    data = json.dumps(str(Path(data_dir).resolve()))
    work = json.dumps(str(Path(workspace).resolve()))
    profile = f'''(version 1)
        (allow default)
        (deny file-read-data file-write*
            (require-all (subpath {data}) (require-not (subpath {work}))))'''
    return [guard, "-p", profile, binary, "app-server", "--listen", "stdio://",
            "-c", 'shell_environment_policy.inherit="core"',
            "-c", 'shell_environment_policy.exclude=["*KEY*","*TOKEN*","*SECRET*","*PASSWORD*"]',
            "-c", 'sandbox_workspace_write.network_access=false',
            "-c", 'features.multi_agent=false']


def app_server_command(binary):
    """Start app-server without blocking its own native sandbox.

    Wrapping app-server in another macOS Seatbelt profile prevents Codex's
    per-turn sandbox from initializing and turns routine actions into prompts.
    """
    return [binary, "app-server", "--listen", "stdio://",
            "-c", 'shell_environment_policy.inherit="core"',
            "-c", 'shell_environment_policy.exclude=["*KEY*","*TOKEN*","*SECRET*","*PASSWORD*"]',
            "-c", 'features.multi_agent=false']


def stamp():
    from datetime import datetime, timezone
    t = time.time()
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z"), t


class Worker:
    def __init__(self, manager, sid):
        self.manager, self.store, self.sid = manager, manager.store, sid
        self.lock = threading.RLock()
        self.rpc = None
        self.approval_ids = {}
        self.last_user_event = None
        self.timer = None
        self.connected = False
        self.voice_sdp_event = None
        self.voice_sdp = None
        self.voice_start_error = None
        session = self.store.load(sid)
        self.workspace = manager.workspace(sid)
        self.workspace.mkdir(parents=True, exist_ok=True)
        brief = self.workspace / "TASK.md"
        if not brief.exists():
            brief.write_text(session["prompt"]["text"] + "\n", encoding="utf-8")
        command = manager.command(self.workspace)
        try:
            self.rpc = manager.rpc_factory(command, str(self.workspace), self.notification, self.disconnected)
            self.rpc.request("initialize", {"clientInfo": {"name": "spark_annotator",
                             "title": "Spark Annotator", "version": "0.2.0"},
                             "capabilities": {"experimentalApi": True}})
            self.rpc.send({"method": "initialized", "params": {}})
            account = self.rpc.request("account/read", {"refreshToken": False})
            if not account.get("account") and os.environ.get("OPENAI_API_KEY"):
                # Authentication travels only over the private subprocess pipe;
                # it is never persisted in study data or returned to the browser.
                self.rpc.request("account/login/start", {"type": "apiKey", "apiKey": os.environ["OPENAI_API_KEY"]})
                account = self.rpc.request("account/read", {"refreshToken": False})
            if not account.get("account"):
                raise RuntimeUnavailable("Codex is not signed in. Run `codex login` on this Mac, or configure an API key locally; do not paste a key into the chat.")
            runtime = session["runtime"]
            # The profile is selected once at thread start/resume and persists
            # across turns; turn/start must not also send legacy sandbox fields.
            params = {"cwd": str(self.workspace), "approvalPolicy": APPROVAL_POLICY,
                      "permissions": PERMISSION_PROFILE}
            if runtime.get("model"):
                params["model"] = runtime["model"]
            if runtime.get("thread_id"):
                params["threadId"] = runtime["thread_id"]
                result = self.rpc.request("thread/resume", params)
            else:
                params["developerInstructions"] = session["config_snapshot"]["persona"]["system"] + (
                    "\n\nWork with the participant on the task in TASK.md. Use only the task workspace. "
                    "Do not search for study records, participant annotations, authentication files, or other sessions.")
                result = self.rpc.request("thread/start", params)
            self.thread_id = result["thread"]["id"]
            def ready(s):
                s["runtime"].update(status="ready", error=None, thread_id=self.thread_id,
                    model=result.get("model") or runtime.get("model"),
                    model_provider=result.get("modelProvider"), sandbox=PERMISSION_PROFILE,
                    approval_policy=APPROVAL_POLICY, auth_mode=account["account"].get("type"),
                    active_turn=None, approvals=[], version=manager.version,
                    voice={"status": "inactive"})
            self.store.update(sid, ready, {"method": "runtime/ready"})
            # Restore completed/partial items without a new inference request.
            for turn in result["thread"].get("turns", []):
                for item in turn.get("items", []):
                    if turn.get("status") != "completed":
                        item = dict(item, status="interrupted" if turn.get("status") == "inProgress" else turn.get("status", "interrupted"))
                    self._item(item, turn["id"], completed=True)
            self.connected = True
        except BaseException:
            if self.rpc:
                self.rpc.close()
            raise

    def disconnected(self):
        self.connected = False
        self.approval_ids.clear()
        if self.timer:
            self.timer.cancel()
        def fail(s):
            s["runtime"].update(status="error", active_turn=None, approvals=[],
                                 error="Codex disconnected. Reconnect to recover the transcript and continue.",
                                 voice={"status": "error", "error": "Voice connection ended with Codex."})
            for event in s["events"]:
                if event.get("status") == "inProgress":
                    event["status"] = "interrupted"
        self.store.update(self.sid, fail, {"method": "runtime/disconnected"})

    def _item(self, item, turn_id, completed=False, delta=None, output=False):
        kind, iid = item.get("type"), item.get("id")
        if not iid:
            return
        if kind == "userMessage" and any(c.get("type") == "text" and
                c.get("text", "").lstrip().startswith("<realtime_delegation>")
                for c in item.get("content", [])):
            # Native voice delegates to the Codex agent using an internal
            # thread message. It is not a second participant utterance.
            return
        role = {"userMessage": "user", "agentMessage": "assistant", "commandExecution": "tool_call",
                "fileChange": "tool_call", "mcpToolCall": "tool_call", "webSearch": "tool_call",
                "imageView": "tool_call"}.get(kind)
        # Reasoning summaries and unknown protocol items remain in the raw feed,
        # never masquerade as messages that the participant saw.
        if role is None:
            return
        def mutate(s):
            event = next((e for e in s["events"] if e.get("meta", {}).get("runtime_item_id") == iid
                          and e["meta"].get("runtime_thread_id") == self.thread_id), None)
            if role == "user" and event is None:
                event = next((e for e in s["events"] if e["id"] == self.last_user_event), None)
                if event is None:
                    # Codex does not always stream a userMessage item. Its ID
                    # may first appear on thread/resume; attach it to the user
                    # event already saved when turn/start was accepted.
                    event = next((e for e in s["events"] if e["role"] == "user"
                                  and e.get("meta", {}).get("runtime_turn_id") == turn_id
                                  and not e.get("meta", {}).get("runtime_item_id")), None)
            if event is None:
                iso, t = stamp()
                event = {"id": "ev_" + uuid.uuid4().hex, "ts": iso, "t": t,
                         "role": role, "text": "", "tool": None, "meta": {}}
                s["events"].append(event)
            event["meta"].update(runtime_thread_id=self.thread_id, runtime_turn_id=turn_id,
                                 runtime_item_id=iid, runtime_item_type=kind)
            event["status"] = item.get("status") or ("completed" if completed else "inProgress")
            if delta is not None:
                field = "output" if output else "text"
                event[field] = event.get(field, "") + delta
            elif kind == "agentMessage":
                event["text"] = item.get("text", event["text"])
                event["meta"]["phase"] = item.get("phase")
            elif kind == "userMessage":
                content = item.get("content", [])
                event["text"] = "\n".join(c.get("text", "") for c in content if c.get("type") == "text") or event["text"]
            else:
                event["tool"] = kind
                event["text"] = item.get("command") or json.dumps(item.get("changes") or item.get("action") or
                                             item.get("arguments") or item.get("query") or kind, ensure_ascii=False)
                event["output"] = item.get("aggregatedOutput") or event.get("output", "")
                event["meta"]["item"] = item
            if completed and kind in ("commandExecution", "mcpToolCall"):
                rid = event["id"] + "_result"
                result = next((e for e in s["events"] if e["id"] == rid), None)
                text = event.get("output", "") or json.dumps(item.get("result") or item.get("error") or {}, ensure_ascii=False)
                iso, t = stamp()
                value = {"id": rid, "ts": result["ts"] if result else iso,
                         "t": result["t"] if result else t, "role": "tool_result", "text": text,
                         "tool": kind, "status": event["status"], "meta": {"call_event_id": event["id"],
                         "runtime_item_id": iid, "runtime_thread_id": self.thread_id, "runtime_turn_id": turn_id}}
                if result:
                    result.update(value)
                else:
                    s["events"].append(value)
        self.store.update(self.sid, mutate, {"method": "item/updated", "itemId": iid})

    def _voice_item(self, item, completed):
        if item.get("type") != "transcriptSegment" or item.get("role") not in ("user", "assistant") or not item.get("id"):
            return
        iid = item["id"]
        def mutate(s):
            event = next((e for e in s["events"] if e.get("meta", {}).get("realtime_item_id") == iid), None)
            if event is None:
                iso, t = stamp()
                event = {"id": "ev_" + uuid.uuid4().hex, "ts": iso, "t": t,
                         "role": item["role"], "text": "", "tool": None,
                         "status": "inProgress", "meta": {"voice": True, "realtime_item_id": iid,
                         "runtime_thread_id": self.thread_id}}
                s["events"].append(event)
            if item.get("text") is not None:
                event["text"] = item["text"]
            if completed:
                event["status"] = "completed"
        self.store.update(self.sid, mutate, {"method": "thread/realtime/item", "itemId": iid})

    def _voice_delta(self, iid, delta):
        if not iid or not delta:
            return
        def mutate(s):
            event = next((e for e in s["events"] if e.get("meta", {}).get("realtime_item_id") == iid), None)
            if event is not None:
                event["text"] += delta
        self.store.update(self.sid, mutate, {"method": "thread/realtime/item/transcript/delta", "itemId": iid})

    def notification(self, message):
        method, params = message["method"], message.get("params", {})
        # Save raw protocol evidence apart from participant-facing events. This
        # callback never handles account/login responses or credentials. SDP and
        # audio frames are deliberately excluded from the study trace.
        if method not in ("thread/realtime/sdp", "thread/realtime/outputAudio/delta"):
            self.store.publish(self.sid, message)
        if method == "thread/realtime/sdp":
            self.voice_sdp = params.get("sdp")
            if self.voice_sdp_event:
                self.voice_sdp_event.set()
            return
        if method == "thread/realtime/started":
            def started(s):
                voice = s["runtime"].setdefault("voice", {})
                if voice.get("status") == "starting":
                    voice["status"] = "connecting"
                voice.update(version=params.get("version"), session_id=params.get("realtimeSessionId"))
            self.store.update(self.sid, started, {"method": method})
            return
        if method == "thread/realtime/closed":
            self.store.update(self.sid, lambda s: s["runtime"].update(voice={
                "status": "inactive", "reason": params.get("reason")}), {"method": method})
            return
        if method == "thread/realtime/error":
            message = params.get("message", "Voice session failed")
            if "AVAS requires OpenAI-Alpha: quicksilver=v2" in message:
                message = "Native Codex voice is unavailable on this app-server setup (OpenAI requires a quicksilver v2 header)."
            self.voice_start_error = message
            if self.voice_sdp_event:
                self.voice_sdp_event.set()
            self.store.update(self.sid, lambda s: s["runtime"].update(voice={
                "status": "error", "error": self.voice_start_error}), {"method": method})
            return
        if method in ("thread/realtime/item/started", "thread/realtime/item/completed"):
            self._voice_item(params.get("item") or {}, method == "thread/realtime/item/completed")
            return
        if method == "thread/realtime/item/transcript/delta":
            self._voice_delta(params.get("itemId"), params.get("delta", ""))
            return
        if method == "thread/realtime/transcript/delta":
            def caption(s):
                voice = s["runtime"].setdefault("voice", {})
                if voice.get("live_role") != params.get("role"):
                    voice["live_caption"] = ""
                voice["live_role"] = params.get("role")
                voice["live_caption"] = (voice.get("live_caption", "") + params.get("delta", ""))[-1000:]
            self.store.update(self.sid, caption, {"method": method})
            return
        if method == "thread/realtime/transcript/done":
            self.store.update(self.sid, lambda s: s["runtime"].setdefault("voice", {}).update(
                live_caption="", last_caption=params.get("text", ""), live_role=params.get("role")), {"method": method})
            return
        if "id" in message:
            if method not in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                self.rpc.send({"id": message["id"], "error": {"code": -32601,
                               "message": "This study client does not support that request."}})
                return
            aid = "approval_" + uuid.uuid4().hex
            self.approval_ids[aid] = message["id"]
            value = {"id": aid, "method": method, "params": params}
            def approval(s):
                s["runtime"].setdefault("approvals", []).append(value)
                s["runtime"]["status"] = "awaiting_approval"
            self.store.update(self.sid, approval, {"method": "runtime/approval"})
            return
        if method == "turn/started":
            turn = params["turn"]
            def started(s):
                s["runtime"].update(status="running", active_turn=turn["id"], error=None)
            self.store.update(self.sid, started, {"method": method})
        elif method in ("item/started", "item/completed"):
            if not hasattr(self, "thread_id"):
                self.thread_id = params.get("threadId")
            self._item(params["item"], params.get("turnId"), method == "item/completed")
        elif method in ("item/agentMessage/delta", "item/commandExecution/outputDelta"):
            if not hasattr(self, "thread_id"):
                self.thread_id = params.get("threadId")
            self._item({"id": params["itemId"], "type": "commandExecution" if "outputDelta" in method else "agentMessage"},
                       params.get("turnId"), delta=params.get("delta", ""), output="outputDelta" in method)
        elif method == "serverRequest/resolved":
            resolved = [a for a, rid in self.approval_ids.items() if rid == params.get("requestId")]
            for aid in resolved:
                self.approval_ids.pop(aid, None)
            self.store.update(self.sid, lambda s: s["runtime"].update(
                approvals=[a for a in s["runtime"].get("approvals", []) if a["id"] not in resolved]), {"method": method})
        elif method == "turn/completed":
            if self.timer:
                self.timer.cancel()
            turn = params["turn"]
            status = turn.get("status", "failed")
            def finished(s):
                err = turn.get("error")
                s["runtime"].update(status="ready" if status == "completed" else status,
                    active_turn=None, approvals=[], last_turn_status=status,
                    error=(err or {}).get("message") if isinstance(err, dict) else err)
                for e in s["events"]:
                    if e.get("meta", {}).get("runtime_turn_id") == turn["id"] and e.get("status") == "inProgress":
                        e["status"] = status
            self.approval_ids.clear()
            self.store.update(self.sid, finished, {"method": method})
        elif method == "error":
            # Codex may retry internally. Report the failure without launching an
            # extra generation or pretending a failed turn was completed.
            error = params.get("error") or {}
            self.store.update(self.sid, lambda s: s["runtime"].update(error=error.get("message", "Codex error")),
                              {"method": method})

    def start_voice(self, sdp):
        if not isinstance(sdp, str) or len(sdp) > 200_000 or not sdp.startswith("v=0"):
            raise ValueError("A valid WebRTC offer is required")
        with self.lock:
            session = self.store.load(self.sid)
            if session.get("interaction_mode") != "voice" or session["phase"] != "chat":
                raise RuntimeUnavailable("Voice is not enabled for this conversation")
            if session["runtime"].get("active_turn") or session["runtime"].get("status") != "ready":
                raise RuntimeUnavailable("Wait for Codex to finish its current turn")
            if session["runtime"].get("voice", {}).get("status") in ("starting", "connecting", "active", "stopping"):
                raise RuntimeUnavailable("Voice chat is already running")
            self.voice_sdp, self.voice_sdp_event, self.voice_start_error = None, threading.Event(), None
            self.store.update(self.sid, lambda s: s["runtime"].update(voice={"status": "starting"}),
                              {"method": "thread/realtime/start_requested"})
            try:
                # This is Codex's own thread-scoped voice path. Pin the tested
                # v3 protocol: this host's app-server default selected an AVAS
                # path that the provider rejected. The model, voice, startup
                # context, and automatic handoffs remain Codex-managed.
                params = {"threadId": self.thread_id, "outputModality": "audio",
                          "transport": {"type": "webrtc", "sdp": sdp}, "version": "v3"}
                self.rpc.request("thread/realtime/start", params, timeout=45)
                if not self.voice_sdp_event.wait(25):
                    raise RuntimeUnavailable("Codex did not return a voice connection answer")
                if self.voice_start_error:
                    raise RuntimeUnavailable(self.voice_start_error)
                if not self.voice_sdp:
                    raise RuntimeUnavailable("Codex did not return a voice connection answer")
                answer = self.voice_sdp
                self.store.update(self.sid, lambda s: s["runtime"].setdefault("voice", {}).update(status="active"),
                                  {"method": "thread/realtime/connected"})
                return answer
            except Exception as exc:
                try:
                    self.rpc.request("thread/realtime/stop", {"threadId": self.thread_id}, timeout=10)
                except Exception:
                    pass
                self.store.update(self.sid, lambda s: s["runtime"].update(voice={"status": "error", "error": str(exc)}),
                                  {"method": "thread/realtime/start_failed"})
                raise
            finally:
                self.voice_sdp_event = None
                self.voice_sdp = None
                self.voice_start_error = None

    def stop_voice(self):
        with self.lock:
            status = self.store.load(self.sid)["runtime"].get("voice", {}).get("status")
            if status not in ("starting", "connecting", "active", "stopping"):
                return
            self.store.update(self.sid, lambda s: s["runtime"].setdefault("voice", {}).update(status="stopping"),
                              {"method": "thread/realtime/stop_requested"})
            try:
                self.rpc.request("thread/realtime/stop", {"threadId": self.thread_id}, timeout=15)
            except Exception as exc:
                self.store.update(self.sid, lambda s: s["runtime"].update(voice={"status": "error", "error": str(exc)}),
                                  {"method": "thread/realtime/stop_failed"})
                raise
            else:
                self.store.update(self.sid, lambda s: s["runtime"].update(voice={"status": "inactive"}),
                                  {"method": "thread/realtime/stopped"})

    def message(self, text, client_id, compose_ms=None):
        with self.lock:
            session = self.store.load(self.sid)
            if session["runtime"].get("voice", {}).get("status") in ("starting", "connecting", "active", "stopping"):
                raise RuntimeUnavailable("End voice chat before sending a typed message.")
            previous = next((e for e in session["events"] if e.get("meta", {}).get("client_message_id") == client_id), None)
            if previous:
                return previous
            if session["runtime"].get("active_turn") or session["runtime"].get("status") in ("starting", "running", "awaiting_approval"):
                raise RuntimeUnavailable("The agent is working. Stop it or wait before sending another message.")
            if session["phase"] != "chat":
                raise RuntimeUnavailable("Conversation phase is over.")
            iso, t = stamp()
            event = {"id": "ev_" + uuid.uuid4().hex, "ts": iso, "t": t, "role": "user", "text": text,
                     "tool": None, "status": "submitted", "meta": {"client_message_id": client_id, "draft_ms": compose_ms}}
            def submit(s):
                s["events"].append(event)
                s["runtime"].update(status="starting", error=None)
            self.store.update(self.sid, submit, {"method": "turn/submitted"})
            self.last_user_event = event["id"]
            self.timer = threading.Timer(self.manager.turn_timeout, self._timeout)
            self.timer.daemon = True
            self.timer.start()
            try:
                result = self.rpc.request("turn/start", {"threadId": self.thread_id,
                    "input": [{"type": "text", "text": text}], "cwd": str(self.workspace),
                    "approvalPolicy": APPROVAL_POLICY, "effort": session["runtime"].get("effort", "medium")})
                # turn/started and even turn/completed can precede this response.
                turn_id = result["turn"]["id"]
                def accepted(s):
                    e = next(e for e in s["events"] if e["id"] == event["id"])
                    e["meta"].update(runtime_thread_id=self.thread_id, runtime_turn_id=turn_id)
                    if s["runtime"]["status"] == "starting":
                        s["runtime"].update(status="running", active_turn=turn_id)
                self.store.update(self.sid, accepted, {"method": "turn/accepted"})
            except Exception as exc:
                self.timer.cancel()
                if getattr(self.rpc, "closed", False):
                    self.connected = False
                self.store.update(self.sid, lambda s: s["runtime"].update(status="error", error=str(exc), active_turn=None),
                                  {"method": "turn/start_failed"})
                raise
            return event

    def approve(self, aid, decision):
        if decision not in ("accept", "acceptForSession", "decline", "cancel"):
            raise ValueError("Unsupported approval decision")
        with self.lock:
            rid = self.approval_ids.get(aid)
            if rid is None:
                raise RuntimeUnavailable("Approval expired. Reload the current session.")
            self.rpc.send({"id": rid, "result": {"decision": decision}})
            self.approval_ids.pop(aid, None)
            def decided(s):
                s["runtime"]["approvals"] = [a for a in s["runtime"].get("approvals", []) if a["id"] != aid]
                s["runtime"]["status"] = "awaiting_approval" if s["runtime"]["approvals"] else "running"
                s["runtime"].setdefault("approval_history", []).append({"id": aid, "decision": decision, "t": time.time()})
            self.store.update(self.sid, decided, {"method": "runtime/approval_decided", "id": aid, "decision": decision})

    def interrupt(self):
        runtime = self.store.load(self.sid)["runtime"]
        if runtime.get("active_turn"):
            self.rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": runtime["active_turn"]})

    def _timeout(self):
        try:
            self.interrupt()
            self.store.update(self.sid, lambda s: s["runtime"].update(error="Turn exceeded the local runtime limit and was stopped."),
                              {"method": "runtime/timeout"})
        except Exception:
            self.rpc.close()
            self.disconnected()

    def close(self):
        self.connected = False
        if self.timer:
            self.timer.cancel()
        if self.rpc:
            self.rpc.close()
        self.disconnected()


class RuntimeManager:
    def __init__(self, store, data_dir, rpc_factory=RpcClient, command=None):
        self.store, self.data_dir = store, Path(data_dir).resolve()
        self.rpc_factory, self.command_override = rpc_factory, command
        self.workers, self.lock = {}, threading.RLock()
        self.turn_timeout = int(os.environ.get("SPARK_TURN_TIMEOUT", "600"))
        self.binary = os.environ.get("SPARK_CODEX_BIN") or shutil.which("codex")
        if self.binary is None:
            bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
            self.binary = str(bundled) if bundled.is_file() else None
        self.version = None
        if self.binary:
            try:
                self.version = subprocess.check_output([self.binary, "--version"], text=True, timeout=5).strip()
            except (OSError, subprocess.SubprocessError):
                self.binary = None

    def status(self):
        return {"kind": "codex", "installed": bool(self.binary), "version": self.version,
                "sandbox": "permission-profile:workspace",
                "model": os.environ.get("SPARK_CODEX_MODEL") or None,
                "local_only": True, "note": "Local prototype. Hosting and multi-user credential isolation are not implemented."}

    def workspace(self, sid):
        if not __import__("re").fullmatch(r"s_[0-9a-f]{8}", sid):
            raise KeyError(sid)
        path = self.data_dir / "workspaces" / sid
        if path.exists() and (path.is_symlink() or not path.resolve().is_relative_to(self.data_dir / "workspaces")):
            raise ValueError("Invalid workspace")
        return path

    def command(self, workspace):
        if self.command_override:
            return self.command_override
        if not self.binary:
            raise RuntimeUnavailable("Codex is not installed on the execution host.")
        return app_server_command(self.binary)

    def get(self, sid):
        with self.lock:
            session = self.store.load(sid)
            if session.get("runtime", {}).get("kind") != "codex":
                raise ValueError("This session does not use Codex")
            worker = self.workers.get(sid)
            if worker and worker.connected:
                return worker
            if worker:
                worker.close()
            try:
                worker = Worker(self, sid)
            except Exception as exc:
                self.store.update(sid, lambda s: s["runtime"].update(status="error", error=str(exc)),
                                  {"method": "runtime/connect_failed"})
                raise
            self.workers[sid] = worker
            return worker

    def files(self, sid):
        root = self.workspace(sid)
        if not root.exists():
            return []
        return [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size}
                for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()
                and p.resolve().is_relative_to(root.resolve())][:200]

    def file(self, sid, relative):
        root = self.workspace(sid).resolve()
        path = (root / relative).resolve()
        if not relative or not path.is_relative_to(root) or not path.is_file():
            raise KeyError(relative)
        return path

    def upload(self, sid, name, data):
        if not name or name != Path(name).name or name in (".", "..", "TASK.md") or any(c in name for c in ("\\", "\r", "\n", "\x00")):
            raise ValueError("Use a plain filename; TASK.md is reserved for the task brief.")
        if len(data) > 5 * 1024 * 1024:
            raise ValueError("Maximum workspace upload is 5 MB")
        self.store.load(sid)
        root = self.workspace(sid)
        root.mkdir(parents=True, exist_ok=True)
        # Never overwrite existing task artifacts or follow a symlink.
        with (root / name).open("xb") as f:
            f.write(data)
        return {"path": name, "bytes": len(data)}

    def close(self):
        for worker in list(self.workers.values()):
            worker.close()
