"""A failure the model cannot quote is a failure that did not happen.

The V8 prompt already states this rule — "If you cannot quote the required
evidence, the verdict for that dimension is PASS. An unquotable failure is a
hallucinated failure" — but enforced it on the honour system. The evidence the
model returned was never checked against what it was shown, and was then thrown
away.

Auditing a 2,107-trace corpus found roughly one flagged trace in three did not
exhibit the issue its row claimed, including rows asserting an agent had
fabricated figures that were present verbatim in the tool output. Those claims
fail this gate deterministically, with no model change.

The gate is fail-closed on purpose: a dropped real issue costs recall, which is
recoverable, while a surfaced invented one costs the user's trust in every other
row, which is not.
"""

import pytest

from ee.agenthub.trace_scanner.scanner import (
    TraceScanner,
    _evidence_is_quotable,
    _norm_for_match,
)

TRACE = _norm_for_match(
    "User: how long did the export take?\n"
    "Tool generate_insight returned: total 80.27 seconds, 9.21 seconds average\n"
    "Agent: The export took ~80 seconds, averaging ~9 seconds per file."
)


class TestEvidenceIsQuotable:
    def test_exact_quote_passes(self):
        assert _evidence_is_quotable("total 80.27 seconds, 9.21 seconds average", TRACE)

    def test_quote_with_different_whitespace_and_case_passes(self):
        assert _evidence_is_quotable("Total 80.27   Seconds, 9.21 Seconds Average", TRACE)

    def test_trimmed_trailing_clause_passes(self):
        """Models routinely quote the head of a sentence and drop the tail."""
        assert _evidence_is_quotable(
            "The export took ~80 seconds, averaging ~9 seconds per file. "
            "This was reported to the user in the final response.",
            TRACE,
        )

    def test_invented_quote_is_rejected(self):
        """The exact shape of the audited false positive."""
        assert not _evidence_is_quotable(
            "Agent stated the export took 45 minutes with no supporting tool output",
            TRACE,
        )

    def test_short_evidence_is_rejected(self):
        """Fragments this small match by accident and prove nothing."""
        assert not _evidence_is_quotable("error", TRACE)
        assert not _evidence_is_quotable("the user", TRACE)

    def test_empty_evidence_is_rejected(self):
        for empty in ("", None, "   "):
            assert not _evidence_is_quotable(empty, TRACE)


class TestFailedDimensionsNeedEvidence:
    @staticmethod
    def _parsed(evidence):
        return {
            "dimensions": {
                "grounding": {"evidence": evidence, "verdict": "FAIL"},
                "goal": {"evidence": "", "verdict": "PASS"},
            },
            "issues": [
                {"dim": "grounding", "cat": "Tool Output Misinterpretation",
                 "conf": "H", "brief": "agent fabricated the timing figures"}
            ],
        }

    def test_fail_with_real_evidence_becomes_an_issue(self):
        out = TraceScanner._v8_to_trace_output(
            self._parsed("total 80.27 seconds, 9.21 seconds average"),
            "user asked. tool returned total 80.27 seconds, 9.21 seconds average.",
        )
        assert len(out["issues"]) == 1
        assert out["issues"][0]["cat"] == "Tool Output Misinterpretation"

    def test_fail_with_invented_evidence_is_dropped(self):
        out = TraceScanner._v8_to_trace_output(
            self._parsed("agent claimed the export took 45 minutes"),
            "user asked. tool returned total 80.27 seconds, 9.21 seconds average.",
        )
        assert out["issues"] == [], "an unquotable failure was surfaced to the user"

    def test_fail_with_no_evidence_is_dropped(self):
        out = TraceScanner._v8_to_trace_output(
            self._parsed(""), "user asked. tool returned 80.27 seconds."
        )
        assert out["issues"] == []

    def test_gate_is_skipped_when_the_input_is_unknown(self):
        """No seen_text means we cannot judge; do not silently drop everything."""
        out = TraceScanner._v8_to_trace_output(self._parsed("anything at all here"), "")
        assert len(out["issues"]) == 1

    def test_passing_dimensions_never_produce_issues(self):
        parsed = {
            "dimensions": {"goal": {"evidence": "", "verdict": "PASS"}},
            "issues": [{"dim": "goal", "cat": "Goal Deviation", "brief": "x"}],
        }
        assert TraceScanner._v8_to_trace_output(parsed, "some trace text here")["issues"] == []


class TestBreadcrumbsDoNotInventQuotes:
    """A breadcrumb's `verbatim` field is rendered to the user as a quote from
    the trace. When recovery failed it returned the model's own paraphrase, so
    the UI pointed engineers at words nobody said — 161 of 278 breadcrumbs on the
    audited corpus. No quote is honest; an invented one is not.
    """

    def test_unmatchable_excerpt_yields_no_quote(self):
        from ee.agenthub.trace_scanner.compress import recover_verbatim

        assert recover_verbatim(
            "agent refused escalation request entirely",
            "The user asked about billing. The agent transferred them to support.",
        ) == ""

    def test_matchable_excerpt_returns_the_real_sentence(self):
        from ee.agenthub.trace_scanner.compress import recover_verbatim

        raw = "The user asked about billing. The agent transferred them to support."
        got = recover_verbatim("user asked billing", raw)
        assert got, "a recoverable quote was dropped"
        assert got in raw, f"returned text is not from the trace: {got!r}"


class TestLowConfidenceIsWithheldNotForced:
    """The prompt used to forbid L ("drop anything you'd rate L"), so a model
    that was unsure had nowhere to put that except an H or M assertion. On the
    audited corpus every single one of 405 issues came back H. Giving L back and
    withholding it from the feed converts forced false certainty into a signal we
    keep for recall work.
    """

    @staticmethod
    def _parsed(conf):
        return {
            "dimensions": {"goal": {"evidence": "the agent never answered", "verdict": "FAIL"}},
            "issues": [{"dim": "goal", "cat": "Goal Deviation",
                        "brief": "did not answer the question", "conf": conf}],
        }

    SEEN = "user asked a question. the agent never answered it."

    def test_low_confidence_is_not_surfaced(self):
        out = TraceScanner._v8_to_trace_output(self._parsed("L"), self.SEEN)
        assert out["issues"] == [], "an issue the model itself could not establish was shown"

    @pytest.mark.parametrize("conf", ["H", "M"])
    def test_established_findings_still_surface(self, conf):
        out = TraceScanner._v8_to_trace_output(self._parsed(conf), self.SEEN)
        assert len(out["issues"]) == 1
        assert out["issues"][0]["conf"] == conf

    def test_unknown_confidence_defaults_to_medium_not_dropped(self):
        out = TraceScanner._v8_to_trace_output(self._parsed("banana"), self.SEEN)
        assert len(out["issues"]) == 1
        assert out["issues"][0]["conf"] == "M"

    def test_lowercase_l_is_still_withheld(self):
        assert TraceScanner._v8_to_trace_output(self._parsed("l"), self.SEEN)["issues"] == []
