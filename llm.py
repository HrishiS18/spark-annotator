"""Optional Anthropic text-only chat/judge adapter and explicitly selected demo.

Live mode needs `pip install anthropic` plus credentials the SDK can resolve
(ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile).
This adapter is separate from the default Codex agent and restricted Codex judge.
Missing credentials raise errors; scripted responses require explicit demo mode.
"""

import json
import os
import random

MODEL = os.environ.get("SPARK_MODEL", "claude-opus-5-5")
CHAT_EFFORT = os.environ.get("SPARK_CHAT_EFFORT", "medium")
JUDGE_MODEL = os.environ.get("SPARK_JUDGE_MODEL", MODEL)
JUDGE_EFFORT = os.environ.get("SPARK_JUDGE_EFFORT", "high")
FALLBACK_BETA = "server-side-fallback-2026-07-01"

try:
    import anthropic
except ImportError:  # demo mode
    anthropic = None

_client = None
_client_error = None


class LLMError(RuntimeError):
    pass


def _has_credentials():
    # The SDK resolves credentials lazily, so check the sources it would use.
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if os.environ.get("ANTHROPIC_PROFILE") or os.environ.get("ANTHROPIC_FEDERATION_RULE_ID"):
        return True
    return os.path.isdir(os.path.expanduser("~/.config/anthropic"))


def client():
    global _client, _client_error
    if _client is None and _client_error is None:
        if anthropic is None:
            _client_error = "anthropic SDK not installed"
        elif os.environ.get("SPARK_DEMO") == "1":
            _client_error = "SPARK_DEMO=1"
        elif not _has_credentials():
            _client_error = "no Anthropic credentials (set ANTHROPIC_API_KEY)"
        else:
            try:
                _client = anthropic.Anthropic()
            except Exception as exc:  # no resolvable credentials
                _client_error = str(exc)
    return _client


def status():
    c = client()
    return {"live": c is not None, "model": MODEL if c else None,
            "reason": None if c else _client_error}


def _create(**kwargs):
    c = client()
    if c is None:
        raise LLMError(f"LLM unavailable: {_client_error}")
    with _api_errors():
        resp = c.beta.messages.create(model=MODEL, betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    if resp.stop_reason == "refusal":
        raise LLMError("The model declined this request")
    return resp


class _api_errors:
    """Translate SDK exceptions into LLMError with a readable message (most specific first)."""

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if exc is None or anthropic is None:
            return False
        if isinstance(exc, anthropic.BadRequestError):
            raise LLMError(f"Bad request: {exc.message}") from exc
        if isinstance(exc, anthropic.AuthenticationError):
            raise LLMError("Authentication failed: check ANTHROPIC_API_KEY") from exc
        if isinstance(exc, anthropic.RateLimitError):
            raise LLMError("Rate limited by the API; try again shortly") from exc
        if isinstance(exc, anthropic.APIStatusError):
            raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
        if isinstance(exc, anthropic.APIConnectionError):
            raise LLMError("Could not reach the Anthropic API") from exc
        return False


def judge_call(system, prompt, schema, model=None, effort=None):
    """Long-context structured call for the LLM judge. Streams (the transcript can be long)
    and returns (parsed_json, usage_dict)."""
    c = client()
    if c is None:
        raise LLMError(f"LLM unavailable: {_client_error}")
    model = model or JUDGE_MODEL
    with _api_errors():
        with c.messages.stream(
            model=model, max_tokens=64000, system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort or JUDGE_EFFORT, "format": {"type": "json_schema", "schema": schema}},
        ) as stream:
            resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        raise LLMError("The model declined this request")
    if resp.stop_reason == "max_tokens":
        raise LLMError("The judge's answer was cut off (max_tokens)")
    u = resp.usage
    usage = {"model": resp.model, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens}
    usage["cost_usd"] = None
    return json.loads(_text(resp)), usage


def _text(resp):
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


def chat(system, messages):
    """messages: [{"role": "user"|"assistant", "content": str}] (alternating, user last)."""
    resp = _create(max_tokens=16000, system=system, messages=messages,
                   output_config={"effort": CHAT_EFFORT})
    return _text(resp)




# --------------------------------------------------------------------------- demo partner

_DEMO_MOVES = {
    "partner": [
        "That's a clear starting point. One thing you haven't mentioned yet is who else is affected besides the people directly involved. Does bringing in a third party change your view at all?",
        "Interesting. It sounds like you're treating this mainly as a question about outcomes. Another way to frame it is as a question about what kind of relationship or trust is at stake. Does that framing fit how you actually feel about it?",
        "What would have to be different about the situation for you to reach the opposite conclusion?",
        "You said something that seems important: you care about whether people chose their situation. Is that the principle doing most of the work in your answer?",
        "Here's a consideration that pulls the other way: if everyone followed your rule, would the result still look reasonable?",
    ],
    "pushback": [
        "I'm not convinced. Your argument seems to assume the consequences are predictable, but in most real cases they aren't. If you can't know the outcome, does your position still hold?",
        "That rule seems to prove too much. Applied consistently, it would also justify cases I suspect you'd reject. Where exactly would you draw the line, and why there?",
        "Let me push on that: you're weighting your own intuitions heavily, but intuitions in cases like this are often shaped by self-interest. What's the strongest argument against your view?",
    ],
    "socratic": [
        "What do you mean by 'fair' here? Fair to whom?",
        "Why does that matter to you? What would be lost if it didn't hold?",
        "Can you think of a case where your principle gives an answer you'd be uncomfortable with?",
        "If you had to explain your view to someone who disagrees, what's the one reason you'd lead with?",
    ],
    "agreeable": [
        "That's a really thoughtful way to look at it. Building on your point, it seems like you're valuing honesty because it respects people's ability to make their own decisions.",
        "I think you're right to weigh that heavily. Could you say more about why it matters so much to you?",
    ],
    "trading": [
        "Before rules, let's pin down your edge. What do you know about this market that a typical investor doesn't? That's usually where a thesis should start.",
        "Good. Now think about what would prove you wrong. What specific signal would make you exit, and how much are you willing to lose before that happens?",
        "One thing to consider: a lot of short-term price movement is noise. Would your thesis still make sense over 6 to 12 months, or does it depend on a near-term event?",
        "Let's make it concrete: which instruments would you use (individual stocks, an ETF, options), and how would you size each position?",
    ],
}


def demo_reply(persona_key, history):
    moves = _DEMO_MOVES.get(persona_key, _DEMO_MOVES["partner"])
    n_ai = sum(1 for m in history if m["role"] == "assistant")
    if n_ai < len(moves):
        return "[demo] " + moves[n_ai]
    return "[demo] " + random.choice(moves)
