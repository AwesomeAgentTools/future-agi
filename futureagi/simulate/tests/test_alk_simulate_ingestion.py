"""Integration tests for the ALK sim ingestion surface.

Covers the full external-runner flow end to end against the real DB:
  start test execution -> batch call executions -> ingest result
plus recording upload and the backend-owned derivations (conversation
metrics, duration backfill, token usage, CSAT preservation).

External side effects are patched at the service boundary: Temporal
dispatch (evals / CSAT / monitor), the websocket notification, and the
object-storage upload. Everything else — metric computation, DB writes,
the API envelope — runs for real.
"""

import json
from unittest.mock import patch

import pytest

from model_hub.models.choices import StatusType
from simulate.models import (
    AgentDefinition,
    RunTest,
    Scenarios,
    SimulatorAgent,
)
from simulate.models.test_execution import (
    CallExecution,
    CallTranscript,
)
from simulate.models.test_execution import TestExecution as SimTestExecution

ALK_BASE = "/simulate/api/alk-simulate"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_definition(db, organization, workspace):
    return AgentDefinition.objects.create(
        agent_name="ALK Ingestion Agent",
        agent_type=AgentDefinition.AgentTypeChoices.VOICE,
        contact_number="+12813716796",
        inbound=True,
        description="Agent under test for ALK ingestion",
        organization=organization,
        workspace=workspace,
        languages=["en"],
    )


@pytest.fixture
def simulator_agent(db, organization, workspace):
    return SimulatorAgent.objects.create(
        name="ALK Simulator",
        prompt="You are a customer.",
        voice_provider="livekit",
        voice_name="alk-simulator",
        model="gpt-4o",
        initial_message="Hi!",
        organization=organization,
        workspace=workspace,
    )


@pytest.fixture
def scenario(db, organization, workspace, agent_definition, simulator_agent):
    return Scenarios.objects.create(
        name="ALK Ingestion Scenario",
        description="Scenario for ALK ingestion tests",
        source="test",
        scenario_type=Scenarios.ScenarioTypes.DATASET,
        organization=organization,
        workspace=workspace,
        agent_definition=agent_definition,
        simulator_agent=simulator_agent,
        status=StatusType.COMPLETED.value,
    )


@pytest.fixture
def run_test(db, organization, workspace, agent_definition, scenario, simulator_agent):
    rt = RunTest.objects.create(
        name="ALK Ingestion Run Test",
        description="Run for ALK ingestion tests",
        agent_definition=agent_definition,
        simulator_agent=simulator_agent,
        organization=organization,
        workspace=workspace,
    )
    rt.scenarios.add(scenario)
    return rt


@pytest.fixture(autouse=True)
def _patch_side_effects():
    """No-op the async dispatch + websocket so the service runs inline."""
    targets = (
        "simulate.services.alk_simulate_ingestion.notify_simulation_update",
        "simulate.services.test_executor._run_simulate_evaluations_task.apply_async",
    )
    with patch(targets[0]), patch(targets[1]):
        with (
            patch(
                "simulate.tasks.chat_sim.monitor_test_execution_for_chat.apply_async"
            ),
            patch("simulate.tasks.alk_sim.calculate_alk_voice_csat_score.apply_async"),
        ):
            yield


def _transcript_payload():
    """A short two-turn transcript with real speech offsets (ms)."""
    return [
        {
            "speaker_role": "user",
            "content": "Hi, my package is late. Can you check the status?",
            "start_time_ms": 0,
            "end_time_ms": 5000,
        },
        {
            "speaker_role": "assistant",
            "content": "Of course, let me look that up for you right away.",
            "start_time_ms": 6000,
            "end_time_ms": 11000,
        },
        {
            "speaker_role": "user",
            "content": "Thank you, I appreciate it.",
            "start_time_ms": 12000,
            "end_time_ms": 15000,
        },
    ]


def _start_and_batch(auth_client, run_test):
    """Helper: start a test execution and allocate its call executions."""
    start = auth_client.post(
        f"{ALK_BASE}/run-tests/{run_test.id}/test-executions/",
        {},
        format="json",
    )
    assert start.status_code == 200, start.content
    test_execution_id = start.json()["result"]["test_execution_id"]

    batch = auth_client.post(
        f"{ALK_BASE}/test-executions/{test_execution_id}/batch/",
        {},
        format="json",
    )
    assert batch.status_code == 200, batch.content
    call_ids = batch.json()["result"]["call_execution_ids"]
    return test_execution_id, call_ids


# ---------------------------------------------------------------------------
# provision (SDK-first RunTest + scenario-of-record)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.django_db
class TestProvisionRunTest:
    """Stand up a chat RunTest + scenario-of-record from SDK personas (no async
    generation), then confirm it feeds the normal ingestion flow."""

    def _provision(self, auth_client, **body):
        return auth_client.post(f"{ALK_BASE}/run-tests/provision/", body, format="json")

    def test_provision_creates_text_agent_scenario_and_run_test(self, auth_client):
        resp = self._provision(
            auth_client,
            name="sdk-e2e",
            personas=[
                {"name": "Sam", "situation": "refund please", "outcome": "refunded"}
            ],
        )
        assert resp.status_code == 200, resp.content
        result = resp.json()["result"]
        assert len(result["scenario_ids"]) == 1

        run_test = RunTest.objects.get(id=result["run_test_id"])
        assert (
            run_test.agent_definition.agent_type
            == AgentDefinition.AgentTypeChoices.TEXT
        )
        assert run_test.scenarios.count() == 1
        scenario = Scenarios.objects.get(id=result["scenario_ids"][0])
        assert scenario.status == StatusType.COMPLETED.value
        assert scenario.metadata["persona"]["name"] == "Sam"

        # A real 1-row persona dataset backs the scenario so it renders with a
        # row and the {{persona}}/{{situation}} placeholders resolve.
        from model_hub.models.develop_dataset import Cell, Row

        assert scenario.dataset_id is not None
        rows = Row.objects.filter(dataset=scenario.dataset)
        assert rows.count() == 1
        cell_values = {
            c.column.name: c.value
            for c in Cell.objects.filter(row=rows.first()).select_related("column")
        }
        assert cell_values["situation"] == "refund please"
        assert cell_values["outcome"] == "refunded"
        assert json.loads(cell_values["persona"])["name"] == "Sam"

    def test_provisioned_run_test_batches_one_call_per_persona(self, auth_client):
        resp = self._provision(
            auth_client,
            name="sdk-e2e-batch",
            personas=[{"name": "Morgan", "situation": "late delivery"}],
        )
        run_test = RunTest.objects.get(id=resp.json()["result"]["run_test_id"])
        _te_id, call_ids = _start_and_batch(auth_client, run_test)
        assert len(call_ids) == 1

    def test_provision_reuses_existing_scenario(self, auth_client, scenario):
        before = Scenarios.objects.count()
        resp = self._provision(
            auth_client, name="sdk-reuse", scenario_ids=[str(scenario.id)]
        )
        assert resp.status_code == 200, resp.content
        result = resp.json()["result"]
        assert result["scenario_ids"] == [str(scenario.id)]
        # No scenario fabricated — the existing one is attached as-is.
        assert Scenarios.objects.count() == before

        run_test = RunTest.objects.get(id=result["run_test_id"])
        assert list(run_test.scenarios.values_list("id", flat=True)) == [scenario.id]
        # Run-test-level simulator agent set from the scenario so batch never
        # writes simulator_agent back onto the shared scenario.
        assert run_test.simulator_agent_id == scenario.simulator_agent_id
        # Chat run test carries a fresh TEXT agent, not the scenario's VOICE one.
        assert (
            run_test.agent_definition.agent_type
            == AgentDefinition.AgentTypeChoices.TEXT
        )

    def test_provision_rejects_both_personas_and_scenario_ids(
        self, auth_client, scenario
    ):
        resp = self._provision(
            auth_client,
            name="both",
            personas=[{"name": "x"}],
            scenario_ids=[str(scenario.id)],
        )
        assert resp.status_code == 400, resp.content

    def test_provision_rejects_neither_personas_nor_scenario_ids(self, auth_client):
        resp = self._provision(auth_client, name="neither")
        assert resp.status_code == 400, resp.content

    def test_provision_reuse_rejects_missing_scenario(self, auth_client):
        import uuid as _uuid

        resp = self._provision(
            auth_client, name="missing", scenario_ids=[str(_uuid.uuid4())]
        )
        assert resp.status_code == 400, resp.content

    def test_provision_rejects_voice_agent_definition(
        self, auth_client, agent_definition
    ):
        # `agent_definition` fixture is VOICE — provisioning must refuse it so it
        # cannot bypass the voice entitlement gate.
        resp = self._provision(
            auth_client,
            name="voice-nope",
            personas=[{"name": "x"}],
            agent_definition_id=str(agent_definition.id),
        )
        assert resp.status_code == 400, resp.content


# ---------------------------------------------------------------------------
# start_test_execution
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.django_db
class TestStartTestExecution:
    def test_creates_execution_inheriting_run_test(
        self, auth_client, run_test, scenario
    ):
        resp = auth_client.post(
            f"{ALK_BASE}/run-tests/{run_test.id}/test-executions/",
            {},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        result = resp.json()["result"]
        assert result["run_test_id"] == str(run_test.id)
        assert result["total_scenarios"] == 1
        assert str(scenario.id) in result["scenario_ids"]

        te = SimTestExecution.objects.get(id=result["test_execution_id"])
        # Inherits agent_definition + scenarios from the run test, no orchestration.
        assert te.agent_definition_id == run_test.agent_definition_id
        assert te.scenario_ids == [str(scenario.id)]
        assert te.status == SimTestExecution.ExecutionStatus.PENDING

    def test_unknown_run_test_returns_404(self, auth_client):
        unknown = "00000000-0000-4000-8000-0000deadbeef"
        resp = auth_client.post(
            f"{ALK_BASE}/run-tests/{unknown}/test-executions/", {}, format="json"
        )
        assert resp.status_code == 404
        assert resp.json()["status"] is False

    def test_scenario_not_on_run_test_returns_400(self, auth_client, run_test):
        other = "11111111-1111-4111-8111-111111111111"
        resp = auth_client.post(
            f"{ALK_BASE}/run-tests/{run_test.id}/test-executions/",
            {"scenario_ids": [other]},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["status"] is False


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.django_db
class TestBatchCreate:
    def test_batch_creates_pending_voice_call(self, auth_client, run_test, scenario):
        te_id, call_ids = _start_and_batch(auth_client, run_test)
        assert len(call_ids) == 1

        call = CallExecution.objects.get(id=call_ids[0])
        assert call.status == CallExecution.CallStatus.PENDING
        assert call.simulation_call_type == CallExecution.SimulationCallType.VOICE
        assert call.scenario_id == scenario.id
        assert call.call_metadata["external_runner"] == "alk"

    def test_second_batch_has_nothing_to_create(self, auth_client, run_test):
        te_id, _ = _start_and_batch(auth_client, run_test)
        second = auth_client.post(
            f"{ALK_BASE}/test-executions/{te_id}/batch/", {}, format="json"
        )
        assert second.status_code == 400
        assert second.json()["status"] is False

    def test_hosted_execute_precreates_rows_and_batch_adopts_them(
        self, auth_client, run_test, scenario
    ):
        from simulate.views.run_test import RunTestExecutionView

        view = RunTestExecutionView()

        def assert_rows_visible_before_dispatch(**_kwargs):
            execution = SimTestExecution.objects.get(run_test=run_test)
            assert execution.calls.count() == 1
            assert execution.calls.get().status == CallExecution.CallStatus.PENDING
            return f"sim-runner-{execution.id}"

        with (
            patch.object(view, "_hosted_runner_mode", return_value="voice_webrtc"),
            patch(
                "simulate.temporal.client.start_simulation_runner_workflow",
                side_effect=assert_rows_visible_before_dispatch,
            ),
        ):
            result = view._execute_with_hosted_runner(
                run_test=run_test,
                scenario_ids=[str(scenario.id)],
                simulator_id=None,
            )

        execution = SimTestExecution.objects.get(id=result["execution_id"])
        precreated_id = str(execution.calls.get().id)
        assert result["total_calls"] == 1
        assert execution.total_calls == 1

        batch = auth_client.post(
            f"{ALK_BASE}/test-executions/{execution.id}/batch/", {}, format="json"
        )

        assert batch.status_code == 200, batch.content
        assert batch.json()["result"]["call_execution_ids"] == [precreated_id]
        assert execution.calls.count() == 1
        adopted_call = execution.calls.get()
        assert adopted_call.call_metadata["alk_batch_claimed"] is True


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.django_db
class TestInternalServiceIngestion:
    def test_internal_service_batches_and_ingests_precreated_execution(
        self, auth_client, api_client, run_test
    ):
        from django.test import override_settings

        start = auth_client.post(
            f"{ALK_BASE}/run-tests/{run_test.id}/test-executions/",
            {},
            format="json",
        )
        test_execution_id = start.json()["result"]["test_execution_id"]
        api_client.credentials(
            HTTP_AUTHORIZATION="Bearer hosted-runner-internal-secret"
        )

        with override_settings(INTERNAL_API_SECRET="hosted-runner-internal-secret"):
            batch = api_client.post(
                f"{ALK_BASE}/test-executions/{test_execution_id}/batch/",
                {},
                format="json",
            )
            assert batch.status_code == 200, batch.content
            call_id = batch.json()["result"]["call_execution_ids"][0]

            result = api_client.patch(
                f"{ALK_BASE}/call-executions/{call_id}/result/",
                {"status": "completed", "transcript": _transcript_payload()},
                format="json",
            )

        assert result.status_code == 200, result.content
        call = CallExecution.objects.get(id=call_id)
        assert call.status == CallExecution.CallStatus.COMPLETED
        assert call.test_execution.run_test.organization_id == run_test.organization_id

    def test_internal_service_cannot_create_execution(self, api_client, run_test):
        from django.test import override_settings

        api_client.credentials(
            HTTP_AUTHORIZATION="Bearer hosted-runner-internal-secret"
        )
        with override_settings(INTERNAL_API_SECRET="hosted-runner-internal-secret"):
            response = api_client.post(
                f"{ALK_BASE}/run-tests/{run_test.id}/test-executions/",
                {},
                format="json",
            )

        assert response.status_code == 404

    def test_wrong_internal_secret_is_rejected(self, api_client, run_test):
        from simulate.services.alk_simulate_ingestion import (
            create_alk_sim_test_execution,
        )

        test_execution = create_alk_sim_test_execution(run_test)
        api_client.credentials(HTTP_AUTHORIZATION="Bearer wrong-secret")
        response = api_client.post(
            f"{ALK_BASE}/test-executions/{test_execution.id}/batch/",
            {},
            format="json",
        )

        assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.django_db
class TestMixedResultRollup:
    def test_failed_calls_do_not_block_completed_call_evaluations(
        self, auth_client, run_test
    ):
        from simulate.services.test_executor import TestExecutor

        test_execution_id, call_ids = _start_and_batch(auth_client, run_test)
        test_execution = SimTestExecution.objects.get(id=test_execution_id)
        completed_call = CallExecution.objects.get(id=call_ids[0])
        completed_call.status = CallExecution.CallStatus.COMPLETED
        completed_call.call_metadata = {"eval_started": True, "eval_completed": True}
        completed_call.save(update_fields=["status", "call_metadata"])
        CallExecution.objects.create(
            test_execution=test_execution,
            scenario=completed_call.scenario,
            status=CallExecution.CallStatus.FAILED,
            simulation_call_type=completed_call.simulation_call_type,
            call_metadata={},
        )
        test_execution.status = SimTestExecution.ExecutionStatus.EVALUATING
        test_execution.save(update_fields=["status"])

        TestExecutor(
            initialize_voice_service=False
        )._check_and_update_test_execution_completion(test_execution.id)

        test_execution.refresh_from_db()
        assert test_execution.status == SimTestExecution.ExecutionStatus.COMPLETED
        assert test_execution.completed_at is not None
        assert test_execution.total_calls == 2
        assert test_execution.completed_calls == 1
        assert test_execution.failed_calls == 1


# ---------------------------------------------------------------------------
# result ingest — metrics, duration, tokens, csat
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.django_db
class TestResultIngest:
    def test_ingest_computes_metrics_and_duration(self, auth_client, run_test):
        _, call_ids = _start_and_batch(auth_client, run_test)
        call_id = call_ids[0]

        resp = auth_client.patch(
            f"{ALK_BASE}/call-executions/{call_id}/result/",
            {"status": "completed", "transcript": _transcript_payload()},
            format="json",
        )
        assert resp.status_code == 200, resp.content

        call = CallExecution.objects.get(id=call_id)
        assert call.status == CallExecution.CallStatus.COMPLETED
        # Transcript persisted.
        assert CallTranscript.objects.filter(call_execution=call).count() == 3
        # Metrics recomputed server-side from the transcript.
        cmd = call.conversation_metrics_data or {}
        assert cmd.get("turn_count") == 1  # one assistant/bot turn
        assert cmd.get("message_count") == 3
        assert call.user_wpm is not None and call.user_wpm > 0
        # Duration backfilled from the last transcript offset (15000 ms).
        assert call.duration_seconds == 15

    def test_ingest_writes_token_usage_from_provider_data(self, auth_client, run_test):
        _, call_ids = _start_and_batch(auth_client, run_test)
        call_id = call_ids[0]

        resp = auth_client.patch(
            f"{ALK_BASE}/call-executions/{call_id}/result/",
            {
                "status": "completed",
                "transcript": _transcript_payload(),
                "provider_call_data": {
                    "vapi": {
                        "usage": {
                            "llm": {
                                "prompt_tokens": 1200,
                                "completion_tokens": 450,
                                "total_tokens": 1650,
                            }
                        }
                    }
                },
                "costs": {"cost_cents": 16},
            },
            format="json",
        )
        assert resp.status_code == 200, resp.content

        call = CallExecution.objects.get(id=call_id)
        cmd = call.conversation_metrics_data or {}
        assert cmd.get("input_tokens") == 1200
        assert cmd.get("output_tokens") == 450
        assert cmd.get("total_tokens") == 1650
        assert call.cost_cents == 16

    def test_retell_total_only_tokens(self, auth_client, run_test):
        _, call_ids = _start_and_batch(auth_client, run_test)
        call_id = call_ids[0]

        resp = auth_client.patch(
            f"{ALK_BASE}/call-executions/{call_id}/result/",
            {
                "status": "completed",
                "transcript": _transcript_payload(),
                "provider_call_data": {
                    "retell": {"usage": {"llm": {"total_tokens": 1500}}}
                },
            },
            format="json",
        )
        assert resp.status_code == 200, resp.content

        cmd = CallExecution.objects.get(id=call_id).conversation_metrics_data or {}
        assert cmd.get("total_tokens") == 1500
        assert cmd.get("input_tokens") is None

    def test_reingest_preserves_csat(self, auth_client, run_test):
        _, call_ids = _start_and_batch(auth_client, run_test)
        call_id = call_ids[0]
        body = {"status": "completed", "transcript": _transcript_payload()}

        first = auth_client.patch(
            f"{ALK_BASE}/call-executions/{call_id}/result/", body, format="json"
        )
        assert first.status_code == 200

        # Simulate the async CSAT task having written a score.
        call = CallExecution.objects.get(id=call_id)
        cmd = dict(call.conversation_metrics_data or {})
        cmd["csat_score"] = 6.0
        call.conversation_metrics_data = cmd
        call.overall_score = 6.0
        call.save(update_fields=["conversation_metrics_data", "overall_score"])

        # A second idempotent ingest must not wipe csat_score.
        second = auth_client.patch(
            f"{ALK_BASE}/call-executions/{call_id}/result/", body, format="json"
        )
        assert second.status_code == 200
        cmd = CallExecution.objects.get(id=call_id).conversation_metrics_data or {}
        assert cmd.get("csat_score") == 6.0

    def test_unknown_call_returns_404(self, auth_client):
        unknown = "00000000-0000-4000-8000-0000deadbeef"
        resp = auth_client.patch(
            f"{ALK_BASE}/call-executions/{unknown}/result/",
            {"status": "completed"},
            format="json",
        )
        assert resp.status_code == 404
        assert resp.json()["status"] is False


# ---------------------------------------------------------------------------
# recording upload
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.django_db
class TestRecordingUpload:
    def test_multipart_upload_persists_recording(self, auth_client, run_test):
        _, call_ids = _start_and_batch(auth_client, run_test)
        call_id = call_ids[0]

        from unittest.mock import MagicMock

        from django.core.files.uploadedfile import SimpleUploadedFile

        audio = SimpleUploadedFile(
            "combined.wav", b"RIFFfakewavdata", content_type="audio/wav"
        )

        fake_client = MagicMock()
        with (
            patch(
                "simulate.services.alk_simulate_ingestion.get_storage_client",
                return_value=fake_client,
            ),
            patch(
                "simulate.services.alk_simulate_ingestion.get_object_url",
                return_value="https://storage.example.com/fi-content/alk-sim/recordings/x.wav",
            ),
        ):
            resp = auth_client.post(
                f"{ALK_BASE}/call-executions/{call_id}/recording/",
                {"file": audio, "filename": "combined.wav"},
                format="multipart",
            )

        assert resp.status_code == 200, resp.content
        result = resp.json()["result"]
        assert result["recording_url"].endswith(".wav")
        assert result["object_key"].startswith("alk-sim/recordings/")
        # Bytes were written to the upload bucket via the storage client.
        fake_client.put_object.assert_called_once()

    def test_missing_file_returns_400(self, auth_client, run_test):
        _, call_ids = _start_and_batch(auth_client, run_test)
        resp = auth_client.post(
            f"{ALK_BASE}/call-executions/{call_ids[0]}/recording/",
            {},
            format="multipart",
        )
        assert resp.status_code == 400
        assert resp.json()["status"] is False


# ---------------------------------------------------------------------------
# Hosted runner (chat / TEXT mode)
# ---------------------------------------------------------------------------


@pytest.fixture
def text_agent_definition(db, organization, workspace):
    return AgentDefinition.objects.create(
        agent_name="ALK Chat Agent",
        agent_type=AgentDefinition.AgentTypeChoices.TEXT,
        contact_number="",
        inbound=True,
        description="Chat agent under test for the hosted runner",
        organization=organization,
        workspace=workspace,
        languages=["en"],
    )


@pytest.fixture
def text_run_test(
    db, organization, workspace, text_agent_definition, scenario, simulator_agent
):
    rt = RunTest.objects.create(
        name="ALK Chat Run Test",
        description="Chat run for the hosted runner",
        agent_definition=text_agent_definition,
        simulator_agent=simulator_agent,
        organization=organization,
        workspace=workspace,
    )
    rt.scenarios.add(scenario)
    return rt


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.django_db
class TestTextModeIngestion:
    def test_batch_creates_text_call(self, auth_client, text_run_test, scenario):
        te_id, call_ids = _start_and_batch(auth_client, text_run_test)
        assert len(call_ids) == 1

        call = CallExecution.objects.get(id=call_ids[0])
        assert call.simulation_call_type == CallExecution.SimulationCallType.TEXT
        assert call.call_metadata["call_channel"] == "chat"
        assert call.call_metadata["external_runner"] == "alk"

    def test_text_result_ingest_writes_chat_messages(self, auth_client, text_run_test):
        from simulate.models.chat_message import ChatMessageModel

        _, call_ids = _start_and_batch(auth_client, text_run_test)
        resp = auth_client.patch(
            f"{ALK_BASE}/call-executions/{call_ids[0]}/result/",
            {"status": "completed", "transcript": _transcript_payload()},
            format="json",
        )
        assert resp.status_code == 200, resp.content

        call = CallExecution.objects.get(id=call_ids[0])
        assert call.status == CallExecution.CallStatus.COMPLETED
        # Chat runs render from ChatMessage (not voice CallTranscript).
        chat_rows = ChatMessageModel.objects.filter(call_execution=call)
        assert chat_rows.count() == 3
        assert CallTranscript.objects.filter(call_execution=call).count() == 0
        # turn_count = number of ASSISTANT rows (one agent turn in the fixture).
        cmd = call.conversation_metrics_data or {}
        assert cmd.get("turn_count") == 1

    def test_text_result_ingest_folds_tool_calls_into_agent_turn(
        self, auth_client, text_run_test
    ):
        from simulate.models.chat_message import ChatMessageModel

        _, call_ids = _start_and_batch(auth_client, text_run_test)
        transcript = [
            {"speaker_role": "user", "content": "My order A1 arrived damaged."},
            {
                "speaker_role": "tool_calls",
                "content": 'lookup_order({"order_id": "A1"})',
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "lookup_order",
                        "arguments": {"order_id": "A1"},
                    }
                ],
            },
            {
                "speaker_role": "tool_call_result",
                "content": "order A1: eligible for refund",
                "tool_call_id": "c1",
            },
            {"speaker_role": "assistant", "content": "You're eligible — refund done."},
        ]
        resp = auth_client.patch(
            f"{ALK_BASE}/call-executions/{call_ids[0]}/result/",
            {"status": "completed", "transcript": transcript},
            format="json",
        )
        assert resp.status_code == 200, resp.content

        call = CallExecution.objects.get(id=call_ids[0])
        rows = list(
            ChatMessageModel.objects.filter(call_execution=call).order_by("created_at")
        )
        # One exchange: 1 USER row + 1 folded ASSISTANT row (tool call + result +
        # final text). turn_count stays 1 (native exchange semantic).
        assert len(rows) == 2
        assistant = next(r for r in rows if r.role == "assistant")
        blob = json.dumps(assistant.content)
        assert "lookup_order" in blob
        assert "eligible for refund" in blob
        assert any(item.get("tool_calls") for item in assistant.content)
        assert (call.conversation_metrics_data or {}).get("turn_count") == 1


@pytest.mark.integration
@pytest.mark.django_db
class TestBuildRunnerJob:
    def test_builds_chat_job_for_text_run(self, text_run_test, scenario):
        from django.test import override_settings

        from simulate.services.alk_simulate_ingestion import (
            create_alk_sim_test_execution,
        )
        from simulate.services.hosted_runner import build_start_runner_job

        te = create_alk_sim_test_execution(text_run_test)

        with override_settings(
            ALK_RUNNER_DEFAULT_CHAT_TARGET="my_module:reply",
            ALK_RUNNER_API_URL="http://localhost:8000",
        ):
            job = build_start_runner_job(
                test_execution_id=str(te.id),
                run_test_id=str(text_run_test.id),
                scenario_ids=[str(scenario.id)],
                mode="chat",
            )

        assert job["schema_version"] == "futureagi.runner-job.v1"
        assert job["mode"] == "chat"
        assert job["spec"]["environment"]["adapter"] == "chat"
        # No provider server_url -> falls back to the configured callable target.
        assert job["spec"]["target"]["adapter"] == "callable"
        assert job["spec"]["target"]["config"]["target"] == "my_module:reply"
        assert len(job["spec"]["scenario"]["dataset"]) == 1
        # Sink points at the pre-created execution + carries secret refs only.
        assert job["sink"]["test_execution_id"] == str(te.id)
        assert job["sink"]["run_test_id"] == str(text_run_test.id)
        internal_ref = job["sink"]["secret_refs"]["internal_api_secret"]
        assert internal_ref == {
            "manager": "env",
            "key": "INTERNAL_API_SECRET",
            "purpose": "internal_api_secret",
        }


@pytest.mark.integration
@pytest.mark.django_db
class TestBuildVoiceRunnerJob:
    """#149 — the voice branch maps a platform VOICE run test to a voice job
    (VoiceRunConfig shape) with the transport derived from provider + phone."""

    def _voice_agent(
        self,
        organization,
        workspace,
        *,
        provider=None,
        phone="",
        inbound=True,
        assistant_id="",
        server_url="",
        agent_name="",
    ):
        agent = AgentDefinition.objects.create(
            agent_name="Voice Target",
            agent_type=AgentDefinition.AgentTypeChoices.VOICE,
            contact_number=phone,
            inbound=inbound,
            description="Voice agent under test",
            provider=provider,
            assistant_id=assistant_id,
            organization=organization,
            workspace=workspace,
            languages=["en"],
        )
        if provider in {"vapi", "retell", "livekit"}:
            from simulate.models.agent_definition import ProviderCredentials

            ProviderCredentials.objects.create(
                agent_definition=agent,
                provider_type=provider,
                api_key="secret-key-value",
                api_secret="secret-secret-value" if provider == "livekit" else "",
                assistant_id=assistant_id,
                server_url=server_url,
                agent_name=agent_name,
            )
        return agent

    def _run_test(self, organization, workspace, agent, simulator_agent, scenario):
        rt = RunTest.objects.create(
            name="Voice Run",
            description="voice",
            agent_definition=agent,
            simulator_agent=simulator_agent,
            organization=organization,
            workspace=workspace,
        )
        rt.scenarios.add(scenario)
        return rt

    def _scenario(self, organization, workspace, agent, simulator_agent):
        return Scenarios.objects.create(
            name="Voice Scenario",
            description="A late delivery.",
            source="test",
            scenario_type=Scenarios.ScenarioTypes.DATASET,
            organization=organization,
            workspace=workspace,
            agent_definition=agent,
            simulator_agent=simulator_agent,
            status=StatusType.COMPLETED.value,
        )

    def _build(self, organization, workspace, simulator_agent, agent):
        from simulate.services.alk_simulate_ingestion import (
            create_alk_sim_test_execution,
        )
        from simulate.services.hosted_runner import (
            build_start_runner_job,
            resolve_runner_mode,
        )

        scenario = self._scenario(organization, workspace, agent, simulator_agent)
        rt = self._run_test(organization, workspace, agent, simulator_agent, scenario)
        te = create_alk_sim_test_execution(rt)
        mode = resolve_runner_mode(agent)
        job = build_start_runner_job(
            test_execution_id=str(te.id),
            run_test_id=str(rt.id),
            scenario_ids=[str(scenario.id)],
            mode=mode,
        )
        return job, mode

    def test_resolve_runner_mode(self, organization, workspace):
        from simulate.services.hosted_runner import resolve_runner_mode

        web = self._voice_agent(organization, workspace, provider="vapi", phone="")
        phoned = self._voice_agent(
            organization, workspace, provider="vapi", phone="+15551234567"
        )
        assert resolve_runner_mode(web) == "voice_webrtc"
        assert resolve_runner_mode(phoned) == "voice_sip"

    def test_builds_vapi_websocket_job(self, organization, workspace, simulator_agent):
        agent = self._voice_agent(
            organization, workspace, provider="vapi", phone="", assistant_id="asst_123"
        )
        job, mode = self._build(organization, workspace, simulator_agent, agent)
        assert mode == "voice_webrtc"
        assert job["mode"] == "voice_webrtc"
        assert "spec" not in job
        adef = job["voice"]["agent_definition"]
        assert adef["transport"]["kind"] == "vapi_websocket"
        assert adef["target"] == {
            "provider": "vapi",
            "assistant_id": "asst_123",
            "api_key_env": "VAPI_API_KEY",
        }
        assert job["voice"]["params"]["record_audio"] is True
        # Provider secret resolves from ProviderCredentials, LiveKit from env.
        keys = {r["key"]: r for r in job["metadata"]["secret_env"]}
        assert keys["VAPI_API_KEY"]["manager"] == "provider_credentials"
        assert keys["LIVEKIT_API_KEY"]["manager"] == "env"
        assert job["metadata"]["run_id"] == job["sink"]["test_execution_id"]

    def test_builds_retell_webcall_job(self, organization, workspace, simulator_agent):
        agent = self._voice_agent(
            organization,
            workspace,
            provider="retell",
            phone="",
            assistant_id="agent_xyz",
        )
        job, mode = self._build(organization, workspace, simulator_agent, agent)
        adef = job["voice"]["agent_definition"]
        assert mode == "voice_webrtc"
        assert adef["transport"]["kind"] == "retell_webcall"
        assert adef["target"]["provider"] == "retell"
        assert adef["target"]["agent_id"] == "agent_xyz"
        assert adef["target"]["api_key_env"] == "RETELL_API_KEY"

    def test_builds_webrtc_job_uses_customer_livekit(
        self, organization, workspace, simulator_agent
    ):
        agent = self._voice_agent(
            organization,
            workspace,
            provider="livekit",
            phone="",
            server_url="wss://customer.livekit.cloud",
            agent_name="target-worker",
        )
        job, mode = self._build(organization, workspace, simulator_agent, agent)
        adef = job["voice"]["agent_definition"]
        assert mode == "voice_webrtc"
        assert adef["transport"] == {"kind": "webrtc"}
        assert adef["agent_name"] == "target-worker"
        rt = job["voice"]["livekit_runtime"]
        assert rt["url"] == "wss://customer.livekit.cloud"
        keys = {r["key"]: r for r in job["metadata"]["secret_env"]}
        assert keys["LIVEKIT_API_KEY"]["manager"] == "provider_credentials"
        assert keys["LIVEKIT_API_SECRET"]["field"] == "api_secret"

    def test_dataset_rows_expand_to_one_voice_case_each(
        self, organization, workspace, simulator_agent
    ):
        from model_hub.models.choices import DatasetSourceChoices, SourceChoices
        from model_hub.models.develop_dataset import Cell, Column, Dataset, Row
        from simulate.services.alk_simulate_ingestion import (
            create_alk_sim_call_execution_batch,
            create_alk_sim_test_execution,
            precreate_alk_sim_call_executions,
        )
        from simulate.services.hosted_runner import build_start_runner_job

        agent = self._voice_agent(
            organization, workspace, provider="vapi", assistant_id="asst_123"
        )
        dataset = Dataset.no_workspace_objects.create(
            name="ten hosted cases",
            organization=organization,
            workspace=workspace,
            source=DatasetSourceChoices.SCENARIO.value,
        )
        columns = {
            name: Column.objects.create(
                dataset=dataset,
                name=name,
                data_type="persona" if name == "persona" else "text",
                source=SourceChoices.OTHERS.value,
            )
            for name in ("persona", "situation", "outcome", "branch_category")
        }
        for index in range(10):
            row = Row.objects.create(dataset=dataset, order=index)
            row_values = {
                "persona": str({"name": f"Customer {index}", "age_group": "25-35"}),
                "situation": f"Situation {index}",
                "outcome": f"Outcome {index}",
                "branch_category": f"Branch {index}",
            }
            for name, value in row_values.items():
                Cell.objects.create(
                    dataset=dataset, column=columns[name], row=row, value=value
                )

        scenario = self._scenario(organization, workspace, agent, simulator_agent)
        scenario.dataset = dataset
        scenario.save(update_fields=["dataset"])
        run_test = self._run_test(
            organization, workspace, agent, simulator_agent, scenario
        )
        execution = create_alk_sim_test_execution(run_test)
        precreated_ids = precreate_alk_sim_call_executions(execution)
        batch = create_alk_sim_call_execution_batch(execution)

        assert len(precreated_ids) == 10
        assert batch.call_execution_ids == precreated_ids
        assert execution.calls.count() == 10
        assert (
            execution.calls.filter(status=CallExecution.CallStatus.PENDING).count()
            == 10
        )

        job = build_start_runner_job(
            test_execution_id=str(execution.id),
            run_test_id=str(run_test.id),
            scenario_ids=[str(scenario.id)],
            mode="voice_webrtc",
        )

        cases = job["voice"]["scenario"]["dataset"]
        assert len(cases) == 10
        assert [case["persona"]["name"] for case in cases] == [
            f"Customer {index}" for index in range(10)
        ]
        assert cases[4]["situation"] == "Situation 4"
        assert cases[4]["outcome"] == "Outcome 4"
        assert cases[4]["persona"]["branch_category"] == "Branch 4"
        # ALK 0.1 calculates its outer run deadline from these params. The
        # allowance must cover all ten sequential voice cases, not one call.
        params = job["voice"]["params"]
        per_case_budget = 120.0 + 60.0 + 120.0 + 30.0
        assert params["cleanup_timeout"] == 30.0 + 9 * per_case_budget

    def test_builds_sip_outbound_job(self, organization, workspace, simulator_agent):
        from django.test import override_settings

        agent = self._voice_agent(
            organization,
            workspace,
            provider="livekit",
            phone="+15551230000",
            inbound=True,
        )
        with override_settings(
            LIVEKIT_OUTBOUND_TRUNK_ID="ST_trunk", PSTN_CALLER_NUMBER="+15550009999"
        ):
            job, mode = self._build(organization, workspace, simulator_agent, agent)
        assert mode == "voice_sip"
        t = job["voice"]["agent_definition"]["transport"]
        assert t["kind"] == "sip_outbound"
        assert t["sip_trunk_id"] == "ST_trunk"
        assert t["sip_number"] == "+15550009999"
        assert t["sip_call_to"] == "+15551230000"

    def test_builds_sip_inbound_job_no_did_at_build(
        self, organization, workspace, simulator_agent
    ):
        # Outbound agent (inbound=False) dials the simulator DID -> sip_inbound;
        # the DID/dispatch rule are leased by the runner activity, not here.
        agent = self._voice_agent(
            organization,
            workspace,
            provider="vapi",
            phone="+15551230000",
            inbound=False,
        )
        job, mode = self._build(organization, workspace, simulator_agent, agent)
        assert mode == "voice_sip"
        t = job["voice"]["agent_definition"]["transport"]
        assert t["kind"] == "sip_inbound"
        assert "dispatch_rule_name" not in t
        assert t["inbound_call_originator"] == "vapi"
        assert job["voice"]["agent_definition"]["provider_evidence"] == {
            "provider": "vapi",
            "call_id_source": "originator_response",
        }


class TestHostedRunnerActivityHelpers:
    def test_default_voice_simulator_uses_openai(self, monkeypatch, settings):
        from simulate.services.hosted_runner import _voice_simulator_config

        settings.SIMULATOR_LLM_PROVIDER = ""
        settings.SIMULATOR_LLM_MODEL = ""
        monkeypatch.delenv("SIMULATOR_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SIMULATOR_LLM_MODEL", raising=False)

        simulator = _voice_simulator_config()

        assert simulator["llm"] == {
            "provider": "openai",
            "model": "gpt-4.1",
        }

    def test_voice_conversation_direction_is_configurable(self, monkeypatch, settings):
        from simulate.services.hosted_runner import _voice_params

        settings.SIMULATOR_CONVERSATION_DIRECTION = ""
        monkeypatch.setenv("SIMULATOR_CONVERSATION_DIRECTION", "agent_first")

        assert _voice_params("webrtc")["conversation_direction"] == "agent_first"

    """The DID pool is touched only for sip_inbound (mirrors _needs_phone)."""

    def test_inject_did_slot_only_for_sip_inbound(self):
        from simulate.temporal.activities.hosted_runner import (
            _child_environment,
            _inject_did_slot,
        )

        slot = {
            "did": "+15557654321",
            "dispatch_rule_name": "rule-1",
            "room_name": "sim-slot-01",
            "slot_id": "s1",
        }
        inbound = {
            "voice": {
                "agent_definition": {"transport": {"kind": "sip_inbound"}},
                "livekit_runtime": {"room_name": "hosted-{test_case_id}"},
                "params": {},
                "scenario": {"dataset": [{"persona": {"name": "Caller"}}]},
            },
            "metadata": {},
        }
        _inject_did_slot(inbound, slot)
        t = inbound["voice"]["agent_definition"]["transport"]
        assert t["dispatch_rule_name"] == "rule-1"
        assert inbound["voice"]["params"]["inbound_did"] == "+15557654321"
        assert _child_environment(inbound)["LIVEKIT_INBOUND_DID"] == "+15557654321"
        assert inbound["voice"]["livekit_runtime"] == {
            "room_name": "sim-slot-01",
            "room_name_verbatim": True,
        }

        outbound = {
            "voice": {
                "agent_definition": {
                    "transport": {"kind": "sip_outbound", "sip_call_to": "+1"}
                }
            }
        }
        _inject_did_slot(outbound, slot)
        # sip_outbound dials the target directly; never consumes a leased DID.
        assert (
            "dispatch_rule_name"
            not in (outbound["voice"]["agent_definition"]["transport"])
        )

    def test_acquire_did_slot_none_without_script(self, monkeypatch):
        import asyncio

        from simulate.temporal.activities.hosted_runner import _acquire_did_slot

        monkeypatch.delenv("ALK_SIM_SLOT_LEASE_SCRIPT", raising=False)
        assert asyncio.run(_acquire_did_slot("job-1")) is None

    def test_child_environment_maps_internal_sink_secret(self, monkeypatch):
        from simulate.temporal.activities.hosted_runner import _child_environment

        monkeypatch.setenv("INTERNAL_API_SECRET", "shared-service-secret")
        job = {
            "sink": {
                "secret_refs": {
                    "internal_api_secret": {
                        "manager": "env",
                        "key": "INTERNAL_API_SECRET",
                        "purpose": "internal_api_secret",
                    }
                }
            }
        }

        child_env = _child_environment(job)

        assert child_env["FI_INTERNAL_API_SECRET"] == "shared-service-secret"

    def test_waiting_for_child_slot_heartbeats(self, monkeypatch):
        import asyncio

        from simulate.temporal.activities import hosted_runner as hr

        semaphore = asyncio.Semaphore(0)
        heartbeats = []
        monkeypatch.setattr(hr, "_child_semaphore", semaphore)
        monkeypatch.setattr(hr, "_CHILD_SLOT_HEARTBEAT_SECONDS", 0.001)
        monkeypatch.setattr(hr.activity, "heartbeat", heartbeats.append)

        async def exercise():
            acquire = asyncio.create_task(hr._acquire_child_slot())
            await asyncio.sleep(0.01)
            semaphore.release()
            await acquire

        asyncio.run(exercise())

        assert "waiting_for_child_slot" in heartbeats

    def test_acquire_did_slot_uses_livekit_infra_contract(self, monkeypatch):
        import asyncio

        from simulate.temporal.activities import hosted_runner as hr

        calls = []

        class LeaseProc:
            returncode = 0

            async def communicate(self):
                return (
                    b'{\n  "slot": "07",\n'
                    b'  "phone_number": "+15557654321",\n'
                    b'  "dispatch_rule_name": "sim-slot-07"\n}\n',
                    b"",
                )

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return LeaseProc()

        monkeypatch.setenv("ALK_SIM_SLOT_LEASE_SCRIPT", "/infra/lease_sim_slot.py")
        monkeypatch.setenv("ALK_RUNNER_PYTHON", "/venv/bin/python")
        monkeypatch.setattr(hr.asyncio, "create_subprocess_exec", fake_exec)

        slot = asyncio.run(hr._acquire_did_slot("job-123"))

        assert calls == [
            (
                "/venv/bin/python",
                "/infra/lease_sim_slot.py",
                "acquire",
                "--run-id",
                "job-123",
            )
        ]
        assert slot["slot_id"] == "07"
        assert slot["did"] == "+15557654321"


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProc:
    def __init__(self, lines, return_code=0):
        self.stdout = _FakeStdout(lines)
        self.returncode = return_code
        self._rc = return_code

    async def wait(self):
        self.returncode = self._rc
        return self._rc

    def terminate(self):  # pragma: no cover - cancel path only
        pass


class TestRunHostedSdkJob:
    """Runtime-exercise the restructured run_hosted_sdk_job (try/finally + DID
    lease + secret env), spawning a fake child instead of the real SDK."""

    def _run(
        self, monkeypatch, *, mode, job, status_lines, acquire=None, released=None
    ):
        import asyncio

        from simulate.temporal.activities import hosted_runner as hr
        from simulate.temporal.types.hosted_runner import RunHostedJobInput

        async def _fake_exec(*args, **kwargs):
            return _FakeProc([ln.encode() for ln in status_lines])

        monkeypatch.setattr(hr.asyncio, "create_subprocess_exec", _fake_exec)
        # Bare-calling the activity (no worker) => no Temporal activity context.
        monkeypatch.setattr(hr.activity, "heartbeat", lambda *a, **k: None)
        if acquire is not None:
            monkeypatch.setattr(hr, "_acquire_did_slot", acquire)
        if released is not None:
            monkeypatch.setattr(hr, "_release_did_slot", released)

        inp = RunHostedJobInput(
            job_id="job-x", run_id="run-x", mode=mode, job_json=json.dumps(job)
        )
        return asyncio.run(hr.run_hosted_sdk_job(inp))

    def test_chat_job_runs_and_completes(self, monkeypatch):
        job = {
            "mode": "chat",
            "spec": {"run_id": "run-x", "target": {"secret_refs": {}}},
            "sink": {"api_url": "http://localhost:8000"},
            "metadata": {"run_id": "run-x"},
        }
        lines = [
            '{"phase": "running", "job_id": "job-x"}',
            '{"phase": "completed", "job_id": "job-x", "report_hash": "h1", '
            '"submission_status": "submitted"}',
        ]
        out = self._run(monkeypatch, mode="chat", job=job, status_lines=lines)
        assert out.phase == "completed"
        assert out.return_code == 0
        assert out.submission_status == "submitted"

    def test_voice_sip_leases_injects_and_releases(self, monkeypatch):
        released_slots = []

        async def _acquire(job_id):
            return {
                "did": "+15557654321",
                "dispatch_rule_name": "rule-9",
                "slot_id": "s9",
            }

        async def _release(slot):
            released_slots.append(slot["slot_id"])

        job = {
            "mode": "voice_sip",
            "voice": {
                "agent_definition": {"transport": {"kind": "sip_inbound"}},
                "params": {},
            },
            "sink": {"api_url": "http://localhost:8000"},
            "metadata": {"run_id": "run-x", "secret_env": []},
        }
        lines = [
            '{"phase": "completed", "job_id": "job-x", "submission_status": "submitted"}'
        ]
        out = self._run(
            monkeypatch,
            mode="voice_sip",
            job=job,
            status_lines=lines,
            acquire=_acquire,
            released=_release,
        )
        assert out.phase == "completed"
        # The leased slot was released in finally.
        assert released_slots == ["s9"]

    def test_web_voice_never_leases(self, monkeypatch):
        async def _acquire(job_id):  # pragma: no cover - must not be called
            raise AssertionError("web voice must not lease a DID")

        job = {
            "mode": "voice_webrtc",
            "voice": {
                "agent_definition": {"transport": {"kind": "webrtc"}},
                "params": {},
            },
            "sink": {"api_url": "http://localhost:8000"},
            "metadata": {"run_id": "run-x", "secret_env": []},
        }
        lines = [
            '{"phase": "completed", "job_id": "job-x", "submission_status": "submitted"}'
        ]
        out = self._run(
            monkeypatch,
            mode="voice_webrtc",
            job=job,
            status_lines=lines,
            acquire=_acquire,
        )
        assert out.phase == "completed"


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
class TestVoiceSecretResolution:
    def test_resolve_provider_credential_decrypts(self, organization, workspace):
        import asyncio

        from simulate.models.agent_definition import (
            AgentDefinition,
            ProviderCredentials,
        )
        from simulate.temporal.activities.hosted_runner import (
            _resolve_voice_secret_env,
        )

        agent = AgentDefinition.objects.create(
            agent_name="v",
            agent_type=AgentDefinition.AgentTypeChoices.VOICE,
            inbound=True,
            description="d",
            organization=organization,
            workspace=workspace,
        )
        creds = ProviderCredentials.objects.create(
            agent_definition=agent,
            provider_type="vapi",
            api_key="plain-vapi-key",
        )
        job = {
            "metadata": {
                "secret_env": [
                    {
                        "key": "VAPI_API_KEY",
                        "manager": "provider_credentials",
                        "credential_id": str(creds.id),
                        "field": "api_key",
                    }
                ]
            }
        }
        resolved = asyncio.run(_resolve_voice_secret_env(job))
        assert resolved["VAPI_API_KEY"] == "plain-vapi-key"
