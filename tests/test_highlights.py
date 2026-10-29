import unittest

import annotations
from storage import ConflictError


def session(**annotation):
    return {"id": "s_00000001", "participant_id": "P1", "phase": "review",
            "phase_order": ["pre", "chat", "post", "review", "done"], "marks": [], "highlights": [], "candidates": [],
            "config_snapshot": {"annotation": annotations._merge(annotations.ANNOTATION, annotation)},
            "writing": {"pre": {"text": "I think promises bind."}, "post": {"text": "Promises bind unless harm is grave."}},
            "events": [{"id": "e1", "role": "assistant", "text": "What if the harm is grave?", "output": None},
                       {"id": "e2", "role": "tool_result", "text": "ran", "output": "exit 0 — ünïcode"},
                       {"id": "e3", "role": "thinking", "text": "hidden"}]}


def span(target, text, quote):
    start = text.index(quote)
    return {"target": target, "start": start, "end": start + len(quote), "text": quote}


class HighlightTests(unittest.TestCase):
    def setUp(self):
        self.s = session()

    def test_standalone_highlight_in_transcript_writing_and_tool_output(self):
        h = annotations.create_highlight(self.s, {**span({"kind": "event", "event_id": "e1"}, self.s["events"][0]["text"], "harm is grave"),
                                                  "note": "Made me rethink", "tags": ["reframe", " Reframe ", "what-if"]}, "h_00000001")
        self.assertEqual(h["target"], {"kind": "event", "event_id": "e1", "field": "text"})
        self.assertEqual(h["tags"], ["reframe", "what-if"])
        self.assertIsNone(h["mark_id"])
        self.assertEqual(h["source"], "review")
        w = annotations.create_highlight(self.s, span({"kind": "writing", "writing": "post"}, self.s["writing"]["post"]["text"], "unless harm is grave"), "h_00000002")
        self.assertEqual(w["target"], {"kind": "writing", "writing": "post"})
        out = self.s["events"][1]["output"]
        o = annotations.create_highlight(self.s, {"target": {"kind": "event", "event_id": "e2", "field": "output"},
                                                  "start": 9, "end": 16, "text": out[9:16]}, "h_00000003")
        self.assertEqual(o["text"], "ünïcode")

    def test_spans_must_match_stored_text_and_allowed_targets(self):
        with self.assertRaises(ValueError):
            annotations.create_highlight(self.s, {"target": {"kind": "event", "event_id": "e1"}, "start": 0, "end": 4, "text": "nope"}, "h_00000001")
        with self.assertRaises(ValueError):
            annotations.create_highlight(self.s, span({"kind": "event", "event_id": "e3"}, "hidden", "hidden"), "h_00000001")
        with self.assertRaises(ValueError):
            annotations.create_highlight(self.s, span({"kind": "writing", "writing": "notes"}, "x", "x"), "h_00000001")
        with self.assertRaisesRegex(ValueError, "name their suggestion"):
            annotations.create_highlight(self.s, {**span({"kind": "event", "event_id": "e1"}, self.s["events"][0]["text"], "grave"),
                                                  "source": "candidate"}, "h_00000001")

    def test_limits_and_targets_come_from_session_settings(self):
        s = session(highlights={"max_chars": 5, "max_tags": 1, "standalone": False, "targets": {"writing": ["pre"]}})
        e1 = s["events"][0]["text"]
        with self.assertRaisesRegex(ValueError, "5 characters"):
            annotations.create_highlight(s, span({"kind": "event", "event_id": "e1"}, e1, "harm is grave"), "h_00000001")
        with self.assertRaisesRegex(ValueError, "moment"):
            annotations.create_highlight(s, span({"kind": "event", "event_id": "e1"}, e1, "grave"), "h_00000001")
        with self.assertRaises(ValueError):
            annotations.create_highlight(s, span({"kind": "writing", "writing": "post"}, s["writing"]["post"]["text"], "harm"), "h_00000001")
        m = annotations.create_mark(s, {}, "m_00000001")
        with self.assertRaisesRegex(ValueError, "1 tags"):
            annotations.create_highlight(s, {**span({"kind": "event", "event_id": "e1"}, e1, "grave"), "mark_id": m["id"], "tags": ["a", "b"]}, "h_00000001")
        with self.assertRaisesRegex(ValueError, "5 characters"):
            annotations.validate_quote_selection(s, {"event_id": "e1", "start": 0, "end": 7, "text": e1[:7]})

    def test_writing_only_in_review_and_live_highlights_in_chat(self):
        self.s["phase"] = "chat"
        with self.assertRaises(ValueError):
            annotations.create_highlight(self.s, span({"kind": "writing", "writing": "pre"}, self.s["writing"]["pre"]["text"], "promises"), "h_00000001")
        h = annotations.create_highlight(self.s, span({"kind": "event", "event_id": "e1"}, self.s["events"][0]["text"], "grave"), "h_00000002")
        self.assertEqual((h["source"], h["phase"]), ("live", "chat"))
        self.s["phase"] = "post"
        with self.assertRaises(ValueError):
            annotations.create_highlight(self.s, span({"kind": "event", "event_id": "e1"}, self.s["events"][0]["text"], "grave"), "h_00000003")

    def test_patch_versions_boundaries_link_and_conflicts(self):
        text = self.s["events"][0]["text"]
        h = annotations.create_highlight(self.s, span({"kind": "event", "event_id": "e1"}, text, "harm"), "h_00000001")
        m = annotations.create_mark(self.s, {}, "m_00000001")
        annotations.patch_highlight(self.s, h, {"expected_version": 1, "note": "n", "tags": ["x"], "mark_id": m["id"]})
        self.assertEqual((h["version"], h["mark_id"]), (2, m["id"]))
        annotations.patch_highlight(self.s, h, {"expected_version": 2, **{k: v for k, v in span(None, text, "harm is grave").items() if k != "target"}})
        self.assertEqual(h["text"], "harm is grave")
        with self.assertRaises(ConflictError):
            annotations.patch_highlight(self.s, h, {"expected_version": 2, "note": "stale"})
        with self.assertRaises(ValueError):
            annotations.patch_highlight(self.s, h, {"expected_version": 3, "start": 0, "end": 3, "text": "nope"})
        self.assertEqual(h["version"], 3)

    def test_independent_lock_preserves_highlights_but_allows_suggested_moments(self):
        text = self.s["events"][0]["text"]
        h = annotations.create_highlight(self.s, span({"kind": "event", "event_id": "e1"}, text, "harm"), "h_00000001")
        snapshot = annotations.complete_independent_review(self.s)
        self.assertEqual(snapshot["highlight_ids"], [h["id"]])
        with self.assertRaises(ConflictError):
            annotations.patch_highlight(self.s, h, {"expected_version": 1, "note": "late"})
        with self.assertRaises(ConflictError):
            annotations.create_highlight(self.s, span({"kind": "event", "event_id": "e1"}, text, "grave"), "h_00000002")
        self.s["candidates"] = [{"id": "c_1"}]
        m = annotations.create_mark(self.s, {"source": "candidate_confirm"}, "m_00000001")
        c = annotations.create_highlight(self.s, {**span({"kind": "event", "event_id": "e1"}, text, "grave"),
                                                  "mark_id": m["id"], "candidate_id": "c_1", "source": "candidate"}, "h_00000003")
        annotations.patch_highlight(self.s, c, {"expected_version": 1, **{k: v for k, v in span(None, text, "is grave").items() if k != "target"}})
        with self.assertRaises(ConflictError):
            annotations.patch_highlight(self.s, c, {"expected_version": 2, "mark_id": None})

    def test_live_selection_never_loses_a_bookmark(self):
        self.s["phase"] = "chat"
        m = annotations.create_mark(self.s, {"anchor": "selection", "quote": "grave", "event_id": "e1",
                                             "quote_selection": {"event_id": "e1", "start": 0, "end": 5, "text": "grave"}}, "m_00000001")
        self.assertIsNone(m["quote_selection"])
        self.assertEqual(m["quote"], "grave")
        with self.assertRaises(ValueError):
            annotations.create_mark(self.s, {"source": "review_add", "quote_selection": {"event_id": "e1", "start": 0, "end": 5, "text": "grave"}}, "m_00000002")

    def test_tag_suggestions_are_the_participants_own(self):
        other = session(); other["participant_id"] = "P2"
        mine = session()
        for s, tags in ((self.s, ["reframe"]), (mine, ["reframe", "pushback"]), (other, ["secret"])):
            annotations.create_highlight(s, {**span({"kind": "event", "event_id": "e1"}, s["events"][0]["text"], "grave"), "tags": tags}, "h_00000001")
        self.assertEqual(annotations.tag_suggestions([self.s, mine, other], self.s), ["reframe", "pushback"])
        self.s["config_snapshot"]["annotation"]["highlights"]["tag_suggestions"] = "session"
        self.assertEqual(annotations.tag_suggestions([self.s, mine, other], self.s), ["reframe"])
        self.s["config_snapshot"]["annotation"]["highlights"]["tag_suggestions"] = "off"
        self.assertEqual(annotations.tag_suggestions([self.s, mine, other], self.s), [])

    def test_study_overrides_merge_over_defaults(self):
        merged = annotations.settings_for_study({"annotation": {"highlights": {"max_chars": 10}}})
        self.assertEqual(merged["highlights"]["max_chars"], 10)
        self.assertEqual(merged["highlights"]["targets"], annotations.ANNOTATION["highlights"]["targets"])
        legacy = {"config_snapshot": {"study": {}}}
        self.assertEqual(annotations.settings(legacy), annotations.ANNOTATION)


if __name__ == "__main__":
    unittest.main()
