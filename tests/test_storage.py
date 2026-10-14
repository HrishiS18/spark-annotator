import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from storage import ConflictError, Store, import_events, record_analysis_run


def session(sid="s_12345678"):
    return {"id": sid, "created_at": "2026-10-13T00:00:00Z", "phase": "chat", "study": "dilemma",
            "prompt": {"text": "An invented test task"}, "writing": {"pre": {"text": "A baseline"}},
            "events": [{"id": "e1", "role": "assistant", "text": "Test evidence", "t": 1, "meta": {}}],
            "marks": [{"id": "m_12345678", "event_id": "e1", "t": 2, "review": {"after": "An insight"}}],
            "candidates": [], "codes": {}, "imports": [], "analysis_runs": [], "counter": 0}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "study.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_preserves_bytes_ids_reviews_and_unknown_fields(self):
        legacy = Path(self.tmp.name) / "legacy"
        legacy.mkdir()
        original = session()
        original["custom_field"] = {"keep": True}
        path = legacy / "s_12345678.json"
        raw = json.dumps(original).encode()
        path.write_bytes(raw)
        self.assertEqual(self.store.migrate_legacy(legacy), 1)
        self.assertEqual(self.store.migrate_legacy(legacy), 0)
        loaded = self.store.load(original["id"])
        for key in original:
            self.assertEqual(loaded[key], original[key])
        self.assertEqual(path.read_bytes(), raw)
        loaded["phase"] = "review"
        self.store.save(loaded)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(Store(self.store.path).load(loaded["id"])["phase"], "review")

    def test_stale_save_rejected_without_losing_bookmark(self):
        s = session()
        self.store.save(s)
        first, stale = self.store.load(s["id"]), self.store.load(s["id"])
        first["marks"].append({"id": "m_22222222", "event_id": "e1", "review": None})
        self.store.save(first)
        stale["events"].append({"id": "e2", "text": "stale", "role": "user"})
        with self.assertRaises(ConflictError):
            self.store.save(stale)
        loaded = self.store.load(s["id"])
        self.assertEqual(len(loaded["marks"]), 2)
        self.assertEqual(len(loaded["events"]), 1)

    def test_concurrent_transactional_updates(self):
        s = session()
        self.store.save(s)
        def increment(_):
            self.store.update(s["id"], lambda state: state.update(counter=state["counter"] + 1))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(increment, range(40)))
        self.assertEqual(self.store.load(s["id"])["counter"], 40)

    def test_events_bookmarks_and_reviews_have_separate_history(self):
        s = session()
        self.store.save(s)
        s["events"][0]["text"] += " streamed"
        s["marks"][0]["review"] = {"after": "Revised explanation"}
        self.store.save(s)
        self.assertEqual(len(self.store.versions(s["id"], "event", "e1")), 2)
        self.assertEqual(len(self.store.versions(s["id"], "bookmark", "m_12345678")), 1)
        self.assertEqual(len(self.store.versions(s["id"], "review", "m_12345678")), 2)

    def test_reruns_never_reuse_answers_or_collide_on_shared_trigger(self):
        s = session()
        cands = [{"type": "additive", "trigger_event_id": "e1", "uptake_event_id": "e2"},
                 {"type": "additive", "trigger_event_id": "e1", "uptake_event_id": "e3"}]
        first = record_analysis_run(s, "codebook", cands, {"version": 1}, {"rules": [1]})
        ids = [c["id"] for c in s["candidates"]]
        self.assertEqual(len(set(ids)), 2)
        s["candidates"][0]["participant_response"] = {"answer": "yes"}
        self.store.save(s)
        second = record_analysis_run(s, "codebook", cands, {"version": 2}, {"rules": [2]})
        self.store.save(s)
        self.assertNotEqual(first, second)
        self.assertTrue(all(c["participant_response"] is None for c in self.store.load(s["id"])["candidates"]))
        self.assertEqual(self.store.versions(s["id"], "candidate_response", ids[0])[0]["response"]["answer"], "yes")
        self.assertEqual(len(self.store.load(s["id"])["analysis_runs"]), 2)
        exported = self.store.export(s["id"])
        self.assertTrue(any(r["kind"] == "candidate_response" and r["document"]["response"]["answer"] == "yes"
                            for r in exported["archived_records"]))

    def test_imports_deduplicate_without_renumbering_or_reanchoring(self):
        s = session()
        incoming = [{"id": "e1", "role": "assistant", "text": "New evidence", "t": 3, "meta": {}}]
        imported = import_events(s, incoming, "log.jsonl")
        anchor = imported[0]["id"]
        s["marks"].append({"id": "m_22222222", "event_id": anchor, "review": {"after": "Keep me"}})
        self.store.save(s)
        import_events(s, incoming, "log.jsonl")
        self.store.save(s)
        self.assertEqual(len(s["events"]), 2)
        self.assertEqual(s["marks"][0]["event_id"], "e1")
        self.assertEqual(s["marks"][1]["event_id"], anchor)
        import_events(s, [dict(incoming[0], text="Changed log")], "different.jsonl")
        self.store.save(s)
        self.assertTrue(any(e["id"] == anchor for e in self.store.load(s["id"])["events"]))
        self.assertEqual(s["marks"][1]["review"]["after"], "Keep me")

    def test_researcher_codes_do_not_overwrite_other_coder(self):
        s = session()
        s["codes"]["event:e1"] = {"label": "spark", "coder": "A"}
        self.store.save(s)
        s["codes"]["event:e1"] = {"label": "unclear", "coder": "B"}
        self.store.save(s)
        codes = self.store.load(s["id"])["researcher_annotations"]
        self.assertEqual({r["code"]["coder"] for r in codes}, {"A", "B"})

    def test_validation_codes_are_separate_and_versioned(self):
        sample = {"id":"vs_123456789abc","created_t":1,"items":[{"id":"vi_one"}],
                  "coders":[{"id":"coder_a"},{"id":"coder_b"}]}
        self.store.create_validation_sample(sample)
        self.store.save_validation_code(sample["id"], "vi_one", "coder_a", {"evidence":"possible"})
        self.store.save_validation_code(sample["id"], "vi_one", "coder_b", {"evidence":"expressed"})
        self.store.save_validation_code(sample["id"], "vi_one", "coder_a", {"evidence":"insufficient"})
        loaded, codes = self.store.validation_sample(sample["id"])
        self.assertEqual(loaded["id"], sample["id"])
        self.assertEqual(len(codes), 2)
        self.assertEqual(next(c for c in codes if c["coder_id"] == "coder_a")["code"]["evidence"], "insufficient")
        self.assertEqual(len(self.store.validation_code_history(sample["id"], "vi_one", "coder_a")), 2)

    def test_failed_mutation_rolls_back_state_and_feed(self):
        s = session()
        self.store.save(s)
        def broken(state):
            state["phase"] = "done"
            raise ValueError("test failure")
        with self.assertRaises(ValueError):
            self.store.update(s["id"], broken, {"method": "test"})
        self.assertEqual(self.store.load(s["id"])["phase"], "chat")
        self.assertEqual(self.store.feed(s["id"]), [])

    def test_duplicate_ids_fail_atomically(self):
        s = session()
        s["events"].append(copy.deepcopy(s["events"][0]))
        with self.assertRaises(ValueError):
            self.store.save(s)
        with self.assertRaises(KeyError):
            self.store.load(s["id"])


if __name__ == "__main__":
    unittest.main()
