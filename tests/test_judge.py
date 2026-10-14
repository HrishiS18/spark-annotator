import copy
import json
import unittest
from unittest.mock import patch

import judge
import llm
import transcript


def session():
    return {"external": False, "condition": "ai", "prompt": {"text": "Choose how to handle the problem"},
            "events": [
                {"id":"e1","role":"user","text":"I think the manager has to fix this.","t":100},
                {"id":"e2","role":"assistant","text":"Does your manager control the promotion decision?","t":102},
                {"id":"e3","role":"user","text":"I realized I confused who caused the problem with who can fix it.","t":105},
                {"id":"e4","role":"assistant","text":"Consider checking who has authority.","t":108}],
            "writing":{"pre":{"text":"Confront the manager."}}, "marks":[], "candidates":[]}


def candidate():
    return {"start_event_id":"e1","end_event_id":"e3","trigger_event_id":"e2","uptake_event_id":"e3",
            "type":"representational","secondary_type":"none","insight_evidence":"expressed",
            "persuasion":"not_observed","copying":"not_observed","uptake":"unclear","strength":"high",
            "trigger_quote":"Does your manager control the promotion decision?",
            "uptake_quote":"I realized I confused who caused the problem with who can fix it.",
            "before":"The manager had to solve it.","after":"Distinguished cause from authority.","rationale":"Explicit participant insight."}


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.s = session()

    def test_full_quote_checked_not_only_prefix(self):
        c = candidate()
        self.assertIsNone(judge.validate(c,self.s["events"]))
        c["uptake_quote"] += " This fabricated suffix must not pass."
        self.assertIn("not found",judge.validate(c,self.s["events"]))

    def test_missing_or_nonparticipant_uptake_is_rejected_not_silently_repaired(self):
        c = candidate(); c["uptake_event_id"] = "missing"
        self.assertIn("Unknown",judge.validate(c,self.s["events"]))
        c["uptake_event_id"] = "e4"
        self.assertIn("Invalid",judge.validate(c,self.s["events"]))

    def test_episode_and_uptake_chronology_validated(self):
        c = candidate(); c["end_event_id"] = "e1"
        self.assertIn("chronology",judge.validate(c,self.s["events"]))
        c = candidate(); c["uptake_event_id"] = "e1"
        self.assertIn("chronology",judge.validate(c,self.s["events"]))

    def test_unused_and_assistant_only_candidates_are_not_forced_to_show_uptake(self):
        c = candidate(); c.update(insight_evidence="possible",uptake_event_id="",uptake_quote="",uptake="not_observed")
        self.assertIsNone(judge.validate(c,self.s["events"]))
        c["insight_evidence"] = "expressed"
        self.assertIn("participant",judge.validate(c,self.s["events"]))
        c.update(trigger_event_id="e3",trigger_quote=self.s["events"][2]["text"])
        self.assertIsNone(judge.validate(c,self.s["events"]))

    def test_copying_and_persuasion_can_coexist_with_expressed_insight(self):
        c = candidate(); c.update(copying="observed",persuasion="observed")
        self.assertIsNone(judge.validate(c,self.s["events"]))

    def test_model_input_excludes_flags_reviews_predictions_codes_and_reasoning(self):
        self.s["marks"] = [{"t":1,"review":{"after":"PRIVATE_REVIEW_SENTINEL"}}]
        self.s["candidates"] = [{"rationale":"PRIOR_MODEL_SENTINEL"}]
        self.s["codes"] = {"x":"CODER_SENTINEL"}
        self.s["events"].append({"id":"thought","role":"thinking","text":"HIDDEN_REASONING_SENTINEL"})
        with patch.object(judge.judge_backend,"call",return_value=({"summary":"None","candidates":[]},{})) as call:
            output, meta = judge.run(self.s)
        prompt = call.call_args.args[1]
        for word in ("PRIVATE_REVIEW_SENTINEL","PRIOR_MODEL_SENTINEL","CODER_SENTINEL","HIDDEN_REASONING_SENTINEL"):
            self.assertNotIn(word,prompt)
        self.assertEqual(transcript.base_time(self.s),100)
        self.assertEqual(output,[])
        self.assertEqual(meta["input_prompts"],[prompt])
        self.assertIn("baseline",prompt)

    def test_invalid_predictions_preserved_as_rejected_evidence(self):
        bad = candidate(); bad["trigger_quote"] = "Fabricated."
        with patch.object(judge.judge_backend,"call",return_value=({"summary":"S","candidates":[bad]},{})):
            found, meta = judge.run(self.s)
        self.assertEqual(found,[])
        self.assertEqual(meta["rejected_candidates"][0]["prediction"],bad)

    def test_dedup_keeps_distinct_episodes_and_no_heuristic_fallback(self):
        first = candidate(); second = copy.deepcopy(first); second["start_event_id"] = "e2"
        with patch.object(judge.judge_backend,"call",return_value=({"summary":"S","candidates":[first,first,second]},{})):
            found, _ = judge.run(self.s)
        self.assertEqual(len(found),2)
        with patch.object(judge.judge_backend,"call",side_effect=llm.LLMError("Unavailable")):
            with self.assertRaises(llm.LLMError):
                judge.run(self.s)

    def test_oversized_inputs_rejected_without_silent_truncation(self):
        self.s["events"][0]["text"] = "X" * 10000
        with patch.object(judge,"MAX_INPUT_TOKENS",2000):
            with self.assertRaisesRegex(llm.LLMError,"not silently truncated"):
                judge.run(self.s)
        self.s["writing"]["pre"]["text"] = "X" * 10000
        with patch.object(judge,"MAX_INPUT_TOKENS",2000):
            with self.assertRaisesRegex(llm.LLMError,"baseline context"):
                judge.run(self.s)

    def test_long_events_preserved_and_window_scope_explicit(self):
        self.s["events"][0]["text"] = "A" * 4500 + "FINAL_TEXT_SENTINEL"
        with patch.object(judge.judge_backend,"call",return_value=({"summary":"S","candidates":[]},{})) as call:
            judge.run(self.s)
        self.assertIn("FINAL_TEXT_SENTINEL",call.call_args.args[1])
        self.s["events"] = [dict(e,text="A"*4500) for e in self.s["events"]]
        with patch.object(judge,"MAX_INPUT_TOKENS",3500), patch.object(judge.judge_backend,"call",return_value=({"summary":"S","candidates":[]},{})):
            _, meta = judge.run(self.s)
        self.assertEqual(meta["scope"],"overlapping_windows")
        self.assertGreater(meta["windows"],1)


if __name__ == "__main__":
    unittest.main()
