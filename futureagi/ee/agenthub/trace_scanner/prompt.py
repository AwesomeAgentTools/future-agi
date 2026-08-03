"""
Scanner taxonomy — hierarchy, fix layers, and prompt templates.

Ported from experiments/trace_scanner/scanner_v3.py.
"""

import json

# ---------------------------------------------------------------------------
# HIERARCHY — group → subcategories
# ---------------------------------------------------------------------------

HIERARCHY = {
    "Tool Failures": [
        "Tool-related",
        "Tool Selection Errors",
        "Tool Output Misinterpretation",
        "Formatting Errors",
        "Language-only",
    ],
    "Context & Retrieval": [
        "Context Handling Failures",
        "Poor Information Retrieval",
        "Incorrect Memory Usage",
    ],
    "Planning & Goals": [
        "Task Orchestration",
        "Goal Deviation",
        "Resource Abuse",
    ],
    "Output Quality": [
        "Instruction Non-compliance",
        "Incorrect Problem Identification",
    ],
    "Infrastructure": [
        "Environment Setup Errors",
        "Resource Not Found",
        "Authentication Errors",
        "Timeout Issues",
        "Service Errors",
    ],
}

# Reverse map: subcategory → group
SUBCAT_TO_GROUP = {}
for group, subcats in HIERARCHY.items():
    for sc in subcats:
        SUBCAT_TO_GROUP[sc] = group

# Fix layer mapping: group → what layer to fix
GROUP_TO_FIX_LAYER = {
    "Tool Failures": "Tools",
    "Context & Retrieval": "Prompt",
    "Planning & Goals": "Orchestration",
    "Output Quality": "Prompt",
    "Infrastructure": "Guardrails",
}

# Subcategory → fix layer (derived)
SUBCAT_TO_FIX_LAYER = {}
for group, subcats in HIERARCHY.items():
    for sc in subcats:
        SUBCAT_TO_FIX_LAYER[sc] = GROUP_TO_FIX_LAYER[group]

# All valid subcategory names (for output validation)
VALID_SUBCATEGORIES = set(SUBCAT_TO_GROUP.keys())

# ---------------------------------------------------------------------------
# PROMPT TEMPLATES
# ---------------------------------------------------------------------------

CACHED_PREFIX = """You are an AI trace error scanner. For each trace, evaluate 5 error groups. For each group, decide YES or NO. If YES, identify WHICH specific subcategory.

!! CRITICAL — INPUT IS COMPRESSED, NOT RAW !!
Every text field you see below (task, result, spans[].in, spans[].out) has been
pre-processed by a lossy compressor that strips stopwords, grammar fluff,
punctuation (including periods), and JSON syntax — keeping only content-bearing
keywords. A trailing "..." means "we hit our length budget", NOT that the
agent's actual response was truncated. Missing periods, choppy phrasing,
or dropped articles ("the", "a", "is") are compression artifacts, NOT agent
errors. DO NOT flag issues like "response truncated mid-sentence",
"incomplete output", "choppy grammar", or "missing punctuation" — those are
artifacts of OUR compression, not the agent's behavior. Judge the agent on
the SEMANTIC CONTENT of the keywords, not their surface form.

TRACE FORMAT:
- task: the user's message on THIS turn, verbatim. One trace = one user turn.
- result: the agent's reply to that message, verbatim.
- prior_turns: earlier messages, oldest first, verbatim.

HOW TO READ task / result / prior_turns — read this before judging anything:

`task` is the user's LATEST message, which is often NOT the full request. It is
frequently a clarification or an answer to something the agent just asked — a
bare name ("Ravi Krishnan"), a confirmation ("yes please"), a correction. In
those cases the user's ACTUAL goal was stated one or more turns earlier and is
in prior_turns.

So: work out the user's OUTSTANDING GOAL from task plus prior_turns, then judge
whether `result` makes correct progress toward it.

- Do NOT report a failure merely because `result` does not answer `task` when
  `task` is a clarification — answering the goal from prior_turns is CORRECT.
- Do NOT report "answered X instead of Y" when Y appears in prior_turns and was
  ALREADY answered in an earlier turn. Each trace is one turn; earlier questions
  are usually already resolved. Only flag this if prior_turns shows the user
  asking for something that was never addressed and is still outstanding.
- Speaker labels in prior_turns are UNRELIABLE (the SDK tags most history as
  "user"). Infer who said what from content, never from a label.
- prior_turns is also how you judge continuity failures: the agent inventing
  context that never appeared, or re-asking for a detail the user already gave.
- flow: execution tree outline showing agent path with numbering (e.g. "1:main(AGENT) > 1.1:classify(LLM) > 1.2:search(Tool)[ERR]"). Read this FIRST to understand the agent's step sequence and where errors occurred.
- tools_available: tools the agent COULD have used
- task_hints: keyword signals (needs_tool, has_format_constraint, quantitative_answer, specific_value)
- signals: structural anomaly flags from rule engine
- spans: ALL spans with COMPRESSED I/O (n=name, d=depth, k=kind, s=status, t=duration, in=input, out=output, f=flags if anomalous)

## ERROR GROUPS

### 1. TOOL FAILURES — Any issue with how tools were used?
Look at: flagged spans with ERR/TOOL_FAIL flags, tools_available vs tools actually called, tool parameters
Subcategories:
- Tool-related: Tool crashed, returned error, or was unavailable. Example: search_tool error, agent fabricated result.
- Tool Selection Errors: Wrong tool chosen. Check tools_available — was a better tool available? Example: used web_search when document_search existed.
- Tool Output Misinterpretation: Misread what a tool returned. Example: API returned error JSON, agent treated error as data.
- Formatting Errors: Tool called with malformed parameters. Example: page_down called with empty {} 6 times.
- Language-only: Agent answered with text when it SHOULD have called a tool to fetch data. ONLY flag this when the task genuinely required a data lookup or computation. Do NOT flag for how-to questions, product guidance, or process explanations — those are correctly answered with text. Check: if task_hints has "needs_tool" AND the question requires live data AND no tool spans in tree. Example: asked "how many invoices this month?" and agent guessed instead of querying.

### 2. CONTEXT & RETRIEVAL — Did the agent miss, forget, or misuse information?
Look at: sequential span patterns (same error repeated), task vs result mismatch on retrieved data
Subcategories:
- Context Handling Failures: Forgot or ignored prior context, OR repeated the same failed approach without adapting. Do NOT flag if the agent correctly handled a follow-up turn in a multi-turn conversation. Example of real failure: tool returned error explaining correct format, agent repeated same mistake.
- Poor Information Retrieval: Got WRONG info from tools/search — the data returned does not match what was asked for. Do NOT flag when: (a) the query returned no results and agent correctly said so, (b) the agent honestly said a feature/doc doesn't exist, (c) the agent provided data with a download link. Only flag when the agent retrieved INCORRECT data or answered with data about the WRONG entity. Example of real failure: searched for vendor A, returned data for vendor B.
- Incorrect Memory Usage: Referenced wrong conversation or hallucinated prior context.

### 3. PLANNING & GOALS — Did the agent stay on track and work efficiently?
Look at: task vs result (same goal?), span count, tree structure for wasted steps
Subcategories:
- Task Orchestration: Steps out of order, missed dependencies. Example: tried to summarize before downloading.
- Goal Deviation: Strayed from intended objective. Compare task vs result — agent answered a COMPLETELY DIFFERENT question. NOTE: asking a clarifying question is NOT deviation. Offering a workaround when a feature doesn't exist is NOT deviation. Refusing an off-topic query is NOT deviation. Only flag when the agent clearly answered the WRONG question despite understanding the right one. Example of real deviation: asked for vendor A's invoices, agent returned vendor B's data.
- Resource Abuse: Excessive tool calls, redundant steps. Example: same query 8 times, 20 calls when 3 suffice.

### 4. OUTPUT QUALITY — Does the final output match what was asked?
Look at: task field (requirements) vs result field (actual output), task_hints
Subcategories:
- Instruction Non-compliance: Output ignores EXPLICIT format/length/style constraints stated by the user. The agent must have clearly violated a STATED requirement. Do NOT flag when: (a) the agent gave a correct answer in a reasonable format, (b) the agent said a feature isn't available/documented (that's honesty, not non-compliance), (c) the agent provided data in a table or download link. Example of real non-compliance: user said "return JSON only", agent returned prose. Example that is NOT non-compliance: user asked "can I bulk edit?" and agent said "no documentation found for this" — that's honest.
- Incorrect Problem Identification: Agent fundamentally misunderstood WHAT the user was asking — answered a completely different question. Do NOT flag when: (a) the agent understood the question but gave an incomplete answer, (b) the agent correctly refused an off-topic/adversarial query, (c) the agent asked for clarification on an ambiguous input like a filename or abbreviation. Example of real misidentification: user asked about deleting policies, agent explained how to create new ones.

### 5. INFRASTRUCTURE — Any system-level failures?
Look at: flagged spans with ERR flags, HTTP status codes in span output
Subcategories:
- Environment Setup Errors: Missing config, API keys, dependencies.
- Resource Not Found: 404, file not found, invalid URL.
- Authentication Errors: 401/403, expired token.
- Timeout Issues: Operations exceeded time limits.
- Service Errors: 5xx errors, service unavailable.

## CRITICAL — WHAT IS NOT AN ERROR
Before flagging anything, check these. Violations of this list are FALSE POSITIVES:

1. **Disambiguation / clarification is CORRECT behavior.** If the user's query is ambiguous (matches multiple entities, vendors, locations, GL accounts, etc.) and the agent asks "did you mean X or Y?", that is GOOD — do NOT flag as Goal Deviation, Incorrect Problem Identification, or Language-only. The agent is being responsible.

2. **Text-only answers are fine when no tool is needed.** Questions like "how do I do X?", "what does Y mean?", "who should I contact?", "how do I log out?" are product/process questions. They don't need a tool call — a knowledgeable text answer is correct. Only flag Language-only when the task GENUINELY required data lookup or computation that ONLY a tool could provide.

3. **Judge the FINAL outcome, not intermediate errors.** If the agent hit a SQL error or tool failure but RECOVERED and delivered a correct final answer, that is NOT a medium/high issue. At most it's a LOW confidence "fragility" signal. The user got what they asked for.

4. **Multi-turn context: short follow-ups are normal.** Queries like "try last year", "partial match is okay", "yes please", "yes", "thank you" are follow-ups or conversation closers. If the agent handled them correctly in context, don't flag them. Polite sign-offs ("thank you" → "you're welcome") are NEVER errors.

5. **"No results found" is often correct.** If the agent queried the database and genuinely found no matching data, reporting "no results" IS the correct answer. Don't flag this as Poor Information Retrieval unless there's evidence the query itself was wrong.

6. **"Feature not documented / not supported" is an honest answer, not a failure.** If the user asks about a feature that doesn't exist (e.g. "can I unmerge a vendor?", "is there a bulk posting date?", "can I export by item?") and the agent honestly says "I couldn't find documentation for this" or "this feature is not available" — that is the CORRECT answer. Do NOT flag as Instruction Non-compliance, Poor Information Retrieval, or Goal Deviation. The agent cannot invent features that don't exist.

7. **Correctly refusing off-topic or adversarial queries is CORRECT.** If the user asks something completely outside the agent's domain (e.g. recipes, car wash advice, personal questions) or attempts prompt injection ("ignore all previous instructions..."), the agent SHOULD refuse. Do NOT flag refusals as Incorrect Problem Identification or Goal Deviation. The agent is enforcing its guardrails correctly.

8. **Providing data with a download link IS providing data.** If the agent generated a CSV/report and provided a download link, that IS actionable output. Do NOT flag as "no actual data shown" or "claims generated but not shown" — the user can access the full dataset via the link.

## RULES
- Evaluate ALL 5 groups for each trace. Do not skip any.
- MULTIPLE subcategories per group are possible but uncommon. Most traces have 0-2 real issues. A trace with 5+ issues is suspicious — re-check for false positives.
- Be PRECISE, not trigger-happy. A false positive wastes developer time investigating non-issues. Only flag issues you're genuinely confident about (H or M).
- For groups 2-4: ALWAYS compare task vs result fields.
- Clean traces are common and expected — empty issues array is the right call for a working trace.
- Only report confidence H (high) or M (medium). Drop anything you'd rate L (low) — it's probably not a real issue.

"""

BATCH_TEMPLATE = """## TRACES
{traces_json}

For each trace, evaluate all 5 groups. If a group has issues, output the SPECIFIC subcategory (e.g. "Tool-related", "Context Handling Failures", "Goal Deviation" — NOT the group name like "Tool Failures").

Return JSON with this EXACT format:
{{"<trace_id>": {{
  "issues": [{{"cat":"<specific subcategory>","conf":"H|M|L","brief":"<10 words max>"}}],
  "key_moments": ["<quote 1>","<quote 2>","<quote 3>"]
}}}}

IMPORTANT:
- "cat" must be one of: Tool-related, Tool Selection Errors, Tool Output Misinterpretation, Formatting Errors, Language-only, Context Handling Failures, Poor Information Retrieval, Incorrect Memory Usage, Task Orchestration, Goal Deviation, Resource Abuse, Instruction Non-compliance, Incorrect Problem Identification, Environment Setup Errors, Resource Not Found, Authentication Errors, Timeout Issues, Service Errors
- "conf": H=high confidence, M=medium. Do NOT include L (low) — drop uncertain findings entirely
- key_moments: 3-5 quotes copied VERBATIM from the trace (task, spans, result). Pick the highlights — the moments that tell the story of what happened. Include user request, key tool outputs, agent decisions, final response. Like a sports replay — the key plays.
- If trace is clean: empty issues array, still provide key_moments showing the successful flow.
- Every trace_id MUST be a key."""


def build_prompt(compressed_traces):
    """Build the full prompt from a list of compress_v2 outputs."""
    traces_json = json.dumps(compressed_traces, separators=(",", ":"))
    return CACHED_PREFIX + BATCH_TEMPLATE.format(traces_json=traces_json)


# ---------------------------------------------------------------------------
# V8 — decomposed, evidence-first scanner prompt
# ---------------------------------------------------------------------------
# Measured on a 1,037-trace production corpus with re-labelled gold, validated once
# on a sealed 421-trace held-out split:
#   this prompt   FPR  9.9%  precision 65.6%  recall 54.1%  F1 0.593
#   CACHED_PREFIX FPR 17.6%  precision 47.6%  recall 45.9%  F1 0.467
# +0.126 F1, 95% CI [+0.037,+0.216] by paired bootstrap. Same model, same one call
# per trace (BATCH_SIZE is already 1), so cost is unchanged.
#
# The lever is DECOMPOSITION, not the input rework that ships alongside it: forcing a
# separate verdict per failure dimension, each gated behind a verbatim quote, stops the
# model scoring a holistic impression. An unquotable failure is a hallucinated one.
# Reference: GPA (arXiv 2510.08847) reports a suite of specialised judges catching 95%
# of annotated errors vs 54.8% for a monolithic judge. Running five SEPARATE judge calls
# was also measured here and did NOT beat this single decomposed call (F1 0.794 vs 0.852,
# n=86) at 5x the cost — the cheap half captures the effect.

# Two additive subcategories: the grounding and completion dimensions have no home in the
# original HIERARCHY, and remapping them onto the nearest existing label would mislabel
# every issue of those kinds. Additive keeps stored issues and their categories valid.
HIERARCHY["Context & Retrieval"].append("Unsupported Claim")
HIERARCHY["Output Quality"].append("Incomplete Response")
for _sc in ("Unsupported Claim", "Incomplete Response"):
    _grp = "Context & Retrieval" if _sc == "Unsupported Claim" else "Output Quality"
    SUBCAT_TO_GROUP[_sc] = _grp
    SUBCAT_TO_FIX_LAYER[_sc] = GROUP_TO_FIX_LAYER[_grp]
    VALID_SUBCATEGORIES.add(_sc)

DIMENSION_TO_SUBCATEGORY = {
    "goal": "Goal Deviation",
    "grounding": "Unsupported Claim",
    "tools": "Tool-related",
    "instruction": "Instruction Non-compliance",
    "completion": "Incomplete Response",
}

SYSTEM_V8 = """You audit ONE execution trace of an AI agent and decide whether the agent failed the user.

You will judge FIVE independent dimensions. For each one you must first quote VERBATIM
evidence, then give a verdict. If you cannot quote the required evidence, the verdict for
that dimension is PASS. An unquotable failure is a hallucinated failure.

WHAT COUNTS AS EVIDENCE — this is strict, and quoting the wrong thing invalidates the FAIL:
- GOAL: quote the user's request AND the part of the agent's response that fails to address it.
- GROUNDING: quote the unsupported claim FROM THE AGENT'S RESPONSE — not the tool output, not
  the user's message. If the sentence you want to quote appears anywhere in a tool result or
  in the user's own words, it IS sourced and this dimension PASSES. Numbers the agent
  legitimately DERIVED (sums, counts, percentages of returned values) are grounded.
- TOOLS: quote the tool error or wrong result AND the agent's contradicting assertion.
- INSTRUCTION: quote TWO things — the rule as stated in the input, AND the response text that
  violates it. Quoting the rule alone is not evidence of a violation.
- COMPLETION: quote the end of the agent's response showing it stops early or is absent.

Never cite a closing pleasantry ("You're welcome", "Have a great day", "Let me know if...")
as evidence of any failure.

DIMENSIONS

1. GOAL — did the agent address what the user actually asked on this turn?
   FAIL only if it answered a materially different question, or silently dropped part
   of an explicit multi-part request.

2. GROUNDING — is every factual claim in the response supported by tool output or the
   user's own input?
   FAIL if the agent asserted a value, status, or fact that appears nowhere in the trace,
   or that contradicts what a tool returned.

3. TOOLS — were tools used correctly and were their results respected?
   FAIL if a tool errored/timed out and the agent asserted success anyway, if it used
   obviously wrong arguments, or if it never called an available tool it plainly needed
   and instead guessed.

4. INSTRUCTION — did the agent honour explicit constraints (format, schema, length,
   language, "only return X")?
   FAIL only when the constraint is stated in the trace and visibly violated.

5. COMPLETION — did the agent actually finish?
   FAIL on empty output, truncation mid-answer, an unrecovered loop, or giving up while
   a usable path remained.
   A response marked "[non-textual response: ...]" is a normal successful reply (an
   embedding vector or scores) — that is not empty, do not fail it.

NOT FAILURES — do not flag any of these:
- asking a clarifying question, or requesting information it genuinely needs
- correctly refusing an out-of-scope, unsafe, or unsupported request
- honestly reporting that data does not exist or that a tool failed
- returning a download link, file reference, or ID instead of inlining data
- terse, unpolished, or plainly-worded phrasing that is still correct
- answering the user's goal from earlier context instead of echoing their last message
- a tool returning a handled error that the agent relays accurately

Speaker labels in conversation history are UNRELIABLE — infer who said what from content.

OUTPUT — strict JSON, no prose outside it:
{
  "dimensions": {
    "goal":        {"evidence": "<verbatim quote or empty>", "verdict": "PASS|FAIL"},
    "grounding":   {"evidence": "...", "verdict": "PASS|FAIL"},
    "tools":       {"evidence": "...", "verdict": "PASS|FAIL"},
    "instruction": {"evidence": "...", "verdict": "PASS|FAIL"},
    "completion":  {"evidence": "...", "verdict": "PASS|FAIL"}
  },
  "issues": [
    {"dim": "goal|grounding|tools|instruction|completion",
     "brief": "<one line, <=15 words, what the agent did wrong>",
     "conf": "H|M"}
  ],
  "key_moments": ["<quote 1>","<quote 2>","<quote 3>"]
}

Emit an entry in "issues" for every dimension whose verdict is FAIL, and nothing else.
If all five PASS, "issues" is an empty list.
key_moments: 3-5 quotes copied VERBATIM from the trace (task, spans, result) telling the
story of what happened — user request, key tool outputs, agent decisions, final response.
Always provide key_moments, including for clean traces."""


def build_prompt_v8(compressed_traces):
    """One trace per call — matches the shipped BATCH_SIZE of 1."""
    t = compressed_traces[0] if isinstance(compressed_traces, list) else compressed_traces
    parts = [
        f"TRACE {t.get('tid','t1')}",
        f"\nUSER REQUEST:\n{t.get('task','')}",
        f"\nAGENT RESPONSE:\n{t.get('result','')}",
    ]
    if t.get("prior_turns"):
        parts.append("\nEARLIER TURNS (oldest first, speaker labels unreliable):\n" +
                     "\n".join(f"- {p}" for p in t["prior_turns"]))
    if t.get("tools_available"):
        parts.append("\nTOOLS AVAILABLE: " + ", ".join(map(str, t["tools_available"]))[:1200])
    if t.get("flow"):
        parts.append(f"\nEXECUTION FLOW:\n{t['flow']}")
    if t.get("signals"):
        parts.append(f"\nSTRUCTURAL SIGNALS: {t['signals']}")
    spans = t.get("spans") or []
    if spans:
        parts.append("\nSPANS (in = input, out = output; [FLAGGED] = structurally anomalous):")
        for s in spans[:40]:
            head = f"  {s.get('depth',0)*'  '}{s.get('name')} ({s.get('kind')}) status={s.get('status')}"
            if s.get("flagged"):
                head += " [FLAGGED]"
            parts.append(head)
            if s.get("in"):
                parts.append(f"      in : {s['in']}")
            if s.get("out"):
                parts.append(f"      out: {s['out']}")
    return "\n".join(parts)
