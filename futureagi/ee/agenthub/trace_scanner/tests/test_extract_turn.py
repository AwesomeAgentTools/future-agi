"""Regression tests for extract_turn — the turn-pairing fix.

The scanner used to receive the whole conversation as `task` and every span's
input/output as two unordered bags, so it paired a question from one turn with
an answer from another and reported failures that never happened. Hand-verifying
200 traces put that at a 78% false-positive rate.

These pin the properties that fix depends on. They deliberately encode the SDK's
BROKEN role tagging (history re-serialised as "user", only the generated message
tagged "assistant"), because that is what the extractor has to cope with —
measured across 200 traces: user 4644, assistant 386, in two-party conversations.
"""

from ee.agenthub.trace_scanner.compress import _plain, extract_turn


def _span(messages, outputs=()):
    """Build a span whose attributes carry gen_ai per-message keys."""
    attrs = {}
    for i, (role, content) in enumerate(messages):
        attrs[f"gen_ai.input.messages.{i}.message.role"] = role
        attrs[f"gen_ai.input.messages.{i}.message.content"] = content
    for i, (role, content) in enumerate(outputs):
        attrs[f"gen_ai.output.messages.{i}.message.role"] = role
        attrs[f"gen_ai.output.messages.{i}.message.content"] = content
    return ({"span_attributes": attrs}, 0)


class TestExtractTurn:
    def test_picks_the_turn_on_trial_not_the_whole_conversation(self):
        """The core fix: `task` is THIS turn's request, not every question asked.

        Mirrors the trace that produced "Failed to provide requested list of
        holdings" — the holdings WERE provided, three turns earlier.
        """
        flat = [_span(
            [
                ("system", "You are Ava."),
                ("user", "Hi!"),
                ("user", "Hello! What would you like help with?"),      # agent, mislabelled
                ("user", "Could you provide a list of my current holdings?"),
                ("user", "Could you provide your client ID?"),          # agent, mislabelled
                ("user", "Dr. Alistair Finch."),
                ("user", "Your portfolio holds: XOM, PG, VNQ..."),      # agent, mislabelled
                ("user", "And what is the total value of the portfolio?"),
            ],
            [("assistant", "As of June 7 2024, the total value is $289,210.78.")],
        )]
        history, request, response = extract_turn(flat)

        assert request == "And what is the total value of the portfolio?"
        assert "289,210.78" in response
        # the earlier holdings question is context, NOT the request on trial
        assert any("list of my current holdings" in h for h in history)
        assert "holdings" not in request

    def test_skips_the_trailing_tool_call_block(self):
        """The current request sits BEFORE this turn's assistant/tool messages."""
        flat = [_span(
            [
                ("system", "You are Ava."),
                ("user", "What is my largest position?"),
                ("assistant", ""),                       # tool-call message
                ("tool", '{"top_position_pct": 34.6}'),
            ],
            [("assistant", "Your largest position is 34.6% of the portfolio.")],
        )]
        _history, request, response = extract_turn(flat)

        assert request == "What is my largest position?"
        assert "34.6%" in response

    def test_consecutive_same_role_messages_do_not_break_it(self):
        """Why alternation was rejected: users double-message, tools interleave.

        An alternating reconstruction would mis-assign every message after the
        doubled turn; anchoring on the trailing block does not.
        """
        flat = [_span(
            [
                ("system", "You are Ava."),
                ("user", "I need my holdings."),
                ("user", "Actually, make that my performance."),   # two user turns in a row
            ],
            [("assistant", "Your 1-month return is 2.9%.")],
        )]
        _history, request, response = extract_turn(flat)

        assert request == "Actually, make that my performance."
        assert "2.9%" in response

    def test_picks_the_span_with_the_richest_history(self):
        """Orchestration spans carry only the first message; the LLM span has all.

        A LangGraph/agent span reports just "Hi!" no matter how far the
        conversation has run, so choosing the wrong span truncates everything.
        """
        thin = _span([("user", "Hi!")])
        rich = _span(
            [("system", "You are Ava."), ("user", "Hi!"), ("user", "What is my risk profile?")],
            [("assistant", "Your risk profile is aggressive.")],
        )
        _history, request, response = extract_turn([thin, rich, thin])

        assert request == "What is my risk profile?"
        assert "aggressive" in response

    def test_degrades_to_empty_when_no_per_message_attributes(self):
        """Producers that emit no structured messages must fall back safely,
        not blank out task/result."""
        flat = [({"span_attributes": {"input.value": "raw blob", "output.value": "reply"}}, 0)]
        history, request, response = extract_turn(flat)

        assert (history, request, response) == ([], "", "")

    def test_system_prompt_never_becomes_the_request(self):
        flat = [_span([("system", "You are Ava, a portfolio assistant.")],
                      [("assistant", "Hello!")])]
        history, request, _response = extract_turn(flat)

        assert request == ""
        assert history == []


class TestPlainTruncation:
    def test_preserves_negations_that_kevinify_destroyed(self):
        """The reason task/result are no longer kevinified.

        "didn't get the benchmark" -> "n't get benchmark" inverted the meaning of
        the exact sentence the model is asked to judge.
        """
        text = "I retrieved the 1-year return of 8.1%. However, I couldn't get the benchmark."
        out = _plain(text, 600)

        assert "couldn't" in out
        assert "n't get benchmark" not in out
        assert out == text

    def test_normalises_whitespace_and_truncates_with_ellipsis(self):
        out = _plain("a\n\n  b\tc", 600)
        assert out == "a b c"

        long = "x" * 700
        out = _plain(long, 600)
        assert len(out) == 600
        assert out.endswith("…")

    def test_empty_input_is_empty_string(self):
        assert _plain(None, 100) == ""
        assert _plain("", 100) == ""
