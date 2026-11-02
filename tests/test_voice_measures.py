import copy
import unittest

import annotations
import voice_measures as vm

T0 = 1_800_000_000_000


def feed_for(segments):
    """Fake Codex realtime notifications: (id, role, start_s, end_s, text)."""
    rows = []
    for sid, role, start, end, text in segments:
        rows.append({"received_t": 0, "notification": {"method": "thread/realtime/item/started", "emittedAtMs": T0 + start * 1000,
                     "params": {"item": {"id": sid, "role": role, "type": "transcriptSegment", "text": ""}}}})
        rows.append({"received_t": 0, "notification": {"method": "thread/realtime/item/transcript/delta", "emittedAtMs": T0 + start * 1000 + 100,
                     "params": {"itemId": sid, "delta": text[:5]}}})
        rows.append({"received_t": 0, "notification": {"method": "thread/realtime/item/completed", "emittedAtMs": T0 + end * 1000,
                     "params": {"item": {"id": sid, "role": role, "type": "transcriptSegment", "text": text}}}})
    rows.sort(key=lambda r: r["notification"]["emittedAtMs"])
    return rows


def session(segments, **voice):
    cfg = annotations._merge(vm.DEFAULTS, voice)
    return {"id": "s_00000001", "interaction_mode": "voice", "phase": "chat", "voice_samples": [],
            "config_snapshot": {"voice_measures": cfg},
            "events": [{"id": "ev_" + sid, "role": role, "text": text, "meta": {"voice": True, "realtime_item_id": sid}}
                       for sid, role, _, _, text in segments]}


# Six exchanges; the participant's fourth reply comes after a long silence and opens with "oh".
CONVERSATION = [
    ("a1", "assistant", 0, 10, "Here is a first thought about credit for assisted work and who deserves it."),
    ("u1", "user", 12, 20, "I think the person deserves very little credit if the tool wrote it"),
    ("a2", "assistant", 21, 31, "Consider a photographer, whose camera does much of the work too, yet we credit them."),
    ("u2", "user", 33, 41, "That is fair but a camera does not choose the words for you"),
    ("a3", "assistant", 42, 52, "Then perhaps credit follows the decisions someone makes, not who typed the words."),
    ("u3", "user", 54, 60, "So the choices matter more than the typing itself here"),
    ("bc", "assistant", 57, 57.5, "Mm-hmm."),
    ("u3b", "user", 58, 62, "at least most of the time"),
    ("a4", "assistant", 63, 73, "Yes, and disclosure lets others judge how much of the work was yours."),
    ("u4", "user", 85, 93, "Oh I see so honesty about the process is what keeps it fair to others"),
    ("a5", "assistant", 94, 104, "Exactly, the obligation is about honesty more than the amount of help."),
    ("u5", "user", 106, 113, "Right that makes sense to me now overall I think"),
]


def sample(sid, start_s, end_s, pitch, loud, syllables, **extra):
    speech = (end_s - start_s) * 1000
    return {"id": "vs_" + sid.ljust(16, "0"), "start_ms": T0 + start_s * 1000, "end_ms": T0 + end_s * 1000,
            "speech_ms": speech * 0.8, "voiced_ms": speech * 0.6, "overlap_ms": 0, "pause_count": 1,
            "longest_pause_ms": 400, "syllables": syllables, "pitch_hz_median": pitch, "pitch_hz_p10": pitch * 0.9,
            "pitch_hz_p90": pitch * 1.1, "pitch_end_slope_st": 0.5, "loudness_db_mean": loud, "loudness_db_p90": loud + 4,
            "remote_gap_ms": None, "started_during_remote": False,
            "quality": {"sample_rate": 48000, "auto_gain_control": True, "echo_cancellation": True, "noise_suppression": True}, **extra}


class TimingTests(unittest.TestCase):
    def test_backchannel_does_not_split_a_turn(self):
        s = session(CONVERSATION)
        turns = vm.turns(vm.segments(feed_for(CONVERSATION)), s["events"], vm.settings(s))
        user = [t for t in turns if t["role"] == "user"]
        self.assertEqual(len(user), 5)
        self.assertEqual(user[2]["event_ids"], ["ev_u3", "ev_u3b"])
        self.assertEqual(user[2]["backchannels"], 1)
        self.assertTrue(user[2]["text"].endswith("most of the time"))

    def test_timing_profile_relative_to_own_turns(self):
        s = session(CONVERSATION)
        p = vm.profile(s, feed_for(CONVERSATION))
        self.assertEqual(p["available"], {"timing": True, "acoustic": False})
        self.assertEqual(p["pace_unit"], "words_per_min")
        turns = p["turns"]
        self.assertEqual([t["gap_s"] for t in turns], [2.0, 2.0, 2.0, 12.0, 2.0])
        self.assertEqual(p["baseline"]["gap_s"], 2.0)
        self.assertEqual(turns[3]["change_tokens"], ["oh", "i see"])
        keys = [f["key"] for f in turns[3]["flags"]]
        self.assertIn("long_gap", keys)
        self.assertEqual(turns[3]["flags"][0]["value"], 12.0)
        self.assertEqual(p["notable"][0], 3)
        self.assertEqual(turns[0]["flags"], [])

    def test_acknowledgements_are_not_fillers_and_fillers_are_a_rate(self):
        convo = [list(row) for row in CONVERSATION]
        convo[9][4] = "Mm-hmm. I see, so honesty about the process is what keeps it fair to others"
        convo[11][4] = "Um so uh I um think uh that makes sense"
        convo = [tuple(row) for row in convo]
        p = vm.profile(session(convo), feed_for(convo))
        self.assertEqual(p["turns"][3]["fillers"], 0)
        self.assertEqual(p["turns"][3]["change_tokens"], ["i see"])
        last = p["turns"][4]
        self.assertEqual(last["fillers"], 4)
        self.assertIn({"key": "fillers", "value": 4}, last["flags"])

    def test_too_few_turns_describe_without_flags(self):
        short = CONVERSATION[:4]
        p = vm.profile(session(short), feed_for(short))
        self.assertFalse(p["enough_turns"])
        self.assertFalse(any(f["key"] == "long_gap" for t in p["turns"] for f in t["flags"]))

    def test_settings_come_from_session_snapshot(self):
        s = session(CONVERSATION, notable={"min_delta": {"gap_s": 20}})
        p = vm.profile(s, feed_for(CONVERSATION))
        self.assertNotIn("long_gap", [f["key"] for f in p["turns"][3]["flags"]])


class AcousticTests(unittest.TestCase):
    def setUp(self):
        self.s = session(CONVERSATION)
        # Steady voice, then a higher, louder fourth reply.
        for i, (start, end) in enumerate([(12.2, 19.8), (33.1, 40.9), (54.2, 61.8), (85.1, 92.8), (106.2, 112.9)]):
            pitch, loud = (150, -30) if i != 3 else (190, -22)
            self.s["voice_samples"].append(sample(f"{i:x}", start, end, pitch, loud, int((end - start) * 4),
                                                  remote_gap_ms=11800 if i == 3 else 1800))

    def test_samples_matched_to_turns_and_compared_with_baseline(self):
        p = vm.profile(self.s, feed_for(CONVERSATION))
        self.assertTrue(p["available"]["acoustic"])
        self.assertEqual(p["pace_unit"], "syllables_per_s")
        self.assertEqual(p["quality"]["matched_samples"], 5)
        fourth = p["turns"][3]
        self.assertEqual(p["gap_source"], "audio")
        self.assertEqual(fourth["gap_s"], 11.8)
        self.assertEqual(fourth["transcript_gap_s"], 12.0)
        self.assertAlmostEqual(fourth["relative"]["pitch_st"], 4.1, places=1)
        self.assertEqual(fourth["relative"]["loudness_db"], 8.0)
        keys = {f["key"] for f in fourth["flags"]}
        self.assertTrue({"pitch_up", "louder", "long_gap"} <= keys)
        self.assertEqual(p["baseline"]["pitch_hz"], 150)

    def test_gap_source_is_never_mixed_within_a_session(self):
        self.s["voice_samples"][1]["remote_gap_ms"] = None
        p = vm.profile(self.s, feed_for(CONVERSATION))
        self.assertEqual(p["gap_source"], "transcript")
        self.assertEqual([t["gap_s"] for t in p["turns"]], [2.0, 2.0, 2.0, 12.0, 2.0])

    def test_syllable_rate_uses_time_the_agent_was_silent(self):
        v = self.s["voice_samples"][0]
        v.update(speech_ms=4000, overlap_ms=2000, syllables=8)
        a = vm._acoustic([v])
        self.assertEqual(a["syllable_rate"], 4.0)

    def test_add_samples_validates_and_is_idempotent(self):
        cfg = vm.settings(self.s)
        fresh = {"id": "vs_aaaaaaaaaaaaaaaa", **{k: v for k, v in sample("x", 1, 2, 120, -40, 4).items() if k != "id"}}
        self.assertEqual(len(vm.add_samples(self.s, {"samples": [fresh, fresh]}, cfg)), 1)
        self.assertEqual(vm.add_samples(self.s, {"samples": [fresh]}, cfg), [])
        for bad in ({"id": "nope"}, {**fresh, "id": "vs_bbbbbbbbbbbbbbbb", "end_ms": fresh["start_ms"]},
                    {**fresh, "id": "vs_cccccccccccccccc", "loudness_db_mean": 12},
                    {**fresh, "id": "vs_dddddddddddddddd", "speech_ms": 99999},
                    {**fresh, "id": "vs_eeeeeeeeeeeeeeee", "pitch_hz_median": float("nan")},
                    {**fresh, "id": "vs_ffffffffffffffff", "pitch_hz_median": cfg["acoustic"]["pitch_max_hz"] + 1}):
            with self.assertRaises(ValueError):
                vm.add_samples(self.s, {"samples": [bad]}, cfg)
        text = copy.deepcopy(self.s)
        text["interaction_mode"] = "text"
        with self.assertRaisesRegex(ValueError, "voice sessions"):
            vm.add_samples(text, {"samples": [fresh]}, cfg)
        done = copy.deepcopy(self.s)
        done["phase"] = "review"
        with self.assertRaisesRegex(ValueError, "during the conversation"):
            vm.add_samples(done, {"samples": [fresh]}, cfg)
        off = annotations._merge(cfg, {"acoustic": {"enabled": False}})
        with self.assertRaisesRegex(ValueError, "turned off"):
            vm.add_samples(self.s, {"samples": [fresh]}, off)

    def test_visibility_follows_settings(self):
        cfg = vm.settings(self.s)
        self.assertFalse(vm.visible_to_participant(self.s, cfg))
        self.s["independent_review"] = {"mark_ids": []}
        self.assertTrue(vm.visible_to_participant(self.s, cfg))
        self.assertFalse(vm.visible_to_participant(self.s, {**cfg, "show_to_participant": "never"}))


if __name__ == "__main__":
    unittest.main()
