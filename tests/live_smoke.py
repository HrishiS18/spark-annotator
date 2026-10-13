"""Explicitly opt-in, small real Codex test. Uses only synthetic temporary data.

    .venv/bin/python tests/live_smoke.py --run-live

Unlike the unit/browser tests, this performs inference using the host's configured
Codex account. It does not inspect or alter real participant sessions.
"""

import argparse
import json
import shlex
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from storage import Store
from codex_runtime import RuntimeManager


def allowed(approval, workspace):
    params = approval["params"]
    if approval["method"] == "item/fileChange/requestApproval":
        return params.get("grantRoot") in (None, str(workspace))
    command = params.get("command", "")
    words = shlex.split(command)
    if len(words) == 3 and words[0] in ("/bin/zsh", "/bin/bash", "/bin/sh") and words[1] in ("-lc", "-c"):
        command = words[2]
    return command.strip() in ("cat TASK.md", "pwd", "ls", "ls -la", "cat smoke.txt")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args()
    if not args.run_live:
        parser.error("This invokes real inference; explicitly pass --run-live.")
    with tempfile.TemporaryDirectory(prefix="spark-live-smoke-") as root:
        server._STORE = Store(Path(root) / "study.sqlite3", write_lock=server.LOCK)
        manager = RuntimeManager(server._STORE, root)
        try:
            s = server.create_session({"study": "dilemma", "participant_id": "SYNTHETIC-LIVE-TEST",
                                       "task": "Development smoke test. Create a text artifact in this workspace."})
            s["phase"] = "chat"; server.save(s)
            worker = manager.get(s["id"])
            worker.message("Run exactly `cat TASK.md` to read the task. Then use the file-editing tool to create smoke.txt "
                           "containing exactly 'hello spark' and a newline. Do not use network access or read anything outside "
                           "this workspace. Give a one-sentence final reply.", "live-smoke-0001")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                saved = server.load(s["id"])
                runtime = saved["runtime"]
                for approval in runtime.get("approvals", []):
                    decision = "accept" if allowed(approval, worker.workspace) else "decline"
                    worker.approve(approval["id"], decision)
                    print(json.dumps({"approval": approval["method"], "decision": decision}), flush=True)
                if runtime.get("last_turn_status") or runtime.get("status") == "error":
                    break
                time.sleep(.2)
            else:
                worker.interrupt()
                raise RuntimeError("Live test did not finish within 90 seconds")
            saved = server.load(s["id"])
            artifact = worker.workspace / "smoke.txt"
            if saved["runtime"].get("last_turn_status") != "completed" or not artifact.is_file() or artifact.read_text() != "hello spark\n":
                # Only synthetic error details are printed, never credentials.
                raise RuntimeError("Live smoke failed: " + str(saved["runtime"].get("error")) +
                                   "; roles=" + str([e["role"] for e in saved["events"]]))
            print(json.dumps({"passed": True, "model": saved["runtime"]["model"],
                              "roles": [e["role"] for e in saved["events"]], "artifact": artifact.name,
                              "stream_notifications": len(server._STORE.feed(s["id"], limit=10000))}), flush=True)
            before_ids = [e["id"] for e in saved["events"]]
            manager.close()
            manager.get(s["id"])
            recovered = server.load(s["id"])
            if [e["id"] for e in recovered["events"]] != before_ids:
                raise RuntimeError("Resume changed event identities or duplicated transcript items")
            print(json.dumps({"resume_passed": True, "events": len(before_ids)}), flush=True)
        finally:
            manager.close()


if __name__ == "__main__":
    main()
