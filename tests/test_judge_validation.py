import unittest

import judge_validation as validation


def session():
    events = [
        {"id":"e1","role":"user","text":"I think I should confront my manager.","t":1,"meta":{}},
        {"id":"e2","role":"assistant","text":"Does that manager actually control promotion decisions?","t":2,"meta":{}},
        {"id":"e3","role":"user","text":"I had conflated who caused the problem with who can fix it.","t":3,"meta":{}},
        {"id":"e4","role":"assistant","text":"What options follow from that distinction?","t":4,"meta":{}},
        {"id":"e5","role":"user","text":"I can document it and speak to the skip-level manager.","t":5,"meta":{}},
        {"id":"e6","role":"assistant","text":"You could also wait a week.","t":6,"meta":{}},
        {"id":"e7","role":"user","text":"Okay, what would that involve?","t":7,"meta":{}},
        {"id":"e8","role":"assistant","text":"Write down the dates and revisit it later.","t":8,"meta":{}},
        {"id":"e9","role":"user","text":"Thanks, that is clear.","t":9,"meta":{}},
    ]
    return {
        "id":"s_12345678", "participant_id":"PRIVATE-PARTICIPANT", "condition":"ai",
        "prompt":{"title":"Workplace dilemma","text":"Decide how to respond."},
        "writing":{"pre":{"text":"I initially favor confrontation."}}, "events":events,
        "candidates":[{"id":"c_one","source":"judge","start_event_id":"e2","end_event_id":"e5",
                       "trigger_event_id":"e2","type":"representational","secondary_type":"none",
                       "insight_evidence":"expressed","uptake":"observed","persuasion":"not_observed",
                       "copying":"not_observed","strength":"high","rationale":"PRIVATE JUDGE RATIONALE"}],
        "marks":[
            {"id":"m_overlap","source":"realtime","event_id":"e3","episode":{"start_event_id":"e2","end_event_id":"e5"},
             "review":{"status":"submitted","decision":"confirmed","primary":"representational","after":"PRIVATE REPORT"}},
            {"id":"m_missed","source":"realtime","event_id":"e8","episode":{"start_event_id":"e8","end_event_id":"e9"},
             "review":{"status":"submitted","decision":"confirmed","primary":"additive","after":"PRIVATE MISSED REPORT"}},
        ],
    }


class JudgeValidationTests(unittest.TestCase):
    def setUp(self):
        self.sample = validation.create_sample([session()], {
            "name":"Pilot", "seed":"fixed", "candidate_n":1, "report_n":1, "control_n":0,
            "coder_a":"Alice", "coder_b":"Bob",
        }, ["additive","representational","reflective","contrastive","counterfactual","associative","generative_error"])

    def test_sample_keeps_sources_private_and_deduplicates_overlap(self):
        self.assertEqual(self.sample["actual"], {"judge_candidate":1,"participant_report":1})
        self.assertEqual(self.sample["reference"]["confirmed_independent_reports"], 2)
        self.assertEqual(self.sample["reference"]["reports_with_passage_overlap"], 1)
        view = validation.coder_view(self.sample, [], self.sample["coders"][0]["token"])
        rendered = repr(view)
        for secret in ("PRIVATE-PARTICIPANT", "PRIVATE JUDGE RATIONALE", "PRIVATE REPORT", "PRIVATE MISSED REPORT",
                       "judge_candidate", "participant_report", "candidate_id", "mark_id", "session_id"):
            self.assertNotIn(secret, rendered)
        self.assertNotIn("private", view["items"][0])
        self.assertNotIn("id", view["items"][0]["events"][0])

    def test_two_independent_codes_and_metrics(self):
        codes = []
        for coder, evidence in zip(self.sample["coders"], ("expressed","possible")):
            for item in self.sample["items"]:
                coder_id, item_id, code = validation.validate_code(self.sample, coder["token"], {
                    "item_id":item["id"], "evidence":evidence, "primary_type":"representational",
                    "uptake":"observed", "span_quality":"adequate", "confidence":"high", "notes":"Reason",
                })
                codes.append({"item_id":item_id,"coder_id":coder_id,"code":code})
        result = validation.results(self.sample, codes)
        self.assertEqual(result["complete_items"], 2)
        self.assertEqual(result["agreement"]["evidence"]["raw"], 0)
        self.assertEqual(result["agreement"]["primary_type"]["raw"], 1)
        self.assertEqual(result["candidate_support"]["judge_candidate"]["both_transcript_supported"], 1)
        self.assertEqual({c["coder_id"] for c in result["rows"][0]["codes"]}, {"coder_a","coder_b"})
        exported = validation.export_csv(self.sample, codes)
        self.assertIn("judge_candidate", exported)
        self.assertIn("coder_a_evidence", exported)

    def test_rejects_invalid_or_wrong_token(self):
        item = self.sample["items"][0]
        with self.assertRaises(KeyError):
            validation.validate_code(self.sample, "wrong", {"item_id":item["id"]})
        with self.assertRaises(ValueError):
            validation.validate_code(self.sample, self.sample["coders"][0]["token"], {
                "item_id":item["id"], "evidence":"yes", "primary_type":"additive",
                "uptake":"observed", "span_quality":"adequate", "confidence":"high",
            })


if __name__ == "__main__":
    unittest.main()
