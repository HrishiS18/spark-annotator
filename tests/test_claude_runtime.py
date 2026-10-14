import json
import os
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_runtime import ClaudeRuntimeManager, claude_privacy_command
from claude_sdk_worker import allowed_path, allowed_tool
from codex_runtime import RuntimeUnavailable
from storage import Store
from tests.test_runtime import runtime_session


class ClaudeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/"study.sqlite3")
        self.sid="s_12345678"
        s=runtime_session(); s["runtime"].update(kind="claude",model="sonnet")
        self.store.save(s)
        self.manager=ClaudeRuntimeManager(self.store,self.root,command=[
            sys.executable,str(Path(__file__).with_name("mock_claude_worker.py"))])

    def tearDown(self):
        self.manager.close(); self.tmp.cleanup()

    def wait(self,predicate):
        for _ in range(200):
            s=self.store.load(self.sid)
            if predicate(s): return s
            time.sleep(.02)
        self.fail("Claude state timed out")

    def turn(self,text="Synthetic task",cid="test-message-0001"):
        w=self.manager.get(self.sid)
        self.manager.upload(self.sid,"input.txt",b"Synthetic task file")
        user=w.message(text,cid)
        s=self.wait(lambda s:bool(s["runtime"]["approvals"]))
        return w,user,s

    def test_streaming_tools_approval_idempotency_and_resume(self):
        w,user,s=self.turn()
        assistant=next(e for e in s["events"] if e["role"]=="assistant")
        self.assertIn("inspect",assistant["text"])
        self.assertEqual(w.message("Synthetic task","test-message-0001")["id"],user["id"])
        aid=s["runtime"]["approvals"][0]["id"]
        w.approve(aid,"accept")
        final=self.wait(lambda s:s["runtime"].get("last_turn_status")=="completed")
        self.assertEqual(final["runtime"]["model"],"TEST ONLY Claude SDK")
        self.assertTrue(any(e["role"]=="tool_result" for e in final["events"]))
        self.assertTrue((w.workspace/"summary.txt").exists())
        ids=[e["id"] for e in final["events"]]
        w.close()
        resumed=self.manager.get(self.sid)
        restored=self.store.load(self.sid)
        self.assertEqual([e["id"] for e in restored["events"]],ids)
        self.assertEqual(restored["runtime"]["thread_id"],final["runtime"]["thread_id"])
        self.assertEqual(restored["runtime"]["status"],"ready")
        self.assertNotIn(".runtime",{f["path"] for f in self.manager.files(self.sid)})
        with self.assertRaises(KeyError): self.manager.file(self.sid,".runtime/claude/fake-history.json")

    def test_questions_logged_as_participant_input(self):
        w,_,s=self.turn("[ASK] Make a file")
        a=s["runtime"]["approvals"][0]
        self.assertEqual(a["method"],"claude/question")
        with self.assertRaises(ValueError): w.approve(a["id"],"accept",{})
        w.approve(a["id"],"accept",{"Which format should I use?":"Text, because I need prose"})
        final=self.wait(lambda s:s["runtime"].get("last_turn_status")=="completed")
        answers=[e for e in final["events"] if e.get("meta",{}).get("source")=="agent_question_answer"]
        self.assertEqual(len(answers),1)
        self.assertIn("because I need prose",answers[0]["text"])
        answer_index = final["events"].index(answers[0])
        result_index = next(i for i,e in enumerate(final["events"]) if e["role"] == "tool_result")
        self.assertLess(answer_index, result_index)
        self.assertEqual(answers[0]["meta"]["delivery_status"], "delivered")
        with self.assertRaises(RuntimeUnavailable): w.approve(a["id"],"accept",{"Which format should I use?":"CSV"})

    def test_interrupt_pending_permission_does_not_execute(self):
        w,_,s=self.turn()
        w.interrupt()
        stopped=self.wait(lambda s:s["runtime"].get("last_turn_status")=="interrupted")
        self.assertEqual(stopped["runtime"]["approvals"],[])
        self.assertFalse((w.workspace/"summary.txt").exists())

    def test_declining_does_not_create_artifact(self):
        w,_,s=self.turn()
        w.approve(s["runtime"]["approvals"][0]["id"],"decline")
        self.wait(lambda s:s["runtime"].get("last_turn_status")=="completed")
        self.assertFalse((w.workspace/"summary.txt").exists())

    def test_credentials_and_private_baseline_not_sent_to_agent_context_or_records(self):
        with patch.dict(os.environ,{"ANTHROPIC_API_KEY":"TEST_KEY_SENTINEL","OPENAI_API_KEY":"OTHER_KEY_SENTINEL",
                                    "CLAUDE_CODE_OAUTH_TOKEN":"SUBSCRIPTION_SENTINEL"}):
            env=self.manager.environment()
            self.assertEqual(env["ANTHROPIC_API_KEY"],"TEST_KEY_SENTINEL")
            self.assertNotIn("OPENAI_API_KEY",env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN",env)
            w,_,s=self.turn()
            w.approve(s["runtime"]["approvals"][0]["id"],"accept")
            self.wait(lambda s:s["runtime"].get("last_turn_status")=="completed")
        export=json.dumps(self.store.export(self.sid))
        for key in ("TEST_KEY_SENTINEL","OTHER_KEY_SENTINEL","SUBSCRIPTION_SENTINEL"):
            self.assertNotIn(key,export)
        context=(w.workspace/"TASK.md").read_text()
        self.assertNotIn("PRIVATE BASELINE",context)

    def test_missing_key_is_explicit_error_not_subscription_fallback(self):
        manager=ClaudeRuntimeManager(self.store,self.root)
        with patch.dict(os.environ,{"ANTHROPIC_API_KEY":""}):
            self.assertFalse(manager.status()["configured"])
            with self.assertRaises(RuntimeUnavailable): manager.get(self.sid)
        manager.close()

    def test_setup_message_distinguishes_missing_sdk_and_missing_key(self):
        manager=ClaudeRuntimeManager(self.store,self.root)
        with patch.dict(os.environ,{"ANTHROPIC_API_KEY":""}):
            status=manager.status()
            self.assertTrue(status["installed"])
            self.assertIn("Claude is installed",status["reason"])
            self.assertIn("ANTHROPIC_API_KEY",status["reason"])
            self.assertNotIn("Install claude-agent-sdk",status["reason"])
        with patch.dict(os.environ,{"ANTHROPIC_API_KEY":"TEST_KEY_SENTINEL"}):
            self.assertIsNone(manager.status()["reason"])
            self.assertTrue(manager.status()["configured"])
            manager.binary=None
            self.assertIn("not installed",manager.status()["reason"])
            self.assertFalse(manager.status()["configured"])
        manager.close()

    def test_path_policy_blocks_private_and_symlink_targets(self):
        root=self.manager.workspace(self.sid); root.mkdir(parents=True)
        (root/"escape").symlink_to(self.root/"study.sqlite3")
        self.assertTrue(allowed_path(root,"TASK.md"))
        self.assertFalse(allowed_path(root,"../study.sqlite3"))
        self.assertFalse(allowed_path(root,"escape"))
        self.assertFalse(allowed_path(root,".runtime/claude/history"))

    def test_tool_policy_blocks_escape_and_sandbox_bypass_not_grep_regex(self):
        root=self.manager.workspace(self.sid).resolve()
        self.assertFalse(allowed_tool(root,"Read",{"file_path":"../study.sqlite3"}))
        self.assertFalse(allowed_tool(root,"Glob",{"pattern":"/etc/*"}))
        self.assertFalse(allowed_tool(root,"Glob",{"pattern":"../*"}))
        self.assertFalse(allowed_tool(root,"Glob",{"pattern":".runtime/**/*"}))
        self.assertFalse(allowed_tool(root,"Bash",{"command":"pwd","dangerouslyDisableSandbox":True}))
        self.assertTrue(allowed_tool(root,"Glob",{"pattern":"**/*.txt"}))
        self.assertTrue(allowed_tool(root,"Grep",{"pattern":"..","path":"."}))

    def test_reconnecting_unfinished_tool_preserves_ids_without_execution(self):
        w,_,s=self.turn()
        ids=[e["id"] for e in s["events"]]
        w.close()
        self.manager.get(self.sid)
        restored=self.store.load(self.sid)
        self.assertEqual([e["id"] for e in restored["events"]],ids)
        tool=next(e for e in restored["events"] if e["role"]=="tool_call")
        self.assertEqual(tool["status"],"interrupted")
        self.assertFalse((w.workspace/"summary.txt").exists())
        self.assertEqual(restored["runtime"]["approvals"],[])

    @unittest.skipUnless(Path('/usr/bin/sandbox-exec').exists(), "macOS privacy fence")
    def test_process_tree_fence_blocks_records_peers_and_host_writes(self):
        workspace=self.manager.workspace(self.sid); workspace.mkdir(parents=True)
        peer=self.root/"workspaces"/"s_87654321"; peer.mkdir()
        (peer/"private.txt").write_text("PRIVATE PEER")
        # Pick a path outside the explicitly writable OS temp directory.
        host_target=Path.home()/f"spark-claude-fence-test-{os.getpid()}.txt"
        script="""import pathlib,sys
work,record,peer,host=map(pathlib.Path,sys.argv[1:])
(work/'safe.txt').write_text('allowed')
for path in (record,peer):
    try: path.read_bytes()
    except PermissionError: pass
    else: raise AssertionError('Private study path was readable')
try: host.write_text('must be denied')
except PermissionError: pass
else: raise AssertionError('Host writes were allowed')
"""
        result=subprocess.run(claude_privacy_command(self.root,workspace)+[
            sys.executable,"-c",script,str(workspace),str(self.root/"study.sqlite3"),str(peer/"private.txt"),str(host_target)],
            text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((workspace/"safe.txt").read_text(),"allowed")
        self.assertFalse(host_target.exists())
