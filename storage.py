"""Transactional study storage with stable identities and append-only revisions.

The old session-shaped API is a projection, not the persistence format. Conversation
events, bookmarks, participant reviews, researcher codes, and predictions are stored
separately. Legacy JSON files are imported once and are never modified or removed.
"""

import copy
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class ConflictError(ValueError):
    """A stale session projection must be reloaded before writing."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    COLLECTIONS = {"events", "marks", "candidates", "codes", "analysis_runs",
                   "researcher_annotations", "_revision", "analysis"}

    def __init__(self, path, write_lock=None):
        # The compatibility server still has read/modify/save blocks. Sharing
        # their lock prevents runtime deltas interleaving inside such a block;
        # SQLite transactions and revision checks also protect other processes.
        self.write_lock = write_lock or threading.RLock()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    session_id TEXT NOT NULL REFERENCES sessions(id), kind TEXT NOT NULL,
                    id TEXT NOT NULL, ordinal INTEGER NOT NULL, active INTEGER NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY(session_id, kind, id));
                CREATE TABLE IF NOT EXISTS record_versions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    kind TEXT NOT NULL, id TEXT NOT NULL, recorded_t REAL NOT NULL,
                    document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    source TEXT NOT NULL, created_t REAL NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime_feed (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    received_t REAL NOT NULL, document TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS runtime_feed_session ON runtime_feed(session_id, seq);
                CREATE TABLE IF NOT EXISTS migrations (
                    path TEXT PRIMARY KEY, checksum TEXT NOT NULL, session_id TEXT NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _load(self, db, sid):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise KeyError(sid)
        s = json.loads(row["document"])
        s["_revision"] = row["revision"]
        records = db.execute("SELECT * FROM records WHERE session_id=? ORDER BY ordinal, id", (sid,)).fetchall()
        by_kind = {}
        for record in records:
            if record["active"]:
                by_kind.setdefault(record["kind"], []).append(json.loads(record["document"]))
        s["events"] = by_kind.get("event", [])
        reviews = {r["id"]: r["review"] for r in by_kind.get("review", [])}
        s["marks"] = by_kind.get("bookmark", [])
        for m in s["marks"]:
            m["review"] = reviews.get(m["id"])
        responses = {r["id"]: r["response"] for r in by_kind.get("candidate_response", [])}
        s["candidates"] = by_kind.get("prediction", [])
        for c in s["candidates"]:
            c["participant_response"] = responses.get(c["id"])
        s["researcher_annotations"] = by_kind.get("researcher_code", [])
        s["codes"] = {r["target_key"]: r["code"] for r in s["researcher_annotations"]}
        s["analysis_runs"] = [json.loads(r[0]) for r in db.execute(
            "SELECT document FROM analysis_runs WHERE session_id=? ORDER BY created_t, id", (sid,))]
        return s

    def load(self, sid):
        with self.connect() as db:
            # All component queries must see the same committed revision.
            db.execute("BEGIN")
            return self._load(db, sid)

    def all_sessions(self):
        with self.connect() as db:
            db.execute("BEGIN")
            ids = [r[0] for r in db.execute("SELECT id FROM sessions ORDER BY id")]
            return [self._load(db, sid) for sid in ids]

    def _record(self, db, sid, kind, rid, document, ordinal=0):
        value = encode(document)
        old = db.execute("SELECT document FROM records WHERE session_id=? AND kind=? AND id=?",
                         (sid, kind, rid)).fetchone()
        if not old or old[0] != value:
            db.execute("INSERT INTO record_versions(session_id,kind,id,recorded_t,document) VALUES(?,?,?,?,?)",
                       (sid, kind, rid, time.time(), value))
        db.execute("""INSERT INTO records VALUES(?,?,?,?,1,?)
                      ON CONFLICT(session_id,kind,id) DO UPDATE SET
                      ordinal=excluded.ordinal,active=1,document=excluded.document""",
                   (sid, kind, rid, ordinal, value))

    def _save(self, db, session):
        s = copy.deepcopy(session)
        sid = s["id"]
        for key in ("events", "marks", "candidates"):
            ids = [record["id"] for record in s.get(key, [])]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate {key} IDs; no records were saved.")
        row = db.execute("SELECT revision FROM sessions WHERE id=?", (sid,)).fetchone()
        expected = s.get("_revision")
        if row and expected != row[0]:
            raise ConflictError("Session changed; reload and retry. Nothing was overwritten.")
        revision = (row[0] if row else 0) + 1
        metadata = {k: v for k, v in s.items() if k not in self.COLLECTIONS}
        db.execute("INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET revision=?,document=?",
                   (sid, revision, encode(metadata), revision, encode(metadata)))
        # Predictions from a rerun are different records, never a rewrite of a
        # participant's answer to an earlier prediction.
        for kind in ("event", "bookmark", "prediction", "review", "candidate_response"):
            db.execute("UPDATE records SET active=0 WHERE session_id=? AND kind=?", (sid, kind))
        for i, e in enumerate(s.get("events", [])):
            self._record(db, sid, "event", e["id"], e, i)
        for i, m in enumerate(s.get("marks", [])):
            bookmark = {k: v for k, v in m.items() if k != "review"}
            self._record(db, sid, "bookmark", m["id"], bookmark, i)
            if m.get("review") is not None:
                self._record(db, sid, "review", m["id"], {"id": m["id"], "review": m["review"]}, i)
        for i, c in enumerate(s.get("candidates", [])):
            self._record(db, sid, "prediction", c["id"],
                         {k: v for k, v in c.items() if k != "participant_response"}, i)
            if c.get("participant_response") is not None:
                self._record(db, sid, "candidate_response", c["id"],
                             {"id": c["id"], "response": c["participant_response"]}, i)
        for key, code in s.get("codes", {}).items():
            coder = code.get("coder") or "unspecified"
            rid = encode([key, coder])
            self._record(db, sid, "researcher_code", rid,
                         {"id": rid, "target_key": key, "code": code}, revision)
        for run in s.get("analysis_runs", []):
            db.execute("INSERT OR IGNORE INTO analysis_runs VALUES(?,?,?,?,?)",
                       (run["id"], sid, run["source"], run["created_t"], encode(run)))
        return revision

    def save(self, session):
        with self.write_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = self._save(db, session)
        session["_revision"] = revision

    def update(self, sid, mutate, notification=None):
        """Read/modify/write (and optional feed publication) in one transaction."""
        with self.write_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            s = self._load(db, sid)
            result = mutate(s)
            s["_revision"] = self._save(db, s)
            if notification is not None:
                self._publish(db, sid, notification)
            return result

    def _publish(self, db, sid, notification):
        cursor = db.execute("INSERT INTO runtime_feed(session_id,received_t,document) VALUES(?,?,?)",
                            (sid, time.time(), encode(notification)))
        return cursor.lastrowid

    def publish(self, sid, notification):
        with self.connect() as db:
            return self._publish(db, sid, notification)

    def feed(self, sid, after=0, limit=200):
        with self.connect() as db:
            return [{"seq": r["seq"], "received_t": r["received_t"],
                     "notification": json.loads(r["document"])} for r in db.execute(
                "SELECT * FROM runtime_feed WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                (sid, after, limit))]

    def versions(self, sid, kind, rid):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT document FROM record_versions WHERE session_id=? AND kind=? AND id=? ORDER BY seq",
                (sid, kind, rid))]

    def export(self, sid):
        """Complete research export, including superseded evidence and responses."""
        with self.connect() as db:
            db.execute("BEGIN")
            s = self._load(db, sid)
            s["record_history"] = [{"kind": r["kind"], "id": r["id"], "recorded_t": r["recorded_t"],
                                    "document": json.loads(r["document"])} for r in db.execute(
                "SELECT * FROM record_versions WHERE session_id=? ORDER BY seq", (sid,))]
            s["archived_records"] = [{"kind": r["kind"], "id": r["id"], "document": json.loads(r["document"])}
                for r in db.execute("SELECT * FROM records WHERE session_id=? AND active=0 ORDER BY kind,ordinal", (sid,))]
            s["runtime_trace"] = [{"seq": r["seq"], "received_t": r["received_t"],
                                   "notification": json.loads(r["document"])} for r in db.execute(
                "SELECT * FROM runtime_feed WHERE session_id=? ORDER BY seq", (sid,))]
            return s

    def migrate_legacy(self, directory):
        count = 0
        for path in sorted(Path(directory).glob("s_*.json")):
            # No participant text is logged. Original bytes remain untouched.
            raw = path.read_bytes()
            checksum = hashlib.sha256(raw).hexdigest()
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM migrations WHERE path=?", (str(path.resolve()),)).fetchone():
                    continue
                s = json.loads(raw)
                if not db.execute("SELECT 1 FROM sessions WHERE id=?", (s["id"],)).fetchone():
                    s.pop("_revision", None)
                    s.setdefault("schema_version", 1)
                    s.setdefault("runtime", {"kind": "external" if s.get("external") else
                                             "human" if s.get("condition") == "human" else "legacy"})
                    self._save(db, s)
                    count += 1
                db.execute("INSERT INTO migrations VALUES(?,?,?)", (str(path.resolve()), checksum, s["id"]))
        return count


def import_events(session, incoming, source, append=False):
    """Stable content identities; old anchored evidence stays available on replace."""
    seen = {}
    imported = []
    for e in incoming:
        e = copy.deepcopy(e)
        e.setdefault("meta", {})
        original = e.pop("id", None)
        identity = encode({"source": source, "id": original, "event": e})
        ordinal = seen.get(identity, 0)
        seen[identity] = ordinal + 1
        e["id"] = "ev_" + hashlib.sha256((identity + str(ordinal)).encode()).hexdigest()[:24]
        e["meta"]["import_source"] = source
        imported.append(e)
    existing = {e["id"]: e for e in session["events"]}
    for e in existing.values():
        if not append and e.get("meta", {}).get("import_source") is not None:
            e["meta"]["superseded"] = True
    for e in imported:
        # Identical re-imports must not change IDs or reorder anchored events.
        existing[e["id"]] = e
    session["events"] = sorted(existing.values(), key=lambda e: (e.get("t") is None, e.get("t") or 0))
    return imported


def record_analysis_run(session, source, candidates, metadata, configuration):
    run_id = "run_" + uuid.uuid4().hex
    run = {"id": run_id, "source": source, "created_t": time.time(),
           "input_event_ids": [e["id"] for e in session["events"]],
           "configuration": copy.deepcopy(configuration), "metadata": copy.deepcopy(metadata),
           "candidates": copy.deepcopy(candidates)}
    session.setdefault("analysis_runs", []).append(run)
    fresh = []
    for c in candidates:
        c = dict(c, id="c_" + uuid.uuid4().hex, run_id=run_id, source=source, participant_response=None)
        fresh.append(c)
    session["candidates"] = [c for c in session["candidates"] if c.get("source", "codebook") != source] + fresh
    return run_id
