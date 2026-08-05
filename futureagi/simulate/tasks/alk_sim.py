"""Async tasks for ALK sim ingestion post-processing.

Computes CSAT for a completed voice call and writes ``overall_score`` +
``conversation_metrics_data['csat_score']`` so the frontend detail drawer
and KPI aggregate both light up.

Uses the same DeterministicEvaluator path that
``simulate.utils.chat_simulation._calculate_csat_score`` uses for chat, so the
implementation and scoring criteria stay identical across text and voice.
Audio-native scoring (turing_large on the recording URL) is used when the SDK
already supplied a public recording URL, matching the native voice path in
``ee.voice.temporal.activities.voice_xl.calculate_voice_csat_score``.
"""

from __future__ import annotations

import structlog
from django.db import close_old_connections

from simulate.constants.csat_score_prompt import CSAT_SCORE_PROMPT
from simulate.models import CallExecution
from tfc.temporal.drop_in import temporal_activity

logger = structlog.get_logger(__name__)


@temporal_activity(
    time_limit=600,
    max_retries=0,
    queue="tasks_xl",
)
def calculate_alk_voice_csat_score(call_execution_id: str) -> None:
    close_old_connections()
    try:
        call = CallExecution.objects.select_related(
            "test_execution", "test_execution__run_test"
        ).get(id=call_execution_id)
    except CallExecution.DoesNotExist:
        logger.warning("alk_csat_call_missing", call_execution_id=call_execution_id)
        return

    if call.overall_score is not None:
        return

    csat_score = _score_from_recording(call)
    if csat_score is None:
        csat_score = _score_from_transcript(call)
    if csat_score is None:
        logger.info("alk_csat_unavailable", call_execution_id=str(call.id))
        return

    call.overall_score = csat_score
    metrics = dict(call.conversation_metrics_data or {})
    metrics["csat_score"] = csat_score
    call.conversation_metrics_data = metrics
    call.save(update_fields=["overall_score", "conversation_metrics_data"])
    logger.info(
        "alk_csat_scored",
        call_execution_id=str(call.id),
        csat_score=csat_score,
    )


def _score_from_recording(call: CallExecution) -> float | None:
    """Priority-1 CSAT via audio-native AgentEvaluator (turing_large).

    Runs only when the SDK supplied a public ``recording_url`` — otherwise
    the transcript-text path is used.
    """
    if not call.recording_url:
        return None
    try:
        from ee.evals.llm.agent_evaluator.evaluator import AgentEvaluator

        evaluator = AgentEvaluator(
            rule_prompt=(
                CSAT_SCORE_PROMPT["criteria"]
                + "\n\n## Inputs\n\n<output>{{output}}</output>"
            ),
            model="turing_large",
            output_type="choices",
            choices=list(CSAT_SCORE_PROMPT["choices"]),
            agent_mode="agent",
        )
        batch_result = evaluator.run(
            output=call.recording_url,
            required_keys=["output"],
        )
        return float(batch_result.eval_results[0]["data"]["result"])
    except Exception:
        logger.exception("alk_csat_recording_failed", call_execution_id=str(call.id))
        return None


def _score_from_transcript(call: CallExecution) -> float | None:
    """Priority-2 CSAT — DeterministicEvaluator on the stored transcript.

    Same code path chat CSAT uses (``chat_simulation._calculate_csat_score``);
    keeps the scoring criteria unified across chat and voice.
    """
    transcript_text = _build_transcript_text(call)
    if not transcript_text:
        return None
    try:
        from ee.evals.futureagi.eval_deterministic.evaluator import (
            DeterministicEvaluator,
        )

        evaluator = DeterministicEvaluator(
            multi_choice=CSAT_SCORE_PROMPT["multi_choice"],
            choices=list(CSAT_SCORE_PROMPT["choices"]),
            rule_prompt=CSAT_SCORE_PROMPT["criteria"],
            input=[transcript_text],
            input_type=["text"],
        )
        result = evaluator._evaluate()
        data = result.get("data") or []
        if not data:
            return None
        return float(data[0])
    except (ValueError, TypeError, IndexError):
        logger.warning(
            "alk_csat_transcript_parse_failed", call_execution_id=str(call.id)
        )
        return None
    except Exception:
        logger.exception("alk_csat_transcript_failed", call_execution_id=str(call.id))
        return None


def _build_transcript_text(call: CallExecution) -> str | None:
    from simulate.models.test_execution import CallTranscript

    segments = list(
        CallTranscript.objects.filter(call_execution=call).order_by("start_time_ms")
    )
    if not segments:
        return None
    lines: list[str] = []
    for seg in segments:
        role = (
            "Customer"
            if seg.speaker_role == CallTranscript.SpeakerRole.USER
            else "Agent"
        )
        content = (seg.content or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else None
