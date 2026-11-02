"""How a participant spoke in a voice session, described relative to themselves.

Timing comes from Codex's timestamped realtime transcript, so it exists for every
voice session. Acoustic numbers (pitch, loudness, pauses, syllable rate) are
measured in the participant's browser; audio never reaches this server. Nothing
here infers an insight: values are descriptions the participant and researcher
read alongside the participant's own moments.
"""

import json
import math
import re
import statistics
from pathlib import Path

from annotations import _merge

DEFAULTS = json.loads((Path(__file__).parent / "config/voice_measures.json").read_text())
SAMPLE_ID = re.compile(r"vs_[0-9a-f]{8,32}")
PITCH_FIELDS = ("pitch_hz_median", "pitch_hz_p10", "pitch_hz_p90")
# field: (lowest, highest, may be null). These reject impossible numbers (a
# level above full scale, a negative duration); pitch is held to the study's
# own pitch range, which is all the browser was allowed to report.
SAMPLE_NUMBERS = {
    "speech_ms": (0, None, False), "voiced_ms": (0, None, False), "overlap_ms": (0, None, False),
    "pause_count": (0, None, False), "longest_pause_ms": (0, None, False), "syllables": (0, None, False),
    "pitch_end_slope_st": (None, None, True), "loudness_db_mean": (None, 0, True), "loudness_db_p90": (None, 0, True),
    "remote_gap_ms": (0, None, True),
}


def settings_for_study(study):
    return _merge(DEFAULTS, (study or {}).get("voice_measures"))


def settings(s):
    """Settings snapshotted at session creation; older sessions use the defaults."""
    snapshot = s.get("config_snapshot") or {}
    return _merge(settings_for_study(snapshot.get("study")), snapshot.get("voice_measures"))


def visible_to_participant(s, cfg):
    mode = cfg["show_to_participant"]
    return cfg["enabled"] and s.get("interaction_mode") == "voice" and (
        mode == "always" or mode == "after_independent_review" and bool(s.get("independent_review")))


# --------------------------------------------------------------------------- browser samples

def _number(row, key, lo, hi, nullable):
    value = row.get(key)
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Invalid voice measure: {key}")
    if lo is not None and value < lo or hi is not None and value > hi:
        raise ValueError(f"Voice measure out of range: {key}")
    return round(float(value), 3)


def validate_sample(row, cfg):
    if not isinstance(row, dict) or not SAMPLE_ID.fullmatch(str(row.get("id", ""))):
        raise ValueError("Invalid voice sample id")
    start, end = _number(row, "start_ms", 0, None, False), _number(row, "end_ms", 0, None, False)
    duration = end - start
    if duration <= 0 or duration > cfg["acoustic"]["max_utterance_ms"]:
        raise ValueError("Invalid voice sample duration")
    sample = {"id": row["id"], "start_ms": start, "end_ms": end}
    for key, (lo, hi, nullable) in SAMPLE_NUMBERS.items():
        sample[key] = _number(row, key, lo, hi, nullable)
    for key in PITCH_FIELDS:
        sample[key] = _number(row, key, cfg["acoustic"]["pitch_min_hz"], cfg["acoustic"]["pitch_max_hz"], True)
    for key in ("speech_ms", "voiced_ms", "overlap_ms", "longest_pause_ms"):
        if sample[key] > duration + 1:
            raise ValueError(f"Voice measure longer than its utterance: {key}")
    quality = row.get("quality") or {}
    if not isinstance(quality, dict):
        raise ValueError("Invalid voice sample quality")
    sample["started_during_remote"] = bool(row.get("started_during_remote"))
    sample["quality"] = {
        "sample_rate": int(quality["sample_rate"]) if isinstance(quality.get("sample_rate"), (int, float)) else None,
        **{k: quality[k] if isinstance(quality.get(k), bool) else None
           for k in ("auto_gain_control", "echo_cancellation", "noise_suppression")}}
    return sample


def add_samples(s, body, cfg):
    if s.get("interaction_mode") != "voice":
        raise ValueError("Voice measures apply only to voice sessions")
    if not (cfg["enabled"] and cfg["acoustic"]["enabled"]):
        raise ValueError("Voice measurement is turned off for this study")
    # A final batch can arrive just after the participant leaves the conversation.
    if s["phase"] not in ("chat", "post"):
        raise ValueError("Voice measures are only accepted during the conversation")
    rows = body.get("samples")
    if not isinstance(rows, list) or not rows or len(rows) > cfg["acoustic"]["max_batch"]:
        raise ValueError("Send between 1 and %d voice samples" % cfg["acoustic"]["max_batch"])
    stored = s.setdefault("voice_samples", [])
    known = {v["id"] for v in stored}
    fresh = [validate_sample(row, cfg) for row in rows]
    fresh = [v for i, v in enumerate(fresh) if v["id"] not in known and v["id"] not in {x["id"] for x in fresh[:i]}]
    if len(stored) + len(fresh) > cfg["acoustic"]["max_samples"]:
        raise ValueError("This session already has the maximum number of voice samples")
    stored.extend(fresh)
    return fresh


# --------------------------------------------------------------------------- transcript timing

def _words(text):
    # Hyphenated forms stay one word, so a spoken "mm-hmm" is not read as two fillers.
    return re.findall(r"[\w'’]+(?:-[\w'’]+)*", (text or "").lower())


def segments(feed):
    """Transcript segments from the runtime feed, in the order Codex started them."""
    found, order = {}, []
    for row in feed:
        n = row.get("notification") or {}
        method, params = n.get("method"), n.get("params") or {}
        t = n.get("emittedAtMs")
        t = float(t) if isinstance(t, (int, float)) else float(row.get("received_t") or 0) * 1000
        if method == "thread/realtime/item/started":
            item = params.get("item") or {}
            if item.get("type") == "transcriptSegment" and item.get("role") in ("user", "assistant") and item.get("id") not in found:
                found[item["id"]] = {"id": item["id"], "role": item["role"], "start_ms": t, "end_ms": None,
                                     "last_ms": t, "text": item.get("text") or ""}
                order.append(item["id"])
        elif method == "thread/realtime/item/transcript/delta" and params.get("itemId") in found:
            found[params["itemId"]]["last_ms"] = t
        elif method == "thread/realtime/item/completed":
            item = params.get("item") or {}
            if item.get("id") in found:
                found[item["id"]].update(end_ms=t, text=item.get("text") or found[item["id"]]["text"])
    out = []
    for sid in order:
        seg = found[sid]
        seg["end_ms"] = seg["end_ms"] or seg["last_ms"]
        del seg["last_ms"]
        out.append(seg)
    return out


def turns(segs, events, cfg):
    """Merge segments into turns. A short agent acknowledgement ("Mm-hmm") said
    while the participant talks splits Codex's transcript; it is not a turn."""
    by_item = {e["meta"]["realtime_item_id"]: e["id"] for e in events if (e.get("meta") or {}).get("realtime_item_id")}
    out = []
    for seg in segs:
        backchannel = seg["role"] == "assistant" and len(_words(seg["text"])) <= cfg["timing"]["backchannel_max_words"]
        if backchannel:
            # Kept on the surrounding participant turn as evidence of overlap.
            if out and out[-1]["role"] == "user":
                out[-1]["backchannels"] += 1
            continue
        if out and out[-1]["role"] == seg["role"]:
            turn = out[-1]
            turn["end_ms"] = max(turn["end_ms"], seg["end_ms"])
            turn["text"] = (turn["text"].rstrip() + " " + seg["text"].strip()).strip()
        else:
            turn = {"role": seg["role"], "start_ms": seg["start_ms"], "end_ms": seg["end_ms"],
                    "text": seg["text"].strip(), "segment_ids": [], "event_ids": [], "backchannels": 0}
            out.append(turn)
        turn["segment_ids"].append(seg["id"])
        if seg["id"] in by_item:
            turn["event_ids"].append(by_item[seg["id"]])
    return out


def _phrases(words, phrases):
    joined = " " + " ".join(words) + " "
    return [p for p in phrases if " " + p + " " in joined]


# --------------------------------------------------------------------------- per-turn description

def _weighted_median(pairs):
    pairs = sorted((v, w) for v, w in pairs if v is not None and w > 0)
    if not pairs:
        return None
    total, running = sum(w for _, w in pairs), 0
    for value, weight in pairs:
        running += weight
        if running >= total / 2:
            return value
    return pairs[-1][0]


def _acoustic(samples):
    if not samples:
        return None
    samples = sorted(samples, key=lambda v: v["start_ms"])
    speech = sum(v["speech_ms"] for v in samples)
    own = speech - sum(v["overlap_ms"] for v in samples)
    voiced = sum(v["voiced_ms"] for v in samples)
    gaps = [(b["start_ms"] - a["end_ms"]) for a, b in zip(samples, samples[1:])]
    lows = [v["pitch_hz_p10"] for v in samples if v["pitch_hz_p10"]]
    highs = [v["pitch_hz_p90"] for v in samples if v["pitch_hz_p90"]]
    loud = [(v["loudness_db_mean"], v["speech_ms"] - v["overlap_ms"]) for v in samples if v["loudness_db_mean"] is not None]
    slopes = [v["pitch_end_slope_st"] for v in samples if v["pitch_end_slope_st"] is not None]
    quality = [v.get("quality") or {} for v in samples]
    return {
        "samples": len(samples), "speech_s": speech / 1000, "voiced_s": voiced / 1000,
        "pitch_hz": _weighted_median((v["pitch_hz_median"], v["voiced_ms"]) for v in samples),
        "pitch_range_st": 12 * math.log2(max(highs) / min(lows)) if lows and highs and max(highs) > min(lows) else None,
        "pitch_end_slope_st": slopes[-1] if slopes else None,
        "loudness_db": sum(v * w for v, w in loud) / sum(w for _, w in loud) if sum(w for _, w in loud) > 0 else None,
        "loudness_peak_db": max((v["loudness_db_p90"] for v in samples if v["loudness_db_p90"] is not None), default=None),
        "pause_count": sum(v["pause_count"] for v in samples) + len(gaps),
        "longest_pause_s": max([v["longest_pause_ms"] for v in samples] + gaps) / 1000,
        # Syllables are only counted while the agent is silent, so the rate uses that time too.
        "syllable_rate": sum(v["syllables"] for v in samples) / (own / 1000) if own > 0 else None,
        "overlap_s": sum(v["overlap_ms"] for v in samples) / 1000,
        "remote_gap_s": samples[0]["remote_gap_ms"] / 1000 if samples[0]["remote_gap_ms"] is not None else None,
        "started_during_remote": samples[0]["started_during_remote"],
        "auto_gain_control": any(q.get("auto_gain_control") for q in quality),
    }


def _match(samples, user_turns, tolerance):
    owner = {}
    for sample in samples:
        best, best_overlap = None, 0
        for i, turn in enumerate(user_turns):
            overlap = min(sample["end_ms"], turn["end_ms"] + tolerance) - max(sample["start_ms"], turn["start_ms"] - tolerance)
            if overlap > best_overlap:
                best, best_overlap = i, overlap
        if best is not None:
            owner.setdefault(best, []).append(sample)
    return owner


def _robust(values):
    values = [v for v in values if v is not None]
    if not values:
        return None, None
    middle = statistics.median(values)
    spread = 1.4826 * statistics.median(abs(v - middle) for v in values)
    return middle, spread


def _stands_out(delta, spread, min_delta, z):
    if delta is None or abs(delta) < min_delta:
        return False
    return not spread or abs(delta) / spread >= z


def _round(value, digits=1):
    return round(value, digits) if isinstance(value, (int, float)) else value


def profile(s, feed, cfg=None):
    cfg = cfg or settings(s)
    timing, notable_cfg = cfg["timing"], cfg["notable"]
    least, z, weights = notable_cfg["min_delta"], notable_cfg["z"], notable_cfg["weights"]
    all_turns = turns(segments(feed), s.get("events", []), cfg)
    samples = s.get("voice_samples") or []
    user_turns = [t for t in all_turns if t["role"] == "user"]
    owner = _match(samples, user_turns, cfg["acoustic"]["match_tolerance_ms"])
    rows, previous = [], None
    for turn in all_turns:
        if turn["role"] != "user":
            previous = turn
            continue
        words = _words(turn["text"])
        duration = (turn["end_ms"] - turn["start_ms"]) / 1000
        fillers = sum(w in timing["fillers"] for w in words)
        rows.append({
            "index": len(rows), "event_ids": turn["event_ids"], "start_ms": turn["start_ms"], "end_ms": turn["end_ms"],
            "text": turn["text"], "after_agent": previous is not None,
            "transcript_gap_s": (turn["start_ms"] - previous["end_ms"]) / 1000 if previous else None,
            "duration_s": duration, "words": len(words), "wpm": len(words) / duration * 60 if duration > 0 else None,
            "fillers": fillers, "filler_rate": fillers / len(words) * 100 if words else None,
            "change_tokens": _phrases(words[:timing["change_token_window_words"]], timing["change_tokens"]),
            "agent_backchannels": turn["backchannels"],
            "acoustic": _acoustic(owner.get(len(rows), [])),
        })
        previous = None

    # One source per session, so every reply gap shares a baseline: the agent's
    # audio as the browser heard it when every reply has it, else the transcript.
    replies = [r for r in rows if r["after_agent"]]
    audio_gaps = bool(replies) and all(r["acoustic"] and r["acoustic"]["remote_gap_s"] is not None
                                       or r["acoustic"] and r["acoustic"]["started_during_remote"] for r in replies)
    for r in rows:
        a = r["acoustic"] or {}
        if not r["after_agent"]:
            r["gap_s"] = None
        elif audio_gaps:
            r["gap_s"] = -1.0 * (a.get("overlap_s") or 0) if a.get("started_during_remote") else a["remote_gap_s"]
        else:
            r["gap_s"] = r["transcript_gap_s"]
        r["overlap"] = bool(a.get("started_during_remote") or
                            r["transcript_gap_s"] is not None and r["transcript_gap_s"] <= -timing["overlap_min_s"])

    # Pace uses syllables when the browser measured them; words/min otherwise.
    use_syllables = sum(1 for r in rows if (r["acoustic"] or {}).get("syllable_rate")) >= notable_cfg["min_turns"]
    pace = lambda r: (r["acoustic"] or {}).get("syllable_rate") if use_syllables else r["wpm"]
    metric = {
        "gap_s": lambda r: r["gap_s"] if r["gap_s"] is not None and r["gap_s"] >= 0 else None,
        "pitch_hz": lambda r: (r["acoustic"] or {}).get("pitch_hz"),
        "loudness_db": lambda r: (r["acoustic"] or {}).get("loudness_db"),
        "pace": pace,
        "pause_s": lambda r: (r["acoustic"] or {}).get("longest_pause_s"),
        "filler_rate": lambda r: r["filler_rate"],
    }
    stats = {k: _robust([f(r) for r in rows]) for k, f in metric.items()}
    enough = {k: sum(f(r) is not None for r in rows) >= notable_cfg["min_turns"] for k, f in metric.items()}

    for r in rows:
        rel, flags, score = {}, [], 0.0
        a = r["acoustic"] or {}

        def compare(key, delta, spread, minimum, up, down, shown):
            nonlocal score
            if enough[key] and _stands_out(delta, spread, minimum, z):
                flags.append({"key": up if delta > 0 else down, "value": _round(shown)})
                score += abs(delta) / spread if spread else abs(delta) / minimum

        base_gap, spread_gap = stats["gap_s"]
        if metric["gap_s"](r) is not None and base_gap is not None:
            rel["gap_s"] = r["gap_s"] - base_gap
            if rel["gap_s"] > 0:
                compare("gap_s", rel["gap_s"], spread_gap, least["gap_s"], "long_gap", "long_gap", r["gap_s"])
        if r["overlap"]:
            flags.append({"key": "overlap", "value": None})
            score += weights["overlap"]
        base_pitch, spread_pitch = stats["pitch_hz"]
        if a.get("pitch_hz") and base_pitch:
            rel["pitch_st"] = 12 * math.log2(a["pitch_hz"] / base_pitch)
            spread = 12 * math.log2(1 + spread_pitch / base_pitch) if spread_pitch else None
            compare("pitch_hz", rel["pitch_st"], spread, least["pitch_st"], "pitch_up", "pitch_down", abs(rel["pitch_st"]))
        base_loud, spread_loud = stats["loudness_db"]
        if a.get("loudness_db") is not None and base_loud is not None:
            rel["loudness_db"] = a["loudness_db"] - base_loud
            compare("loudness_db", rel["loudness_db"], spread_loud, least["loudness_db"], "louder", "quieter", abs(rel["loudness_db"]))
        base_pace, spread_pace = stats["pace"]
        if pace(r) and base_pace:
            rel["rate_pct"] = (pace(r) / base_pace - 1) * 100
            spread = spread_pace / base_pace * 100 if spread_pace else None
            compare("pace", rel["rate_pct"], spread, least["rate_pct"], "faster", "slower", round(abs(rel["rate_pct"])))
        base_pause, spread_pause = stats["pause_s"]
        if a.get("longest_pause_s") is not None and base_pause is not None and a["longest_pause_s"] > base_pause:
            compare("pause_s", a["longest_pause_s"] - base_pause, spread_pause, least["pause_s"], "long_pause", "long_pause", a["longest_pause_s"])
        if a.get("pitch_end_slope_st") is not None and a["pitch_end_slope_st"] >= notable_cfg["rising_end_st"]:
            flags.append({"key": "rising_end", "value": _round(a["pitch_end_slope_st"])})
            score += weights["rising_end"]
        for token in r["change_tokens"]:
            flags.append({"key": "change_token", "value": token})
            score += weights["change_token"]
        base_fillers = stats["filler_rate"][0]
        if enough["filler_rate"] and r["filler_rate"] is not None and r["filler_rate"] - base_fillers >= least["filler_rate"]:
            flags.append({"key": "fillers", "value": r["fillers"]})
            score += weights["fillers"]
        r["relative"] = {k: _round(v) for k, v in rel.items()}
        r["flags"], r["score"] = flags, round(score, 2)
        for key in ("gap_s", "transcript_gap_s", "duration_s", "wpm", "filler_rate"):
            r[key] = _round(r[key])
        if r["acoustic"]:
            r["acoustic"] = {k: _round(v, 2) for k, v in r["acoustic"].items()}

    ranked = sorted((r for r in rows if r["flags"]), key=lambda r: -r["score"])
    acoustic_rows = [r for r in rows if r["acoustic"]]
    speech = sum(r["acoustic"]["speech_s"] for r in acoustic_rows)
    return {
        "available": {"timing": bool(rows), "acoustic": bool(acoustic_rows)},
        "gap_source": "audio" if audio_gaps else "transcript",
        "pace_unit": "syllables_per_s" if use_syllables else "words_per_min",
        "baseline": {"gap_s": _round(stats["gap_s"][0]), "wpm": _round(_robust([r["wpm"] for r in rows])[0]),
                     "syllable_rate": _round(stats["pace"][0], 2) if use_syllables else None,
                     "pitch_hz": _round(stats["pitch_hz"][0], 0), "loudness_db": _round(stats["loudness_db"][0]),
                     "filler_rate": _round(stats["filler_rate"][0])},
        "enough_turns": len(rows) >= notable_cfg["min_turns"],
        "turns": rows,
        "notable": [r["index"] for r in ranked[:notable_cfg["max_listed"]]],
        "quality": {"samples": len(samples), "matched_samples": sum(len(v) for v in owner.values()),
                    "echo_share": _round(sum(r["acoustic"]["overlap_s"] for r in acoustic_rows) / speech, 2) if speech else None,
                    "auto_gain_control": any(r["acoustic"]["auto_gain_control"] for r in acoustic_rows)},
    }
