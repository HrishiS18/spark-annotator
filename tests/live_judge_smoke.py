"""Opt-in real inference on synthetic data ONLY; consumes Codex account usage."""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import judge
from tests.test_judge import session

if "--run" not in sys.argv:
    sys.exit("Pass --run to explicitly run synthetic LLM-judge inference.")
with tempfile.TemporaryDirectory(prefix="spark-judge-smoke-") as data, patch.dict("os.environ",{"SPARK_DATA_DIR":data}):
    found, meta = judge.run(session())
    assert meta["provider"] == "codex"
    assert meta["input_tokens"] > 0
    assert all(c["quotes_verified"] for c in found)
    print(json.dumps({k:meta[k] for k in ["provider","model","runtime_version","input_tokens","output_tokens","scope"]} |
                     {"passed":True,"accepted_candidates":len(found),"rejected_candidates":len(meta["rejected_candidates"])}))
