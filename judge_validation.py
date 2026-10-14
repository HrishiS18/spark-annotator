"""Blinded, double-coded validation sets for the spark candidate judge.

The LLM output is deliberately treated as candidate generation.  A validation
set mixes model candidates with participant-confirmed episodes and unflagged
comparison passages, then hides those sources from coders until results are
unblinded.  Unflagged passages are comparisons, not assumed negatives.
"""

import csv
import hashlib
import io
import random
import secrets
import time
import uuid
from collections import Counter
from datetime import datetime, timezone


EVIDENCE = ("expressed", "possible", "insufficient")
UPTAKE = ("observed", "not_observed", "unclear")
SPAN_QUALITY = ("adequate", "too_narrow", "too_broad", "not_coherent")
CONFIDENCE = ("low", "medium", "high")


def _iso(t=None):
    return datetime.fromtimestamp(t or time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _visible(session):
    return [e for e in session.get("events", [])
            if e.get("role") != "thinking" and not e.get("meta", {}).get("superseded")]


def _span(index, start, end=None):
    a, b = index.get(start), index.get(end or start)
    return (a, b) if a is not None and b is not None and a <= b else None


def _overlap(a, b):
    return bool(a and b and max(a[0], b[0]) <= min(a[1], b[1]))


def _confirmed_mark(mark):
    review = mark.get("review") or {}
    return (not mark.get("deleted") and mark.get("source") != "candidate_confirm"
            and not mark.get("review_dirty") and review.get("status") == "submitted"
            and review.get("decision") == "confirmed")


def _episode(session, span, private, salt):
    events = _visible(session)
    lo, hi = span
    context_lo, context_hi = max(0, lo - 2), min(len(events) - 1, hi + 2)
    key = f"{session['id']}:{events[lo]['id']}:{events[hi]['id']}:{salt}"
    item_id = "vi_" + hashlib.sha256(key.encode()).hexdigest()[:16]
    return {
        "id": item_id,
        "task": {"title": session.get("prompt", {}).get("title", "Task"),
                 "text": session.get("prompt", {}).get("text", "")},
        "baseline": session.get("writing", {}).get("pre", {}).get("text", ""),
        "partner": "human" if session.get("condition") == "human" else "AI",
        "events": [{"id": e["id"], "role": e.get("role", "unknown"), "text": e.get("text", ""),
                    "tool": e.get("tool"), "target": lo <= i <= hi}
                   for i, e in enumerate(events[context_lo:context_hi + 1], context_lo)],
        "private": {**private, "session_id": session["id"],
                    "start_event_id": events[lo]["id"], "end_event_id": events[hi]["id"]},
    }


def _pools(sessions):
    candidates, reports, controls = [], [], []
    occupied = {}
    candidate_spans = {}
    mark_spans = {}
    for session in sessions:
        events = _visible(session)
        index = {e["id"]: i for i, e in enumerate(events)}
        cspans, mspans = [], []
        for candidate in session.get("candidates", []):
            if candidate.get("source") != "judge":
                continue
            span = _span(index, candidate.get("start_event_id") or candidate.get("trigger_event_id"),
                         candidate.get("end_event_id") or candidate.get("uptake_event_id"))
            if not span:
                continue
            cspans.append((span, candidate))
        for mark in session.get("marks", []):
            if not _confirmed_mark(mark):
                continue
            episode = mark.get("episode") or {}
            span = _span(index, episode.get("start_event_id") or mark.get("event_id"),
                         episode.get("end_event_id"))
            if span:
                mspans.append((span, mark))
        candidate_spans[session["id"]] = cspans
        mark_spans[session["id"]] = mspans
        occupied[session["id"]] = [x[0] for x in cspans + mspans]

        for span, candidate in cspans:
            overlapping_marks = [m for ms, m in mspans if _overlap(span, ms)]
            candidates.append(_episode(session, span, {
                "stratum": "judge_candidate", "candidate_id": candidate.get("id"),
                "judge": {k: candidate.get(k) for k in ("type", "secondary_type", "insight_evidence",
                          "uptake", "persuasion", "copying", "strength", "rationale")},
                "overlapping_confirmed_mark_ids": [m["id"] for m in overlapping_marks],
            }, "candidate:" + str(candidate.get("id"))))

        # A participant-confirmed passage already represented by a judge passage
        # remains reference evidence for that candidate; do not show it twice.
        for span, mark in mspans:
            if any(_overlap(span, cs) for cs, _ in cspans):
                continue
            reports.append(_episode(session, span, {
                "stratum": "participant_report", "mark_id": mark.get("id"),
                "participant_review": mark.get("review"),
            }, "report:" + str(mark.get("id"))))

        if len(events) >= 2:
            lengths = sorted({min(4, max(1, b - a + 1)) for a, b in occupied[session["id"]]}) or [2]
            for length in lengths:
                for lo in range(0, len(events) - length + 1):
                    span = (lo, lo + length - 1)
                    roles = {events[i].get("role") for i in range(span[0], span[1] + 1)}
                    if any(_overlap(span, x) for x in occupied[session["id"]]):
                        continue
                    if not roles.intersection({"assistant", "partner"}) or "user" not in roles:
                        continue
                    controls.append(_episode(session, span, {
                        "stratum": "unflagged_comparison",
                        "note": "Not assumed to be a true negative.",
                    }, f"control:{lo}:{length}"))
    return candidates, reports, controls, candidate_spans, mark_spans


def create_sample(sessions, body, type_keys):
    if not sessions:
        raise ValueError("Select at least one session")
    seed = str(body.get("seed") or secrets.token_hex(4))[:80]
    rng = random.Random(seed)
    candidates, reports, controls, candidate_spans, mark_spans = _pools(sessions)
    rng.shuffle(candidates); rng.shuffle(reports); rng.shuffle(controls)
    requested = {
        "judge_candidate": max(0, min(int(body.get("candidate_n", 10)), 100)),
        "participant_report": max(0, min(int(body.get("report_n", 5)), 100)),
        "unflagged_comparison": max(0, min(int(body.get("control_n", 5)), 100)),
    }
    primary = (candidates[:requested["judge_candidate"]]
               + reports[:requested["participant_report"]])
    selected_controls = []
    desired_lengths = [sum(e["target"] for e in item["events"]) for item in primary] or [2]
    for i in range(min(requested["unflagged_comparison"], len(controls))):
        target_length = desired_lengths[i % len(desired_lengths)]
        best = min(range(len(controls)), key=lambda j: abs(sum(e["target"] for e in controls[j]["events"]) - target_length))
        selected_controls.append(controls.pop(best))
    selected = primary + selected_controls
    if not selected:
        raise ValueError("No eligible judge candidates, confirmed participant reports, or comparison passages were found")
    rng.shuffle(selected)
    sid = "vs_" + uuid.uuid4().hex[:12]
    for i, item in enumerate(selected):
        # Re-key after randomisation so the identifier reveals neither source nor session.
        item["id"] = "vi_" + hashlib.sha256(f"{sid}:{i}:{item['id']}".encode()).hexdigest()[:16]
    coder_names = [(body.get("coder_a") or "Coder A").strip()[:80],
                   (body.get("coder_b") or "Coder B").strip()[:80]]
    if not all(coder_names) or coder_names[0].casefold() == coder_names[1].casefold():
        raise ValueError("Enter two distinct coder names")
    coders = [{"id": "coder_a", "name": coder_names[0], "token": secrets.token_urlsafe(18)},
              {"id": "coder_b", "name": coder_names[1], "token": secrets.token_urlsafe(18)}]

    confirmed = 0
    overlapped = 0
    for session in sessions:
        cspans = [x[0] for x in candidate_spans[session["id"]]]
        for mspan, _ in mark_spans[session["id"]]:
            confirmed += 1
            overlapped += int(any(_overlap(mspan, cspan) for cspan in cspans))
    actual = Counter(item["private"]["stratum"] for item in selected)
    return {
        "id": sid, "name": (body.get("name") or "Spark judge pilot validation").strip()[:160],
        "created_at": _iso(), "created_t": time.time(), "seed": seed,
        "session_ids": [s["id"] for s in sessions], "coders": coders, "items": selected,
        "requested": requested, "actual": dict(actual),
        "reference": {"confirmed_independent_reports": confirmed,
                      "reports_with_passage_overlap": overlapped,
                      "relation": "passage_overlap_not_recall"},
        "type_keys": list(type_keys),
        "method_note": ("Judge candidates are candidate generation, not ground truth. Participant reports establish perceived value; "
                        "human coding assesses transcript evidence. Unflagged passages are comparisons, not assumed negatives."),
    }


def list_row(sample, codes):
    progress = {c["id"]: sum(1 for code in codes if code["coder_id"] == c["id"])
                for c in sample["coders"]}
    return {k: sample[k] for k in ("id", "name", "created_at", "requested", "actual")} | {
        "items": len(sample["items"]), "progress": progress,
        "coders": [{"id": c["id"], "name": c["name"]} for c in sample["coders"]],
    }


def coder_view(sample, codes, token):
    coder = next((c for c in sample["coders"] if secrets.compare_digest(c["token"], token or "")), None)
    if not coder:
        raise KeyError("invalid coder token")
    own = {c["item_id"]: c["code"] for c in codes if c["coder_id"] == coder["id"]}
    items = [{"id": item["id"], "task": item["task"], "baseline": item["baseline"], "partner": item["partner"],
              "events": [{k: event.get(k) for k in ("role", "text", "tool", "target")} for event in item["events"]]}
             for item in sample["items"]]
    return {"id": sample["id"], "name": sample["name"], "created_at": sample["created_at"],
            "coder": {"id": coder["id"], "name": coder["name"]}, "items": items, "codes": own,
            "type_keys": sample["type_keys"], "method_note": sample["method_note"]}


def validate_code(sample, token, body):
    coder = next((c for c in sample["coders"] if secrets.compare_digest(c["token"], token or "")), None)
    if not coder:
        raise KeyError("invalid coder token")
    item_id = body.get("item_id")
    if item_id not in {item["id"] for item in sample["items"]}:
        raise ValueError("unknown validation item")
    code = {k: body.get(k, "") for k in ("evidence", "primary_type", "uptake", "span_quality", "confidence", "notes")}
    if code["evidence"] not in EVIDENCE:
        raise ValueError("Choose an evidence rating")
    allowed_types = set(sample["type_keys"]) | {"unclear", "none"}
    if code["primary_type"] not in allowed_types:
        raise ValueError("Choose a valid primary type")
    if code["uptake"] not in UPTAKE or code["span_quality"] not in SPAN_QUALITY or code["confidence"] not in CONFIDENCE:
        raise ValueError("Complete each coding field")
    code["notes"] = code["notes"].strip()[:4000]
    return coder["id"], item_id, code


def _kappa(pairs):
    if not pairs:
        return None
    agree = sum(a == b for a, b in pairs) / len(pairs)
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    labels = set(left) | set(right)
    chance = sum(left[x] * right[x] for x in labels) / (len(pairs) ** 2)
    if chance == 1:
        return 1.0 if agree == 1 else None
    return round((agree - chance) / (1 - chance), 3)


def _agreement(rows, field):
    pairs = [(r["codes"][0][field], r["codes"][1][field]) for r in rows if len(r["codes"]) == 2]
    return {"n": len(pairs), "raw": round(sum(a == b for a, b in pairs) / len(pairs), 3) if pairs else None,
            "cohen_kappa": _kappa(pairs)}


def results(sample, codes):
    by_item = {}
    for code in codes:
        by_item.setdefault(code["item_id"], {})[code["coder_id"]] = code["code"]
    rows = []
    for item in sample["items"]:
        item_codes = [by_item.get(item["id"], {}).get(c["id"]) for c in sample["coders"]]
        item_codes = [{"coder_id": sample["coders"][i]["id"], **code}
                      for i, code in enumerate(item_codes) if code]
        rows.append({"id": item["id"], "stratum": item["private"]["stratum"],
                     "private": item["private"], "codes": item_codes,
                     "task": item["task"], "events": item["events"]})
    complete = [r for r in rows if len(r["codes"]) == 2]
    support = {}
    for stratum in ("judge_candidate", "participant_report", "unflagged_comparison"):
        subset = [r for r in complete if r["stratum"] == stratum]
        support[stratum] = {
            "double_coded_n": len(subset),
            "both_transcript_supported": sum(all(c["evidence"] != "insufficient" for c in r["codes"]) for r in subset),
            "both_expressed": sum(all(c["evidence"] == "expressed" for c in r["codes"]) for r in subset),
            "evidence_disagreement": sum(r["codes"][0]["evidence"] != r["codes"][1]["evidence"] for r in subset),
            "coder_ratings": dict(Counter(c["evidence"] for r in subset for c in r["codes"])),
        }
    progress = {c["id"]: sum(code["coder_id"] == c["id"] for code in codes) for c in sample["coders"]}
    return {
        "sample": list_row(sample, codes), "complete_items": len(complete), "total_items": len(rows),
        "agreement": {field: _agreement(complete, field) for field in ("evidence", "primary_type", "uptake", "span_quality")},
        "candidate_support": support, "reference": sample["reference"], "rows": rows,
        "progress": progress, "method_note": sample["method_note"],
        "interpretation": ("Report support rates and coder agreement separately. Passage overlap with participant reports is descriptive coverage, "
                           "not recall; participant reports and transcript codes answer different questions."),
    }


def export_csv(sample, codes):
    result = results(sample, codes)
    buf = io.StringIO()
    fields = ["validation_id", "item_id", "stratum", "session_id", "start_event_id", "end_event_id",
              "candidate_id", "mark_id", "judge_evidence", "judge_type", "overlapping_confirmed_mark",
              "coder_a_evidence", "coder_a_type", "coder_a_uptake", "coder_a_span", "coder_a_confidence", "coder_a_notes",
              "coder_b_evidence", "coder_b_type", "coder_b_uptake", "coder_b_span", "coder_b_confidence", "coder_b_notes"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for row in result["rows"]:
        private = row["private"]
        by_coder = {code["coder_id"]: code for code in row["codes"]}
        cs = [by_coder.get("coder_a", {}), by_coder.get("coder_b", {})]
        judge = private.get("judge") or {}
        writer.writerow({
            "validation_id": sample["id"], "item_id": row["id"], "stratum": row["stratum"],
            "session_id": private.get("session_id"), "start_event_id": private.get("start_event_id"),
            "end_event_id": private.get("end_event_id"), "candidate_id": private.get("candidate_id"),
            "mark_id": private.get("mark_id"), "judge_evidence": judge.get("insight_evidence"),
            "judge_type": judge.get("type"),
            "overlapping_confirmed_mark": bool(private.get("overlapping_confirmed_mark_ids")),
            **{f"coder_{letter}_{suffix}": cs[i].get(key, "") for i, letter in enumerate(("a", "b"))
               for suffix, key in (("evidence", "evidence"), ("type", "primary_type"), ("uptake", "uptake"),
                                   ("span", "span_quality"), ("confidence", "confidence"), ("notes", "notes"))},
        })
    return buf.getvalue()
