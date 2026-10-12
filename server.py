"""Spark Annotator: local study server.

    python3 server.py                  # http://127.0.0.1:8765
    python3 server.py --host 0.0.0.0   # let lab machines on the LAN connect

Storage is one JSON file per session under data/sessions/. No database.
"""

import argparse
import csv
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

import detector
import judge
import llm
import parsers
import transcript

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
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
CODEBOOK = _load_config("spark_codebook.json")
LOCK = threading.RLock()


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
    with open(_path(sid)) as f:
        return json.load(f)


def save(s):
    p = _path(s["id"])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


def all_sessions():
    out = []
    for name in sorted(os.listdir(SESSIONS)):
        if name.endswith(".json"):
            try:
                out.append(load(name[:-5]))
            except (KeyError, ValueError):
                pass
    return out


# --------------------------------------------------------------------------- session logic

PHASE_ORDER = ["pre", "chat", "post", "review", "done"]


def phases_for(study):
    order = [p for p in ("pre", "chat", "post") if p in study["phases"]]
    return order + ["review", "done"]


def create_session(body):
    study = STUDIES.get(body.get("study"))
    if not study:
        raise ValueError("unknown study")
    prompt = next((p for p in study["prompts"] if p["id"] == body.get("prompt_id")), study["prompts"][0])
    persona = body.get("persona") or study["default_persona"]
    if persona not in PERSONAS:
        raise ValueError("unknown persona")
    condition = body.get("condition", "ai")
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
    }
    if body.get("task"):
        s["prompt"] = dict(prompt, text=body["task"].strip()[:2000])
    s["phases"][order[0]]["started_at"] = iso
    save(s)
    return s


def advance(s, phase, text=None):
    if phase != s["phase"]:
        raise ValueError(f"session is in phase {s['phase']}, not {phase}")
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
    ev = {"id": f"e{len(s['events']) + 1}", "ts": iso, "t": t, "role": role, "text": text,
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
    p = PERSONAS[s["persona"]]["system"]
    return f"{p}\n\nThe person is thinking about this question:\n{s['prompt']['text']}"


def ai_reply(sid):
    with LOCK:
        s = load(sid)
        hist = llm_history(s)
        sys_p = system_prompt(s)
    t0 = time.time()
    meta = {}
    if llm.status()["live"]:
        try:
            text = llm.chat(sys_p, hist)
            meta = {"model": llm.MODEL}
        except llm.LLMError as exc:
            text = f"(The AI partner is unavailable right now: {exc}. Please tell the researcher.)"
            meta = {"error": str(exc)}
    else:
        text = llm.demo_reply(s["persona"], hist)
        meta = {"demo": True}
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


def context_for(s):
    return {"task": s["prompt"]["text"], "pre": (s["writing"].get("pre") or {}).get("text", ""),
            "partner_kind": "human" if s["condition"] == "human" else "ai"}


def run_detection(sid):
    with LOCK:
        s = load(sid)
        s["detection"] = {"status": "running", "started_at": now()[0]}
        save(s)
        events, ctx = list(s["events"]), context_for(s)
    try:
        cands, meta, turns = detector.detect(events, ctx)
        status = "done"
    except Exception as exc:  # keep the study running whatever happens
        cands, meta, turns, status = [], {"error": repr(exc)}, [], "error"
    with LOCK:
        s = load(sid)
        _merge_candidates(s, cands, "codebook")
        s["parse_turns"] = turns
        s["detection"] = dict(meta, status=status, finished_at=now()[0], n=len(cands))
        save(s)


def _merge_candidates(s, cands, source):
    """Replace one source's candidates, keeping ids and participant answers where they match."""
    old = {(c["trigger_event_id"], c["type"]): c for c in s["candidates"] if c.get("source", "codebook") == source}
    for c in cands:
        prev = old.get((c["trigger_event_id"], c["type"]))
        c["source"] = source
        c["id"] = prev["id"] if prev else new_id("c")
        c["participant_response"] = prev.get("participant_response") if prev else None
    s["candidates"] = [c for c in s["candidates"] if c.get("source", "codebook") != source] + cands


def run_judge(sid, hints=False):
    with LOCK:
        s = load(sid)
        s["judge"] = {"status": "running", "started_at": now()[0], "hints": hints, **judge.estimate(s)}
        save(s)
    try:
        cands, meta = judge.run(s, hints=hints)
        status, err = "done", None
    except llm.LLMError as exc:
        cands, meta, status, err = None, {}, "error", str(exc)
    except Exception as exc:  # keep the server alive whatever happens
        cands, meta, status, err = None, {}, "error", repr(exc)
    with LOCK:
        s = load(sid)
        if cands is not None:
            _merge_candidates(s, cands, "judge")
        s["judge"] = dict(s.get("judge") or {}, **meta, status=status, error=err, finished_at=now()[0],
                          n=len(cands) if cands is not None else None)
        save(s)


def detect_async(sid):
    threading.Thread(target=run_detection, args=(sid,), daemon=True).start()


def event_index(s):
    return {e["id"]: i for i, e in enumerate(s["events"])}


def _near(idx, a_ids, b_ids, tol):
    ai = [idx.get(x) for x in a_ids if x]
    bi = [idx.get(x) for x in b_ids if x]
    return any(x is not None and y is not None and abs(x - y) <= tol for x in ai for y in bi)


def analysis(s, tol=1):
    """Triangulate participant flags, codebook candidates and LLM-judge candidates
    (and before/after attributions). Two items agree when their trigger/uptake events are
    within `tol` events of each other."""
    idx = event_index(s)
    marks = [m for m in s["marks"] if not m.get("deleted")]
    links = []
    for m in marks:
        for c in s["candidates"]:
            if m.get("event_id") and _near(idx, [m["event_id"]], [c["trigger_event_id"], c.get("uptake_event_id")], tol):
                links.append({"mark": m["id"], "candidate": c["id"], "source": c.get("source", "codebook")})
    by_src = {src: [c for c in s["candidates"] if c.get("source", "codebook") == src] for src in ("codebook", "judge")}
    pair_any = pair_type = 0
    for j in by_src["judge"]:
        near = [c for c in by_src["codebook"] if _near(idx, [j["trigger_event_id"], j.get("uptake_event_id")],
                                                       [c["trigger_event_id"], c.get("uptake_event_id")], tol)]
        pair_any += bool(near)
        pair_type += any(c["type"] == j["type"] for c in near)
    linked_marks = lambda src: len({l["mark"] for l in links if l["source"] == src})  # noqa: E731
    traced = set()
    for u in (s.get("trace") or {}).get("units", []):
        traced.update(u.get("source_event_ids", []))
    types = [t["key"] for t in TAXONOMY["types"]]
    by_type = {t: {"marks": 0, "candidates": 0, "codebook": 0, "judge": 0} for t in types}
    for m in marks:
        t = ((m.get("review") or {}).get("primary")) or m.get("quick_type")
        if t in by_type:
            by_type[t]["marks"] += 1
    for c in s["candidates"]:
        if c["mechanism"] == "spark" and c["type"] in by_type:
            by_type[c["type"]]["candidates"] += 1
            by_type[c["type"]][c.get("source", "codebook")] += 1
    return {
        "marks": len(marks), "candidates": len(s["candidates"]),
        "codebook": len(by_src["codebook"]), "judge": len(by_src["judge"]),
        "matched_marks": len({l["mark"] for l in links}),
        "matched_candidates": len({l["candidate"] for l in links}),
        "marks_found_by": {"codebook": linked_marks("codebook"), "judge": linked_marks("judge")},
        "judge_vs_codebook": {"judge_with_codebook_nearby": pair_any, "same_type": pair_type},
        "links": links, "traced_events": sorted(traced, key=lambda e: idx.get(e, 0)), "by_type": by_type,
        "tolerance_events": tol,
    }


def summary_row(s):
    marks = [m for m in s["marks"] if not m.get("deleted")]
    return {
        "id": s["id"], "participant_id": s["participant_id"], "created_at": s["created_at"],
        "study": s["study"], "prompt": s["prompt"]["title"], "persona": s["persona"],
        "condition": s["condition"], "room": s.get("room"), "phase": s["phase"],
        "events": len(s["events"]), "marks": len(marks),
        "reviewed": sum(1 for m in marks if m.get("review")),
        "candidates": len(s["candidates"]),
        "detection": (s.get("detection") or {}).get("method"),
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


def parse_csv():
    """Per-turn features from the codebook parser (word-level novelty, signals, fired rules)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["session_id", "participant_id", "event_id", "role", "video_offset_s", "words", "signals",
                "new_terms", "adopted_terms", "rules_fired", "copy_overlap", "deferent"])
    for s in all_sessions():
        ev = {e["id"]: e for e in s["events"]}
        for r in s.get("parse_turns") or []:
            w.writerow([s["id"], s["participant_id"], r["event_id"], r["role"], _offset(s, ev.get(r["event_id"])),
                        r["words"], "|".join(r["signals"]), "|".join(r["new_terms"][:40]), "|".join(r["adopted_terms"]),
                        "|".join(r["rules"]), r.get("copy_overlap"), r.get("deferent")])
    return buf.getvalue()


def marks_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["session_id", "participant_id", "study", "prompt_id", "persona", "condition", "mark_id", "created_at",
                "video_offset_s", "source", "anchor", "event_id", "event_role", "event_text", "quote", "quick_type", "review_types",
                "review_primary", "uptake", "latency", "intensity", "before", "after", "matched_candidates",
                "researcher_label"])
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
                        (s["codes"].get(f"mark:{m['id']}") or {}).get("label")])
    return buf.getvalue()


def candidates_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["session_id", "participant_id", "study", "prompt_id", "persona", "condition", "candidate_id",
                "source", "trigger_event_id", "uptake_event_id", "type", "secondary_type", "mechanism", "score", "rules",
                "video_offset_s",
                "trigger_quote", "uptake_quote", "rationale", "participant_response", "matched_marks",
                "researcher_label"])
    for s in all_sessions():
        an = analysis(s)
        ev = {e["id"]: e for e in s["events"]}
        method = (s.get("detection") or {}).get("method")
        for c in s["candidates"]:
            pr = c.get("participant_response") or {}
            w.writerow([s["id"], s["participant_id"], s["study"], s["prompt"]["id"], s["persona"], s["condition"],
                        c["id"], c.get("source", method), c["trigger_event_id"], c.get("uptake_event_id"), c["type"],
                        c.get("secondary_type"), c["mechanism"], c.get("score") if c.get("score") is not None else c.get("confidence"),
                        "|".join(r["rule"] for r in c.get("rules", [])), _offset(s, ev.get(c["trigger_event_id"])), _quick(c.get("trigger_quote")),
                        _quick(c.get("uptake_quote")), _quick(c.get("rationale"), 600), pr.get("answer"),
                        "|".join(l["mark"] for l in an["links"] if l["candidate"] == c["id"]),
                        (s["codes"].get(f"candidate:{c['id']}") or {}).get("label")])
    return buf.getvalue()


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "SparkAnnotator/0.1"

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
        if not n:
            return {}
        return json.loads(self.rfile.read(n))

    def _static(self, rel):
        path = os.path.normpath(os.path.join(STATIC, rel))
        if not path.startswith(STATIC) or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, mimetypes.guess_type(path)[0] or "application/octet-stream")

    def _route(self, method):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        try:
            return self._dispatch(method, parts, query)
        except KeyError:
            return self._send(404, {"error": "not found"})
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
    def _dispatch(self, method, parts, query):
        pages = {"": "index.html", "session": "session.html", "researcher": "researcher.html", "review": "review.html",
                 "record": "record.html"}
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
                "codebook": {"version": CODEBOOK["version"], "rules": len(CODEBOOK["rules"])},
                "judge": {**llm.status(), "model": llm.JUDGE_MODEL, "effort": llm.JUDGE_EFFORT},
            })
        if api == ["sessions"]:
            if method == "GET":
                return self._send(200, [summary_row(s) for s in sorted(all_sessions(), key=lambda s: s["created_at"], reverse=True)])
            if method == "POST":
                return self._send(201, create_session(self._json()))
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
            if api[1:] == ["parse.csv"]:
                return self._send(200, parse_csv(), "text/csv", {"Content-Disposition": "attachment; filename=parse.csv"})
            if api[1:] == ["candidates.csv"]:
                return self._send(200, candidates_csv(), "text/csv", {"Content-Disposition": "attachment; filename=candidates.csv"})

        if len(api) >= 2 and api[0] == "sessions":
            sid = api[1]
            rest = api[2:]
            return self._session_route(method, sid, rest, query)
        return self._send(404, {"error": "not found"})

    def _session_route(self, method, sid, rest, query):
        if method == "GET" and not rest:
            s = load(sid)
            s["analysis"] = analysis(s)
            return self._send(200, s)
        if method == "GET" and rest == ["poll"]:
            s = load(sid)
            after = int((query.get("after") or ["0"])[0])
            return self._send(200, {"phase": s["phase"], "events": s["events"][after:],
                                    "detection": s.get("detection"), "judge": s.get("judge"), "marks": len(s["marks"])})
        if method == "GET" and rest == ["recording"]:
            return self._serve_recording(load(sid))
        if method == "POST" and rest == ["recording"]:
            return self._receive_recording(sid)
        if method == "POST" and rest == ["recording", "chunk"]:
            return self._receive_chunk(sid, query)
        if method == "GET" and rest == ["judge", "estimate"]:
            return self._send(200, {**judge.estimate(load(sid)), "available": llm.status()})
        if method == "GET" and rest in (["transcript.md"], ["transcript.txt"]):
            s = load(sid)
            body = transcript.render_markdown(s, TAXONOMY) if rest[0].endswith(".md") else transcript.render_for_judge(s)
            return self._send(200, body, "text/markdown" if rest[0].endswith(".md") else "text/plain",
                              {"Content-Disposition": f"inline; filename={sid}-{rest[0]}"})
        if method == "GET" and rest == ["export.json"]:
            s = load(sid)
            s["analysis"] = analysis(s)
            return self._send(200, json.dumps(s, indent=1, ensure_ascii=False), "application/json",
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
                nxt = advance(s, body.get("phase"), body.get("text"))
                save(s)
            if nxt == "review":
                cfg = STUDIES[s["study"]]
                if cfg.get("show_candidates_in_review") and s["events"]:
                    detect_async(sid)
            return self._send(200, {"phase": nxt})

        if method == "POST" and rest == ["message"]:
            text = (body.get("text") or "").strip()
            if not text:
                raise ValueError("empty message")
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

        if method == "POST" and rest == ["marks"]:
            iso, t = now()
            with LOCK:
                s = load(sid)
                cid = body.get("id")
                if cid and not re.fullmatch(r"m_[0-9a-f]{8,32}", cid):
                    raise ValueError("bad mark id")
                existing = next((m for m in s["marks"] if cid and m["id"] == cid), None)
                if existing:  # idempotent re-sync from the overlay
                    for key in ("quick_type", "deleted"):
                        if key in body:
                            existing[key] = body[key]
                    save(s)
                    return self._send(200, existing)
                ids = {e["id"] for e in s["events"]}
                eid = body.get("event_id") if body.get("event_id") in ids else None
                if eid is None and s["events"] and not s["external"]:
                    eid = next((e["id"] for e in reversed(s["events"]) if e["role"] in ("assistant", "partner")),
                               s["events"][-1]["id"])
                ct = body.get("client_t")
                mt = float(body["t"]) if body.get("t") else (float(ct) / 1000 if ct else t)
                m = {"id": cid or new_id("m"), "created_at": parsers.parse_ts(mt)[0] or iso, "t": mt,
                     "phase": s["phase"], "source": body.get("source", "overlay"),
                     "anchor": body.get("anchor", "latest"), "event_id": eid,
                     "quote": (body.get("quote") or "")[:2000] or None,
                     "quick_type": body.get("quick_type"), "draft_len": body.get("draft_len"),
                     "review": None}
                s["marks"].append(m)
                save(s)
            return self._send(201, m)

        if method == "PATCH" and len(rest) == 2 and rest[0] == "marks":
            with LOCK:
                s = load(sid)
                m = next(m for m in s["marks"] if m["id"] == rest[1])
                for key in ("quick_type", "event_id", "deleted"):
                    if key in body:
                        m[key] = body[key]
                if "review" in body:
                    m["review"] = dict(body["review"], reviewed_at=now()[0])
                save(s)
            return self._send(200, m)

        if method == "PATCH" and len(rest) == 2 and rest[0] == "candidates":
            with LOCK:
                s = load(sid)
                c = next(c for c in s["candidates"] if c["id"] == rest[1])
                if "participant_response" in body:
                    c["participant_response"] = dict(body["participant_response"], at=now()[0])
                save(s)
            return self._send(200, c)

        if method == "POST" and rest == ["detect"]:
            run_detection(sid)
            s = load(sid)
            return self._send(200, {"detection": s["detection"], "candidates": s["candidates"]})

        if method == "POST" and rest == ["judge"]:
            s = load(sid)
            if not llm.status()["live"]:
                raise ValueError(f"LLM judge unavailable: {llm.status()['reason']}")
            if not s["events"]:
                raise ValueError("No transcript to judge yet")
            if (s.get("judge") or {}).get("status") == "running":
                raise ValueError("The judge is already running for this session")
            threading.Thread(target=run_judge, args=(sid, bool(body.get("hints"))), daemon=True).start()
            return self._send(202, {"status": "running", **judge.estimate(s)})

        if method == "POST" and rest == ["trace"]:
            s = load(sid)
            pre = (s["writing"].get("pre") or {}).get("text", "")
            post = (s["writing"].get("post") or {}).get("text", "")
            if not post:
                raise ValueError("no final write-up to trace")
            out = detector.trace_diff(pre, post, s["events"], context_for(s))
            with LOCK:
                s = load(sid)
                s["trace"] = dict(out, at=now()[0])
                save(s)
            return self._send(200, s["trace"])

        if method == "POST" and rest == ["code"]:
            key = f"{body['kind']}:{body['target']}"
            with LOCK:
                s = load(sid)
                s["codes"][key] = {"label": body.get("label"), "notes": body.get("notes", ""),
                                   "coder": body.get("coder", ""), "at": now()[0]}
                save(s)
            return self._send(200, s["codes"][key])

        if method == "POST" and rest == ["import"]:
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
            if body.get("append"):
                events = load(sid)["events"] + events
            events.sort(key=lambda e: (e.get("t") is None, e.get("t") or 0))
            for i, e in enumerate(events):
                e["id"] = f"e{i + 1}"
            with LOCK:
                s = load(sid)
                s["events"] = events
                for m in s["marks"]:
                    m["event_id"] = None
                s["candidates"] = []
                s["judge"] = None
                parsers.align_marks(s["marks"], events)
                s["imports"].append({"at": now()[0], "file": name, "members": members, "meta": meta,
                                     "events": len(events)})
                save(s)
            if s["phase"] in ("review", "done") and events:
                detect_async(sid)
            return self._send(200, {"events": len(events), "meta": meta,
                                    "aligned": sum(1 for m in s["marks"] if m.get("event_id"))})

        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    st = llm.status()
    mode = f"LIVE ({st['model']})" if st["live"] else f"DEMO ({st['reason']})"
    print(f"Spark Annotator on http://{a.host}:{a.port}  ·  LLM: {mode}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
