"""Deterministic, codebook-driven spark parser (no API, no model interpretation).

The parser walks the normalised trace turn by turn:

  word-by-word      every event is tokenised; a vocabulary timeline records which
                    terms the participant had used and which terms the agent introduced
                    first (and in which event).
  response-by-response  each agent event is tagged with signals from the codebook
                    (asked a question, posed a hypothetical, reported an error).
  prompt-by-prompt  each participant turn is tested against every rule in
                    config/spark_codebook.json, using the preceding agent signals and
                    the vocabulary timeline as context.

Rule weights are summed per spark type for each participant turn; a candidate is
emitted when the total reaches settings.min_score. Each candidate carries the rule ids
and the exact words that fired, so every label is auditable and reproducible.
"""

import json
import math
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
CODEBOOK_PATH = os.path.join(ROOT, "config", "spark_codebook.json")

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_'.\-]*[A-Za-z0-9_]|[A-Za-z]")


def _is_code(tok):
    """Identifier-like tokens (CamelCase, snake_case, dotted paths, digits, ACRONYMS)."""
    return (any(c.isupper() for c in tok[1:]) or "_" in tok or "." in tok
            or any(c.isdigit() for c in tok))


def load_codebook(path=CODEBOOK_PATH):
    with open(path) as f:
        cb = json.load(f)
    flags = re.I | re.M
    cb["_signals"] = {k: re.compile(v, flags) for k, v in cb["signals"].items() if isinstance(v, str)}
    for r in cb["rules"]:
        for key in ("pattern", "ack"):
            if key in r:
                r["_" + key] = re.compile(r[key], flags)
    cb["_stop"] = set(cb["stopwords"].split())
    return cb


# --------------------------------------------------------------------------- word level

def terms(text, cb):
    """Content terms in a text: code-like identifiers keep their case, words are lowercased
    and lightly normalised. Returns {term: is_code_like}."""
    out = {}
    min_len = cb["settings"]["term_min_len"]
    for tok in _WORD.findall(text or ""):
        tok = tok.strip(".-'")
        if len(tok) < min_len:
            continue
        code = _is_code(tok)
        if code:
            out.setdefault(tok, True)
            continue
        w = tok.lower()
        if w in cb["_stop"] or not w.isalpha():
            continue
        for suf in ("'s", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= min_len and not w.endswith("ss"):
                w = w[: -len(suf)]
                break
        out.setdefault(w, False)
    return out


def _ngrams(text, n=5):
    toks = re.findall(r"[a-z0-9']+", (text or "").lower())
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def copy_overlap(text, sources, n=5):
    grams = _ngrams(text, n)
    if not grams:
        return 0.0
    pool = set()
    for s in sources:
        pool |= _ngrams(s, n)
    return round(len(grams & pool) / len(grams), 3)


def _snip(text, m, pad=50):
    a, b = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return ("…" if a else "") + text[a:b].replace("\n", " ") + ("…" if b < len(text) else "")


# --------------------------------------------------------------------------- parse

def parse(events, context=None, cb=None):
    """Returns (candidates, turns). `turns` is a per-event feature table for export."""
    cb = cb or load_codebook()
    st = cb["settings"]
    sig = cb["_signals"]
    P_ROLES = set(cb["roles"]["participant"])
    A_ROLES = set(cb["roles"]["agent"])
    context = context or {}

    seen_by_participant = set(terms(context.get("task", ""), cb)) | set(terms(context.get("pre", ""), cb))
    introduced_by = {}          # term -> event id where the agent used it first
    error_events = set()        # agent events that carry an error signal (+ the agent text right after)
    turns, candidates = [], []
    words_per_turn = []
    last_agent = []             # agent events since the previous participant turn
    prev_was_error = False

    for e in events:
        role, text = e["role"], e.get("text") or ""
        row = {"event_id": e["id"], "role": role, "ts": e.get("ts"), "words": len(text.split()),
               "signals": [], "new_terms": [], "adopted_terms": [], "rules": []}
        if role in A_ROLES:
            is_err = bool(e.get("meta", {}).get("is_error")) or (role != "assistant" and bool(sig["agent_error"].search(text))) \
                or (role == "assistant" and bool(sig["agent_error"].search(text[:400])))
            if is_err:
                row["signals"].append("error")
                error_events.add(e["id"])
            elif prev_was_error and role == "assistant":
                error_events.add(e["id"])  # the agent's explanation of the failure
                row["signals"].append("explains_error")
            prev_was_error = is_err or (prev_was_error and role != "assistant")
            if role in ("assistant", "partner"):
                if sig["agent_question"].search(text):
                    row["signals"].append("question")
                if sig["agent_hypothetical"].search(text):
                    row["signals"].append("hypothetical")
            for t, code in terms(text, cb).items():
                if t not in introduced_by and t not in seen_by_participant:
                    introduced_by[t] = e["id"]
                    row["new_terms"].append(t)
            last_agent.append((e, row))
            turns.append(row)
            continue

        if role not in P_ROLES:
            turns.append(row)
            continue

        # ---- participant turn: evaluate every rule
        low = text.lower()
        n_words = len(text.split())
        words_per_turn.append(n_words)
        p_terms = terms(text, cb)
        agent_text_events = [x for x, _ in last_agent if x["role"] in ("assistant", "partner")]
        trigger_default = (agent_text_events[-1] if agent_text_events else (last_agent[-1][0] if last_agent else None))
        agent_rows = [r for _, r in last_agent]
        fired = {}  # type -> list of evidence

        def fire(rule, weight, evidence, trigger=None):
            fired.setdefault(rule["type"], []).append({
                "rule": rule["id"], "weight": round(weight, 2), "evidence": evidence,
                "description": rule.get("description", ""), "trigger": (trigger or trigger_default or e)["id"]})

        adopted = {t: introduced_by[t] for t in p_terms if t in introduced_by and t not in seen_by_participant}
        row["adopted_terms"] = sorted(adopted)

        for rule in cb["rules"]:
            kind = rule["kind"]
            if kind == "participant_phrase":
                m = rule["_pattern"].search(text)
                if m:
                    fire(rule, rule["weight"], f"“{_snip(text, m)}”")
            elif kind == "question_then_phrase":
                m = rule["_pattern"].search(text)
                if m and any("question" in r["signals"] for r in agent_rows):
                    fire(rule, rule["weight"], "agent asked a question just before")
            elif kind == "disagree_then_reason":
                m = rule["_pattern"].search(text)
                c = sig["connector"].search(text[m.end():]) if m else None
                if m and c and n_words >= rule.get("min_words", 0):
                    fire(rule, rule["weight"], f"“{_snip(text, m, 20)}” … “{c.group(0)}”")
            elif kind == "agent_signal_then_phrase":
                src = next((x for x, r in last_agent if rule["signal"].replace("agent_", "") in r["signals"]), None)
                m = rule["_pattern"].search(text)
                if src and m:
                    fire(rule, rule["weight"], f"after hypothetical in {src['id']}: “{_snip(text, m)}”", src)
            elif kind == "error_then_pivot":
                err = next((x for x, r in reversed(last_agent) if x["id"] in error_events), None)
                a, m = rule["_ack"].search(text), rule["_pattern"].search(text)
                if err and a and m:
                    fire(rule, rule["weight"], f"after error in {err['id']}: “{_snip(text, a, 60)}”", err)
            elif kind == "term_uptake" and adopted:
                by_src = {}
                for t, src in adopted.items():
                    by_src.setdefault(src, []).append(t)
                w = sum(st["code_term_weight"] if p_terms[t] else st["content_term_weight"] for t in adopted)
                w = min(w, st["term_uptake_cap"])
                src = max(by_src, key=lambda k: len(by_src[k]))
                trig = next((x for x in events if x["id"] == src), None)
                if w > 0:
                    fire(rule, w, "adopted " + ", ".join(f"‘{t}’ (from {s})" for s, ts in by_src.items() for t in ts[:6]), trig)
            elif kind == "error_then_term_uptake":
                err_terms = [t for t, src in adopted.items() if src in error_events]
                if err_terms:
                    src = adopted[err_terms[0]]
                    trig = next((x for x in events if x["id"] == src), None)
                    fire(rule, rule["weight"], "adopted ‘" + "’, ‘".join(err_terms[:6]) + f"’ from failed output {src}", trig)

        # ---- mechanism guards (spark vs persuasion vs copying)
        partner_texts = [x["text"] for x in agent_text_events]
        overlap = copy_overlap(text, partner_texts)
        deferent = bool(sig["deference"].search(text)) and n_words <= cb["signals"]["deference_max_words"] \
            and not sig["connector"].search(text)
        mechanism = "copying" if overlap >= st["copy_overlap"] else "persuasion" if deferent else "spark"
        row["copy_overlap"] = overlap
        row["deferent"] = deferent

        for typ, evs in fired.items():
            score = sum(x["weight"] for x in evs)
            row["rules"] += [x["rule"] for x in evs]
            if score < st["min_score"]:
                continue
            trig = max(evs, key=lambda x: x["weight"])["trigger"]
            candidates.append({
                "trigger_event_id": trig, "uptake_event_id": e["id"], "type": typ, "secondary_type": "none",
                "mechanism": mechanism, "score": round(score, 2),
                "confidence": round(1 - math.exp(-score / 3), 2),
                "rules": evs,
                "trigger_quote": _first_words(next((x["text"] for x in events if x["id"] == trig), ""), 30),
                "uptake_quote": _first_words(text, 40),
                "rationale": "; ".join(f"{x['rule']} (+{x['weight']}): {x['evidence']}" for x in evs),
            })

        seen_by_participant |= set(p_terms)
        turns.append(row)
        last_agent = []
        prev_was_error = False

    # Multi-label: mark the strongest type per uptake turn as primary.
    by_turn = {}
    for c in candidates:
        by_turn.setdefault(c["uptake_event_id"], []).append(c)
    for group in by_turn.values():
        group.sort(key=lambda c: -c["score"])
        for i, c in enumerate(group):
            c["rank"] = i + 1
            if i == 0 and len(group) > 1:
                c["secondary_type"] = group[1]["type"]
    return candidates, turns


def _first_words(text, n):
    w = (text or "").split()
    return " ".join(w[:n]) + (" …" if len(w) > n else "")
