"""Participant testimony and evidence anchoring, never automatic spark inference."""

import copy
import math
import time
from datetime import datetime, timezone

from storage import ConflictError

DECISIONS = {"confirmed", "unsure", "rejected"}
UPTAKE = {"used", "not_used", "unclear", "integrated", "adopted", "copied", "none"}
OUTCOME = {"helped", "did_not_help", "not_yet_known", "unclear"}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def exposure(body, valid_ids):
    value = body.get("exposure") or {}
    if not isinstance(value, dict):
        raise ValueError("Invalid exposure snapshot")
    captured = []
    for row in value.get("events", [])[:6]:
        if not isinstance(row, dict) or row.get("event_id") not in valid_ids:
            continue
        captured.append({"event_id": row["event_id"], "rendered_text": str(row.get("rendered_text", ""))[:6000],
                         "rendered_output": str(row.get("rendered_output", ""))[:6000],
                         "status": str(row.get("status", ""))[:40], "tool_open": bool(row.get("tool_open")),
                         "partially_in_view": bool(row.get("partially_in_view"))})
    return {"surface": "external" if value.get("surface") == "external" else "integrated",
            "events": captured, "semantics": "rendered content at click, not proof it was read"}


def create_mark(s, body, mid):
    if s["phase"] == "done":
        raise ValueError("Completed sessions are locked")
    received = time.time()
    t = float(body["t"]) if "t" in body else float(body["client_t"]) / 1000 if body.get("client_t") else received
    if not math.isfinite(t) or t < 0 or t > received + 86400:
        raise ValueError("Invalid bookmark timestamp")
    ids = {e["id"] for e in s["events"]}
    eid = body.get("event_id")
    if eid is not None and eid not in ids:
        raise ValueError("Unknown bookmark event")
    phase = body.get("phase_at_click", s["phase"])
    if phase not in s["phase_order"]:
        raise ValueError("Invalid bookmark phase")
    source = body.get("source", "overlay")
    if source == "candidate_confirm" and not s.get("independent_review"):
        raise ValueError("Complete independent review before adding suggested moments")
    # A timestamp is evidence about when someone clicked, not about what caused
    # the idea. Only an explicit message/selection click establishes an anchor.
    m = {"id": mid, "created_at": iso(t), "t": t, "received_at": iso(received),
         "phase": phase, "source": source, "anchor": body.get("anchor", "timestamp"),
         "event_id": eid, "original_event_id": eid,
         "quote": str(body.get("quote") or "")[:2000] or None,
         "quick_type": body.get("quick_type"), "draft_len": body.get("draft_len"),
         "exposure": exposure(body, ids), "client_version": int(body.get("client_version", 1)),
         "review": None, "review_draft": None, "review_version": 0,
         "deleted": bool(body.get("deleted"))}
    if body.get("episode"):
        m["episode"] = validate_episode(s, body["episode"])
        m["event_id"] = m["episode"]["start_event_id"]
    if body.get("reference_t") is not None:
        reference = float(body["reference_t"])
        if not math.isfinite(reference) or reference < 0 or reference > received + 86400:
            raise ValueError("Invalid reference time")
        m["reference_t"] = reference
    s["marks"].append(m)
    return m


def validate_episode(s, episode):
    if not isinstance(episode, dict):
        raise ValueError("Invalid passage")
    start, end = episode.get("start_event_id"), episode.get("end_event_id")
    index = {e["id"]: i for i, e in enumerate(s["events"])}
    if start not in index or end not in index or index[start] > index[end]:
        raise ValueError("Select a valid, ordered passage")
    return {"start_event_id": start, "end_event_id": end}


def validate_review(value, type_keys, final=False):
    if not isinstance(value, dict):
        raise ValueError("Invalid review")
    r = copy.deepcopy(value)
    if r.get("decision") is not None and r["decision"] not in DECISIONS:
        raise ValueError("Invalid review decision")
    if "editor_seq" in r and (not isinstance(r["editor_seq"], int) or r["editor_seq"] < 0):
        raise ValueError("Invalid draft sequence")
    if r.get("decision") not in DECISIONS and final:
        raise ValueError("Confirm, reject, or mark this moment as unsure first")
    if r.get("decision") == "confirmed" and final and not str(r.get("after", "")).strip():
        raise ValueError("Briefly describe the idea or realization")
    for field in ("before", "after"):
        r[field] = str(r.get(field) or "")[:6000]
    types = r.get("types", [])
    if not isinstance(types, list) or any(t not in type_keys | {"other"} for t in types):
        raise ValueError("Invalid spark type")
    r["types"] = list(dict.fromkeys(types))
    if r.get("primary") is not None and r["primary"] not in types:
        raise ValueError("Primary type must be one of the selected types")
    for field, options in (("uptake", UPTAKE), ("outcome", OUTCOME),
                           ("latency", {"immediate", "delayed", "review", "unclear"})):
        if r.get(field) is not None and r[field] not in options:
            raise ValueError(f"Invalid {field}")
    for field in ("intensity", "perceived_value"):
        if r.get(field) is not None and r[field] not in (1, 2, 3, 4, 5):
            raise ValueError(f"Invalid {field} rating")
    r["status"] = "submitted" if final else "draft"
    return r


def patch_mark(s, m, body, type_keys):
    if s["phase"] == "done" or s.get("independent_review") and m.get("source") != "candidate_confirm":
        raise ConflictError("This independent review is already locked. Its original answers are preserved.")
    content_change = any(k in body for k in ("review", "review_draft", "episode", "event_id"))
    if content_change:
        previous_draft = m.get("review_draft") or m.get("review") or {}
        incoming = body.get("review_draft") or body.get("review") or {}
        if incoming.get("editor_id") and incoming.get("editor_id") == previous_draft.get("editor_id"):
            if int(incoming.get("editor_seq", 0)) <= int(previous_draft.get("editor_seq", 0)):
                return m  # idempotent acknowledgement after a lost response
        expected = body.get("expected_review_version")
        if expected is not None and expected != m.get("review_version", 0):
            raise ConflictError("Another tab changed this review. Your local draft has not been overwritten.")
    if "episode" in body:
        m["episode"] = validate_episode(s, body["episode"]) if body["episode"] else None
        m["event_id"] = m["episode"]["start_event_id"] if m["episode"] else None
    if "event_id" in body:
        if body["event_id"] is not None and body["event_id"] not in {e["id"] for e in s["events"]}:
            raise ValueError("Unknown event anchor")
        m["event_id"] = body["event_id"]
    for key in ("quick_type", "deleted"):
        if key in body:
            m[key] = body[key]
    if "review_draft" in body:
        m["review_draft"] = dict(validate_review(body["review_draft"], type_keys), saved_at=iso(time.time()))
        # Editing a submitted answer makes it a draft again until confirmed.
        m["review_dirty"] = True
    if "review" in body:
        # Legacy callers remain compatible; the new UI always supplies decision.
        final = "decision" in body["review"]
        m["review"] = dict(validate_review(body["review"], type_keys, final=final), reviewed_at=iso(time.time()))
        if not final:
            m["review"]["status"] = "legacy"
        m["review_draft"] = None
        m["review_dirty"] = False
    if content_change:
        m["review_version"] = m.get("review_version", 0) + 1
    return m


def complete_independent_review(s):
    if s.get("independent_review"):
        return s["independent_review"]
    if s["phase"] != "review":
        raise ValueError("Session is not in review")
    marks = [m for m in s["marks"] if m.get("source") != "candidate_confirm" and not m.get("deleted")]
    if any(m.get("review_dirty") or (m.get("review") or {}).get("status") != "submitted" for m in marks):
        raise ValueError("Confirm each moment's review before finishing the independent pass")
    s["independent_review"] = {"completed_at": iso(time.time()), "mark_ids": [m["id"] for m in marks],
                               "reviews": copy.deepcopy(marks), "zero_flags": not bool(marks)}
    return s["independent_review"]
