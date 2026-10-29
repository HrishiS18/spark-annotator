"""Participant testimony and evidence anchoring, never automatic spark inference."""

import copy
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from storage import ConflictError

ANNOTATION = json.loads((Path(__file__).parent / "config/annotation.json").read_text())
HIGHLIGHT_SOURCES = {"live", "review", "candidate"}

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
    if "quote_selection" in body:
        try:
            m["quote_selection"] = validate_quote_selection(s, body["quote_selection"])
        except ValueError:
            # A live bookmark must never be lost because its text moved while
            # streaming; it keeps the quote and timestamp without offsets.
            if source != "overlay" or m["anchor"] != "selection":
                raise
            m["quote_selection"] = None
        if m["quote_selection"]:
            m["quote"] = m["quote_selection"]["text"]
            m["event_id"] = m["quote_selection"]["event_id"]
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


def validate_quote_selection(s, selection):
    """Keep exact participant-selected text anchored to one transcript event."""
    if selection is None:
        return None
    if not isinstance(selection, dict):
        raise ValueError("Invalid text selection")
    event = next((e for e in s["events"] if e["id"] == selection.get("event_id")), None)
    start, end = selection.get("start"), selection.get("end")
    limit = settings(s)["highlights"]["max_chars"]
    if (event is None or type(start) is not int or type(end) is not int or
            start < 0 or end <= start or end > len(event.get("text") or "") or end - start > limit):
        raise ValueError(f"Select up to {limit} characters inside one message")
    exact = event["text"][start:end]
    if selection.get("text") != exact:
        raise ValueError("Selected text does not match the transcript")
    return {"event_id": event["id"], "start": start, "end": end, "text": exact}


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
        raise ValueError("Briefly describe what changed")
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
    content_change = any(k in body for k in ("review", "review_draft", "episode", "event_id", "quote_selection"))
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
    if "quote_selection" in body:
        m["quote_selection"] = validate_quote_selection(s, body["quote_selection"])
        m["quote"] = m["quote_selection"]["text"] if m["quote_selection"] else None
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
    highlights = [h for h in s.get("highlights", []) if not h.get("deleted")]
    s["independent_review"] = {"completed_at": iso(time.time()), "mark_ids": [m["id"] for m in marks],
                               "reviews": copy.deepcopy(marks), "zero_flags": not bool(marks),
                               "highlight_ids": [h["id"] for h in highlights],
                               "highlights": copy.deepcopy(highlights)}
    return s["independent_review"]


# --------------------------------------------------------------------------- highlights
# A highlight is an exact span of text the participant chose: in a transcript
# message, a tool output, or their own written answers. Its note and tags are in
# the participant's words. It may stand alone or belong to a moment (bookmark).

def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else copy.deepcopy(value)
    return out


def settings_for_study(study):
    return _merge(ANNOTATION, (study or {}).get("annotation"))


def settings(s):
    """Settings snapshotted at session creation; older sessions use the defaults."""
    snapshot = s.get("config_snapshot") or {}
    return _merge(settings_for_study(snapshot.get("study")), snapshot.get("annotation"))


def _target_text(s, target, cfg):
    if not isinstance(target, dict):
        raise ValueError("Invalid highlight target")
    allowed = cfg["targets"]
    if target.get("kind") == "event":
        event = next((e for e in s["events"] if e["id"] == target.get("event_id")), None)
        field = target.get("field", "text")
        if event is None or event.get("role") not in allowed["event_roles"] or field not in allowed["event_fields"]:
            raise ValueError("This text cannot be highlighted")
        return {"kind": "event", "event_id": event["id"], "field": field}, event.get(field) or ""
    if target.get("kind") == "writing":
        name = target.get("writing")
        if name not in allowed["writing"] or name not in s.get("writing", {}):
            raise ValueError("This written answer cannot be highlighted")
        return {"kind": "writing", "writing": name}, s["writing"][name].get("text") or ""
    raise ValueError("Invalid highlight target")


def validate_span(s, target, start, end, text, cfg):
    target, source = _target_text(s, target, cfg)
    if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(source):
        raise ValueError("Invalid highlight boundaries")
    if end - start > cfg["max_chars"]:
        raise ValueError(f"Highlight up to {cfg['max_chars']} characters at a time")
    if source[start:end] != text:
        raise ValueError("Highlighted text does not match the stored text")
    return target, source[start:end]


def validate_tags(tags, cfg):
    if tags is None:
        return []
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise ValueError("Tags must be a list of words or short phrases")
    out, seen = [], set()
    for tag in tags:
        tag = " ".join(tag.split())
        if not tag or tag.casefold() in seen:
            continue
        if len(tag) > cfg["tag_max_chars"]:
            raise ValueError(f"Keep each tag under {cfg['tag_max_chars']} characters")
        seen.add(tag.casefold())
        out.append(tag)
    if len(out) > cfg["max_tags"]:
        raise ValueError(f"Use at most {cfg['max_tags']} tags per highlight")
    return out


def _note(value, cfg):
    note = str(value or "")
    if len(note) > cfg["note_max_chars"]:
        raise ValueError(f"Keep notes under {cfg['note_max_chars']} characters")
    return note


def _mark_link(s, mark_id, cfg):
    if mark_id is None:
        if not cfg["standalone"]:
            raise ValueError("Attach this highlight to a moment")
        return None
    mark = next((m for m in s["marks"] if m["id"] == mark_id and not m.get("deleted")), None)
    if mark is None:
        raise ValueError("Unknown moment")
    return mark


def _after_lock_allowed(s, mark):
    # After the independent pass, only highlights of moments added from model
    # suggestions may change; the independent answers stay as submitted.
    return mark is not None and mark.get("source") == "candidate_confirm"


def create_highlight(s, body, hid):
    cfg = settings(s)["highlights"]
    if not cfg["enabled"]:
        raise ValueError("Highlighting is not enabled for this study")
    if s["phase"] not in ("chat", "review"):
        raise ValueError("Highlights can be added during the conversation or review")
    target, text = validate_span(s, body.get("target"), body.get("start"), body.get("end"), body.get("text"), cfg)
    if s["phase"] == "chat" and target["kind"] != "event":
        raise ValueError("Written answers can be highlighted during review")
    source = body.get("source", "live" if s["phase"] == "chat" else "review")
    if source not in HIGHLIGHT_SOURCES:
        raise ValueError("Invalid highlight source")
    mark = _mark_link(s, body.get("mark_id"), cfg)
    if s.get("independent_review") and not _after_lock_allowed(s, mark):
        raise ConflictError("This independent review is already locked. Its original answers are preserved.")
    candidate_id = body.get("candidate_id")
    if candidate_id is not None and not any(c["id"] == candidate_id for c in s["candidates"]):
        raise ValueError("Unknown suggested moment")
    if (source == "candidate") != (candidate_id is not None):
        raise ValueError("Suggested passages must name their suggestion")
    received = time.time()
    h = {"id": hid, "created_at": iso(received), "t": received, "phase": s["phase"], "source": source,
         "target": target, "start": body["start"], "end": body["end"], "text": text,
         "note": _note(body.get("note"), cfg), "tags": validate_tags(body.get("tags"), cfg),
         "mark_id": mark["id"] if mark else None, "candidate_id": candidate_id,
         "deleted": False, "version": 1}
    s.setdefault("highlights", []).append(h)
    return h


def patch_highlight(s, h, body):
    cfg = settings(s)["highlights"]
    if s["phase"] == "done":
        raise ConflictError("Completed sessions are locked")
    if s.get("independent_review"):
        current = next((m for m in s["marks"] if m["id"] == h.get("mark_id")), None)
        if h["id"] in s["independent_review"].get("highlight_ids", []) or not _after_lock_allowed(s, current):
            raise ConflictError("This independent review is already locked. Its original answers are preserved.")
    if body.get("expected_version") != h["version"]:
        raise ConflictError("This highlight changed in another tab. Reload to see the latest version.")
    if "note" in body:
        h["note"] = _note(body["note"], cfg)
    if "tags" in body:
        h["tags"] = validate_tags(body["tags"], cfg)
    if "mark_id" in body:
        mark = _mark_link(s, body["mark_id"], cfg)
        if s.get("independent_review") and not _after_lock_allowed(s, mark):
            raise ConflictError("Highlights cannot be moved into the locked independent review")
        h["mark_id"] = mark["id"] if mark else None
    if any(k in body for k in ("start", "end", "text")):
        # Earlier boundaries stay in the record history.
        h["target"], h["text"] = validate_span(s, h["target"], body.get("start"), body.get("end"), body.get("text"), cfg)
        h["start"], h["end"] = body["start"], body["end"]
    if "deleted" in body:
        h["deleted"] = bool(body["deleted"])
    h["version"] += 1
    h["updated_at"] = iso(time.time())
    return h


def tag_suggestions(sessions, s):
    """Participants only see their own earlier tags, never other people's."""
    mode = settings(s)["highlights"]["tag_suggestions"]
    if mode == "off":
        return []
    pid = s.get("participant_id")
    pool = [s] if mode == "session" or not pid or pid == "anon" else [o for o in sessions if o.get("participant_id") == pid]
    counts = {}
    for session in pool:
        for h in session.get("highlights", []):
            if not h.get("deleted"):
                for tag in h.get("tags", []):
                    counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts, key=lambda t: (-counts[t], t.casefold()))
