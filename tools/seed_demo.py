"""Create two demo sessions so the researcher dashboard has something to show.

    .venv/bin/python tools/seed_demo.py

1. A moral-dilemma session (AI partner) with before/after essays and reviewed marks.
2. An external Claude Code session (the Lean "generative error" example) imported from
   samples/claude_code_lean_session.jsonl, with companion-window marks aligned by time.
No automatic detection or inference runs; researchers can explicitly invoke the LLM judge.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
import parsers  # noqa: E402

DILEMMA_CHAT = [
    ("user", "I think the drunk driver who hit someone is clearly more to blame. Something terrible actually happened because of them."),
    ("assistant", "That's a very common intuition. But consider: both drivers made exactly the same choice, with the same information and the same risk. The only difference is something neither of them controlled: whether a pedestrian happened to step out. If blame tracks what we control, shouldn't they be equally blameworthy?"),
    ("user", "Hmm, I hadn't really separated the choice from the outcome like that. But it still feels like the outcome matters."),
    ("assistant", "It might help to ask what blame is for. If blame is about judging someone's character, the two look identical. If it's about responding to harm, for example compensating the victim's family, the outcome clearly matters. Maybe your intuition and the 'equal blame' view are answering different questions."),
    ("user", "Oh, that's a different way to look at it. So it's really about what we mean by blame. I think I believe both: same moral fault, but different responsibility to repair."),
    ("assistant", "That's a coherent position, and close to what some philosophers call the distinction between culpability and liability. Here's a challenge: if the lucky driver is just as culpable, should they be punished just as severely by the law?"),
    ("user", "No, I don't agree with that. Punishing someone for a crash that didn't happen seems wrong. Now that I say it, I realise I care about punishment being tied to actual harm because otherwise the law would be punishing people for bad luck in the other direction."),
    ("assistant", "Interesting, so you'd accept moral luck in the law but not in moral judgement. What if we could perfectly detect every drunk drive? Would you then want both drivers punished equally?"),
    ("user", "What if we could... then I think I'd want equal punishment for the driving itself, plus extra liability for the harm. That actually reminds me of how speeding tickets work versus being sued after an accident."),
    ("assistant", "That maps neatly onto your view: a penalty for the risky act, and liability for the harm caused. It also suggests your answer to the original question is 'equally blameworthy, unequally liable'."),
]
PRE = ("The driver who hit the pedestrian is more blameworthy. They caused a death and the other driver didn't. "
       "Both did something wrong but the outcome makes one of them much worse. We judge people by what they actually do.")
POST = ("Both drivers are equally blameworthy because they made the same choice with the same risk. "
        "But they are not equally liable: the driver who caused harm owes repair to the victim's family. "
        "Blame and responsibility to repair are different questions. "
        "The law should penalise the risky act itself and add liability for harm, like speeding tickets versus being sued after an accident. "
        "We should not punish people for bad luck.")
# (event index in DILEMMA_CHAT, quick_type, review)
DILEMMA_MARKS = [
    (1, None, {"types": ["additive"], "primary": "additive", "before": "I only thought about the outcome.",
               "after": "Separated the choice from the outcome.", "uptake": "integrated", "latency": "immediate", "intensity": 3}),
    (3, "representational", {"types": ["representational"], "primary": "representational", "before": "Blame is one thing.",
                             "after": "Blame might mean judging character or repairing harm.", "uptake": "integrated", "latency": "immediate", "intensity": 5}),
    (6, "reflective", {"types": ["reflective", "contrastive"], "primary": "reflective", "before": "",
                       "after": "Realised why I tie punishment to harm.", "uptake": "integrated", "latency": "immediate", "intensity": 4}),
    (9, None, None),
]


def seed_dilemma():
    s = server.create_session({"study": "dilemma", "prompt_id": "d-moral-luck", "persona": "pushback",
                               "participant_id": "DEMO-01", "condition": "ai", "runtime": "demo"},allow_legacy=True)
    server.advance(s, "pre", PRE)
    for role, text in DILEMMA_CHAT:
        server.add_event(s, role, text)
    for idx, quick, review in DILEMMA_MARKS:
        iso, t = server.now()
        s["marks"].append({"id": server.new_id("m"), "created_at": iso, "t": t, "phase": "chat", "source": "overlay",
                           "anchor": "message", "event_id": s["events"][idx]["id"], "quote": None,
                           "quick_type": quick, "draft_len": 0, "review": review})
    server.advance(s, "chat")
    server.advance(s, "post", POST)
    server.save(s)
    return s


def seed_agentic():
    s = server.create_session({"study": "agentic", "participant_id": "DEMO-02",
                               "task": "Formalise 'every group of prime order is cyclic' in Lean 4 with a coding agent."})
    path = os.path.join(server.ROOT, "samples", "claude_code_lean_session.jsonl")
    raw = open(path, encoding="utf-8").read()
    events, meta = parsers.parse_transcript(raw)
    t0 = events[0]["t"] - 5  # recording started 5 s before the first prompt
    duration = events[-1]["t"] - t0 + 25
    # Overlay flags (wall-clock): one typed straight away, one bookmarked to explain later.
    for ts, quick in [("2026-10-11T15:03:20Z", "generative_error"), ("2026-10-11T15:08:10Z", None)]:
        iso, t = parsers.parse_ts(ts)
        s["marks"].append({"id": server.new_id("m"), "created_at": iso, "t": t, "phase": "chat", "source": "overlay",
                           "anchor": "timestamp", "event_id": None, "quote": None, "quick_type": quick,
                           "draft_len": None, "review": None})
    s["events"] = events
    parsers.align_marks(s["marks"], events)
    s["imports"].append({"at": server.now()[0], "file": "claude_code_lean_session.jsonl", "members": [path],
                         "meta": meta, "events": len(events)})
    # No video in the seed (a real one comes from /record); the timeline still uses this start time.
    s["recording"] = {"status": "none", "started_t": t0, "started_at": parsers.parse_ts(t0)[0], "duration": duration}
    server.advance(s, "chat")
    server.save(s)
    return s


if __name__ == "__main__":
    a = seed_dilemma()
    b = seed_agentic()
    print("Seeded:", a["id"], "(dilemma)", b["id"], "(external agent)")
    print("No model calls during seeding. Open an external review to generate suggestions, or judge the scripted demo manually.")
