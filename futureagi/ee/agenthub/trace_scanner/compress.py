"""
Trace compression for the scanner — kevinify, structural prefilter, compress_v2.

Ported from experiments/trace_scanner/{scanner_v3.py, compress_trace.py}.
"""

import json
import re
import statistics

import nltk
from nltk.corpus import stopwords as _nltk_stopwords
from nltk.stem import PorterStemmer


def _get_span_kind(attrs: dict) -> str:
    """Span kind can live under any vendor prefix (openinference.span.kind,
    fi.span.kind, etc.). Return the first match so the scanner isn't coupled
    to a single SDK's attribute namespace."""
    if not attrs:
        return ""
    for key, value in attrs.items():
        if key.endswith(".span.kind") or key == "span.kind":
            if value:
                return str(value)
    return ""

# ---------------------------------------------------------------------------
# KEVINIFY — "Why waste time say lot word when few word do trick"
# ---------------------------------------------------------------------------

_STEMMER = PorterStemmer()
_NLTK_STOPS = set(_nltk_stopwords.words("english"))

_EXTRA_STOPS = {
    "please",
    "note",
    "however",
    "therefore",
    "thus",
    "hence",
    "accordingly",
    "additionally",
    "furthermore",
    "moreover",
    "specifically",
    "particularly",
    "essentially",
    "basically",
    "actually",
    "currently",
    "previously",
    "following",
    "regarding",
    "concerning",
    "including",
    "excluding",
    "using",
    "used",
    "also",
    "would",
    "could",
    "should",
    "shall",
    "maybe",
    "perhaps",
    "likely",
    "unlikely",
    "certainly",
    "definitely",
    "obviously",
    "clearly",
    "simply",
    "really",
    "quite",
    "rather",
    "role",
    "assistant",
    "content",
    "type",
    "text",
    "message",
    "messages",
    "null",
    "none",
    "true",
    "false",
    "undefined",
}

_ALL_STOPS = _NLTK_STOPS | _EXTRA_STOPS

_FILLER_RE = re.compile(
    r"(?:based on|in order to|as well as|due to|in terms of|with respect to"
    r"|it should be noted|it is important|it is worth|note that|please note"
    r"|keep in mind|the following|as follows|in this case|at this point"
    r"|as a result|on the other hand|in addition to|for example"
    r"|i will now|let me|i need to|i should|i'll)",
    re.IGNORECASE,
)

_JSON_NOISE_RE = re.compile(r'[{}\[\]"\\]|\\n|\\t|\\r')


def kevinify(text, max_len=2000):
    """Strip grammar fluff, keep semantic content. Few word do trick."""
    if not text:
        return ""
    text = str(text).strip()

    text = _JSON_NOISE_RE.sub(" ", text)
    text = _FILLER_RE.sub(" ", text)

    try:
        words = nltk.word_tokenize(text)
    except Exception:
        words = text.split()

    kept = []
    for w in words:
        clean = w.strip(".,;:!?()-_'\"")
        if not clean or len(clean) <= 1:
            continue
        if clean.lower() in _ALL_STOPS:
            continue
        kept.append(clean)

    result = " ".join(kept)
    result = re.sub(r"\s+", " ", result).strip()

    if len(result) > max_len:
        # Cut at a word boundary and append NOTHING. Any marker like "..."
        # gets interpreted by the scanner LLM as a truncated agent response
        # no matter how strongly the prompt says otherwise — Haiku's training
        # prior on "..." = truncation is too strong to override.
        result = result[:max_len].rsplit(" ", 1)[0]
    return result


# ---------------------------------------------------------------------------
# VERBATIM RECOVERY — Match kevinified LLM excerpts back to raw text
# ---------------------------------------------------------------------------

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _split_sentences(text):
    """Split text into sentence-like chunks."""
    if not text:
        return []
    parts = _SENTENCE_RE.split(text.strip())
    result = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > 200:
            sub = re.split(r"[;]\s*", p)
            result.extend(s.strip() for s in sub if s.strip())
        else:
            result.append(p)
    return result


def recover_verbatim(kevinified_excerpt, raw_text, min_overlap=0.3):
    """Match a kevinified LLM excerpt back to the raw text."""
    if not kevinified_excerpt or not raw_text:
        return kevinified_excerpt or ""

    raw_text = str(raw_text)
    sentences = _split_sentences(raw_text)
    if not sentences:
        return kevinified_excerpt

    excerpt_words = {w for w in kevinified_excerpt.lower().split() if len(w) > 2}
    if not excerpt_words:
        return kevinified_excerpt

    best_score = 0
    best_sentence = ""

    for sent in sentences:
        sent_kev = kevinify(sent, max_len=500)
        sent_words = {w for w in sent_kev.lower().split() if len(w) > 2}
        if not sent_words:
            continue
        overlap = len(excerpt_words & sent_words)
        score = overlap / len(excerpt_words)
        if score > best_score:
            best_score = score
            best_sentence = sent

    if best_score >= min_overlap:
        return best_sentence.strip()

    # Fallback: sliding window matching
    raw_words = raw_text.split()
    window_size = max(len(kevinified_excerpt.split()) * 3, 20)
    for i in range(0, len(raw_words) - window_size + 1, 5):
        window = " ".join(raw_words[i : i + window_size])
        window_kev = kevinify(window, max_len=500)
        window_words = {w for w in window_kev.lower().split() if len(w) > 2}
        if not window_words:
            continue
        overlap = len(excerpt_words & window_words)
        score = overlap / len(excerpt_words)
        if score > best_score:
            best_score = score
            best_sentence = window

    if best_score >= min_overlap:
        return best_sentence.strip()

    return kevinified_excerpt


def recover_key_moments(key_moments, raw_spans_text):
    """Post-process key_moments: recover verbatim text for user_request/agent_response."""
    if not key_moments:
        return key_moments

    result = dict(key_moments)

    if result.get("user_request"):
        result["user_request_raw"] = recover_verbatim(
            result["user_request"],
            raw_spans_text.get("all_inputs", ""),
        )

    if result.get("agent_response"):
        result["agent_response_raw"] = recover_verbatim(
            result["agent_response"],
            raw_spans_text.get("all_outputs", ""),
        )

    return result


# ---------------------------------------------------------------------------
# TASK HINTS — Cheap keyword signals from root input
# ---------------------------------------------------------------------------

_TASK_PATTERNS = {
    "needs_tool": re.compile(
        r"\b(search|find|look\s*up|check|fetch|get|retrieve|download|browse|navigate|open)\b",
        re.IGNORECASE,
    ),
    "has_format_constraint": re.compile(
        r"\b(format\s+as|output\s+in|respond\s+with|return\s+as|json|csv|markdown|xml|table|list\s+of|bullet)\b",
        re.IGNORECASE,
    ),
    "quantitative_answer": re.compile(
        r"\b(how\s+many|count|number\s+of|total|sum|average|percentage|ratio)\b",
        re.IGNORECASE,
    ),
    "specific_value": re.compile(
        r"\b(what\s+is|what\'s|what\s+was|when\s+did|where\s+is|who\s+is|which)\b",
        re.IGNORECASE,
    ),
}


def extract_task_hints(root_input):
    """Extract keyword-based task category hints from root span input."""
    if not root_input:
        return []
    text = str(root_input)
    return [hint for hint, pattern in _TASK_PATTERNS.items() if pattern.search(text)]


# ---------------------------------------------------------------------------
# HELPERS — span tree utilities
# ---------------------------------------------------------------------------


def _truncate(text, max_len=100):
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _parse_duration_seconds(duration_str):
    """Parse ISO 8601 duration like PT1M24.635189S to seconds."""
    if not duration_str:
        return 0
    try:
        s = duration_str.replace("PT", "")
        total = 0
        if "H" in s:
            h, s = s.split("H")
            total += float(h) * 3600
        if "M" in s:
            m, s = s.split("M")
            total += float(m) * 60
        if "S" in s:
            s = s.replace("S", "")
            if s:
                total += float(s)
        return round(total, 2)
    except Exception:
        return 0


def flatten_spans(span, depth=0, result=None):
    """Recursively flatten nested span tree into a list with depth info."""
    if result is None:
        result = []
    result.append((span, depth))
    for child in span.get("child_spans", []):
        flatten_spans(child, depth + 1, result)
    return result


# ---------------------------------------------------------------------------
# STRUCTURAL PRE-FILTER — Rule-based anomaly detection (free, <1ms)
# ---------------------------------------------------------------------------


def _tool_names_from_definitions(attrs: dict) -> set[str]:
    """Extract tool NAMES from the function-calling tool definitions on a span
    (``llm.tools`` / ``gen_ai.tool.definitions``). Tolerates a JSON string or a
    list, and OpenAI-style (``{type, function:{name}}``) or flat (``{name}``)
    schemas. These are the tools the agent had AVAILABLE, regardless of which it
    actually invoked."""
    names: set[str] = set()
    for key in ("llm.tools", "gen_ai.tool.definitions"):
        raw = attrs.get(key)
        if not raw:
            continue
        try:
            defs = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if not isinstance(defs, list):
            continue
        for d in defs:
            if isinstance(d, str):
                names.add(d)
            elif isinstance(d, dict):
                fn = d.get("function") if isinstance(d.get("function"), dict) else {}
                nm = fn.get("name") or d.get("name")
                if nm:
                    names.add(str(nm))
    return names


def structural_prefilter(trace_data):
    """Extract rule-based signals from the trace."""
    flat_spans = []
    for top_span in trace_data["spans"]:
        flat_spans.extend(flatten_spans(top_span))

    signals = {
        "error_spans": [],
        "retry_spans": [],
        "duration_outliers": [],
        "tool_failures": [],
        "empty_output": [],
        "token_anomalies": [],
    }

    spans_flat = []
    # Tools the agent had AVAILABLE — parsed from the function-calling tool
    # definitions sent to the LLM (standard telemetry: llm.tools /
    # gen_ai.tool.definitions). This is broader than the tools actually invoked
    # as spans; without it "available" collapses to "called" and the missing-
    # tool signal can never fire. No bespoke attribute required.
    declared_tools: set = set()
    for span, depth in flat_spans:
        attrs = span.get("span_attributes", {})
        declared_tools.update(_tool_names_from_definitions(attrs))
        duration = _parse_duration_seconds(span.get("duration"))
        status = span.get("status_code", "Unset")
        kind = _get_span_kind(attrs)
        has_input = bool(attrs.get("input.value", ""))
        has_output = bool(attrs.get("output.value", ""))
        prompt_tok = int(attrs.get("llm.token_count.prompt", 0) or 0)
        completion_tok = int(attrs.get("llm.token_count.completion", 0) or 0)

        spans_flat.append(
            {
                "id": span["span_id"],
                "name": span["span_name"],
                "depth": depth,
                "kind": kind,
                "duration": duration,
                "status": status,
                "has_input": has_input,
                "has_output": has_output,
                "prompt_tok": prompt_tok,
                "completion_tok": completion_tok,
            }
        )

    for s in spans_flat:
        if s["status"] == "Error":
            signals["error_spans"].append(s["id"])

    for i in range(1, len(spans_flat)):
        if (
            spans_flat[i]["name"] == spans_flat[i - 1]["name"]
            and spans_flat[i]["depth"] == spans_flat[i - 1]["depth"]
        ):
            signals["retry_spans"].append(s["id"])

    by_depth = {}
    for s in spans_flat:
        by_depth.setdefault(s["depth"], []).append(s)
    for depth, siblings in by_depth.items():
        durations = [s["duration"] for s in siblings if s["duration"] > 0]
        if len(durations) >= 3:
            mean = statistics.mean(durations)
            stdev = statistics.stdev(durations)
            if stdev > 0:
                for s in siblings:
                    if s["duration"] > 0 and abs(s["duration"] - mean) > 2 * stdev:
                        signals["duration_outliers"].append(s["id"])

    tool_names = {"Tool", "TOOL"}
    for s in spans_flat:
        is_tool = s["kind"] in tool_names or "Tool" in s["name"]
        if is_tool and s["status"] == "Error":
            signals["tool_failures"].append(s["id"])

    for s in spans_flat:
        if s["has_input"] and not s["has_output"] and s["kind"]:
            signals["empty_output"].append(s["id"])

    for s in spans_flat:
        if s["prompt_tok"] > 0 and s["completion_tok"] > 0:
            ratio = s["completion_tok"] / s["prompt_tok"]
            if ratio > 3 or ratio < 0.05:
                signals["token_anomalies"].append(s["id"])

    # Absence detection
    tool_spans = [
        s for s in spans_flat if s["kind"] in {"Tool", "TOOL"} or "Tool" in s["name"]
    ]
    llm_spans = [s for s in spans_flat if s["kind"] in {"LLM", "llm"}]
    unique_tools = {s["name"] for s in tool_spans}

    if not tool_spans and llm_spans:
        signals["no_tool_calls"] = True
    retriever_spans = [s for s in spans_flat if s["kind"] in {"Retriever", "RETRIEVER"}]
    if llm_spans and not tool_spans and not retriever_spans:
        signals["llm_only_trace"] = True

    anomalous_ids = set()
    for key, ids in signals.items():
        if isinstance(ids, list):
            anomalous_ids.update(ids)

    signal_summary = {}
    for k, v in signals.items():
        if isinstance(v, list) and v:
            signal_summary[k] = len(v)
        elif isinstance(v, bool) and v:
            signal_summary[k] = True

    return {
        "is_clean": len(anomalous_ids) == 0
        and not signals.get("no_tool_calls")
        and not signals.get("llm_only_trace"),
        "anomalous_span_ids": anomalous_ids,
        "signal_summary": signal_summary,
        "total_signals": len(anomalous_ids),
        "available_tools": list(unique_tools | declared_tools),
    }


def structural_prefilter_with_ids(trace_data):
    """Extended prefilter that also returns per-signal ID sets for flagging."""
    result = structural_prefilter(trace_data)

    flat_spans = []
    for top_span in trace_data["spans"]:
        flat_spans.extend(flatten_spans(top_span))

    error_ids = set()
    retry_ids = set()
    duration_ids = set()
    tool_fail_ids = set()

    spans_flat = []
    for span, depth in flat_spans:
        attrs = span.get("span_attributes", {})
        spans_flat.append(
            {
                "id": span["span_id"],
                "name": span["span_name"],
                "depth": depth,
                "kind": _get_span_kind(attrs),
                "duration": _parse_duration_seconds(span.get("duration")),
                "status": span.get("status_code", "Unset"),
            }
        )

    for s in spans_flat:
        if s["status"] == "Error":
            error_ids.add(s["id"])
    for i in range(1, len(spans_flat)):
        if (
            spans_flat[i]["name"] == spans_flat[i - 1]["name"]
            and spans_flat[i]["depth"] == spans_flat[i - 1]["depth"]
        ):
            retry_ids.add(spans_flat[i]["id"])
    for s in spans_flat:
        is_tool = s["kind"] in {"Tool", "TOOL"} or "Tool" in s["name"]
        if is_tool and s["status"] == "Error":
            tool_fail_ids.add(s["id"])

    by_depth = {}
    for s in spans_flat:
        by_depth.setdefault(s["depth"], []).append(s)
    for depth, siblings in by_depth.items():
        durations = [s["duration"] for s in siblings if s["duration"] > 0]
        if len(durations) >= 3:
            mean = statistics.mean(durations)
            stdev = statistics.stdev(durations)
            if stdev > 0:
                for s in siblings:
                    if s["duration"] > 0 and abs(s["duration"] - mean) > 2 * stdev:
                        duration_ids.add(s["id"])

    result["_error_ids"] = error_ids
    result["_retry_ids"] = retry_ids
    result["_duration_ids"] = duration_ids
    result["_tool_fail_ids"] = tool_fail_ids
    return result


# ---------------------------------------------------------------------------
# COMPRESS V2 — Adaptive budget, kevinified I/O, flow outline
# ---------------------------------------------------------------------------


def build_flow_outline(trace_data):
    """Compact tree outline showing agent execution flow with path numbering."""
    parts = []

    def _walk(span, path_prefix):
        name = span.get("span_name", "?")
        kind = _get_span_kind(span.get("span_attributes", {}))
        status = span.get("status_code", "Unset")

        label = f"{path_prefix}:{name}"
        if kind:
            label += f"({kind})"
        if status == "Error":
            label += "[ERR]"

        parts.append(label)

        children = span.get("child_spans", [])
        for idx, child in enumerate(children, start=1):
            _walk(child, f"{path_prefix}.{idx}")

    for idx, root_span in enumerate(trace_data.get("spans", []), start=1):
        _walk(root_span, str(idx))

    return " > ".join(parts)


def _plain(text, max_len):
    """Whitespace-normalised truncation. Unlike kevinify, grammar is preserved.

    Used for the fields the model reasons over directly — a stripped negation
    ("didn't get X" -> "n't get X") inverts the meaning of the very sentence
    being judged.
    """
    if not text:
        return ""
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


_MSG_RE = re.compile(
    r"^(?:gen_ai\.(input|output)\.messages\.(\d+)\.message"
    r"|llm\.(input|output)_messages\.(\d+)\.message)\.(role|content)$"
)


def extract_turn(all_flat):
    """Pull THIS turn's user request and agent response out of a trace.

    One trace is one user turn, so the pair on trial is unambiguous even though
    the SDK's role tags are unreliable (it re-serialises conversation history as
    ``user``, so the agent's own prior replies arrive mislabelled):

      * agent response  = the output message — tagged ``assistant`` because it
        is the message being generated, which is the one case the SDK gets right
      * user request    = the last ``user``-tagged message BEFORE the trailing
        assistant/tool block

    Alternation was considered and rejected: it breaks on consecutive same-role
    messages, and these traces contain them (interleaved tool results, users
    sending two messages in a row).

    Returns ``(history, request, response)``; empty strings when the span data
    carries no per-message attributes, so callers fall back to old behaviour.
    """
    richest, best = {}, -1
    for span, _depth in all_flat:
        msgs = {}
        for key, val in (span.get("span_attributes") or {}).items():
            m = _MSG_RE.match(key)
            if not m:
                continue
            io = m.group(1) or m.group(3)
            idx = int(m.group(2) or m.group(4))
            msgs.setdefault((io, idx), {})[m.group(5)] = val
        n_in = sum(1 for io, _ in msgs if io == "input")
        if n_in > best:
            best, richest = n_in, msgs
    if not richest:
        return [], "", ""

    inputs = sorted((i, d) for (io, i), d in richest.items() if io == "input")
    outputs = sorted((i, d) for (io, i), d in richest.items() if io == "output")

    request, req_idx = "", None
    for idx, data in reversed(inputs):
        role = str(data.get("role", "")).lower()
        if role in ("tool", "assistant"):
            continue  # trailing tool-call block for THIS turn
        if role == "system":
            break
        request, req_idx = str(data.get("content", "")).strip(), idx
        break

    history = [
        str(d.get("content", "")).strip()
        for i, d in inputs
        if i < (req_idx if req_idx is not None else 0)
        and str(d.get("role", "")).lower() != "system"
    ]
    response = str(outputs[0][1].get("content", "")).strip() if outputs else ""
    return history, request, response


def compress_v2(trace_data, prefilter_result):
    """
    Smart compression — kevinify all spans with adaptive token budget.
    """
    anomalous_ids = prefilter_result["anomalous_span_ids"]

    all_flat = []
    for top_span in trace_data["spans"]:
        all_flat.extend(flatten_spans(top_span))

    # Extract task (root input) and result (final output)
    root_input = ""
    final_output = ""
    if all_flat:
        root_attrs = all_flat[0][0].get("span_attributes", {})
        root_input = root_attrs.get("input.value", "")
        for span, depth in reversed(all_flat):
            out = span.get("span_attributes", {}).get("output.value", "")
            if out:
                final_output = out
                break
        if not final_output:
            final_output = root_attrs.get("output.value", "")

    # THIS turn's request/response. root_input is the whole serialized message
    # list for multi-turn agents, so feeding it to `task` told the model that
    # every question in the conversation was "the request" — and the prompt asks
    # it to compare task against result. That mismatch is what produced briefs
    # like "answered X instead of Y" for traces where each turn was answered
    # correctly, just in different turns. Prefer the real pair when available.
    prior_turns, turn_request, turn_response = extract_turn(all_flat)
    if turn_request:
        root_input = turn_request
    if turn_response:
        final_output = turn_response

    # Adaptive budget: ~3000 chars total, distributed by importance
    TOTAL_IO_BUDGET = 3000
    weights = []
    span_meta = []
    for span, depth in all_flat:
        attrs = span.get("span_attributes", {})
        sid = span["span_id"]
        kind = _get_span_kind(attrs)
        is_flagged = sid in anomalous_ids
        is_decision = kind in {"LLM", "llm", "Tool", "TOOL", "Retriever", "RETRIEVER"}

        w = 3 if is_flagged else (2 if is_decision else 1)
        weights.append(w)
        span_meta.append((span, depth, is_flagged, is_decision, kind))

    total_weight = sum(weights) or 1
    budgets = [max(50, int(TOTAL_IO_BUDGET * w / total_weight)) for w in weights]

    spans = []
    for i, (span, depth, is_flagged, is_decision, kind) in enumerate(span_meta):
        attrs = span.get("span_attributes", {})
        sid = span["span_id"]
        status = span.get("status_code", "Unset")
        duration = _parse_duration_seconds(span.get("duration"))
        io_budget = budgets[i]

        inp = kevinify(attrs.get("input.value", ""), io_budget)
        out = kevinify(attrs.get("output.value", ""), io_budget)

        has_content = inp or out or status != "Unset" or is_flagged
        if not has_content:
            continue

        entry = {"n": span["span_name"], "d": depth}
        if kind:
            entry["k"] = kind
        if status != "Unset":
            entry["s"] = status
        if duration and duration > 0.1:
            entry["t"] = duration
        if inp:
            entry["in"] = inp
        if out:
            entry["out"] = out

        if is_flagged:
            flags = []
            if sid in prefilter_result.get("_error_ids", set()):
                flags.append("ERR")
            if sid in prefilter_result.get("_retry_ids", set()):
                flags.append("RETRY")
            if sid in prefilter_result.get("_duration_ids", set()):
                flags.append("SLOW")
            if sid in prefilter_result.get("_tool_fail_ids", set()):
                flags.append("TOOL_FAIL")
            if not flags:
                flags.append("FLAGGED")
            entry["f"] = ",".join(flags)

        pt = int(attrs.get("llm.token_count.prompt", 0) or 0)
        ct = int(attrs.get("llm.token_count.completion", 0) or 0)
        if pt:
            entry["pt"] = pt
        if ct:
            entry["ct"] = ct

        spans.append(entry)

    trace_label = trace_data.get("_short_label", trace_data["trace_id"])
    flow_outline = build_flow_outline(trace_data)

    # task/result are the pair the model is asked to compare, so they are NOT
    # kevinified. Stopword stripping mangles exactly the words that decide the
    # verdict — "didn't get the benchmark" becomes "n't get benchmark", and a
    # destroyed negation flips the meaning of the sentence being judged. Plain
    # truncation keeps the grammar; the budget is spent where it matters.
    task_text = turn_request or root_input
    result_text = turn_response or final_output
    result = {
        "tid": trace_label,
        "task": _plain(task_text, 600),
        "result": _plain(result_text, 600),
        "flow": flow_outline,
        "signals": prefilter_result["signal_summary"],
        "spans": spans,
    }
    # Earlier turns, clearly separated from the turn being judged. Supplied so
    # genuine cross-turn failures (hallucinated context, ignoring a name given
    # earlier) stay detectable, WITHOUT letting an earlier turn's question be
    # mistaken for this turn's request.
    # Prior turns are also left un-kevinified: the model needs them to work out
    # what the user is actually asking for when THIS turn is only a clarification
    # ("Ravi Krishnan", "yes please"), and stripped grammar makes that unreadable.
    if prior_turns:
        result["prior_turns"] = [_plain(t, 300) for t in prior_turns[-6:]]

    available_tools = prefilter_result.get("available_tools", [])
    if available_tools:
        result["tools_available"] = available_tools

    task_hints = extract_task_hints(root_input)
    if task_hints:
        result["task_hints"] = task_hints

    return result


def extract_programmatic_metadata(trace_data, prefilter_result):
    """Extract metadata that doesn't need LLM — pure trace parsing."""
    flat_spans = []
    for top_span in trace_data["spans"]:
        flat_spans.extend(flatten_spans(top_span))

    tools_called = []
    for span, depth in flat_spans:
        attrs = span.get("span_attributes", {})
        kind = _get_span_kind(attrs)
        if kind in {"Tool", "TOOL"} or "Tool" in span["span_name"]:
            tools_called.append(
                {"name": span["span_name"], "status": span.get("status_code", "Unset")}
            )

    llm_spans = [
        s
        for s, d in flat_spans
        if _get_span_kind(s.get("span_attributes", {})) in {"LLM", "llm"}
    ]
    turn_count = len(llm_spans)

    raw_user_request = ""
    raw_agent_response = ""
    if flat_spans:
        root_attrs = flat_spans[0][0].get("span_attributes", {})
        raw_user_request = str(root_attrs.get("input.value", ""))[:500]
        for span, depth in reversed(flat_spans):
            out = span.get("span_attributes", {}).get("output.value", "")
            if out:
                raw_agent_response = str(out)[:500]
                break
        if not raw_agent_response:
            raw_agent_response = str(root_attrs.get("output.value", ""))[:500]

    all_inputs = []
    all_outputs = []
    for span, depth in flat_spans:
        attrs = span.get("span_attributes", {})
        inp = str(attrs.get("input.value", ""))
        out = str(attrs.get("output.value", ""))
        if inp:
            all_inputs.append(inp[:1000])
        if out:
            all_outputs.append(out[:1000])

    return {
        "tools_called": tools_called,
        "tools_available": prefilter_result.get("available_tools", []),
        "turn_count": turn_count,
        "raw_user_request": raw_user_request,
        "raw_agent_response": raw_agent_response,
        "raw_spans_text": {
            "all_inputs": "\n".join(all_inputs),
            "all_outputs": "\n".join(all_outputs),
        },
    }


# ---------------------------------------------------------------------------
# KEY-MOMENT SPAN ATTRIBUTION — deterministic, no LLM
# ---------------------------------------------------------------------------
#
# The scanner LLM emits flat verbatim quotes. We attribute each quote back to
# the real span it came from (by word-overlap) and read role/status off THAT
# span — so the breadcrumb's structure is grounded in actual span data, never
# model-guessed. Keeps the scanner model-agnostic (the LLM call is untouched).

# Status strings that mean "fine" — anything else is treated as a failure.
_OK_STATUS = {"", "ok", "unset", "status_code_ok", "ok ", "none"}


def _role_from_kind(kind: str, is_root_input: bool) -> str:
    """Map a span kind to a breadcrumb role label."""
    if is_root_input:
        return "User"
    k = (kind or "").lower()
    if "tool" in k:
        return "Tool"
    if "retriev" in k:
        return "Retrieval"
    if k in {"llm", "agent", "chain"}:
        return "Agent"
    return "Step"


def attribute_key_moments(quotes, trace_data):
    """For each key-moment quote, find its source span by word overlap and
    return ``{role, span, status, is_failure}`` per quote (same order/length).

    Deterministic: role/status come from the matched span, never the LLM.
    Returns empty fields for a quote that doesn't confidently match a span.
    """
    flat = []
    for top_span in (trace_data or {}).get("spans", []) or []:
        for span, _depth in flatten_spans(top_span):
            attrs = span.get("span_attributes", {}) or {}
            flat.append(
                {
                    "name": span.get("span_name", "?"),
                    "kind": _get_span_kind(attrs),
                    "status": span.get("status_code", "Unset"),
                    "input": str(attrs.get("input.value", "")),
                    "output": str(attrs.get("output.value", "")),
                }
            )
    if flat:
        flat[0]["is_root"] = True

    out = []
    for quote in quotes:
        q_words = {w for w in re.findall(r"\w+", (quote or "").lower()) if len(w) > 2}
        if not q_words:
            out.append({"role": "", "span": "", "status": "", "is_failure": False})
            continue
        best, best_score, from_input = None, 0.0, False
        for sp in flat:
            for is_inp, text in ((True, sp["input"]), (False, sp["output"])):
                if not text:
                    continue
                t_words = set(re.findall(r"\w+", text.lower()))
                if not t_words:
                    continue
                score = len(q_words & t_words) / len(q_words)
                if score > best_score:
                    best, best_score, from_input = sp, score, is_inp
        if best and best_score >= 0.5:
            is_failure = str(best["status"]).strip().lower() not in _OK_STATUS
            out.append(
                {
                    "role": _role_from_kind(
                        best["kind"], best.get("is_root", False) and from_input
                    ),
                    "span": best["name"],
                    "status": "fail" if is_failure else "ok",
                    "is_failure": is_failure,
                }
            )
        else:
            out.append({"role": "", "span": "", "status": "", "is_failure": False})
    return out


# ---------------------------------------------------------------------------
# compress_v3 — input-representation rework (ships with the V8 prompt)
# ---------------------------------------------------------------------------
# Same output contract as compress_v2. Four measured defects fixed:
#  1. NEGATIONS SURVIVE. compress_v2 kevinify()s every span's evidence and NLTK's
#     stopword list contains not/no/nor/don/didn/isn/wasn/couldn/t, so every negation
#     is deleted from the text the model reasons over ("tool did not return data" ->
#     "tool return data"). The earlier fix applied _plain to task/result only.
#  2. PROVIDER ENVELOPES ARE UNWRAPPED (OpenAI choices / Gemini candidates /
#     LangChain generations / Anthropic content), so the budget is not spent on
#     metadata while the real answer never reaches the model.
#  3. ALL FOUR PRODUCER MESSAGE SHAPES, plus answers handed to a delivery tool.
#  4. VOICE/REALTIME traces reconstructed from the span timeline.
# Budget raised 3000 -> 20000 chars; the 3000 figure predates long-context models.
#
# Helpers are prefixed _v3_ so this cannot shadow compress_v2's own _plain/_loads.
# On its own this is NOT the accuracy win (measured tied with baseline); the win is
# the V8 prompt. It ships together because that is the combination validated on the
# sealed split.


TOTAL_IO_BUDGET = 20000
TASK_BUDGET = 3000
RESULT_BUDGET = 3000

_ENVELOPE_HINTS = ("choices", "candidates", "generations", "usage", "object", "created")


def _v3_plain(text, max_len):
    """Whitespace-normalised truncation. Grammar and negations preserved."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _v3_loads(v):
    if isinstance(v, (dict, list)):
        return v
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or s[0] not in "[{":
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


_ANSWER_ARG_KEYS = ("response_text", "final_response", "answer", "message",
                    "text", "content", "chart_summary")


def _delivered_answer(all_flat):
    """Recover an answer handed to a delivery tool instead of returned.

    Matches spans whose name marks final delivery (display_final_response,
    display_final_chart, final_answer, send_message...) and pulls the text out of
    the tool ARGUMENTS. Multiple segments are concatenated in trace order.
    """
    parts = []
    for span, _d in all_flat:
        name = (span.get("span_name") or "").lower()
        if not (("final" in name and ("display" in name or "response" in name or "answer" in name))
                or name in ("send_message", "respond_to_user", "final_answer")):
            continue
        d = _v3_loads((span.get("span_attributes") or {}).get("input.value", ""))
        if not isinstance(d, dict):
            continue
        for k in _ANSWER_ARG_KEYS:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
                break
    return "\n\n".join(parts)


def _is_nontextual(d):
    """True when a response legitimately carries no text: embeddings, rerank
    scores, or a bare numeric payload. These must not be reported as empty."""
    if not isinstance(d, dict):
        return False
    for k in ("data", "embedding", "embeddings", "predictions", "results", "scores"):
        v = d.get(k)
        if isinstance(v, list) and v:
            head = v[0]
            if isinstance(head, (int, float)):
                return True
            if isinstance(head, dict) and any(
                isinstance(head.get(kk), list) and head.get(kk)
                and isinstance(head[kk][0], (int, float))
                for kk in ("embedding", "values", "vector", "scores")
            ):
                return True
    if str(d.get("object", "")).lower() in ("list", "embedding"):
        return True
    return False


def unwrap_output(raw):
    """Pull the assistant's actual text out of a provider response envelope.

    Handles OpenAI (choices[].message.content), Gemini
    (candidates[].content.parts[].text), LangChain (generations[][].text),
    Anthropic (content[].text). Returns the raw string unchanged when it is
    already plain text or an unrecognised shape — never worse than the input.
    """
    d = _v3_loads(raw)
    if d is None:
        return raw or ""

    def _txt(x):
        if isinstance(x, str):
            return x
        if isinstance(x, list):
            return " ".join(_txt(i) for i in x if _txt(i))
        if isinstance(x, dict):
            for k in ("text", "content", "output_text"):
                if isinstance(x.get(k), str) and x[k].strip():
                    return x[k]
            # Structured-output agents answer ENTIRELY via function_call.arguments,
            # with content=null. Without this the whole response reads as empty and
            # the completion dimension fires. 11 corpus roots are function-call-only.
            fc = x.get("function_call") or x.get("functionCall")
            if isinstance(fc, dict) and str(fc.get("arguments") or "").strip():
                return f"[function_call {fc.get('name','')}] {fc['arguments']}"
            tcs = x.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                got = []
                for tc in tcs[:4]:
                    # `(tc or {})` guarded None and empty but not a str, and some
                    # producers emit tool_calls as a list of strings. Every other
                    # branch in this function type-checks before descending; this one
                    # did not, and it raised rather than degrading. The scanner fails
                    # open, so the trace came back has_issues=False and was
                    # indistinguishable from a clean one.
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function")
                    if not isinstance(fn, dict):
                        continue
                    if str(fn.get("arguments") or "").strip():
                        got.append(f"[tool_call {fn.get('name','')}] {fn['arguments']}")
                if got:
                    return "\n".join(got)
            if isinstance(x.get("content"), (list, dict)):
                return _txt(x["content"])
            if isinstance(x.get("parts"), list):
                return _txt(x["parts"])
            if isinstance(x.get("message"), dict):
                return _txt(x["message"])
        return ""

    if isinstance(d, dict):
        for key in ("choices", "candidates", "generations", "content", "messages"):
            v = d.get(key)
            if isinstance(v, list) and v:
                t = _txt(v)
                if t.strip():
                    return t
        t = _txt(d)
        if t.strip():
            return t
        # Non-textual responses are NOT empty. An embeddings call returns a vector
        # and a rerank returns scores; both legitimately have no text. Measured:
        # 4 of 10 sampled v8 false positives were `completion` firing on
        # "[empty model output]" for embedding responses and voice session roots.
        if _is_nontextual(d):
            return "[non-textual response: embedding/scores returned as expected]"
        # recognised envelope but no text found (e.g. empty generation) — say so
        if any(h in d for h in _ENVELOPE_HINTS):
            err = d.get("error") or (d.get("response_metadata") or {}).get("error")
            return f"[empty model output]{' error=' + _v3_plain(json.dumps(err), 300) if err else ''}"
    elif isinstance(d, list):
        t = _txt(d)
        if t.strip():
            return t
    return raw or ""


def extract_messages(raw):
    """[(role, content)] from any producer shape; [] if there is no message list."""
    d = _v3_loads(raw)
    out = []
    if isinstance(d, list) and d and isinstance(d[0], dict) and "role" in d[0]:
        src = d
    elif isinstance(d, dict):
        src = None
        for k in ("messages", "contents"):
            v = d.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                src = v
                break
        if src is None:
            return []
    else:
        return []
    for m in src:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        c = m.get("content")
        if c is None and isinstance(m.get("parts"), list):
            c = " ".join(str(p.get("text", "")) for p in m["parts"] if isinstance(p, dict))
        if isinstance(c, list):
            c = " ".join(
                (p.get("text") or p.get("content") or "") if isinstance(p, dict) else str(p)
                for p in c
            )
        out.append((role, str(c or "")))
    return out


def _v3_flat(trace_data, flatten_spans):
    fl = []
    for top in trace_data.get("spans", []):
        fl.extend(flatten_spans(top))
    return fl


def _v3_build(trace_data, prefilter_result, flatten_spans, build_flow_outline,
          extract_task_hints, retry_ids=()):
    """Produce the compress_v2 dict shape with properly extracted content."""
    all_flat = _v3_flat(trace_data, flatten_spans)

    # ---- the turn on trial -------------------------------------------------
    # Richest message list wins, but ONLY from spans that are plausibly the main
    # agent: a sub-agent span can carry more messages than the orchestrator, and
    # picking it substitutes an internal step for the user's request (this is the
    # measured failure mode of ee#186's extract_turn).
    root_attrs = all_flat[0][0].get("span_attributes", {}) if all_flat else {}
    root_input = root_attrs.get("input.value", "") or ""
    root_output = root_attrs.get("output.value", "") or ""

    # Message list is taken from the ROOT span first. Scanning all spans for the
    # "richest" one is what made ee#186 substitute a sub-agent's internal step for
    # the user's request (measured: recall 68.8%->58.4% on agentic traces). Only
    # fall through to other spans when the root carries no message list at all.
    best = extract_messages(root_input)
    if not best:
        for span, _d in all_flat:
            msgs = extract_messages((span.get("span_attributes") or {}).get("input.value", ""))
            if len(msgs) > len(best):
                best = msgs

    history, request = [], ""
    if best:
        for i in range(len(best) - 1, -1, -1):
            role, content = best[i]
            r = role.lower()
            if r in ("tool", "assistant", "model"):
                continue
            if r == "system":
                break
            request = content.strip()
            history = [c.strip() for rr, c in best[:i] if rr.lower() != "system" and c.strip()]
            break

    # Prefer the recovered user turn, but only when it actually reads like a
    # request rather than a serialized blob — otherwise the root input is better.
    root_plain = unwrap_output(root_input) or root_input or ""
    if request and not request.lstrip().startswith(("{", "[")):
        task = request
    else:
        task = root_plain or request

    # Some agents deliver the final answer through a DELIVERY TOOL rather than
    # returning it: display_final_response / display_final_chart take the text as
    # a tool ARGUMENT (input.response_text) and return only "Response segment
    # stored". 114 of 1037 corpus traces (11%) do this, and reading outputs finds
    # the confirmation instead of the answer. Segments are concatenated in order.
    delivered = _delivered_answer(all_flat)

    # Final answer: the root's own output when it has one, else the LONGEST
    # unwrapped span output. Taking the last span yields a streaming chunk —
    # measured, one trace's `result` came out as the 3 characters "Cad".
    result = delivered or unwrap_output(root_output)
    if not result or result.startswith("[empty model output]"):
        cands = []
        for span, _d in all_flat:
            o = (span.get("span_attributes") or {}).get("output.value", "")
            if not o:
                continue
            u = unwrap_output(o)
            if u and not u.startswith("[empty model output]"):
                cands.append(u)
        if cands:
            tail = cands[-6:]
            result = max(tail, key=len) if max(map(len, tail)) > 40 else "".join(cands[-20:])

    # ---- span evidence -----------------------------------------------------
    weights, meta = [], []
    flagged = set(prefilter_result.get("anomalous_span_ids") or []) | set(retry_ids)
    for span, depth in all_flat:
        sid = span.get("span_id")
        w = 3 if sid in flagged else 1
        weights.append(w)
        meta.append((span, depth, sid in flagged))
    total = sum(weights) or 1

    spans = []
    for i, (span, depth, is_flagged) in enumerate(meta):
        budget = max(200, int(TOTAL_IO_BUDGET * weights[i] / total))
        a = span.get("span_attributes") or {}
        inp = _v3_plain(a.get("input.value", ""), budget)
        out = _v3_plain(unwrap_output(a.get("output.value", "")), budget)
        if not (inp or out or span.get("status_code") not in (None, "Unset") or is_flagged):
            continue
        spans.append({
            "id": span.get("span_id"), "name": span.get("span_name"),
            "depth": depth, "kind": (a.get("span.kind") or "CHAIN"),
            "status": span.get("status_code", "Unset"),
            "in": inp, "out": out, **({"flagged": True} if is_flagged else {}),
        })

    # Voice/realtime: conversation lives in many tiny ordered spans and neither
    # the root nor per-message attrs are populated.
    if len(task) < 60 and len(result) < 60 and len(spans) > 20:
        tl = []
        for s in spans:
            if s["in"]: tl.append(f"[{s['name']}] IN: {s['in'][:300]}")
            if s["out"]: tl.append(f"[{s['name']}] OUT: {s['out'][:300]}")
            if len(tl) >= 80: break
        if tl:
            task = task or "(streaming/voice session — conversation in span timeline)"
            result = result or _v3_plain(" \n".join(tl[-40:]), RESULT_BUDGET)

    res = {
        "tid": trace_data.get("_short_label", trace_data.get("trace_id")),
        "task": _v3_plain(task, TASK_BUDGET),
        "result": _v3_plain(result, RESULT_BUDGET),
        "flow": build_flow_outline(trace_data),
        "signals": prefilter_result.get("signal_summary"),
        "spans": spans,
    }
    if history:
        res["prior_turns"] = [_v3_plain(h, 500) for h in history[-8:]]
    tools = prefilter_result.get("available_tools") or []
    if tools:
        res["tools_available"] = tools
    hints = extract_task_hints(task)
    if hints:
        res["task_hints"] = hints
    return res


def compress_v3(trace_data, prefilter_result, retry_ids=()):
    """compress_v2-shaped payload with properly extracted content."""
    return _v3_build(trace_data, prefilter_result, flatten_spans, build_flow_outline,
                     extract_task_hints, retry_ids)
