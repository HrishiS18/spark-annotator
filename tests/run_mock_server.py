"""Browser smoke-test server. Uses temporary data and an explicitly fake runtime."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from codex_runtime import RuntimeManager

manager = RuntimeManager(server.get_store(), server.DATA,
                         command=[sys.executable, str(Path(__file__).with_name("mock_app_server.py"))])
manager.version = "mock-app-server (TEST ONLY)"
server._RUNTIME = manager
server.main()
