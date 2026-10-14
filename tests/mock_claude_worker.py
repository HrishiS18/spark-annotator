"""Run the actual SDK bridge against a synthetic client, never a live model."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import claude_sdk_worker
from tests.fake_claude_sdk import FakeClient,history
claude_sdk_worker.get_session_messages=history
asyncio.run(claude_sdk_worker.main(client_factory=FakeClient))
