"""Spark detection dispatch and before/after attribution.

Detection is the deterministic codebook parser in spark_parser.py (no API). The LLM judge
that reads the whole timestamped transcript lives in judge.py and is run on demand.
Before/after tracing is lexical by default; SPARK_USE_LLM=1 lets Claude do it instead.
"""

import json
import os
import re

import llm
from parsers import sentences


TAXONOMY = json.load(open(__file__.rsplit("/", 1)[0] + "/config/taxonomy.json"))
TYPE_KEYS = [t["key"] for t in TAXONOMY["types"]]
PARTICIPANT_ROLES = ("user",)
LABEL = {"user": "PARTICIPANT", "assistant": "AI", "partner": "PARTNER", "thinking": "AI-REASONING",
         "tool_call": "AGENT TOOL CALL", "tool_result": "TOOL RESULT", "system": "SYSTEM"}
LIMIT = {"user": 3000, "assistant": 2500, "partner": 2500, "thinking": 400,
         "tool_call": 600, "tool_result": 800, "system": 200}


# --------------------------------------------------------------------------- blocks

def render(e):
    text = e["text"]
    lim = LIMIT.get(e["role"], 1000)
    if len(text) > lim:
        text = text[:lim] + f" …[+{len(e['text']) - lim} chars]"
    tool = f" ({e['tool']})" if e.get("tool") else ""
    return f"[{e['id']}] {LABEL.get(e['role'], e['role'].upper())}{tool}: {text}"


def _taxonomy_text():
    return "\n".join(f"- {t['key']}: {t['definition']} Cues: {t['cues']}" for t in TAXONOMY["types"])


# --------------------------------------------------------------------------- dispatch

from spark_parser import copy_overlap, load_codebook, parse as codebook_parse  # noqa: E402


def detect(events, context):
    """Codebook parser (spark_parser.py): deterministic, no API. Returns (candidates, meta, turns).
    The LLM judge is separate (judge.py) and is run on demand by the researcher."""
    cb = load_codebook()
    cands, turns = codebook_parse(events, context, cb)
    for c in cands:
        c["source"] = "codebook"
    return cands, {"method": "codebook", "codebook_version": cb["version"], "rules": len(cb["rules"])}, turns


# --------------------------------------------------------------------------- before/after attribution

_DIFF_SCHEMA = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "status": {"type": "string", "enum": ["new", "changed", "retained"]},
                    "pre_quote": {"type": "string"},
                    "source_event_ids": {"type": "array", "items": {"type": "string"}},
                    "mechanism": {"type": "string", "enum": ["spark", "persuasion", "copying", "own", "unclear"]},
                    "spark_type": {"type": "string", "enum": TYPE_KEYS + ["none"]},
                    "rationale": {"type": "string"},
                },
                "required": ["text", "status", "pre_quote", "source_event_ids", "mechanism", "spark_type", "rationale"],
                "additionalProperties": False,
            },
        },
        "dropped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"pre_quote": {"type": "string"}, "source_event_ids": {"type": "array", "items": {"type": "string"}},
                               "rationale": {"type": "string"}},
                "required": ["pre_quote", "source_event_ids", "rationale"],
                "additionalProperties": False,
            },
        },
        "position_shift": {"type": "string", "enum": ["none", "refined", "moderate", "reversed"]},
        "summary": {"type": "string"},
    },
    "required": ["units", "dropped", "position_shift", "summary"],
    "additionalProperties": False,
}

DIFF_SYSTEM = f"""You compare a participant's written position BEFORE and AFTER a conversation, and trace each element of the AFTER text back to the conversation.

Split the AFTER text into its distinct claims, considerations or rules ("units", usually one or two sentences each). For each unit:
- status: new (absent before), changed (present before but altered), retained (essentially the same).
- pre_quote: the matching BEFORE excerpt, or "" if new.
- source_event_ids: ids of conversation events that plausibly introduced or shaped it ([] if it owes nothing to the conversation).
- mechanism: spark (participant integrated an idea into their own reasoning), persuasion (adopted the partner's conclusion without own reasoning), copying (reproduces the partner's wording), own (the participant's own development, not traceable to the partner), unclear.
- spark_type: the closest type if mechanism is spark, else "none".
Spark types:
{_taxonomy_text()}
Also list BEFORE elements that were dropped, with the events that plausibly led to dropping them."""


def trace_diff(pre, post, events, context):
    convo = [e for e in events if e["role"] in ("user", "assistant", "partner")]
    partner_texts = [e["text"] for e in events if e["role"] in ("assistant", "partner")]
    if os.environ.get("SPARK_USE_LLM") == "1" and llm.status()["live"]:
        prompt = (f"TASK:\n{context.get('task', '')}\n\nBEFORE:\n{pre}\n\nAFTER:\n{post}\n\nCONVERSATION:\n"
                  + "\n\n".join(render(e) for e in convo))
        try:
            out = llm.json_call(DIFF_SYSTEM, prompt, _DIFF_SCHEMA)
            valid = {e["id"] for e in events}
            for u in out["units"]:
                u["source_event_ids"] = [x for x in u["source_event_ids"] if x in valid]
                u["copy_overlap"] = copy_overlap(u["text"], partner_texts)
            out["method"] = "llm"
            return out
        except llm.LLMError as exc:
            res = _heuristic_diff(pre, post, convo, partner_texts)
            res["error"] = str(exc)
            return res
    return _heuristic_diff(pre, post, convo, partner_texts)


_STOP = set("the a an and or but of to in on for is are was were be been it this that with as at by from i you we they he she my your our their not no do does did so if then than there here what which who whom whose why how can could should would will just very really more most also about into over".split())


def _content(text):
    return {w for w in re.findall(r"[a-z']+", text.lower()) if w not in _STOP and len(w) > 2}


def _jacc(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def _heuristic_diff(pre, post, convo, partner_texts):
    pre_s = sentences(pre)
    units = []
    for s in sentences(post):
        cs = _content(s)
        best_pre = max(pre_s, key=lambda p: _jacc(cs, _content(p)), default="")
        sim = _jacc(cs, _content(best_pre)) if best_pre else 0.0
        status = "retained" if sim >= 0.5 else "changed" if sim >= 0.25 else "new"
        sources = []
        if status != "retained":
            scored = sorted(((len(cs & _content(e["text"])) / (len(cs) or 1), e["id"]) for e in convo if e["role"] != "user"),
                            reverse=True)
            sources = [eid for score, eid in scored[:2] if score >= 0.2]
        ov = copy_overlap(s, partner_texts)
        mech = "copying" if ov >= 0.5 else ("unclear" if sources else "own")
        units.append({"text": s, "status": status, "pre_quote": best_pre if status != "new" else "",
                      "source_event_ids": sources, "mechanism": mech, "spark_type": "none",
                      "rationale": f"Heuristic: lexical similarity to BEFORE={sim:.2f}.", "copy_overlap": ov})
    return {"units": units, "dropped": [], "position_shift": "none" if all(u["status"] == "retained" for u in units) else "refined",
            "summary": "Heuristic attribution (no LLM). Treat as a rough first pass.", "method": "heuristic"}
