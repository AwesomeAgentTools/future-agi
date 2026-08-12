"""Build the ``StartRunnerJob`` payload the hosted runner hands to the SDK.

The backend never imports the Agent Learning Kit — it emits the job as a plain
dict matching the SDK's JSON schema, and the child process
(``fi.simulate.hosted.child_entrypoint``) validates it. Keeping this a service
(not activity/view code) means it is unit-testable and reusable.

Slice 1 supports the chat mode. The target agent-under-test is resolved from the
run test's agent definition:

- an HTTP chat surface (``ProviderCredentials.server_url``) → an ``http`` target;
- otherwise a configured local ``callable`` target (``ALK_RUNNER_DEFAULT_CHAT_TARGET``)
  used for the local proof and tests.

Secrets are emitted as references only (env-var names); the runner activity
resolves them into the child's environment.
"""

from __future__ import annotations

import ast
import json
import os
import uuid
from collections import defaultdict
from typing import Any

import structlog
from django.conf import settings

from model_hub.models.develop_dataset import Cell, Row
from simulate.models import AgentDefinition, Scenarios, TestExecution

logger = structlog.get_logger(__name__)

_SPEC_SCHEMA_VERSION = "futureagi.simulation-spec.v1"
_JOB_SCHEMA_VERSION = "futureagi.runner-job.v1"

_MODE_TO_ENVIRONMENT = {
    "chat": ("chat", "conversation"),
}

_CHAT_MODE = "chat"
_VOICE_WEBRTC_MODE = "voice_webrtc"
_VOICE_SIP_MODE = "voice_sip"
_VOICE_MODES = {_VOICE_WEBRTC_MODE, _VOICE_SIP_MODE}

# Env-var names the runner exposes per provider; the child reads secrets from
# these and the runner activity resolves them from ``ProviderCredentials``.
_PROVIDER_ENV = {
    "vapi": {"api_key": "VAPI_API_KEY"},
    "retell": {"api_key": "RETELL_API_KEY"},
    "livekit": {"api_key": "LIVEKIT_API_KEY", "api_secret": "LIVEKIT_API_SECRET"},
}

# Provider profiles — the factory that replaces the hardcoded provider->transport
# and per-provider agent-definition branches. Adding a web provider is one entry
# here, with no dispatch edits. This is the Django-side twin of the SDK's
# ``endpoints.profiles`` registry; the two repos share only the transport-kind
# string vocabulary (webrtc / vapi_websocket / retell_webcall / sip_outbound /
# sip_inbound) — that is the cross-repo job contract. The backend never imports
# the SDK.
_PROVIDER_PROFILES: dict[str, dict[str, Any]] = {
    "vapi": {
        "web_transport_kind": "vapi_websocket",
        "target_id_field": "assistant_id",
        "api_key_env": _PROVIDER_ENV["vapi"]["api_key"],
        "emits_web_evidence": True,
        "sip_inbound_originator": "vapi",
    },
    "retell": {
        "web_transport_kind": "retell_webcall",
        "target_id_field": "agent_id",
        "api_key_env": _PROVIDER_ENV["retell"]["api_key"],
        "emits_web_evidence": True,
        "sip_inbound_originator": None,
    },
    "livekit": {
        "web_transport_kind": "webrtc",
        "target_id_field": None,
        "api_key_env": None,
        "emits_web_evidence": False,
        "sip_inbound_originator": None,
    },
}

_DEFAULT_PROVIDER = "livekit"


def _provider_profile(provider: str) -> dict[str, Any]:
    return _PROVIDER_PROFILES.get(provider, _PROVIDER_PROFILES[_DEFAULT_PROVIDER])


class HostedRunnerBuildError(Exception):
    """Raised when a runner job cannot be assembled from the run test."""


def resolve_runner_mode(agent_definition) -> str:
    """Runner mode for a run test's agent definition (view + builder share this).

    TEXT → ``chat``. VOICE resolves to ``voice_sip`` when the target is reached
    over the phone (a ``contact_number`` is set) and ``voice_webrtc`` otherwise
    (web bridge / native LiveKit). Only ``voice_sip`` leases a DID slot — mirrors
    the native ``_needs_phone`` gate.
    """
    if agent_definition is None:
        raise HostedRunnerBuildError("run test has no agent definition")
    if agent_definition.agent_type == AgentDefinition.AgentTypeChoices.TEXT:
        return _CHAT_MODE
    if _target_uses_phone(agent_definition):
        return _VOICE_SIP_MODE
    return _VOICE_WEBRTC_MODE


# Voice providers the released SDK cannot drive yet — runs targeting these fall
# back to the native (legacy) simulation runner instead of the hosted path.
_HOSTED_UNSUPPORTED_PROVIDERS = {"bland"}


def hosted_runner_supports(agent_definition) -> bool:
    """False when the target's provider isn't supported by the released SDK
    (e.g. Bland) so the caller can route the run to the native runner."""
    if agent_definition is None:
        return False
    provider = (
        getattr(
            getattr(agent_definition, "provider_credentials", None),
            "provider_type",
            None,
        )
        or getattr(agent_definition, "provider", None)
        or ""
    )
    return str(provider).strip().lower() not in _HOSTED_UNSUPPORTED_PROVIDERS


def build_start_runner_job(
    *,
    test_execution_id: str,
    run_test_id: str,
    scenario_ids: list[str],
    mode: str = "chat",
) -> dict[str, Any]:
    if mode != _CHAT_MODE and mode not in _VOICE_MODES:
        raise HostedRunnerBuildError(f"unsupported runner mode: {mode}")

    test_execution = TestExecution.objects.select_related(
        "run_test", "run_test__agent_definition", "agent_version"
    ).get(id=test_execution_id, deleted=False)
    run_test = test_execution.run_test
    agent_definition = run_test.agent_definition
    if agent_definition is None:
        raise HostedRunnerBuildError("run test has no agent definition")

    ordered_ids = [str(sid) for sid in (scenario_ids or test_execution.scenario_ids)]
    scenarios = _load_scenarios(ordered_ids)
    if not scenarios:
        raise HostedRunnerBuildError("no scenarios available for the runner job")

    run_id = str(test_execution.id)

    if mode in _VOICE_MODES:
        return _build_voice_job(
            mode=mode,
            run_id=run_id,
            run_test=run_test,
            agent_definition=agent_definition,
            agent_version=test_execution.agent_version,
            scenarios=scenarios,
        )

    adapter, world_kind = _MODE_TO_ENVIRONMENT[mode]

    spec = {
        "schema_version": _SPEC_SCHEMA_VERSION,
        "run_id": run_id,
        "environment": {
            "adapter": adapter,
            "adapter_version": "1",
            "world_kind": world_kind,
            "config": {"max_turns": 6, "min_turns": 2, "modality": "text"},
            "secret_refs": {},
        },
        "target": _build_target(agent_definition),
        "simulator": {
            "adapter": "synthetic_user",
            "adapter_version": "1",
            "config": {},
            "secret_refs": {},
        },
        "scenario": {
            "name": run_test.name or "hosted-run",
            "dataset": _personas_for_scenarios(scenarios),
        },
        "evidence": {"sources": [], "required_capabilities": []},
        "metadata": {
            "run_test_id": str(run_test.id),
            "test_execution_id": run_id,
            "organization_id": str(run_test.organization_id),
        },
    }

    job = {
        "schema_version": _JOB_SCHEMA_VERSION,
        "job_id": uuid.uuid4().hex,
        "mode": mode,
        "spec": spec,
        "sink": {
            "api_url": _sink_api_url(),
            "run_test_id": str(run_test.id),
            "test_execution_id": run_id,
            "secret_refs": {
                "internal_api_secret": _env_secret_ref(
                    "INTERNAL_API_SECRET", "internal_api_secret"
                ),
            },
        },
        "metadata": {
            "organization_id": str(run_test.organization_id),
            "run_id": run_id,
        },
    }
    return job


def _load_scenarios(scenario_ids: list[str]) -> list[Scenarios]:
    by_id = {
        str(scenario.id): scenario
        for scenario in Scenarios.objects.filter(id__in=scenario_ids, deleted=False)
    }
    return [by_id[sid] for sid in scenario_ids if sid in by_id]


def _persona_for_scenario(scenario: Scenarios) -> dict[str, Any]:
    situation = (scenario.description or scenario.source or scenario.name or "").strip()
    return {
        "persona": {"name": scenario.name or "customer"},
        "situation": situation or f"Interact about: {scenario.name}",
        "outcome": "Get the request resolved.",
    }


def _personas_for_scenarios(scenarios: list[Scenarios]) -> list[dict[str, Any]]:
    """Expand dataset scenarios into one SDK test case per platform row.

    The ALK sink allocates CallExecutions in scenario order and then dataset-row
    order. The hosted job must use that same order: otherwise a ten-row scenario
    allocates ten calls but the SDK submits only one report, leaving nine calls
    permanently pending.
    """
    dataset_ids = [scenario.dataset_id for scenario in scenarios if scenario.dataset_id]
    rows_by_dataset: dict[Any, list[Row]] = defaultdict(list)
    rows = list(Row.objects.filter(dataset_id__in=dataset_ids).order_by("order"))
    for row in rows:
        rows_by_dataset[row.dataset_id].append(row)

    values_by_row: dict[Any, dict[str, str | None]] = defaultdict(dict)
    for cell in Cell.objects.filter(row_id__in=[row.id for row in rows]).select_related(
        "column"
    ):
        values_by_row[cell.row_id][cell.column.name] = cell.value

    personas: list[dict[str, Any]] = []
    for scenario in scenarios:
        if not scenario.dataset_id:
            personas.append(_persona_for_scenario(scenario))
            continue

        scenario_rows = rows_by_dataset.get(scenario.dataset_id, [])
        if not scenario_rows:
            raise HostedRunnerBuildError(
                f"scenario {scenario.id} has a dataset with no rows"
            )
        personas.extend(
            _persona_for_dataset_row(scenario, values_by_row[row.id])
            for row in scenario_rows
        )
    return personas


def _persona_for_dataset_row(
    scenario: Scenarios, row_values: dict[str, str | None]
) -> dict[str, Any]:
    values = dict(row_values)
    raw_persona = values.pop("persona", None)
    persona = _parse_persona(raw_persona)
    persona.setdefault("name", scenario.name or "customer")

    situation = str(values.pop("situation", "") or "").strip()
    outcome = str(values.pop("outcome", "") or "").strip()
    for key, value in values.items():
        if value not in (None, ""):
            persona.setdefault(key, value)

    return {
        "persona": persona,
        "situation": situation or f"Interact about: {scenario.name}",
        "outcome": outcome or "Get the request resolved.",
    }


def _parse_persona(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(value)
        except (SyntaxError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"description": value}


def _build_target(agent_definition) -> dict[str, Any]:
    server_url = _provider_server_url(agent_definition)
    if server_url:
        return {
            "adapter": "http",
            "adapter_version": "1",
            "config": {"url": server_url},
            "secret_refs": {
                "api_key": _env_secret_ref("ALK_RUNNER_TARGET_API_KEY", "api_key"),
            },
            "required_capabilities": ["text"],
        }

    default_target = getattr(
        settings, "ALK_RUNNER_DEFAULT_CHAT_TARGET", None
    ) or os.getenv("ALK_RUNNER_DEFAULT_CHAT_TARGET")
    if not default_target:
        raise HostedRunnerBuildError(
            "no chat target: agent definition has no server_url and "
            "ALK_RUNNER_DEFAULT_CHAT_TARGET is unset"
        )
    return {
        "adapter": "callable",
        "adapter_version": "1",
        "config": {"target": default_target},
        "secret_refs": {},
        "required_capabilities": ["text"],
    }


def _provider_server_url(agent_definition) -> str | None:
    credentials = getattr(agent_definition, "provider_credentials", None)
    server_url = getattr(credentials, "server_url", None) if credentials else None
    return server_url or None


def _sink_api_url() -> str | None:
    return (
        getattr(settings, "ALK_RUNNER_API_URL", None)
        or os.getenv("ALK_RUNNER_API_URL")
        or os.getenv("FI_BASE_URL")
    )


def _env_secret_ref(env_var: str, purpose: str) -> dict[str, str]:
    return {"manager": "env", "key": env_var, "purpose": purpose}


# ---------------------------------------------------------------------------
# Voice runner jobs (#149) — webrtc / vapi_websocket / retell_webcall / sip
#
# The backend never imports the SDK: a voice job carries the same JSON-shaped
# ``VoiceRunConfig`` the child hydrates into ``AgentDefinition`` /
# ``LiveKitSimulatorRuntime`` / ``Scenario`` / ``SimulatorAgentDefinition``. The
# transport is derived from the platform's provider + phone the same way the
# native ``prepare_call`` does. Only ``voice_sip`` reaches the DID pool.
# ---------------------------------------------------------------------------

_ENV_LIVEKIT_URL = "LIVEKIT_URL"
_ENV_LIVEKIT_API_KEY = "LIVEKIT_API_KEY"
_ENV_LIVEKIT_API_SECRET = "LIVEKIT_API_SECRET"


def _build_voice_job(
    *,
    mode: str,
    run_id: str,
    run_test,
    agent_definition,
    agent_version,
    scenarios: list[Scenarios],
) -> dict[str, Any]:
    credentials = _voice_credentials(agent_definition, agent_version)
    provider = _voice_provider(agent_definition, credentials)
    transport_kind = _voice_transport_kind(agent_definition, provider)
    dataset = _personas_for_scenarios(scenarios)

    if (transport_kind in {"sip_inbound", "sip_outbound"}) != (mode == _VOICE_SIP_MODE):
        raise HostedRunnerBuildError(
            f"mode {mode} does not match transport {transport_kind}"
        )

    secret_env: list[dict[str, Any]] = []
    agent_def, target_secret = _voice_agent_definition(
        agent_definition, provider, transport_kind, credentials
    )
    secret_env.extend(target_secret)

    livekit_runtime, runtime_secret = _voice_livekit_runtime(
        run_id, provider, transport_kind, credentials
    )
    secret_env.extend(runtime_secret)

    job = {
        "schema_version": _JOB_SCHEMA_VERSION,
        "job_id": uuid.uuid4().hex,
        "mode": mode,
        "voice": {
            "agent_definition": agent_def,
            "scenario": {
                "name": run_test.name or "hosted-voice-run",
                "dataset": dataset,
            },
            "livekit_runtime": livekit_runtime,
            "simulator": _voice_simulator_config(dataset),
            "params": _voice_params(
                transport_kind,
                case_count=len(dataset),
                max_concurrency=_livekit_max_concurrency(credentials),
            ),
        },
        "sink": {
            "api_url": _sink_api_url(),
            "run_test_id": str(run_test.id),
            "test_execution_id": run_id,
            "secret_refs": {
                "internal_api_secret": _env_secret_ref(
                    "INTERNAL_API_SECRET", "internal_api_secret"
                ),
            },
        },
        "metadata": {
            "organization_id": str(run_test.organization_id),
            "run_id": run_id,
            "provider": provider,
            "transport": transport_kind,
            "secret_env": secret_env,
        },
    }
    return job


def _target_uses_phone(agent_definition) -> bool:
    return bool((getattr(agent_definition, "contact_number", "") or "").strip())


def _voice_credentials(agent_definition, agent_version):
    """Provider credentials for the target, preferring the versioned row (the
    native path reads ``agent_version.credentials``) then the legacy 1:1 on the
    agent definition. Returns None when neither exists."""
    if agent_version is not None:
        credentials = getattr(agent_version, "credentials", None)
        if credentials is not None:
            return credentials
    return getattr(agent_definition, "credentials_legacy", None)


def _voice_provider(agent_definition, credentials) -> str:
    provider = (
        getattr(credentials, "provider_type", None)
        or getattr(agent_definition, "provider", None)
        or "livekit"
    )
    return str(provider).strip().lower()


def _voice_transport_kind(agent_definition, provider: str) -> str:
    if _target_uses_phone(agent_definition):
        # From the target agent's perspective: an inbound agent receives the
        # call, so the simulator dials out to it (sip_outbound); an outbound
        # agent places the call, so it dials the simulator's leased DID
        # (sip_inbound). Mirrors the native is_outbound gate.
        return "sip_outbound" if agent_definition.inbound else "sip_inbound"
    return _provider_profile(provider)["web_transport_kind"]


def _provider_credential_ref(env_var: str, credentials, field: str) -> dict[str, Any]:
    return {
        "key": env_var,
        "manager": "provider_credentials",
        "credential_id": str(credentials.id),
        "field": field,
    }


def _env_passthrough_ref(env_var: str) -> dict[str, Any]:
    return {"key": env_var, "manager": "env", "source": env_var}


def _voice_agent_definition(
    agent_definition, provider: str, transport_kind: str, credentials
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    name = agent_definition.agent_name or f"{provider}-target"
    system_prompt = (
        agent_definition.description or ""
    ).strip() or "You are a helpful voice agent."
    agent_def: dict[str, Any] = {"name": name, "system_prompt": system_prompt}
    secret_env: list[dict[str, Any]] = []

    profile = _provider_profile(provider)
    if transport_kind in {"webrtc", "vapi_websocket", "retell_webcall"}:
        if profile["target_id_field"] is None:
            # webrtc: reached by managed dispatch on the agent name, no target
            agent_def["agent_name"] = (
                getattr(credentials, "agent_name", "")
                or agent_definition.assistant_id
                or name
            )
            agent_def["transport"] = {"kind": transport_kind}
        else:
            env_var = profile["api_key_env"]
            agent_def["target"] = {
                "provider": provider,
                profile["target_id_field"]: _require(
                    getattr(credentials, "assistant_id", None)
                    or agent_definition.assistant_id,
                    f"{provider} {profile['target_id_field']}",
                ),
                "api_key_env": env_var,
            }
            agent_def["transport"] = {"kind": transport_kind}
            if profile["emits_web_evidence"]:
                agent_def["provider_evidence"] = {
                    "provider": provider,
                    "call_id_source": "originator_response",
                }
            secret_env.append(_credential_or_env(env_var, credentials, "api_key"))
    elif transport_kind == "sip_outbound":
        agent_def["transport"] = {
            "kind": "sip_outbound",
            "sip_trunk_id": _require(
                _voice_setting("LIVEKIT_OUTBOUND_TRUNK_ID"), "LIVEKIT_OUTBOUND_TRUNK_ID"
            ),
            "sip_number": _require(
                _voice_setting("PSTN_CALLER_NUMBER"), "PSTN_CALLER_NUMBER"
            ),
            "sip_call_to": _require(
                agent_definition.contact_number, "agent contact_number"
            ),
            "participant_identity": "sip-caller-{invocation_id}-{test_case_id}",
            "answer_timeout_seconds": 60,
        }
    elif transport_kind == "sip_inbound":
        # The DID + dispatch rule are leased by the runner activity (voice_sip
        # only) and injected before the child runs — not known at build time.
        transport: dict[str, Any] = {
            "kind": "sip_inbound",
            "readiness_timeout_seconds": 120,
        }
        originator = profile["sip_inbound_originator"]
        if originator is not None:
            transport["inbound_call_originator"] = originator
            env_var = _PROVIDER_ENV[originator]["api_key"]
            secret_env.append(_credential_or_env(env_var, credentials, "api_key"))
            # ALK requires provider evidence for an API-originated Vapi call so
            # the originator response's call id is retained and reconciled.
            if originator == "vapi":
                agent_def["provider_evidence"] = {
                    "provider": "vapi",
                    "call_id_source": "originator_response",
                }
        agent_def["transport"] = transport
    else:  # pragma: no cover - guarded upstream
        raise HostedRunnerBuildError(f"unknown transport: {transport_kind}")

    return agent_def, secret_env


def _voice_livekit_runtime(
    run_id: str, provider: str, transport_kind: str, credentials
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The simulator's LiveKit room. For a native LiveKit target (webrtc) the
    room lives on the customer's project (their agent is registered there), so
    the runtime uses the customer credentials. Every other transport runs the
    simulator on the platform (system) LiveKit."""
    room_name = f"hosted-{run_id}-{{test_case_id}}"
    if transport_kind == "webrtc" and credentials is not None:
        url = getattr(credentials, "server_url", "") or _voice_setting("LIVEKIT_URL")
        runtime = {
            "url": _require(url, "livekit server_url"),
            "room_name": room_name,
            "api_key_env": _ENV_LIVEKIT_API_KEY,
            "api_secret_env": _ENV_LIVEKIT_API_SECRET,
        }
        secret_env = [
            _credential_or_env(_ENV_LIVEKIT_API_KEY, credentials, "api_key"),
            _credential_or_env(_ENV_LIVEKIT_API_SECRET, credentials, "api_secret"),
        ]
        return runtime, secret_env

    runtime = {
        "url": _require(_voice_setting("LIVEKIT_URL"), "system LIVEKIT_URL"),
        "room_name": room_name,
        "api_key_env": _ENV_LIVEKIT_API_KEY,
        "api_secret_env": _ENV_LIVEKIT_API_SECRET,
    }
    secret_env = [
        _env_passthrough_ref(_ENV_LIVEKIT_API_KEY),
        _env_passthrough_ref(_ENV_LIVEKIT_API_SECRET),
    ]
    return runtime, secret_env


def _voice_simulator_config(
    dataset: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stt = {
        "provider": _voice_setting("SIMULATOR_STT_PROVIDER") or "deepgram",
        "model": _voice_setting("SIMULATOR_STT_MODEL") or "nova-3",
    }
    language = _voice_setting("SIMULATOR_STT_LANGUAGE") or _dataset_language(dataset)
    if language:
        stt["language"] = language
    return {
        "llm": {
            # Match ALK's supported default and the credential present in the
            # hosted worker. The previous Google default selected Vertex from
            # a configured-but-unmounted credentials path, so the simulator
            # never spoke and otherwise-connected calls timed out.
            "provider": _voice_setting("SIMULATOR_LLM_PROVIDER") or "openai",
            "model": _voice_setting("SIMULATOR_LLM_MODEL") or "gpt-4.1",
        },
        "stt": stt,
        "tts": {
            "provider": _voice_setting("SIMULATOR_TTS_PROVIDER") or "deepgram",
            "model": _voice_setting("SIMULATOR_TTS_MODEL") or "aura-2-andromeda-en",
        },
    }


def _dataset_language(dataset: list[dict[str, Any]] | None) -> str | None:
    languages = {
        str(persona["language"]).strip().lower()
        for case in dataset or []
        if isinstance((persona := case.get("persona")), dict)
        and persona.get("language")
    }
    if not languages:
        return None
    if len(languages) > 1:
        return "multi"
    label_to_code = {
        label.lower(): code for code, label in AgentDefinition.LanguageChoices.choices
    }
    return label_to_code.get(languages.pop())


def _livekit_max_concurrency(credentials) -> int:
    """Per-agent LiveKit session ceiling that bounds concurrent cases within a
    run. Non-LiveKit targets (or missing creds) fall back to serial execution."""
    if credentials is None or getattr(credentials, "provider_type", None) != "livekit":
        return 1
    try:
        return max(1, int(getattr(credentials, "max_concurrency", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _voice_params(
    transport_kind: str, *, case_count: int = 1, max_concurrency: int = 1
) -> dict[str, Any]:
    is_telephony = transport_kind in {"sip_inbound", "sip_outbound"}
    conversation_direction = (
        _voice_setting("SIMULATOR_CONVERSATION_DIRECTION") or "simulator_first"
    )
    if conversation_direction not in {"agent_first", "simulator_first"}:
        raise HostedRunnerBuildError(
            "SIMULATOR_CONVERSATION_DIRECTION must be agent_first or simulator_first"
        )
    # Telephone leases a single DID, so the engine keeps those cases serial
    # regardless of the requested ceiling; mirror that here for the deadline.
    effective_concurrency = (
        1 if is_telephony else max(1, min(int(max_concurrency or 1), case_count))
    )
    params = {
        "record_audio": True,
        "recording_root": "recordings",
        "max_seconds": 150.0 if is_telephony else 120.0,
        "min_turn_messages": 6,
        "conversation_direction": conversation_direction,
        "connect_timeout": 60.0,
        "readiness_timeout": 120.0,
        "cleanup_timeout": 30.0,
        "max_concurrency": effective_concurrency,
    }

    # ALK derives its outer SimulationSpec deadline from one case's voice
    # budgets. The engine now runs cases in parallel up to
    # ``effective_concurrency``, so the run's wall-clock is ``ceil(N/C)`` case
    # budgets, not ``N``. Contribute that many extra budgets through
    # cleanup_timeout (the component that normally completes immediately) so a
    # multi-case run is not cancelled before its batches finish.
    if case_count > 1:
        per_case_budget = (
            params["max_seconds"]
            + params["connect_timeout"]
            + params["readiness_timeout"]
            + params["cleanup_timeout"]
        )
        batches = -(-case_count // effective_concurrency)  # ceil division
        params["cleanup_timeout"] += (batches - 1) * per_case_budget
    return params


def _credential_or_env(env_var: str, credentials, field: str) -> dict[str, Any]:
    if credentials is not None and getattr(credentials, "id", None) is not None:
        return _provider_credential_ref(env_var, credentials, field)
    return _env_passthrough_ref(env_var)


def _voice_setting(name: str) -> str | None:
    return getattr(settings, name, None) or os.getenv(name)


def _require(value, label: str):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise HostedRunnerBuildError(f"voice runner job missing {label}")
    return value
