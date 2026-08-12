"""Shared eval-output SQL expressions.

A leaf module: ``schema.py`` and the query builders both read these, and
``query_builders/__init__`` imports all 15 builder modules, so putting them
there would pull that whole tree into ``schema.py`` at import time.
"""

# Structured evals nest their number here: {"score": 0.5, "choice": "Partial"}.
EVAL_STRUCTURED_SCORE_KEY = "score"

EVAL_TRUTHY_OUTPUTS = ("passed", "pass", "true", "1")
EVAL_FALSY_OUTPUTS = ("failed", "fail", "false", "0")

# A bare number rendered as text, e.g. "0.8".
EVAL_NUMERIC_OUTPUT_PATTERN = "^-?[0-9]+\\.?[0-9]*$"


def sql_str_set(values: tuple[str, ...]) -> str:
    """Render a tuple of strings as a SQL ``IN`` list, e.g. ``('pass', '1')``."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def eval_has_structured_score(json_args: str) -> str:
    """SQL predicate: does this row's eval output carry a nested score?

    ``json_args`` is spliced into ``JSONHas(...)``: a column holding the
    serialized output, or a comma-joined argument fragment.
    """
    return f"JSONHas({json_args}, '{EVAL_STRUCTURED_SCORE_KEY}')"
