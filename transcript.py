"""Timestamped, human-readable rendering of a parsed session.

Times are offsets from the start of the screen recording (or from the first event when
there is no recording), so `[03:05]` in the transcript is 3:05 in the video.

* render_for_judge(s)  plain transcript the LLM judge reads (no flags, no parser output)
* render_markdown(s)   researcher copy with participant reports and LLM suggestions
                       and judge candidates interleaved under the events they refer to
"""

LIMITS = {"user": 4000, "assistant": 4000, "partner": 4000, "tool_call": 800, "tool_result": 1500,
          "thinking": 600, "system": 300}


def base_time(s):
    rec = s.get("recording") or {}
    if rec.get("started_t"):
        return rec["started_t"]
    times = [e["t"] for e in s["events"] if e.get("t")]
    return min(times) if times else 0


def clock(sec):
    if sec is None:
        return "--:--"
    sign = "-" if sec < 0 else ""
    sec = int(round(abs(sec)))
    h, m, x = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{sign}{h}:{m:02d}:{x:02d}" if h else f"{sign}{m:02d}:{x:02d}"


def offset(s, t, t0=None):
    return None if t is None else t - (base_time(s) if t0 is None else t0)


def label(e, external=True):
    role = e["role"]
    if role == "user":
        return "PARTICIPANT"
    if role == "assistant":
        return "AGENT" if external else "AI"
    if role == "partner":
        return "PARTNER (human)"
    if role == "tool_call":
        return f"AGENT RAN {e.get('tool') or 'tool'}"
    if role == "tool_result":
        return "TOOL RESULT" + (" (error)" if (e.get("meta") or {}).get("is_error") else "")
    if role == "thinking":
        return "AGENT REASONING (may not have been visible)"
    return role.upper()


def _clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f" …[+{len(text) - n} chars]"


def event_line(s, e, t0, external=True, clip=True):
    text = _clip(e['text'], LIMITS.get(e['role'], 1000)) if clip else e['text']
    return f"[{clock(offset(s, e.get('t'), t0))}] {e['id']} {label(e, external)}: {text}"


def render_for_judge(s, events=None, include_reasoning=False, clip=False):
    t0 = base_time(s)
    ext = s.get("external", True)
    evs = events if events is not None else s["events"]
    return "\n\n".join(event_line(s, e, t0, ext, clip=clip) for e in evs if include_reasoning or e["role"] != "thinking")


def render_markdown(s, taxonomy=None):
    t0 = base_time(s)
    ext = s.get("external", True)
    names = {t["key"]: t["label"] for t in (taxonomy or {}).get("types", [])}
    name = lambda k: names.get(k, k or "bookmark")  # noqa: E731
    marks = [m for m in s["marks"] if not m.get("deleted")]
    rec = s.get("recording") or {}
    out = [f"# {s['participant_id']} · {s['prompt']['title']}",
           f"Session `{s['id']}` · study `{s['study']}` · recording started {rec.get('started_at') or 'n/a'}"
           + (f" · {clock(rec.get('duration'))} long" if rec.get("duration") else ""),
           f"Task: {s['prompt']['text']}", "",
           "Legend: ⚑ participant bookmark/report · ★ LLM suggestion (not ground truth). Times are offsets into the recording/session.", ""]
    if (s.get("writing") or {}).get("pre"):
        out += ["## Before (participant's written position)", s["writing"]["pre"]["text"], ""]
    out.append("## Conversation")
    unattached = [m for m in marks if not m.get("event_id")]
    for e in s["events"]:
        out.append(event_line(s, e, t0, ext, clip=False))
        for m in marks:
            if m.get("event_id") == e["id"]:
                r = m.get("review") or {}
                bits = [f"> ⚑ FLAG at [{clock(offset(s, m.get('t'), t0))}] · {name(m.get('quick_type'))}"]
                if r:
                    bits.append(f"decision: {r.get('decision', 'legacy')}; annotated: {', '.join(name(x) for x in r.get('types', []))}"
                                f"; uptake: {r.get('uptake') or '-'}; size {r.get('intensity') or '-'}/5")
                    if r.get("before") or r.get("after"):
                        bits.append(f"before: “{r.get('before', '')}” → changed: “{r.get('after', '')}”")
                out.append(" · ".join(bits))
        for c in s["candidates"]:
            if c.get("source") != "judge" or c["trigger_event_id"] != e["id"]:
                continue
            out.append(f"> ★ LLM SUGGESTION {name(c['type'])} · evidence: {c.get('insight_evidence', 'legacy')}"
                       f" · strength: {c.get('strength', 'not recorded')} · uptake {c.get('uptake_event_id') or '-'}: {c.get('rationale', '')}")
        out.append("")
    if unattached:
        out += ["## Flags not matched to a transcript event"]
        out += [f"- ⚑ [{clock(offset(s, m.get('t'), t0))}] {name(m.get('quick_type'))}" for m in unattached]
    if (s.get("writing") or {}).get("post"):
        out += ["", "## After (participant's written position)", s["writing"]["post"]["text"]]
    j = s.get("judge") or {}
    if j.get("summary"):
        out += ["", "## LLM judge summary", j["summary"]]
    return "\n".join(out) + "\n"
