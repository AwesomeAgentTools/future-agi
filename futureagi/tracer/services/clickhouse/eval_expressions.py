"""Shared eval-output SQL expressions.

A leaf module on purpose: both ``schema.py`` (which renders the DDL) and the
query builders read these, and ``query_builders/__init__`` eagerly imports
every builder, so ``schema.py`` must not reach into that package.
"""

# Structured (choice-based) evals nest their number under this key —
# {"score": 0.5, "choice": "Partial"} — while score evals emit a bare scalar.
EVAL_STRUCTURED_SCORE_KEY = "score"

# Eval outputs that are pass/fail strings rather than numbers.
EVAL_TRUTHY_OUTPUTS = ("passed", "pass", "true", "1")
EVAL_FALSY_OUTPUTS = ("failed", "fail", "false", "0")

# A bare number rendered as text, e.g. "0.8" — the scalar-output eval shape.
EVAL_NUMERIC_OUTPUT_PATTERN = "^-?[0-9]+\\.?[0-9]*$"


def sql_str_set(values: tuple[str, ...]) -> str:
    """Render a tuple of strings as a SQL ``IN`` list, e.g. ``('pass', '1')``."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def eval_has_structured_score(json_args: str) -> str:
    """SQL predicate: does this row's eval output carry a nested numeric score?

    ``json_args`` is spliced straight into ``JSONHas(...)``, so it is
    deliberately called two ways: with a bare column that already holds the
    serialized output (``e.eval_output_str``), and with the comma-joined
    argument fragment that digs the output out of ``config``
    (``EVAL_OUTPUT_JSON_ARGS``). Callers that read ``eval_score`` need this to
    tell a genuine 0.0 from "no number here".
    """
    return f"JSONHas({json_args}, '{EVAL_STRUCTURED_SCORE_KEY}')"
