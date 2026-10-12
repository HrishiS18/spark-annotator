"""Run the LLM judge from the command line (e.g. over a whole study after data collection).

    export ANTHROPIC_API_KEY=...
    .venv/bin/python tools/run_judge.py --session s_1a2b3c4d
    .venv/bin/python tools/run_judge.py --all              # every session with a transcript
    .venv/bin/python tools/run_judge.py --all --dry-run    # just show the cost estimate
    .venv/bin/python tools/run_judge.py --all --hints      # let the judge see codebook hints

Results are saved into each session exactly as if run from the dashboard.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import judge  # noqa: E402
import llm  # noqa: E402
import server  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--session")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--hints", action="store_true", help="show the judge the codebook parser's hits")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-done", action="store_true", help="skip sessions already judged")
    a = ap.parse_args()

    sessions = [server.load(a.session)] if a.session else [s for s in server.all_sessions() if s["events"]]
    if a.skip_done:
        sessions = [s for s in sessions if (s.get("judge") or {}).get("status") != "done"]
    total = 0.0
    for s in sessions:
        est = judge.estimate(s)
        total += est["approx_cost_usd"]
        print(f"{s['id']} {s['participant_id']:<12} ~{est['input_tokens']:>7,} tokens  ~${est['approx_cost_usd']}")
    print(f"{len(sessions)} session(s), roughly ${total:.2f} with {llm.JUDGE_MODEL}")
    if a.dry_run or not sessions:
        return
    if not llm.status()["live"]:
        sys.exit(f"LLM judge unavailable: {llm.status()['reason']}")
    for s in sessions:
        server.run_judge(s["id"], hints=a.hints)
        j = server.load(s["id"]).get("judge") or {}
        if j.get("status") == "done":
            print(f"{s['id']}: {j['n']} candidates, {j.get('unverified_quotes', 0)} with unverified quotes, ${j.get('cost_usd')}")
        else:
            print(f"{s['id']}: FAILED: {j.get('error')}")


if __name__ == "__main__":
    main()
