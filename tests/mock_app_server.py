"""Test-only stdio fixture; it never calls a model or runs the proposed command."""

import json
import sys
import threading
import time
from pathlib import Path

lock = threading.Lock()
approved, interrupted = threading.Event(), threading.Event()
decision = None


def send(value):
    with lock:
        print(json.dumps(value), flush=True)


def notify(method, **params):
    send({"method": method, "params": params})


def item(method, value):
    notify(method, threadId="thr_mock", turnId="turn_mock", item=value)


def turn(text):
    global decision
    approved.clear(); interrupted.clear(); decision = None
    notify("turn/started", threadId="thr_mock", turn={"id": "turn_mock", "status": "inProgress"})
    item("item/completed", {"id": "user_mock", "type": "userMessage", "content": [{"type": "text", "text": text}]})
    item("item/started", {"id": "intro_mock", "type": "agentMessage", "text": ""})
    for chunk in ["[TEST MOCK] ", "I will inspect the ", "task workspace first."]:
        notify("item/agentMessage/delta", threadId="thr_mock", turnId="turn_mock", itemId="intro_mock", delta=chunk)
        time.sleep(.05)
    item("item/completed", {"id": "intro_mock", "type": "agentMessage", "text": "[TEST MOCK] I will inspect the task workspace first."})
    command = {"id": "cmd_mock", "type": "commandExecution", "command": "[TEST ONLY] produce summary.txt", "status": "inProgress"}
    item("item/started", command)
    send({"id": "approval_mock", "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "thr_mock", "turnId": "turn_mock", "itemId": "cmd_mock", "command": command["command"],
        "reason": "Test approval flow", "availableDecisions": ["accept", "acceptForSession", "decline"]}})
    approved.wait(30)
    if interrupted.is_set():
        return
    if decision != "accept":
        item("item/completed", dict(command, status="declined", aggregatedOutput="Declined"))
        notify("turn/completed", threadId="thr_mock", turn={"id": "turn_mock", "status": "completed"})
        return
    Path("summary.txt").write_text("Test-only artifact. No model was called.\n", encoding="utf-8")
    item("item/completed", dict(command, status="completed", aggregatedOutput="summary.txt saved", exitCode=0))
    item("item/completed", {"id": "final_mock", "type": "agentMessage", "text": "[TEST MOCK] Your test artifact is ready."})
    notify("turn/completed", threadId="thr_mock", turn={"id": "turn_mock", "status": "completed"})


for line in sys.stdin:
    message = json.loads(line)
    if "method" not in message:
        decision = message.get("result", {}).get("decision")
        approved.set()
        continue
    method, rid = message["method"], message.get("id")
    params = message.get("params", {})
    if rid is None:
        continue
    result = {}
    if method == "account/read":
        result = {"account": {"type": "mock"}}
    elif method in ("thread/start", "thread/resume"):
        result = {"thread": {"id": "thr_mock", "turns": []}, "model": "mock-model", "modelProvider": "test"}
    elif method == "turn/start":
        result = {"turn": {"id": "turn_mock", "status": "inProgress"}}
    send({"id": rid, "result": result})
    if method == "turn/start":
        threading.Thread(target=turn, args=(params["input"][0]["text"],), daemon=True).start()
    elif method == "turn/interrupt":
        interrupted.set(); approved.set()
        notify("turn/completed", threadId="thr_mock", turn={"id": "turn_mock", "status": "interrupted"})
    elif method == "thread/realtime/start":
        notify("thread/realtime/started", threadId="thr_mock", version="v3", realtimeSessionId="voice_mock")
        notify("thread/realtime/sdp", threadId="thr_mock", sdp="v=0\r\nmock-answer")
        notify("thread/realtime/item/completed", threadId="thr_mock", item={
            "id": "voice_mock_assistant", "realtimeSessionId": "voice_mock", "type": "transcriptSegment",
            "role": "assistant", "text": "[TEST MOCK] Voice transcript."})
    elif method == "thread/realtime/stop":
        notify("thread/realtime/closed", threadId="thr_mock", reason="ended")
