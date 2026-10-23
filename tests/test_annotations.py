import copy
import unittest

import annotations
from storage import ConflictError


def session():
    return {"phase": "review", "phase_order": ["pre","chat","post","review","done"], "marks": [],
            "events": [{"id":"e1","role":"assistant","text":"Partial output"}, {"id":"e2","role":"user","text":"My realization"}]}


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.s = session()
        self.m = annotations.create_mark(self.s, {"phase_at_click":"chat", "exposure":{"events":[{
            "event_id":"e1","rendered_text":"Partial","status":"inProgress"}]}}, "m_12345678")

    def test_timestamp_bookmark_does_not_infer_latest_agent_trigger(self):
        self.assertIsNone(self.m["event_id"])
        self.assertEqual(self.m["phase"], "chat")
        self.s["events"][0]["text"] = "Finished output"
        self.assertEqual(self.m["exposure"]["events"][0]["rendered_text"], "Partial")

    def test_passage_can_span_agent_and_participant_without_rewriting_click(self):
        click = self.m["t"]
        annotations.patch_mark(self.s, self.m, {"episode":{"start_event_id":"e1","end_event_id":"e2"}}, {"reflective"})
        self.assertEqual(self.m["episode"]["end_event_id"], "e2")
        self.assertIsNone(self.m["original_event_id"])
        self.assertEqual(self.m["t"], click)
        with self.assertRaises(ValueError):
            annotations.validate_episode(self.s, {"start_event_id":"e2","end_event_id":"e1"})

    def test_exact_text_selection_is_stored_and_can_be_changed_in_review(self):
        selection = {"event_id":"e1", "start":0, "end":7, "text":"Partial"}
        annotations.patch_mark(self.s, self.m, {"quote_selection":selection}, set())
        self.assertEqual(self.m["quote_selection"], selection)
        self.assertEqual(self.m["quote"], "Partial")
        self.assertIsNone(self.m["original_event_id"])
        annotations.patch_mark(self.s, self.m, {"quote_selection":None}, set())
        self.assertIsNone(self.m["quote"])

    def test_text_selection_must_match_one_transcript_event(self):
        for invalid in (
            {"event_id":"e1", "start":0, "end":7, "text":"Changed"},
            {"event_id":"e1", "start":0, "end":99, "text":"Partial output"},
            {"event_id":"missing", "start":0, "end":7, "text":"Partial"},
        ):
            with self.assertRaises(ValueError):
                annotations.create_mark(self.s, {"event_id":"e1", "quote_selection":invalid}, "m_bad1234")
        self.s["events"][0]["text"] = "Idea 🧠 shift"
        selected = annotations.validate_quote_selection(self.s, {"event_id":"e1", "start":7, "end":12, "text":"shift"})
        self.assertEqual(selected["text"], "shift")

    def test_types_optional_and_unused_insights_valid(self):
        annotations.patch_mark(self.s, self.m, {"review":{"decision":"confirmed","after":"An unused idea", "uptake":"not_used"}}, {"additive"})
        self.assertEqual(self.m["review"]["status"], "submitted")
        self.assertEqual(self.m["review"]["types"], [])

    def test_confirmed_requires_explanation_but_unsure_does_not(self):
        with self.assertRaises(ValueError):
            annotations.validate_review({"decision":"confirmed"}, set(), final=True)
        self.assertEqual(annotations.validate_review({"decision":"unsure"}, set(), final=True)["status"], "submitted")

    def test_autosave_is_not_final_and_stale_edits_rejected(self):
        draft = {"after":"Still editing","editor_id":"editor1","editor_seq":1}
        annotations.patch_mark(self.s, self.m, {"review_draft":draft,"expected_review_version":0}, set())
        self.assertIsNone(self.m["review"])
        self.assertTrue(self.m["review_dirty"])
        with self.assertRaises(ConflictError):
            annotations.patch_mark(self.s, self.m, {"review_draft":dict(draft,editor_id="editor2"),"expected_review_version":0}, set())
        # A lost acknowledgement does not produce another write or version.
        annotations.patch_mark(self.s, self.m, {"review_draft":draft,"expected_review_version":0}, set())
        self.assertEqual(self.m["review_version"], 1)

    def test_edit_after_confirmation_requires_reconfirmation(self):
        annotations.patch_mark(self.s, self.m, {"review":{"decision":"rejected"}}, set())
        annotations.patch_mark(self.s, self.m, {"review_draft":{"decision":"unsure"}}, set())
        with self.assertRaises(ValueError):
            annotations.complete_independent_review(self.s)

    def test_independent_answers_locked_and_cannot_be_overwritten_by_suggestions(self):
        annotations.patch_mark(self.s, self.m, {"review":{"decision":"confirmed","after":"My own insight"}}, set())
        snapshot = copy.deepcopy(annotations.complete_independent_review(self.s))
        with self.assertRaises(ConflictError):
            annotations.patch_mark(self.s, self.m, {"review":{"decision":"rejected"}}, set())
        self.assertEqual(self.s["independent_review"], snapshot)
        followup = annotations.create_mark(self.s, {"source":"candidate_confirm", "event_id":"e1"}, "m_22222222")
        self.assertNotIn(followup["id"], snapshot["mark_ids"])

    def test_suggestions_cannot_enter_an_unfinished_independent_pass(self):
        with self.assertRaises(ValueError):
            annotations.create_mark(self.s, {"source":"candidate_confirm"}, "m_22222222")

    def test_zero_bookmarks_records_zero_flags_not_a_negative_spark_label(self):
        self.s["marks"] = []
        snapshot = annotations.complete_independent_review(self.s)
        self.assertTrue(snapshot["zero_flags"])
        self.assertNotIn("no_sparks", snapshot)

    def test_final_save_acknowledgement_can_be_retried(self):
        body = {"expected_review_version":0,"review":{"decision":"rejected","editor_id":"tab","editor_seq":1}}
        annotations.patch_mark(self.s,self.m,body,set())
        annotations.patch_mark(self.s,self.m,body,set())
        self.assertEqual(self.m["review_version"],1)

    def test_clear_passage_preserves_original_click_and_done_locks_followups(self):
        annotations.patch_mark(self.s,self.m,{"episode":{"start_event_id":"e1","end_event_id":"e2"}},set())
        annotations.patch_mark(self.s,self.m,{"episode":None},set())
        self.assertIsNone(self.m["episode"])
        self.assertIsNone(self.m["event_id"])
        self.s["phase"] = "done"
        with self.assertRaises(ConflictError):
            annotations.patch_mark(self.s,self.m,{"review":{"decision":"rejected"}},set())


if __name__ == "__main__":
    unittest.main()
