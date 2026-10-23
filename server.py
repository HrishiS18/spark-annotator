"""Spark Annotator: local study server.

    python3 server.py                  # http://127.0.0.1:8765
    python3 server.py --host 0.0.0.0   # let lab machines on the LAN connect

Storage is transactional SQLite. Original data/sessions/*.json files are retained
as read-only migration backups. Integrated agent execution is localhost-only.
"""

import argparse
import copy
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import judge
import llm
import parsers
import transcript
import storage
import annotations
import judge_validation
from codex_runtime import RuntimeUnavailable
from agent_runtime import RuntimeManager, AGENT_KINDS

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get("SPARK_DATA_DIR", os.path.join(ROOT, "data")))
SESSIONS = os.path.join(DATA, "sessions")
UPLOADS = os.path.join(DATA, "uploads")
RECORDINGS = os.path.join(DATA, "recordings")
STATIC = os.path.join(ROOT, "static")
for _d in (SESSIONS, UPLOADS, RECORDINGS):
    os.makedirs(_d, exist_ok=True)


def _load_config(name):
    with open(os.path.join(ROOT, "config", name)) as f:
        return json.load(f)


TAXONOMY = _load_config("taxonomy.json")
PERSONAS = {p["key"]: p for p in _load_config("personas.json")["personas"]}
STUDIES = {s["key"]: s for s in _load_config("studies.json")["studies"]}
LOCK = threading.RLock()
_STORE = None
_RUNTIME = None
_JUDGE_JOBS = {}
_JUDGE_SLOTS = threading.BoundedSemaphore(2)


def get_store():
    global _STORE
    with LOCK:
        if _STORE is None:
            _STORE = storage.Store(os.path.join(DATA, "study.sqlite3"), write_lock=LOCK)
            _STORE.migrate_legacy(SESSIONS)
        return _STORE


def get_runtime():
    global _RUNTIME
    with LOCK:
        if _RUNTIME is None:
            _RUNTIME = RuntimeManager(get_store(), DATA)
        return _RUNTIME


def now():
    t = time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat().replace("+00:00", "Z"), t


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- storage

def _path(sid):
    if not re.fullmatch(r"s_[0-9a-f]{8}", sid or ""):
        raise KeyError(sid)
    return os.path.join(SESSIONS, f"{sid}.json")


def load(sid):
    _path(sid)  # preserve ID validation, not JSON-file persistence
    return get_store().load(sid)


def save(s):
    _path(s["id"])
    get_store().save(s)


def all_sessions():
    return get_store().all_sessions()


# --------------------------------------------------------------------------- session logic

PHASE_ORDER = ["pre", "chat", "post", "review", "done"]


def phases_for(study):
    order = [p for p in ("pre", "chat", "post") if p in study["phases"]]
    return order + ["review", "done"]


def create_session(body, allow_legacy=False):
    study = STUDIES.get(body.get("study"))
    if not study:
        raise ValueError("unknown study")
    prompt = next((p for p in study["prompts"] if p["id"] == body.get("prompt_id")), study["prompts"][0])
    persona = body.get("persona") or study["default_persona"]
    if persona not in PERSONAS:
        raise ValueError("unknown persona")
    condition = body.get("condition", "ai")
    if condition not in ("ai", "human"):
        raise ValueError("unknown partner condition")
    runtime_kind = "external" if study.get("external") else "human" if condition == "human" else body.get("runtime", "codex")
    if runtime_kind not in AGENT_KINDS | {"human","external"} and not (allow_legacy and runtime_kind in ("plain","demo")):
        raise ValueError("Choose Codex or Claude agent. Plain chat and scripted demo are not new participant runtimes.")
    iso, _ = now()
    order = phases_for(study)
    s = {
        "id": "s_" + uuid.uuid4().hex[:8], "created_at": iso,
        "participant_id": (body.get("participant_id") or "").strip() or "anon",
        "study": study["key"], "prompt": prompt, "persona": persona,
        "condition": condition, "room": (body.get("room") or "").strip().upper() or None,
        "external": bool(study.get("external")),
        "phase_order": order, "phase": order[0],
        "phases": {p: {"minutes": study["phases"].get(p)} for p in order},
        "writing": {}, "events": [], "marks": [], "candidates": [],
        "detection": None, "trace": None, "codes": {}, "imports": [],
        "recording": None, "parse_turns": [],
        "schema_version": 2, "analysis_runs": [],
        "runtime": {"kind": runtime_kind, "status": "disconnected", "active_turn": None, "approvals": [],
                    "model": os.environ.get("SPARK_CLAUDE_MODEL","sonnet") if runtime_kind == "claude" else os.environ.get("SPARK_CODEX_MODEL") or None,
                    "requested_model": os.environ.get("SPARK_CLAUDE_MODEL","sonnet") if runtime_kind == "claude" else os.environ.get("SPARK_CODEX_MODEL") or None,
                    "effort": os.environ.get("SPARK_CLAUDE_EFFORT" if runtime_kind == "claude" else "SPARK_CODEX_EFFORT", "medium")},
    }
    if body.get("task"):
        s["prompt"] = dict(prompt, text=body["task"].strip()[:2000])
    s["config_snapshot"] = {"study": copy.deepcopy(study), "task": copy.deepcopy(s["prompt"]),
                            "persona": copy.deepcopy(PERSONAS[persona]), "taxonomy": copy.deepcopy(TAXONOMY),
                            "runtime": copy.deepcopy(s["runtime"])}
    s["phases"][order[0]]["started_at"] = iso
    save(s)
    return s


def advance(s, phase, text=None):
    if phase != s["phase"]:
        raise ValueError(f"session is in phase {s['phase']}, not {phase}")
    if phase == "done":
        raise ValueError("Session is already complete")
    if phase == "review":
        if not s.get("independent_review"):
            raise ValueError("Complete independent review first")
        if any(m.get("source") == "candidate_confirm" and not m.get("deleted") and
               (m.get("review_dirty") or (m.get("review") or {}).get("status") != "submitted") for m in s["marks"]):
            raise ValueError("Confirm the suggested moments you added before finishing")
    iso, _ = now()
    if phase in ("pre", "post"):
        s["writing"][phase] = {"text": text or "", "submitted_at": iso}
    s["phases"][phase]["ended_at"] = iso
    nxt = s["phase_order"][s["phase_order"].index(phase) + 1]
    s["phase"] = nxt
    s["phases"].setdefault(nxt, {})["started_at"] = iso
    return nxt


def add_event(s, role, text, **extra):
    iso, t = now()
    ev = {"id": "ev_" + uuid.uuid4().hex, "ts": iso, "t": t, "role": role, "text": text,
          "tool": None, "meta": extra}
    s["events"].append(ev)
    return ev


def llm_history(s):
    msgs = []
    for e in s["events"]:
        if e["role"] not in ("user", "assistant"):
            continue
        if msgs and msgs[-1]["role"] == e["role"]:
            msgs[-1]["content"] += "\n\n" + e["text"]
        else:
            msgs.append({"role": e["role"], "content": e["text"]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def system_prompt(s):
    p = s.get("config_snapshot", {}).get("persona", PERSONAS[s["persona"]])["system"]
    return f"{p}\n\nThe person is thinking about this question:\n{s['prompt']['text']}"


def ai_reply(sid):
    with LOCK:
        s = load(sid)
        hist = llm_history(s)
        sys_p = system_prompt(s)
    t0 = time.time()
    meta = {}
    mode = s.get("runtime", {}).get("kind", "legacy")
    if mode == "demo":
        text = llm.demo_reply(s["persona"], hist)
        meta = {"demo": True}
    elif llm.status()["live"]:
        try:
            text = llm.chat(sys_p, hist)
            meta = {"model": llm.MODEL}
        except llm.LLMError as exc:
            raise RuntimeUnavailable("Plain LLM chat failed; no demo reply was substituted.") from exc
    else:
        raise RuntimeUnavailable("Plain LLM chat is not configured. Choose Codex or an explicitly labeled demo session.")
    meta["latency_s"] = round(time.time() - t0, 2)
    with LOCK:
        s = load(sid)
        ev = add_event(s, "assistant", text, **meta)
        save(s)
    return ev


def room_peers(s):
    if not s.get("room"):
        return []
    return [o for o in all_sessions() if o["id"] != s["id"] and o.get("room") == s["room"]
            and o.get("condition") == "human"]


def _merge_candidates(s, cands, source, metadata=None):
    """Every analysis run has separate predictions; historical answers stay intact."""
    configuration = {**judge.status(), "system_prompt": judge.SYSTEM, "schema": judge.SCHEMA}
    return storage.record_analysis_run(s, source, cands, metadata or {}, configuration)


def auto_judge_enabled():
    return os.environ.get("SPARK_AUTO_JUDGE", "1") != "0"


def judge_fingerprint(s):
    events = [e for e in s["events"] if e["role"] != "thinking" and not e.get("meta", {}).get("superseded")]
    settings = judge.status()
    value = {"context": judge._context(s), "transcript": transcript.render_for_judge(s, events),
             "version": judge.VERSION, "provider": settings.get("provider"),
             "model": settings.get("model"), "effort": settings.get("effort")}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def ensure_judge(sid, manual=False):
    """Idempotently prepare suggestions, never infer on GET or bulk startup."""
    with LOCK:
        s = load(sid)
        if s["phase"] in ("pre", "chat") or not manual and s["phase"] == "done":
            if manual:
                raise ValueError("Finish the conversation before running the judge")
            return {"status": "not_run"}
        previous = s.get("judge") or {}
        if previous.get("status") in ("queued", "running") or sid in _JUDGE_JOBS:
            if manual:
                raise ValueError("The judge is already running for this session")
            return previous
        fingerprint = judge_fingerprint(s)
        if not manual and previous.get("status") in ("done", "error") and previous.get("input_fingerprint", fingerprint) == fingerprint:
            return previous  # No repeated calls after refresh or a failed run.
        visible = [e for e in s["events"] if e["role"] != "thinking" and not e.get("meta", {}).get("superseded")]
        settings = judge.status()
        if not visible:
            if manual:
                raise ValueError("No transcript to judge yet")
            state = {"status": "waiting_for_transcript", "error": None}
        elif not manual and (not auto_judge_enabled() or s.get("runtime", {}).get("kind") == "demo"):
            state = {"status": "disabled", "error": None}
        elif not settings["live"]:
            if manual:
                raise ValueError(f"LLM judge unavailable: {settings.get('reason')}")
            state = {"status": "unavailable", "error": settings.get("reason")}
        else:
            state = {"status": "queued", "requested_at": now()[0], "input_fingerprint": fingerprint,
                     "requested_by": "manual" if manual else "automatic", **judge.estimate(s)}
        if state != previous:
            s["judge"] = state
            save(s)
        if state["status"] != "queued":
            return state
        def work():
            try:
                with _JUDGE_SLOTS:
                    run_judge(sid, reserved=True)
            finally:
                with LOCK:
                    _JUDGE_JOBS.pop(sid, None)
        worker = threading.Thread(target=work, name="spark-judge-" + sid, daemon=True)
        _JUDGE_JOBS[sid] = worker
        worker.start()
        return state


def run_judge(sid, reserved=False):
    with LOCK:
        s = load(sid)
        if s["phase"] in ("pre", "chat"):
            raise ValueError("Finish the conversation before running the judge")
        if not reserved and (s.get("judge") or {}).get("status") in ("queued", "running"):
            raise ValueError("The judge is already running")
        tracking = {k: v for k, v in (s.get("judge") or {}).items()
                    if reserved and k in ("requested_at", "input_fingerprint", "requested_by")}
        s["judge"] = {**tracking, "status": "running", "started_at": now()[0], **judge.estimate(s)}
        s["judge"].setdefault("input_fingerprint", judge_fingerprint(s))
        s["judge"].setdefault("requested_by", "manual")
        tracking = dict(s["judge"])
        save(s)
    try:
        cands, meta = judge.run(s)
        status, err = "done", None
    except llm.LLMError as exc:
        cands, meta, status, err = None, {}, "error", str(exc)
    except Exception as exc:  # keep the server alive whatever happens
        cands, meta, status, err = None, {}, "error", repr(exc)
    with LOCK:
        s = load(sid)
        run_metadata = {**tracking, **meta, "status": status, "error": err, "finished_at": now()[0]}
        # Failed attempts are runs too. Do not present older predictions as if
        # they came from the failed run; previous responses remain archived.
        _merge_candidates(s, cands or [], "judge", run_metadata)
        s["judge"] = dict(s.get("judge") or {}, **meta, status=status, error=err, finished_at=now()[0],
                          n=len(cands) if cands is not None else None)
        save(s)


def event_index(s):
    return {e["id"]: i for i, e in enumerate(s["events"])}


def recover_judges():
    """Single-server startup recovery; never restart inference automatically."""
    with LOCK:
        for s in all_sessions():
            if (s.get("judge") or {}).get("status") not in ("queued", "running"):
                continue
            error = "Server restarted during judging. Run again explicitly."
            meta = {**s["judge"], "status": "error", "error": error, "finished_at": now()[0]}
            _merge_candidates(s, [], "judge", meta)
            s["judge"] = {**meta, "n": None}
            save(s)

def analysis(s, tol=None):
    """Descriptive passage overlap, not accuracy or validation of perceived value."""
    idx = event_index(s)
    marks = [m for m in s["marks"] if not m.get("deleted")]
    candidates = [c for c in s["candidates"] if c.get("source") == "judge"]
    links = []
    def span(start, end):
        a, b = idx.get(start), idx.get(end or start)
        return (a,b) if a is not None and b is not None and a <= b else None
    for m in marks:
        episode = m.get("episode") or {}
        a = span(episode.get("start_event_id",m.get("event_id")), episode.get("end_event_id"))
        for c in candidates:
            b = span(c.get("start_event_id",c["trigger_event_id"]), c.get("end_event_id") or c.get("uptake_event_id"))
            if a and b and max(a[0],b[0]) <= min(a[1],b[1]):
                links.append({"mark":m["id"],"candidate":c["id"],"source":"judge","relation":"passage_overlap"})
    types = [t["key"] for t in TAXONOMY["types"]]
    by_type = {t: {"marks": 0, "judge": 0} for t in types}
    for m in marks:
        t = ((m.get("review") or {}).get("primary")) or m.get("quick_type")
        if t in by_type:
            by_type[t]["marks"] += 1
    for c in candidates:
        if c.get("type") in by_type:
            by_type[c["type"]]["judge"] += 1
    return {
        "marks": len(marks), "candidates": len(candidates), "judge": len(candidates),
        "matched_marks": len({l["mark"] for l in links}),
        "matched_candidates": len({l["candidate"] for l in links}),
        "independent_marks": sum(m.get("source") != "candidate_confirm" for m in marks),
        "suggested_marks": sum(m.get("source") == "candidate_confirm" for m in marks),
        "links": links, "by_type": by_type, "relation": "passage_overlap_not_accuracy",
    }


def summary_row(s):
    marks = [m for m in s["marks"] if not m.get("deleted")]
    return {
        "id": s["id"], "participant_id": s["participant_id"], "created_at": s["created_at"],
        "study": s["study"], "prompt": s["prompt"]["title"], "persona": s["persona"],
        "condition": s["condition"], "room": s.get("room"), "phase": s["phase"],
        "events": len(s["events"]), "marks": len(marks),
        "reviewed": sum(1 for m in marks if m.get("review")),
        "candidates": sum(c.get("source") == "judge" for c in s["candidates"]),
        "judge_status": (s.get("judge") or {}).get("status", "not_run"),
        "recording": round((s.get("recording") or {}).get("duration") or 0) if (s.get("recording") or {}).get("file") else None,
    }


# --------------------------------------------------------------------------- CSV export

def _quick(text, n=300):
    return (text or "").replace("\n", " ")[:n]


def _offset(s, item):
    """Seconds into the screen recording (for jumping to a flag or event in the video)."""
    rec = s.get("recording") or {}
    t = (item or {}).get("t")
    return round(t - rec["started_t"], 1) if t is not None and rec.get("started_t") else None




def marks_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["session_id", "participant_id", "study", "prompt_id", "persona", "condition", "mark_id", "created_at",
                "video_offset_s", "source", "anchor", "event_id", "event_role", "event_text", "quote", "quick_type", "review_types",
                "review_primary", "uptake", "latency", "intensity", "before", "after", "matched_candidates",
                "researcher_label", "decision", "review_status", "perceived_value", "reported_outcome",
                "passage_start", "passage_end", "original_click_t", "reference_t", "original_event_id",
                "quote_event_id", "quote_start", "quote_end"])
    for s in all_sessions():
        ev = {e["id"]: e for e in s["events"]}
        an = analysis(s)
        for m in s["marks"]:
            if m.get("deleted"):
                continue
            r = m.get("review") or {}
            e = ev.get(m.get("event_id"), {})
            w.writerow([s["id"], s["participant_id"], s["study"], s["prompt"]["id"], s["persona"], s["condition"],
                        m["id"], m["created_at"], _offset(s, m), m.get("source"), m.get("anchor"), m.get("event_id"), e.get("role"),
                        _quick(e.get("text")), _quick(m.get("quote")), m.get("quick_type"),
                        "|".join(r.get("types", [])), r.get("primary"), r.get("uptake"), r.get("latency"),
                        r.get("intensity"), _quick(r.get("before"), 1000), _quick(r.get("after"), 1000),
                        "|".join(l["candidate"] for l in an["links"] if l["mark"] == m["id"]),
                        (s["codes"].get(f"mark:{m['id']}") or {}).get("label"), r.get("decision"), r.get("status"),
                        r.get("perceived_value"), r.get("outcome"), (m.get("episode") or {}).get("start_event_id"),
                        (m.get("episode") or {}).get("end_event_id"), m.get("t"), m.get("reference_t"), m.get("original_event_id"),
                        (m.get("quote_selection") or {}).get("event_id"), (m.get("quote_selection") or {}).get("start"),
                        (m.get("quote_selection") or {}).get("end")])
    return buf.getvalue()


def candidates_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["session_id", "participant_id", "study", "prompt_id", "persona", "condition", "candidate_id",
                "source", "trigger_event_id", "uptake_event_id", "type", "secondary_type", "insight_evidence", "strength", "persuasion",
                "video_offset_s",
                "trigger_quote", "uptake_quote", "rationale", "participant_response", "matched_marks",
                "researcher_label", "copying", "uptake", "passage_start", "passage_end"])
    for s in all_sessions():
        an = analysis(s)
        ev = {e["id"]: e for e in s["events"]}
        method = (s.get("detection") or {}).get("method")
        for c in s["candidates"]:
            if c.get("source") != "judge":
                continue  # historical rule outputs remain only in full JSON/history
            pr = c.get("participant_response") or {}
            w.writerow([s["id"], s["participant_id"], s["study"], s["prompt"]["id"], s["persona"], s["condition"],
                        c["id"], c.get("source", method), c["trigger_event_id"], c.get("uptake_event_id"), c["type"],
                        c.get("secondary_type"), c.get("insight_evidence"), c.get("strength"), c.get("persuasion"),
                        _offset(s, ev.get(c["trigger_event_id"])), _quick(c.get("trigger_quote")),
                        _quick(c.get("uptake_quote")), _quick(c.get("rationale"), 600), pr.get("answer"),
                        "|".join(l["mark"] for l in an["links"] if l["candidate"] == c["id"]),
                        (s["codes"].get(f"candidate:{c['id']}") or {}).get("label"), c.get("copying"), c.get("uptake"),
                        c.get("start_event_id"), c.get("end_event_id")])
    return buf.getvalue()


# --------------------------------------------------------------------------- HTTP

class StudyHTTPServer(ThreadingHTTPServer):
    # Browser SSE + file requests + rapid bookmarks can exceed the stdlib's
    # small default accept backlog even during a single local session.
    request_queue_size = 64


class Handler(BaseHTTPRequestHandler):
    server_version = "SparkAnnotator/0.2"

    def log_message(self, fmt, *args):
        if os.environ.get("SPARK_QUIET") != "1":
            super().log_message(fmt, *args)

    # ---- helpers
    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else (
            json.dumps(body, ensure_ascii=False).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 2 * 1024 * 1024:
            raise ValueError("JSON request too large")
        if not n:
            return {}
        return json.loads(self.rfile.read(n))

    def _static(self, rel):
        path = os.path.normpath(os.path.join(STATIC, rel))
        if os.path.commonpath([STATIC, path]) != STATIC or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, mimetypes.guess_type(path)[0] or "application/octet-stream")

    def _route(self, method):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        try:
            # Commands must not be triggerable by a random website visiting the
            # localhost development server (including DNS rebinding).
            host = self.headers.get("Host", "")
            if self.server.server_address[0] in ("127.0.0.1", "::1"):
                allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
                if host not in allowed:
                    return self._send(403, {"error": "Invalid local host"})
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != host:
                return self._send(403, {"error": "Cross-origin access is not allowed"})
            return self._dispatch(method, parts, query)
        except storage.ConflictError as exc:
            return self._send(409, {"error": str(exc)})
        except RuntimeUnavailable as exc:
            return self._send(503, {"error": str(exc)})
        except FileExistsError:
            return self._send(409, {"error": "A file with that name already exists; nothing was overwritten."})
        except KeyError:
            return self._send(404, {"error": "not found"})
        except StopIteration:
            return self._send(404, {"error": "record not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send(400, {"error": str(exc)})

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    # ---- screen recordings
    def _receive_chunk(self, sid, query):
        """Append one MediaRecorder chunk. Chunks must arrive in order; re-sent ones are ignored."""
        seq = int((query.get("seq") or ["-1"])[0])
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else b""
        with LOCK:
            s = load(sid)
            rec = s.get("recording") or {}
            if rec.get("status") != "recording":
                raise ValueError("recording not started")
            if seq < rec["chunks"]:
                return self._send(200, {"ok": True, "duplicate": True, "next": rec["chunks"]})
            if seq > rec["chunks"]:
                return self._send(409, {"error": "out of order", "next": rec["chunks"]})
            with open(os.path.join(RECORDINGS, f"{sid}.{rec['ext']}.part"), "ab") as f:
                f.write(data)
            rec["chunks"] += 1
            rec["bytes"] += len(data)
            s["recording"] = rec
            save(s)
        return self._send(200, {"ok": True, "next": rec["chunks"], "bytes": rec["bytes"]})

    def _receive_recording(self, sid):
        load(sid)  # 404 if unknown
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            raise ValueError("empty recording")
        ctype = self.headers.get("Content-Type") or "video/mp4"
        ext = "webm" if "webm" in ctype else "mp4"
        path = os.path.join(RECORDINGS, f"{sid}.{ext}")
        tmp = path + ".upload"
        left = n
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(tmp)
            raise ValueError("upload interrupted")
        os.replace(tmp, path)
        with LOCK:
            s = load(sid)
            rec = s.get("recording") or {}
            if self.headers.get("X-Recording-Start"):
                rec["started_t"] = float(self.headers["X-Recording-Start"])
                rec["started_at"] = parsers.parse_ts(rec["started_t"])[0]
            if self.headers.get("X-Duration"):
                rec["duration"] = float(self.headers["X-Duration"])
            rec.update({"status": "uploaded", "file": os.path.basename(path), "bytes": n, "uploaded_at": now()[0],
                        "mime": ctype, "ext": ext})
            s["recording"] = rec
            save(s)
        return self._send(200, rec)

    def _serve_recording(self, s):
        rec = s.get("recording") or {}
        if not rec.get("file"):
            return self._send(404, {"error": "no recording"})
        path = os.path.join(RECORDINGS, rec["file"])
        size = os.path.getsize(path)
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        start, end = 0, size - 1
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = size - int(rng.group(2))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", (rec.get("mime") or "video/mp4").split(";")[0])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the player cancels range requests while seeking

    # ---- routes
    def _stream(self, sid, query):
        load(sid)
        after = max(0, int(self.headers.get("Last-Event-ID") or (query.get("after") or ["0"])[0]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        def emit(cursor=None):
            s = load(sid)
            payload = {"events": s["events"], "runtime": s.get("runtime"), "revision": s["_revision"]}
            prefix = f"id: {cursor}\n" if cursor is not None else ""
            self.wfile.write((prefix + "event: session\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
        try:
            emit()  # refreshing recovers the saved projection, not a new turn
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and not getattr(self.server, "stream_shutdown", False):
                messages = get_store().feed(sid, after)
                if messages:
                    after = messages[-1]["seq"]
                    emit(after)
                else:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                time.sleep(0.15)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dispatch(self, method, parts, query):
        pages = {"": "index.html", "session": "session.html", "researcher": "researcher.html", "review": "review.html",
                 "record": "record.html", "validation": "validation.html"}
        if method == "GET" and (not parts or (len(parts) == 1 and parts[0] in pages)):
            return self._static(pages["" if not parts else parts[0]])
        if parts and parts[0] == "static":
            return self._static("/".join(parts[1:]))
        if not parts or parts[0] != "api":
            return self._send(404, {"error": "not found"})
        api = parts[1:]

        if api == ["config"] and method == "GET":
            return self._send(200, {
                "taxonomy": TAXONOMY, "studies": list(STUDIES.values()),
                "personas": [{"key": p["key"], "label": p["label"]} for p in PERSONAS.values()],
                "llm": llm.status(),
                "runtime": get_runtime().status(),
                "runtimes": get_runtime().statuses() if hasattr(get_runtime(),"statuses") else {"codex":get_runtime().status()},
                "judge": {**judge.status(), "automatic": auto_judge_enabled()},
            })
        if api == ["sessions"]:
            if method == "GET":
                return self._send(200, [summary_row(s) for s in sorted(all_sessions(), key=lambda s: s["created_at"], reverse=True)])
            if method == "POST":
                body = self._json()
                if (not STUDIES.get(body.get("study"), {}).get("external") and body.get("condition", "ai") == "ai"
                        and body.get("runtime", "codex") in AGENT_KINDS and self.server.server_address[0] not in ("127.0.0.1", "::1")):
                    raise RuntimeUnavailable("Agent execution is localhost-only until deployment isolation and authentication are implemented.")
                return self._send(201, create_session(body))
        if api == ["upload"] and method == "POST":
            name = os.path.basename(self.headers.get("X-Filename") or "upload.jsonl")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 300 * 1024 * 1024:
                return self._send(413, {"error": "file too large"})
            data = self.rfile.read(n)
            uid = uuid.uuid4().hex[:12]
            with open(os.path.join(UPLOADS, uid), "wb") as f:
                f.write(data)
            with open(os.path.join(UPLOADS, uid + ".name"), "w") as f:
                f.write(name)
            return self._send(200, {"upload_id": uid, "filename": name, "entries": parsers.inspect_upload(name, data)})
        if api[:1] == ["export"] and method == "GET":
            if api[1:] == ["marks.csv"]:
                return self._send(200, marks_csv(), "text/csv", {"Content-Disposition": "attachment; filename=marks.csv"})
            if api[1:] == ["candidates.csv"]:
                return self._send(200, candidates_csv(), "text/csv", {"Content-Disposition": "attachment; filename=candidates.csv"})

        if api == ["validations"]:
            if method == "GET":
                return self._send(200, [judge_validation.list_row(sample, codes)
                                       for sample, codes in get_store().validation_samples()])
            if method == "POST":
                body = self._json()
                session_ids = list(dict.fromkeys(body.get("session_ids") or []))
                if not session_ids or any(not re.fullmatch(r"s_[0-9a-f]{8}", sid or "") for sid in session_ids):
                    raise ValueError("Select one or more valid sessions")
                sample = judge_validation.create_sample([load(sid) for sid in session_ids], body,
                                                        [t["key"] for t in TAXONOMY["types"]])
                get_store().create_validation_sample(sample)
                return self._send(201, {
                    **judge_validation.list_row(sample, []),
                    "coder_links": [{"id": c["id"], "name": c["name"], "token": c["token"]}
                                    for c in sample["coders"]],
                })
        if len(api) >= 2 and api[0] == "validations":
            validation_id, rest = api[1], api[2:]
            if not re.fullmatch(r"vs_[0-9a-f]{12}", validation_id or ""):
                raise KeyError(validation_id)
            sample, codes = get_store().validation_sample(validation_id)
            token = (query.get("token") or [""])[0]
            if method == "GET" and not rest:
                if token:
                    return self._send(200, judge_validation.coder_view(sample, codes, token))
                if (query.get("view") or [""])[0] != "researcher":
                    raise ValueError("A coder token or researcher view is required")
                result = judge_validation.results(sample, codes)
                result["coder_links"] = [{"id": c["id"], "name": c["name"], "token": c["token"]}
                                         for c in sample["coders"]]
                return self._send(200, result)
            if method == "GET" and rest == ["export.csv"]:
                return self._send(200, judge_validation.export_csv(sample, codes), "text/csv",
                                  {"Content-Disposition": f"attachment; filename={validation_id}.csv"})
            if method == "POST" and rest == ["codes"]:
                body = self._json()
                coder_id, item_id, code = judge_validation.validate_code(sample, token, body)
                saved = get_store().save_validation_code(validation_id, item_id, coder_id, code)
                return self._send(200, saved)
            return self._send(404, {"error": "not found"})

        if len(api) >= 2 and api[0] == "sessions":
            sid = api[1]
            rest = api[2:]
            return self._session_route(method, sid, rest, query)
        return self._send(404, {"error": "not found"})

    def _session_route(self, method, sid, rest, query):
        if rest[:1] == ["runtime"] and self.server.server_address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeUnavailable("Agent execution is only enabled on localhost in this prototype.")
        if method == "GET" and rest == ["stream"]:
            return self._stream(sid, query)
        if method == "GET" and rest == ["workspace", "files"]:
            load(sid)
            return self._send(200, get_runtime().files(sid))
        if method == "GET" and rest == ["workspace", "file"]:
            load(sid)
            path = get_runtime().file(sid, (query.get("path") or [""])[0])
            if path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("File too large for this prototype's preview/download")
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            return self._send(200, path.read_bytes(), ctype, {"Content-Disposition": "attachment; filename*=UTF-8''" + __import__("urllib.parse", fromlist=["quote"]).quote(path.name), "X-Content-Type-Options": "nosniff"})
        if method == "POST" and rest == ["workspace", "files"]:
            s = load(sid)
            if s["phase"] not in ("pre", "chat") or s.get("runtime", {}).get("kind") not in AGENT_KINDS:
                raise ValueError("Workspace uploads are only available before/during agent conversations")
            if s["runtime"].get("active_turn") or s["runtime"].get("status") == "starting":
                raise ValueError("Wait for the agent to finish before changing its files")
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 5 * 1024 * 1024:
                raise ValueError("Maximum workspace upload is 5 MB")
            data = self.rfile.read(n)
            if len(data) != n:
                raise ValueError("Upload interrupted")
            return self._send(201, get_runtime().upload(sid, self.headers.get("X-Filename", ""), data))
        if method == "GET" and not rest:
            s = load(sid)
            s["analysis"] = analysis(s)
            if (query.get("view") or [""])[0] != "researcher" and not s.get("independent_review"):
                s["candidates"] = []
                s["judge"] = {"status": (s.get("judge") or {}).get("status", "not_run")}
                s["analysis"] = {"marks": len(s["marks"]), "links": []}
                s["analysis_runs"] = []
            s.pop("parse_turns", None)
            s.pop("detection", None)
            s.pop("trace", None)
            return self._send(200, s)
        if method == "GET" and rest == ["poll"]:
            s = load(sid)
            after = int((query.get("after") or ["0"])[0])
            return self._send(200, {"phase": s["phase"], "events": s["events"][after:],
                                    "judge": {"status": (s.get("judge") or {}).get("status", "not_run")}, "marks": len(s["marks"])})
        if method == "GET" and rest == ["recording"]:
            return self._serve_recording(load(sid))
        if method == "POST" and rest == ["recording"]:
            return self._receive_recording(sid)
        if method == "POST" and rest == ["recording", "chunk"]:
            return self._receive_chunk(sid, query)
        if method == "GET" and rest == ["judge", "estimate"]:
            return self._send(200, {**judge.estimate(load(sid)), "available": judge.status()})
        if method == "GET" and rest in (["transcript.md"], ["transcript.txt"]):
            s = load(sid)
            body = transcript.render_markdown(s, TAXONOMY) if rest[0].endswith(".md") else transcript.render_for_judge(s)
            return self._send(200, body, "text/markdown" if rest[0].endswith(".md") else "text/plain",
                              {"Content-Disposition": f"inline; filename={sid}-{rest[0]}"})
        if method == "GET" and rest == ["export.json"]:
            s = get_store().export(sid)
            s["analysis"] = analysis(s)
            return self._send(200, s, "application/json",
                              {"Content-Disposition": f"attachment; filename={sid}.json"})

        body = self._json() if method in ("POST", "PATCH") else {}

        if method == "POST" and rest == ["recording", "start"]:
            with LOCK:
                s = load(sid)
                mime = str(body.get("mime") or "video/webm")
                s["recording"] = {"status": "recording", "started_t": float(body["started_t"]),
                                  "started_at": parsers.parse_ts(float(body["started_t"]))[0],
                                  "mime": mime, "ext": "mp4" if "mp4" in mime else "webm", "chunks": 0, "bytes": 0,
                                  **{k: body[k] for k in ("width", "height", "fps", "surface", "browser") if k in body}}
                part = os.path.join(RECORDINGS, f"{sid}.{s['recording']['ext']}.part")
                if os.path.exists(part):
                    os.remove(part)
                save(s)
            return self._send(200, s["recording"])

        if method == "POST" and rest == ["recording", "finish"]:
            with LOCK:
                s = load(sid)
                rec = s.get("recording") or {}
                if not rec.get("ext"):
                    raise ValueError("no recording in progress")
                part = os.path.join(RECORDINGS, f"{sid}.{rec['ext']}.part")
                final = os.path.join(RECORDINGS, f"{sid}.{rec['ext']}")
                if os.path.exists(part):
                    os.replace(part, final)
                if not os.path.exists(final):
                    raise ValueError("no recording data received")
                rec.update(status="uploaded", file=os.path.basename(final), bytes=os.path.getsize(final),
                           duration=float(body.get("duration") or 0), ended_at=now()[0])
                s["recording"] = rec
                save(s)
            return self._send(200, rec)

        if method == "POST" and rest == ["advance"]:
            with LOCK:
                s = load(sid)
                if s.get("runtime", {}).get("active_turn") or s.get("runtime", {}).get("status") == "starting":
                    raise ValueError("Stop the agent or wait for it to finish before leaving the conversation")
                nxt = advance(s, body.get("phase"), body.get("text"))
                save(s)
            if body.get("phase") in ("chat", "post"):
                ensure_judge(sid)
            return self._send(200, {"phase": nxt})

        if method == "POST" and rest == ["message"]:
            text = (body.get("text") or "").strip()
            if not text:
                raise ValueError("empty message")
            s = load(sid)
            if s.get("runtime", {}).get("kind") in AGENT_KINDS:
                if self.server.server_address[0] not in ("127.0.0.1", "::1"):
                    raise RuntimeUnavailable("Agent execution is localhost-only")
                if s["phase"] != "chat":
                    raise ValueError("conversation phase is over")
                client_id = body.get("client_message_id")
                if not isinstance(client_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", client_id):
                    raise ValueError("A stable client_message_id is required")
                ev = get_runtime().get(sid).message(text, client_id, body.get("compose_ms"))
                return self._send(202, {"events": [ev], "runtime": load(sid)["runtime"]})
            with LOCK:
                s = load(sid)
                if s["phase"] != "chat":
                    raise ValueError("conversation phase is over")
                ev = add_event(s, "user", text, draft_ms=body.get("compose_ms"))
                save(s)
                if s["condition"] == "human":
                    for peer in room_peers(s):
                        add_event(peer, "partner", text, from_session=sid)
                        save(peer)
            if s["condition"] == "human":
                return self._send(200, {"events": [ev]})
            reply = ai_reply(sid)
            return self._send(200, {"events": [ev, reply]})

        if method == "POST" and rest == ["runtime", "connect"]:
            s = load(sid)
            if s["phase"] != "chat":
                raise ValueError("Agents can only connect during the conversation phase")
            get_runtime().get(sid)
            return self._send(200, load(sid)["runtime"])
        if method == "POST" and rest == ["runtime", "interrupt"]:
            get_runtime().get(sid).interrupt()
            return self._send(200, {"ok": True})
        if method == "POST" and rest == ["runtime", "approval"]:
            s = load(sid)
            if s["phase"] != "chat":
                raise ValueError("Conversation phase is over")
            worker = get_runtime().get(sid)
            if s["runtime"]["kind"] == "claude":
                worker.approve(body.get("id"),body.get("decision"),body.get("answers"))
            else:
                worker.approve(body.get("id"),body.get("decision"))
            return self._send(200, {"ok": True})

        if method == "POST" and rest == ["marks"]:
            with LOCK:
                s = load(sid)
                cid = body.get("id")
                if cid and not re.fullmatch(r"m_[0-9a-f]{8,32}", cid):
                    raise ValueError("bad mark id")
                existing = next((m for m in s["marks"] if cid and m["id"] == cid), None)
                if existing:  # idempotent re-sync from the overlay
                    if int(body.get("client_version", 1)) > existing.get("client_version", 0):
                        if s["phase"] == "done" or s.get("independent_review"):
                            raise ValueError("Review is locked; bookmark changes cannot overwrite it")
                        for key in ("quick_type", "deleted"):
                            if key in body:
                                existing[key] = body[key]
                        existing["client_version"] = int(body.get("client_version", 1))
                    save(s)
                    return self._send(200, existing)
                if s.get("independent_review") and body.get("source") != "candidate_confirm":
                    raise ValueError("Independent review is locked; new moments must be recorded as follow-up suggestions")
                m = annotations.create_mark(s, body, cid or new_id("m"))
                save(s)
            return self._send(201, m)

        if method == "PATCH" and len(rest) == 2 and rest[0] == "marks":
            with LOCK:
                s = load(sid)
                m = next(m for m in s["marks"] if m["id"] == rest[1])
                annotations.patch_mark(s, m, body, {t["key"] for t in TAXONOMY["types"]})
                save(s)
            return self._send(200, m)

        if method == "POST" and rest == ["review", "complete-independent"]:
            with LOCK:
                s = load(sid)
                snapshot = annotations.complete_independent_review(s)
                save(s)
            return self._send(200, snapshot)

        if method == "PATCH" and len(rest) == 2 and rest[0] == "candidates":
            with LOCK:
                s = load(sid)
                if not s.get("independent_review"):
                    raise ValueError("Complete independent review before responding to suggestions")
                if s["phase"] != "review":
                    raise ValueError("Suggestion responses are locked after review")
                c = next(c for c in s["candidates"] if c["id"] == rest[1])
                if "participant_response" in body:
                    c["participant_response"] = dict(body["participant_response"], at=now()[0])
                save(s)
            return self._send(200, c)

        if method == "POST" and rest in (["judge"], ["judge", "ensure"]):
            if body:
                raise ValueError("Judge takes no participant hints or supplied predictions")
            state = ensure_judge(sid, manual=rest == ["judge"])
            return self._send(202 if state["status"] in ("queued", "running") else 200, state)

        if method == "POST" and rest == ["code"]:
            key = f"{body['kind']}:{body['target']}"
            with LOCK:
                s = load(sid)
                s["codes"][key] = {"label": body.get("label"), "notes": body.get("notes", ""),
                                   "coder": body.get("coder", ""), "at": now()[0]}
                save(s)
            return self._send(200, s["codes"][key])

        if method == "POST" and rest == ["import"]:
            if load(sid).get("runtime",{}).get("kind") in AGENT_KINDS:
                raise ValueError("Integrated agent transcripts cannot be replaced with imported logs. Create an external session instead.")
            uid = body.get("upload_id", "")
            if not re.fullmatch(r"[0-9a-f]{12}", uid):
                raise ValueError("bad upload id")
            with open(os.path.join(UPLOADS, uid), "rb") as f:
                data = f.read()
            with open(os.path.join(UPLOADS, uid + ".name")) as f:
                name = f.read()
            members = body.get("members") or [body.get("member")]
            events, meta = [], {}
            for mem in members:
                ev, meta = parsers.load_member(name, data, mem)
                for e in ev:
                    e["meta"]["source"] = mem
                events += ev
            with LOCK:
                s = load(sid)
                if s.get("runtime", {}).get("kind") in AGENT_KINDS:
                    raise ValueError("Integrated agent transcripts cannot be replaced with imported logs. Create an external session instead.")
                imported = storage.import_events(s, events, name, bool(body.get("append")))
                parsers.align_marks(s["marks"], imported)
                s["imports"].append({"at": now()[0], "file": name, "members": members, "meta": meta,
                                     "events": len(imported)})
                save(s)
            ensure_judge(sid)
            return self._send(200, {"events": len(events), "meta": meta,
                                    "aligned": sum(1 for m in s["marks"] if m.get("event_id"))})

        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    get_store()
    print(f"Spark Annotator on http://{a.host}:{a.port} · SQLite storage · default runtime: Codex")
    httpd = StudyHTTPServer((a.host, a.port), Handler)
    recover_judges()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.stream_shutdown = True
        httpd.server_close()
        if _RUNTIME:
            _RUNTIME.close()


if __name__ == "__main__":
    main()
