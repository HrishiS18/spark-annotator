"""Run the LLM judge from the command line (e.g. over a whole study after data collection).

    # Default: uses existing Codex login. Optional SPARK_JUDGE_PROVIDER=anthropic.
    .venv/bin/python tools/run_judge.py --session s_1a2b3c4d
    .venv/bin/python tools/run_judge.py --all              # every session with a transcript
    .venv/bin/python tools/run_judge.py --all --dry-run    # token estimate, no inference

Results are saved into each session exactly as if run from the dashboard.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import judge  # noqa: E402
import server  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--session")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-done", action="store_true", help="skip sessions already judged")
    a = ap.parse_args()

    sessions = [server.load(a.session)] if a.session else [s for s in server.all_sessions() if s["events"] and s["phase"] not in ("pre","chat")]
    if a.skip_done:
        sessions = [s for s in sessions if (s.get("judge") or {}).get("status") != "done"]
    for s in sessions:
        est = judge.estimate(s)
        print(f"{s['id']} ~{est['input_tokens']:>7,} tokens · {est['provider']} / {est['model']}")
    print(f"{len(sessions)} session(s). Provider/account usage applies; no fabricated price estimate.")
    if a.dry_run or not sessions:
        return
    if not judge.status()["live"]:
        sys.exit(f"LLM judge unavailable: {judge.status()['reason']}")
    for s in sessions:
        server.run_judge(s["id"])
        j = server.load(s["id"]).get("judge") or {}
        if j.get("status") == "done":
            print(f"{s['id']}: {j['n']} accepted candidates, {j.get('unverified_quotes', 0)} rejected predictions")
        else:
            print(f"{s['id']}: FAILED: {j.get('error')}")


if __name__ == "__main__":
    main()
