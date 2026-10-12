"""LLM judge: Claude reads the whole parsed, timestamped conversation and decides sparks.

It is a second, independent coder next to the deterministic codebook parser:

  * input   the timestamped transcript from transcript.render_for_judge (participant turns,
            agent replies, tool calls/results with errors marked), the task, and the
            participant's written starting position when there is one
  * blind   it never sees the participant's flags; by default it also never sees the
            codebook parser's output (hints=True opts in), so agreement is meaningful
  * output  candidate sparks with trigger + uptake event ids, type, mechanism
            (spark / persuasion / copying / unclear), confidence, verbatim quotes and a
            rationale, plus a short summary of how the participant's thinking evolved
  * checks  ids must exist; quotes are verified against the cited events
            (`quotes_verified`), so invented evidence is visible

The whole session is judged in one call when it fits (Claude reads the full trajectory,
which matters because sparks are often only visible turns later). Very long sessions are
split into overlapping windows.
"""

import json
import os
import re

import llm
import transcript

ROOT = os.path.dirname(os.path.abspath(__file__))
TAXONOMY = json.load(open(os.path.join(ROOT, "config", "taxonomy.json")))
TYPE_KEYS = [t["key"] for t in TAXONOMY["types"]]
MAX_TOKENS_PER_CALL = int(os.environ.get("SPARK_JUDGE_MAX_INPUT_TOKENS", "350000"))
CHARS_PER_TOKEN = 3.5
OVERLAP_EXCHANGES = 2

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "trigger_event_id": {"type": "string"},
                    "uptake_event_id": {"type": "string"},
                    "type": {"type": "string", "enum": TYPE_KEYS},
                    "secondary_type": {"type": "string", "enum": TYPE_KEYS + ["none"]},
                    "mechanism": {"type": "string", "enum": ["spark", "persuasion", "copying", "unclear"]},
                    "confidence": {"type": "number"},
                    "trigger_quote": {"type": "string"},
                    "uptake_quote": {"type": "string"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["trigger_event_id", "uptake_event_id", "type", "secondary_type", "mechanism",
                             "confidence", "trigger_quote", "uptake_quote", "before", "after", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "candidates"],
    "additionalProperties": False,
}


def _taxonomy_block():
    rows = []
    for t in TAXONOMY["types"]:
        rows.append(f"- {t['key']} ({t['label']}): {t['definition']}\n  Cues: {t['cues']}\n"
                    f"  Example (dilemma): {t['example_dilemma']}\n  Example (agentic): {t['example_agentic']}")
    return "\n".join(rows)


SYSTEM = f"""You are an expert qualitative coder in a research study on "sparks" in human–AI agent interaction.

A SPARK is something in the interaction trajectory that causes a shift or reframing in the PARTICIPANT's own thinking: their mental model, framing, context, or the end goal of their task. The defining feature is uptake: the new idea enters the participant's own reasoning. Interesting agent output that the participant does not take up is not a spark.

Spark types:
{_taxonomy_block()}

Mechanism of a shift (code every shift you report):
- spark: the participant integrates the idea into their OWN reasoning (elaborates, adapts, connects, argues from it, changes their goal).
- persuasion: the participant adopts the agent's recommendation or conclusion without adding reasoning of their own.
- copying: the participant reproduces the agent's wording or output nearly verbatim.
- unclear: a shift happened but the evidence does not separate these.
Report persuasion and copying moments too; researchers need them as contrast cases.

NOT sparks: routine progress (the agent does what was asked and the participant moves on), the participant correcting the agent without any change in their own thinking, politeness or agreement without a change in position, and the participant simply restating their original plan.

How to work:
1. Read the WHOLE transcript first. Track the participant's goal, framing and knowledge over time; a spark often only becomes visible several turns after its trigger.
2. For each candidate, identify the trigger event (an agent message, an agent tool call, a tool result or error, or, for reflective sparks, the agent question or the participant's own articulation) and the participant event that shows uptake.
3. Choose the single best type, plus a secondary type if a second one clearly applies.
4. Be selective and evidence-based. Many sessions contain only a handful of sparks; zero is a valid answer. Do not pad.

Field rules:
- trigger_event_id / uptake_event_id: ids exactly as written in the transcript (e.g. "e12"). uptake_event_id must be a PARTICIPANT event; use "" only if the uptake is inside the trigger itself.
- trigger_quote / uptake_quote: short VERBATIM excerpts (under 25 words) copied from those events. They are checked automatically.
- before / after: one sentence each on the participant's thinking before and after.
- confidence: probability (0 to 1) that a careful human coder would agree this is a genuine shift of this type and mechanism.
- rationale: 1–3 sentences citing the evidence.
- summary: 2–4 sentences on how the participant's thinking and goal evolved across the whole session."""


def _context(s):
    parts = [f"TASK THE PARTICIPANT WAS WORKING ON:\n{s['prompt']['text']}"]
    pre = ((s.get("writing") or {}).get("pre") or {}).get("text")
    if pre:
        parts.append(f"PARTICIPANT'S WRITTEN POSITION BEFORE THE CONVERSATION:\n{pre}")
    if s.get("condition") == "human":
        parts.append("NOTE: the conversation partner in this session is another HUMAN, shown as PARTNER.")
    return "\n\n".join(parts)


def _hints(s, events):
    ids = {e["id"] for e in events}
    rows = [f"- {c['uptake_event_id']} (trigger {c['trigger_event_id']}): {c['type']} via "
            + ", ".join(r["rule"] for r in c.get("rules", []))
            for c in s["candidates"] if c.get("source", "codebook") == "codebook" and c["uptake_event_id"] in ids]
    if not rows:
        return ""
    return ("KEYWORD-PARSER HINTS (a simple rule-based parser flagged these turns; it is often wrong. "
            "Use only as pointers, never as evidence):\n" + "\n".join(rows))


def _windows(s):
    """Split very long sessions into overlapping windows of exchanges."""
    events = [e for e in s["events"] if e["role"] != "thinking"]
    total = len(transcript.render_for_judge(s, events)) / CHARS_PER_TOKEN
    if total <= MAX_TOKENS_PER_CALL:
        return [events]
    exchanges, cur = [], []
    for e in events:
        if e["role"] == "user" and cur:
            exchanges.append(cur)
            cur = []
        cur.append(e)
    if cur:
        exchanges.append(cur)
    windows, start = [], 0
    while start < len(exchanges):
        size, end = 0, start
        while end < len(exchanges) and (size == 0 or size + _len(s, exchanges[end]) <= MAX_TOKENS_PER_CALL):
            size += _len(s, exchanges[end])
            end += 1
        windows.append([e for ex in exchanges[start:end] for e in ex])
        if end >= len(exchanges):
            break
        start = max(start + 1, end - OVERLAP_EXCHANGES)
    return windows


def _len(s, evs):
    return len(transcript.render_for_judge(s, evs)) / CHARS_PER_TOKEN


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[`*_“”\"']", "", (text or "").lower())).strip()


def _verified(quote, event):
    q = _norm(quote)
    if not q or not event:
        return False
    t = _norm(event["text"])
    return q in t or q[:60] in t


def run(s, hints=False, model=None):
    """Judge a session. Returns (candidates, meta). Raises llm.LLMError if the API is unavailable."""
    if not s["events"]:
        raise llm.LLMError("No transcript to judge yet: attach or import the agent log first.")
    by_id = {e["id"]: e for e in s["events"]}
    windows = _windows(s)
    found, summaries, usage = {}, [], {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for i, evs in enumerate(windows):
        parts = [_context(s)]
        if len(windows) > 1:
            parts.append(f"(This is part {i + 1} of {len(windows)} of a long session; windows overlap slightly.)")
        if hints:
            h = _hints(s, evs)
            if h:
                parts.append(h)
        parts.append("TRANSCRIPT (times are offsets into the participant's screen recording):\n\n"
                     + transcript.render_for_judge(s, evs))
        parts.append("Code the sparks in this transcript.")
        out, u = llm.judge_call(SYSTEM, "\n\n".join(parts), SCHEMA, model=model)
        for k in ("input_tokens", "output_tokens", "cost_usd"):
            usage[k] = round(usage[k] + u[k], 4)
        usage["model"] = u["model"]
        summaries.append(out.get("summary", ""))
        valid = {e["id"] for e in evs}
        for c in out.get("candidates", []):
            if c["trigger_event_id"] not in valid:
                continue
            up = by_id.get(c["uptake_event_id"])
            if c["uptake_event_id"] and (not up or up["role"] != "user"):
                c["uptake_event_id"] = ""
            c["confidence"] = max(0.0, min(1.0, float(c["confidence"])))
            c["source"] = "judge"
            c["quotes_verified"] = _verified(c["trigger_quote"], by_id.get(c["trigger_event_id"])) and (
                not c["uptake_event_id"] or _verified(c["uptake_quote"], up))
            key = (c["trigger_event_id"], c["type"])
            if key not in found or c["confidence"] > found[key]["confidence"]:
                found[key] = c
    order = {e["id"]: i for i, e in enumerate(s["events"])}
    cands = sorted(found.values(), key=lambda c: order.get(c["trigger_event_id"], 0))
    meta = {"method": "llm_judge", "windows": len(windows), "hints": hints,
            "summary": " ".join(x for x in summaries if x), **usage,
            "unverified_quotes": sum(1 for c in cands if not c["quotes_verified"])}
    return cands, meta


def estimate(s):
    """Rough token/cost estimate shown before running."""
    tokens = len(transcript.render_for_judge(s)) / CHARS_PER_TOKEN + 4000
    p_in, p_out = llm.PRICES.get(llm.JUDGE_MODEL, (0, 0))
    return {"model": llm.JUDGE_MODEL, "input_tokens": int(tokens),
            "approx_cost_usd": round(tokens / 1e6 * p_in + 6000 / 1e6 * p_out, 3)}
