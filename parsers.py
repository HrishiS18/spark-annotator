"""Normalise agent transcripts into a common event list.

Supported inputs
  * Claude Code session JSONL  (~/.claude/projects/<slug>/<session>.jsonl)
  * Codex rollout JSONL        (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl)
  * Generic chat JSON          ([{role, content, timestamp}] or {"messages": [...]})
  * A zip produced by joebaumann/upskilling-agent-log-exporter (or any zip of the above)

Every event has the shape
  {"id": "e12", "ts": "2026-10-11T18:40:51.123Z", "t": 1791744051.123,
   "role": "user" | "assistant" | "thinking" | "tool_call" | "tool_result" | "partner" | "system",
   "text": "...", "tool": "Bash" | None, "meta": {...}}
"""

import io
import json
import re
import zipfile
from datetime import datetime, timezone

MAX_TEXT = 20000  # per-event cap; tool outputs can be huge

# Harness-injected user-role content that the participant never typed.
_INJECTED_PREFIXES = (
    "<environment_context", "<user_instructions", "<permissions instructions",
    "# AGENTS.md", "<system-reminder", "<command-name>", "<local-command",
    "<command-message>", "Caveat: The messages below", "<user_shell_command",
    "<turn_aborted", "<task-notification", "<external_codex_apps", "<codex_app",
)
_SYSTEM_NOTES = ("[Request interrupted by user",)


def parse_ts(value):
    """ISO string / epoch seconds / epoch ms -> (iso, epoch_seconds)."""
    if value is None:
        return None, None
    try:
        if isinstance(value, (int, float)):
            secs = value / 1000.0 if value > 1e11 else float(value)
            dt = datetime.fromtimestamp(secs, tz=timezone.utc)
        else:
            s = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), dt.timestamp()
    except (ValueError, OSError, OverflowError):
        return None, None


def _clip(text):
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + f"\n…[truncated {len(text) - MAX_TEXT} chars]"
    return text


def _is_injected(text):
    return text.lstrip().startswith(_INJECTED_PREFIXES)


class _Builder:
    def __init__(self):
        self.events = []

    def add(self, role, text, ts=None, tool=None, **meta):
        if text is None:
            return
        text = _clip(text).strip()
        if not text:
            return
        if role == "user" and text.startswith(_SYSTEM_NOTES):
            role = "system"
        iso, t = parse_ts(ts)
        self.events.append({
            "id": f"e{len(self.events) + 1}", "ts": iso, "t": t, "role": role,
            "text": text, "tool": tool, "meta": {k: v for k, v in meta.items() if v is not None},
        })


def _tool_input_summary(name, inp):
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            return inp
    if isinstance(inp, dict):
        for key in ("command", "cmd", "file_path", "path", "pattern", "query", "url", "description"):
            if key in inp and isinstance(inp[key], (str, list)):
                val = inp[key] if isinstance(inp[key], str) else " ".join(map(str, inp[key]))
                rest = {k: v for k, v in inp.items() if k != key}
                return val + (("\n" + json.dumps(rest, ensure_ascii=False)) if rest else "")
    return json.dumps(inp, ensure_ascii=False)


def _flatten_blocks(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") in ("text", "input_text", "output_text"):
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


# --------------------------------------------------------------------------- formats

def _records(raw):
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def detect_format(raw):
    stripped = raw.lstrip()
    if stripped.startswith("[") or (stripped.startswith("{") and "\n" not in stripped.strip()):
        try:
            data = json.loads(stripped)
            if isinstance(data, list) or (isinstance(data, dict) and "messages" in data):
                return "chat"
        except ValueError:
            pass
    recs = _records(raw)[:200]
    if any(r.get("type") in ("response_item", "session_meta", "event_msg") for r in recs):
        return "codex"
    if any(r.get("type") in ("user", "assistant") and isinstance(r.get("message"), dict) for r in recs):
        return "claude"
    return "unknown"


def parse_claude(raw, include_sidechains=False):
    b = _Builder()
    meta = {"agent": "claude-code"}
    for r in _records(raw):
        if r.get("type") not in ("user", "assistant"):
            continue
        if r.get("isSidechain") and not include_sidechains:
            continue
        msg = r.get("message") or {}
        ts = r.get("timestamp")
        meta.setdefault("session_id", r.get("sessionId"))
        meta.setdefault("cwd", r.get("cwd"))
        content = msg.get("content")
        if r["type"] == "user":
            if r.get("isMeta"):
                continue
            if isinstance(content, str):
                if not _is_injected(content):
                    b.add("user", content, ts)
                continue
            for blk in content or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_result":
                    b.add("tool_result", _flatten_blocks(blk.get("content")), ts,
                          tool_use_id=blk.get("tool_use_id"), is_error=blk.get("is_error"))
                elif blk.get("type") == "text" and not _is_injected(blk.get("text", "")):
                    b.add("user", blk.get("text"), ts)
                elif blk.get("type") == "image":
                    b.add("user", "[image attached]", ts)
        else:
            meta.setdefault("model", msg.get("model"))
            for blk in content or []:
                if not isinstance(blk, dict):
                    continue
                kind = blk.get("type")
                if kind == "text":
                    b.add("assistant", blk.get("text"), ts)
                elif kind == "thinking":
                    b.add("thinking", blk.get("thinking"), ts)
                elif kind == "tool_use":
                    b.add("tool_call", _tool_input_summary(blk.get("name"), blk.get("input")), ts,
                          tool=blk.get("name"), tool_use_id=blk.get("id"))
    return b.events, meta


def parse_codex(raw):
    b = _Builder()
    meta = {"agent": "codex"}
    recs = _records(raw)
    has_items = any(r.get("type") == "response_item" for r in recs)
    for r in recs:
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        ts = r.get("timestamp")
        if r.get("type") == "session_meta":
            meta["session_id"] = p.get("id") or p.get("session_id")
            meta["cwd"] = p.get("cwd")
            continue
        if r.get("type") == "turn_context" and p.get("model"):
            meta.setdefault("model", p.get("model"))
            continue
        if has_items and r.get("type") == "response_item":
            kind = p.get("type")
            if kind == "message":
                role = p.get("role")
                text = _flatten_blocks(p.get("content"))
                if role == "user" and not _is_injected(text):
                    b.add("user", text, ts)
                elif role == "assistant":
                    b.add("assistant", text, ts)
            elif kind in ("function_call", "custom_tool_call", "local_shell_call"):
                inp = p.get("arguments", p.get("input", p.get("action")))
                b.add("tool_call", _tool_input_summary(p.get("name"), inp), ts,
                      tool=p.get("name") or kind, call_id=p.get("call_id"))
            elif kind in ("function_call_output", "custom_tool_call_output"):
                out = p.get("output")
                if isinstance(out, dict):
                    out = out.get("content") or out.get("output") or json.dumps(out)
                b.add("tool_result", _flatten_blocks(out) if not isinstance(out, str) else out, ts,
                      call_id=p.get("call_id"))
            elif kind == "reasoning":
                summ = p.get("summary") or []
                text = "\n".join(s.get("text", "") for s in summ if isinstance(s, dict))
                b.add("thinking", text, ts)
        elif not has_items and r.get("type") == "event_msg":
            kind = p.get("type")
            if kind == "user_message" and not _is_injected(p.get("message", "")):
                b.add("user", p.get("message"), ts)
            elif kind == "agent_message":
                b.add("assistant", p.get("message"), ts)
    return b.events, meta


def parse_chat(raw):
    data = json.loads(raw)
    msgs = data.get("messages", []) if isinstance(data, dict) else data
    b = _Builder()
    role_map = {"human": "user", "user": "user", "assistant": "assistant", "ai": "assistant",
                "model": "assistant", "partner": "partner", "system": "system"}
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = role_map.get(str(m.get("role", m.get("sender", ""))).lower(), "assistant")
        b.add(role, _flatten_blocks(m.get("content", m.get("text"))),
              m.get("timestamp", m.get("ts", m.get("created_at"))))
    return b.events, {"agent": "chat"}


def parse_transcript(raw, fmt=None):
    fmt = fmt or detect_format(raw)
    if fmt == "claude":
        return parse_claude(raw)
    if fmt == "codex":
        return parse_codex(raw)
    if fmt == "chat":
        return parse_chat(raw)
    raise ValueError("Unrecognised transcript format (expected Claude Code / Codex JSONL or chat JSON)")


# --------------------------------------------------------------------------- uploads / zips

def _summarise(name, raw):
    try:
        fmt = detect_format(raw)
        if fmt == "unknown":
            return None
        events, meta = parse_transcript(raw, fmt)
    except (ValueError, KeyError, TypeError):
        return None
    users = [e for e in events if e["role"] == "user"]
    if not events:
        return None
    times = [e["t"] for e in events if e["t"]]
    return {
        "member": name, "format": fmt, "agent": meta.get("agent"), "cwd": meta.get("cwd"),
        "events": len(events), "prompts": len(users),
        "start": parse_ts(min(times))[0] if times else None,
        "end": parse_ts(max(times))[0] if times else None,
        "minutes": round((max(times) - min(times)) / 60, 1) if len(times) > 1 else None,
        "opening": (users[0]["text"][:160] if users else ""),
    }


def inspect_upload(filename, data):
    """List importable transcripts in an uploaded file (single transcript or zip)."""
    if filename.lower().endswith(".zip") or data[:2] == b"PK":
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if not name.lower().endswith((".jsonl", ".json")) or "/subagents/" in name:
                    continue
                raw = z.read(name).decode("utf-8", errors="replace")
                s = _summarise(name, raw)
                if s:
                    out.append(s)
        return sorted(out, key=lambda s: s.get("start") or "", reverse=True)
    s = _summarise(filename, data.decode("utf-8", errors="replace"))
    return [s] if s else []


def load_member(filename, data, member):
    if filename.lower().endswith(".zip") or data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            raw = z.read(member).decode("utf-8", errors="replace")
    else:
        raw = data.decode("utf-8", errors="replace")
    return parse_transcript(raw)


def align_marks(marks, events):
    """Suggest context for timestamp-only bookmarks; never infer a causal anchor.

    Explicit participant-selected anchors survive imports unchanged. The review
    asks the participant to select a passage before committing an episode.
    """
    timed = [e for e in events if e.get("t")]
    for m in marks:
        if m.get("event_id") and any(e["id"] == m["event_id"] for e in events):
            continue
        t = m.get("t")
        if t is None:
            continue
        visible = [e for e in timed if e["t"] <= t + 1.0]
        agent_side = [e for e in visible if e["role"] in ("assistant", "tool_call", "tool_result")]
        target = (agent_side or visible or timed[:1])
        if target:
            m["proposed_event_id"] = target[-1]["id"]
            m["alignment"] = "timestamp_context_suggestion"
    return marks


_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text):
    return [s.strip() for s in _SENT.split(text or "") if len(s.strip()) > 2]
