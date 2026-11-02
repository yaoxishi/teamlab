"""One model boundary, one request budget, and independent per-seat clients.

Production calls use the pinned LiteLLM path with private HTTP clients. The
transport permits one HTTP generation send per application attempt, disables
redirects/retries, and retains native finish/refusal information that LiteLLM
otherwise loses during translation. No provider keys/parameters are global.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import importlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

import httpx
from pydantic import SecretStr

from .config import AgentConfig, ConfigError, Study

CALL_TYPES = ("main", "survey", "state_init", "state_update")
EXECUTION_ERRORS = frozenset({"authentication", "permission", "invalid_configuration", "unknown_model", "quota_exhausted", "unsupported_integration"})
RETRYABLE = frozenset({"rate_limited", "timeout", "connection", "service_unavailable", "empty_response", "validation", "truncation"})
GUIDANCE = {
    "authentication": ("The provider rejected authentication.", "Check this seat's key or environment-variable reference."),
    "permission": ("The provider denied access.", "Check permissions for the selected model and account."),
    "invalid_configuration": ("The provider rejected the request configuration.", "Check the selected model, endpoint, and non-secret settings."),
    "unknown_model": ("The requested model was not found.", "Check the model identifier and endpoint."),
    "quota_exhausted": ("The provider reported an exhausted quota or billing limit.", "Check the provider account's quota and billing settings."),
    "unsupported_integration": ("The adapter or returned response does not support this request contract.", "Use a tested model/adapter configuration."),
    "context_limit": ("The request exceeded the model's input context.", "Configure a shorter study or a suitable model for a new execution."),
    "rate_limited": ("The provider temporarily limited request traffic.", "Check provider rate limits before starting a new execution."),
    "timeout": ("The request timed out.", "Check connectivity and provider availability before a new execution."),
    "connection": ("The connection to the provider failed.", "Check connectivity and the configured endpoint."),
    "service_unavailable": ("The provider service was unavailable.", "Check provider availability before a new execution."),
    "empty_response": ("The response contained no usable answer text.", "Inspect the retained attempt records and model configuration."),
    "validation": ("The answer did not meet the required response format.", "Inspect the retained response and configured output limit."),
    "truncation": ("The answer reached its configured output limit.", "Inspect the retained response; consider a larger limit for a new execution."),
    "refusal": ("The provider returned a structured refusal.", "Inspect the retained attempt and study instructions before a new execution."),
    "content_filtered": ("The provider filtered the answer.", "Inspect the study instructions and provider policy before a new execution."),
    "protocol": ("The provider returned an unexpected tool or function call.", "Use a tested model configuration that returns an ordinary answer."),
    "unknown": ("The provider request failed; the exact cause could not be classified.", "Inspect the safe technical details and provider status."),
    "storage": ("Required output data could not be written.", "Check the output folder and available disk space; retain the incomplete folder."),
    "application": ("An unexpected application error stopped execution.", "Retain the output folder and report the affected stage for investigation."),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Redactor:
    """Sanitize every exported string, including dictionary keys and user text."""
    def __init__(self, secrets=()):
        values = {s.get_secret_value() if isinstance(s, SecretStr) else s for s in secrets if s is not None}
        self._secrets = tuple(sorted({value for value in values if value}, key=len, reverse=True))

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        # Credential-bearing URLs and authorization values must not be diagnostics.
        value = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", value, flags=re.I)
        value = re.sub(r"(?i)((?:authorization|api[-_]?key|access[-_]?token|x-api-key)\s*[:=]\s*)(?:Bearer\s+)?[^\s,;\"']+", r"\1[REDACTED]", value)
        return value

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, SecretStr):
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


class CancelledWork(Exception):
    """No request was sent. Do not fabricate a result or attempt audit row."""


class AbortSignal:
    """Only lifecycle signaling and the first safe cause; no participant memory."""
    def __init__(self):
        self.event = threading.Event()
        # CLI SIGINT can arrive while the main thread inspects a cause. A
        # reentrant lock plus atomic setdefault preserves the first cause.
        self._lock = threading.RLock()
        self._cause = {}

    @property
    def cause(self):
        with self._lock:
            value = self._cause.get("details")
            return dict(value) if value else None

    def signal(self, cause: Mapping[str, Any]):
        with self._lock:
            self._cause.setdefault("details", dict(cause))
            self.event.set()

    def check(self):
        if self.event.is_set():
            raise CancelledWork("Execution stopped before another request could be sent.")


@dataclass(frozen=True)
class GenerationParameters:
    seat: int
    call_type: str
    model: str
    api_base: str | None
    requested_temperature: float
    effective_temperature: float | None
    requested_max_tokens: int
    provider_limit_parameter: str
    provider_limit_value: int
    warnings: tuple[str, ...] = ()
    send_n: bool = True

    def to_dict(self):
        return asdict(self)


_import_lock = threading.Lock()
_auxiliary_client_disabled = False


class _NoAuxiliaryNetwork(httpx.BaseTransport):
    """No untracked metadata/cost lookup may use LiteLLM's shared HTTP client."""
    def handle_request(self, request):
        raise httpx.ConnectError("TeamLab disables auxiliary network lookups.", request=request)


def load_litellm():
    # Documented startup controls: packaged offline model map; no .env loading.
    # These are non-credential import flags, never per-agent parameter globals.
    global _auxiliary_client_disabled
    with _import_lock:
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        os.environ["LITELLM_MODE"] = "PRODUCTION"
        os.environ["LITELLM_LOG"] = "CRITICAL"
        lib = importlib.import_module("litellm")
        if not _auxiliary_client_disabled:
            from litellm.llms.custom_httpx.http_handler import HTTPHandler
            # Startup transport control only. This client carries no seat keys
            # or generation parameters. Actual requests always receive a private
            # per-attempt client below. In 1.82.6 Ollama metadata lookups ignore
            # that supplied client and otherwise contact /api/show for bookkeeping.
            lib.module_level_client.close()
            lib.module_level_client = HTTPHandler(client=httpx.Client(
                transport=_NoAuxiliaryNetwork(), trust_env=False))
            _auxiliary_client_disabled = True
        # Existing external global routes/hooks could bypass our frozen contract.
        for name in ("model_fallbacks", "model_alias_map", "callbacks",
                     "service_callback", "retry_policy", "proxy_auth",
                     "api_base", "set_verbose", "global_disable_no_log_param"):
            if getattr(lib, name, None):
                raise ConfigError(f"LiteLLM {name} is globally configured. Start TeamLab in a clean process so requests remain independent.")
        for name in ("success_callback", "input_callback", "failure_callback"):
            if any(callback != "cache" for callback in getattr(lib, name)):
                raise ConfigError(f"LiteLLM {name} is globally configured. Start TeamLab in a clean process.")
        return lib


def capability(agent: AgentConfig, seat: int, call_type: str, max_tokens: int) -> GenerationParameters:
    route, _, model = agent.model.partition("/")
    if agent.model == "mock":
        return GenerationParameters(seat, call_type, agent.model, agent.api_base, agent.temperature,
                                    agent.temperature, max_tokens, "max_tokens", max_tokens)
    if route not in {"openai", "anthropic", "gemini", "ollama"} or not model:
        raise ConfigError(f"Seat {seat}, {call_type}: use mock or a supported openai/, anthropic/, gemini/, ollama/ model.")
    lib = load_litellm()
    adapter = lib.ProviderConfigManager.get_provider_chat_config(
        model=model, provider=lib.LlmProviders(route))
    if route == "ollama":
        adapter = lib.OllamaConfig()
    if adapter is None:
        raise ConfigError(f"Seat {seat}, {call_type}: no supported adapter mapping for the model.")
    supported = adapter.get_supported_openai_params(model=model) or []
    if "max_tokens" not in supported:
        raise ConfigError(f"Seat {seat}, {call_type}: configured output limit cannot be established for this model.")
    expected_parameter = {"openai": "max_tokens", "anthropic": "max_tokens",
                          "gemini": "max_output_tokens", "ollama": "num_predict"}[route]
    # Probe the installed mapper without requests; this catches model-specific
    # max_completion_tokens translations and unsupported temperature behavior.
    def map_params(values):
        return adapter.map_openai_params(non_default_params=dict(values), optional_params={},
                                         model=model, drop_params=True)
    mapped = map_params({"max_tokens": max_tokens})
    if route == "openai" and "max_completion_tokens" in mapped:
        expected_parameter = "max_completion_tokens"
    mapped_parameter = "max_output_tokens" if route == "gemini" else expected_parameter
    if mapped.get(mapped_parameter) != max_tokens:
        raise ConfigError(f"Seat {seat}, {call_type}: the adapter did not preserve the requested output limit.")
    info = next((lib.model_cost[key] for key in (agent.model, model) if key in lib.model_cost), {})
    known_limit = info.get("max_output_tokens")
    if known_limit is not None and max_tokens > known_limit:
        raise ConfigError(f"Seat {seat}, {call_type}: output limit exceeds the installed model capability ({known_limit}).")
    maximum_temperature = {"openai": 2, "anthropic": 1, "gemini": 2}.get(route)
    if maximum_temperature is not None and agent.temperature > maximum_temperature:
        raise ConfigError(f"Seat {seat}, {call_type}: temperature must be between 0 and {maximum_temperature} for this adapter.")
    temp_param = "temperature"
    temperature_supported = "temperature" in supported and temp_param in map_params({"temperature": 0.731})
    effective = agent.temperature if temperature_supported else None
    warnings = () if temperature_supported else (
        f"Seat {seat}: temperature is unsupported for {agent.model} and will be omitted.",)
    return GenerationParameters(seat, call_type, agent.model, agent.api_base, agent.temperature,
                                effective, max_tokens, expected_parameter, max_tokens,
                                warnings, "n" in supported)


def validate_capabilities(study: Study) -> tuple[GenerationParameters, ...]:
    enabled = ["main"]
    if any(item.pre or item.turn or item.post for item in study.items):
        enabled.append("survey")
    if study.run.private_state:
        enabled += ["state_init", "state_update"]
    return tuple(capability(agent, seat, call_type,
                            agent.max_tokens if call_type == "main" else
                            study.run.survey_max_tokens if call_type == "survey" else study.run.state_max_tokens)
                 for seat, agent in enumerate(study.agents, 1) for call_type in enabled)


@dataclass(frozen=True)
class RuntimeAgent:
    seat: int
    display_name: str
    config: AgentConfig
    credential: SecretStr | None = field(default=None, repr=False)
    parameters: tuple[GenerationParameters, ...] = ()
    client: ModelClient | None = field(default=None, repr=False, compare=False)
    mock_state_input: Any = field(default=None, repr=False, compare=False)
    mock_survey_input: Any = field(default=None, repr=False, compare=False)

    @property
    def model(self):
        return self.config.model


@dataclass(frozen=True)
class ValidationOutcome:
    cleaned_text: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CallResult:
    text: str
    cleaned_text: str | None
    usage: dict | None
    attempts: int
    error: str | None
    error_type: str | None
    failure_scope: str | None
    finish_reason: str | None
    truncated: bool
    call_id: str
    native_finish_reason: str | None = None


@dataclass(frozen=True)
class NormalizedResponse:
    text: str = ""
    usage: dict | None = None
    returned_model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    native_finish_reason: str | None = None
    error_type: str | None = None


def _dict(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return None


def normalize_response(response) -> NormalizedResponse:
    """Common normalized contract; normal answer fields only."""
    data = _dict(response)
    if data is None:
        return NormalizedResponse(error_type="unsupported_integration")
    common = {"usage": _dict(data.get("usage")), "returned_model": data.get("model"),
              "response_id": data.get("id")}
    choices = data.get("choices")
    if not choices:
        return NormalizedResponse(**common, error_type="empty_response")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return NormalizedResponse(**common, error_type="unsupported_integration")
    choice = choices[0]
    native = choice.get("native_finish_reason", choice.get("finish_reason"))
    finish = choice.get("finish_reason")
    message = _dict(choice.get("message")) or {}
    content = message.get("content")
    refused = bool(message.get("refusal"))
    tools = bool(message.get("tool_calls") or message.get("function_call"))
    error = None
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    elif isinstance(content, list):
        parts = []
        for block in content:
            block = _dict(block)
            if block is None or not isinstance(block.get("type"), str):
                error = "unsupported_integration"
                continue
            if block["type"] in {"text", "output_text"}:
                if not isinstance(block.get("text"), str):
                    error = "unsupported_integration"
                else:
                    parts.append(block["text"])
            elif block["type"] == "refusal":
                refused = True
            elif block["type"] in {"tool_use", "function_call"}:
                tools = True
            elif block["type"] not in {"thinking", "redacted_thinking", "reasoning"}:
                error = "unsupported_integration"
        text = "".join(parts)
    else:
        text, error = "", "unsupported_integration"
    if refused or finish == "refusal":
        error = "refusal"
    elif finish == "content_filter":
        error = "content_filtered"
    elif tools or finish in {"tool_calls", "function_call"}:
        error = "protocol"
    elif error is None and finish not in {"stop", "length"}:
        error = "unsupported_integration"
    elif error is None and not text.strip():
        error = "empty_response"
    return NormalizedResponse(text, **common, finish_reason=finish,
                              native_finish_reason=native, error_type=error)


def native_response(route: str, body: dict) -> NormalizedResponse:
    """Documented provider fields, preserving native information before translation."""
    if route == "openai":
        return normalize_response(body)
    if route == "anthropic":
        reason = body.get("stop_reason")
        finish = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
                  "model_context_window_exceeded": "length", "tool_use": "tool_calls",
                  "refusal": "refusal"}.get(reason, reason)
        return normalize_response({"choices": [{"message": {"content": body.get("content")},
                                               "finish_reason": finish, "native_finish_reason": reason}],
                                   "model": body.get("model"), "id": body.get("id"), "usage": body.get("usage")})
    if route == "gemini":
        candidates = body.get("candidates", [])
        if not candidates:
            blocked = (body.get("promptFeedback") or {}).get("blockReason")
            return NormalizedResponse(error_type="content_filtered" if blocked else "empty_response",
                                      native_finish_reason=blocked)
        if len(candidates) != 1:
            return NormalizedResponse(error_type="unsupported_integration")
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts")
        content = []
        if parts is not None and not isinstance(parts, list):
            return NormalizedResponse(error_type="unsupported_integration")
        for part in parts or []:
            if not isinstance(part, dict):
                return NormalizedResponse(error_type="unsupported_integration")
            if "functionCall" in part:
                content.append({"type": "function_call"})
            elif part.get("thought"):
                content.append({"type": "thinking"})
            elif "text" in part:
                content.append({"type": "text", "text": part["text"]})
            else:
                return NormalizedResponse(error_type="unsupported_integration")
        reason = candidate.get("finishReason")
        finish = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter",
                  "RECITATION": "content_filter", "BLOCKLIST": "content_filter",
                  "PROHIBITED_CONTENT": "content_filter", "SPII": "content_filter",
                  "IMAGE_SAFETY": "content_filter"}.get(reason, reason)
        return normalize_response({"choices": [{"message": {"content": content},
                                               "finish_reason": finish, "native_finish_reason": reason}],
                                   "model": body.get("modelVersion"), "id": body.get("responseId"),
                                   "usage": body.get("usageMetadata")})
    if route == "ollama":
        reason = body.get("done_reason")
        finish = {"stop": "stop", "length": "length"}.get(reason, reason)
        if body.get("done") is not True:
            return NormalizedResponse(error_type="unsupported_integration", native_finish_reason=reason)
        return normalize_response({"choices": [{"message": {"content": body.get("response")},
                                               "finish_reason": finish, "native_finish_reason": reason}],
                                   "model": body.get("model"),
                                   "usage": {"prompt_tokens": body.get("prompt_eval_count"),
                                             "completion_tokens": body.get("eval_count")}})
    return NormalizedResponse(error_type="unsupported_integration")


class ProviderFailure(Exception):
    """Safe, structured failure, without raw exception bodies or credentials."""
    def __init__(self, category: str):
        self.category = category
        super().__init__(GUIDANCE[category][0])


def classify_exception(error: Exception, body: dict | None = None, status: int | None = None) -> str:
    if isinstance(error, ProviderFailure):
        return error.category
    # Native response error codes are retained independently of SDK translations.
    structured = body or _dict(getattr(error, "body", None)) or {}
    detail = structured.get("error", structured)
    detail = detail if isinstance(detail, dict) else {}
    code = next((value for value in (detail.get("code"), detail.get("type"), detail.get("status"),
                                    getattr(error, "code", None)) if isinstance(value, str)), None)
    codes = {
        "insufficient_quota": "quota_exhausted", "billing_hard_limit_reached": "quota_exhausted",
        "quota_exceeded": "quota_exhausted", "invalid_api_key": "authentication",
        "authentication_error": "authentication", "permission_error": "permission",
        "model_not_found": "unknown_model", "not_found_error": "unknown_model",
        "context_length_exceeded": "context_limit", "context_window_exceeded": "context_limit",
        "rate_limit_exceeded": "rate_limited", "rate_limit_error": "rate_limited",
        "invalid_request_error": "invalid_configuration",
    }
    if code in codes:
        return codes[code]
    status = status or getattr(error, "status_code", None)
    if status in {401, 403, 404, 400, 422, 429, 408, 500, 502, 503, 504}:
        return {401: "authentication", 403: "permission", 404: "unknown_model",
                400: "invalid_configuration", 422: "invalid_configuration", 429: "rate_limited",
                408: "timeout", 500: "service_unavailable", 502: "service_unavailable",
                503: "service_unavailable", 504: "service_unavailable"}[status]
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.TransportError):
        return "connection"
    # Exact documented SDK/LiteLLM exception classes, never text substring guesses.
    lib = load_litellm()
    for name, category in (
        ("ContextWindowExceededError", "context_limit"), ("AuthenticationError", "authentication"),
        ("PermissionDeniedError", "permission"), ("NotFoundError", "unknown_model"),
        ("RateLimitError", "rate_limited"), ("Timeout", "timeout"),
        ("APIConnectionError", "connection"), ("ServiceUnavailableError", "service_unavailable"),
        ("BadRequestError", "invalid_configuration"), ("UnsupportedParamsError", "invalid_configuration"),
    ):
        exception_class = getattr(lib, name, None)
        if exception_class and isinstance(error, exception_class):
            return category
    return "unknown"


class _OneSendTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport, abort: AbortSignal | None = None):
        self.transport = transport
        self.abort = abort
        self.cancelled = False
        self.sends = 0
        self.body = None
        self.status = None
        self.error = None
        self.attempted_extra_send = False

    def handle_request(self, request):
        if self.sends:
            self.attempted_extra_send = True
            raise ProviderFailure("unsupported_integration")
        if self.abort is not None:
            try:
                self.abort.check()
            except CancelledWork:
                self.cancelled = True
                raise
        self.sends += 1
        try:
            response = self.transport.handle_request(request)
            response.read()
            self.status = response.status_code
            try:
                self.body = response.json()
            except (ValueError, UnicodeError):
                self.body = None
            return response
        except httpx.TransportError as error:
            self.error = error
            raise

    def close(self):
        self.transport.close()


def provider_attempt(agent: RuntimeAgent, parameters: GenerationParameters, messages: list[dict],
                     *, transport: httpx.BaseTransport | None = None) -> NormalizedResponse:
    lib = load_litellm()
    route = agent.model.split("/", 1)[0]
    guard = _OneSendTransport(transport or httpx.HTTPTransport(retries=0),
                              agent.client.abort if agent.client else None)
    # Avoid provider/SDK debug logging of raw bodies or credential-bearing URLs.
    # This changes logger levels only, never client parameters or provider keys.
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.startswith(("litellm", "LiteLLM", "openai", "httpx", "httpcore")):
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    with httpx.Client(transport=guard, timeout=120, follow_redirects=False, trust_env=False) as http_client:
        if route == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=agent.credential.get_secret_value(),
                            base_url=agent.config.api_base or "https://api.openai.com/v1",
                            http_client=http_client, max_retries=0, timeout=120)
        else:
            from litellm.llms.custom_httpx.http_handler import HTTPHandler
            client = HTTPHandler(client=http_client, timeout=120)
        kwargs = dict(model=agent.model, messages=messages,
                      api_key=agent.credential.get_secret_value() if agent.credential else None,
                      api_base=agent.config.api_base, max_tokens=parameters.requested_max_tokens,
                      timeout=120, drop_params=True, num_retries=0, max_retries=0,
                      stream=False, cache={"no-cache": True, "no-store": True},
                      client=client, **{"no-log": True})
        if parameters.effective_temperature is not None:
            kwargs["temperature"] = parameters.effective_temperature
        if parameters.send_n:
            kwargs["n"] = 1
        try:
            response = lib.completion(**kwargs)
        except Exception as error:  # only the provider/client path, not validators/writers
            if guard.cancelled:
                # SDKs may translate transport cancellation into a connection
                # exception. No send occurred; do not invent an attempt row.
                raise CancelledWork("Execution stopped before the HTTP request was sent.") from None
            if guard.attempted_extra_send:
                raise ProviderFailure("unsupported_integration") from None
            if guard.error is not None:
                raise ProviderFailure(classify_exception(guard.error)) from None
            if guard.status is not None and guard.status < 300 and isinstance(guard.body, dict):
                # A native exceptional response may be unrecognized by the pinned
                # translator; preserve structured refusal/filter/protocol failures.
                normalized = native_response(route, guard.body)
                if normalized.error_type:
                    return normalized
                raise ProviderFailure("unsupported_integration") from None
            raise ProviderFailure(classify_exception(error, guard.body, guard.status)) from None
        if guard.attempted_extra_send:
            raise ProviderFailure("unsupported_integration")
        if isinstance(guard.body, dict):
            return native_response(route, guard.body)
        # Absence of a native body on the actual HTTP path is not a supported contract.
        if guard.sends:
            return NormalizedResponse(error_type="unsupported_integration")
        return normalize_response(response)


def error_details(category: str, agent: RuntimeAgent | None = None, *, meta=None,
                  call_type=None, call_id=None, attempts=None) -> dict:
    meta = meta or {}
    return {
        "error_type": category, "failure_scope": "execution" if category in EXECUTION_ERRORS or category in {"storage", "application"} else "call",
        "run_id": meta.get("run_id"), "seat": agent.seat if agent else meta.get("seat"),
        "agent": agent.display_name if agent else None, "model": agent.model if agent else None,
        "call_type": call_type, "phase": meta.get("phase"), "turn": meta.get("turn"),
        "call_id": call_id, "attempts": attempts,
        "summary": GUIDANCE[category][0], "suggested_action": GUIDANCE[category][1],
    }


def format_error(details: Mapping, *, folder=None) -> str:
    if details["failure_scope"] == "execution":
        scope = "Execution stopped; no later runs will start."
    elif details.get("call_type") == "survey":
        scope = "Questionnaire measurement warning; the conversation can continue."
    else:
        scope = "This Run failed; later requested Runs can continue."
    location = []
    for key, label in (("run_id", "Run"), ("seat", "seat"), ("agent", "participant"), ("model", "requested model")):
        if details.get(key) is not None:
            location.append(f"{label} {details[key]}")
    stage = {"state_init": "initializing private state", "state_update": "updating private state",
             "main": "speaking", "survey": f"{details.get('phase') or 'unknown-phase'} questionnaire"}.get(details.get("call_type"))
    if stage:
        location.append(f"stage: {stage}")
    if details.get("turn") is not None:
        location.append(f"opportunity {details['turn']}")
    if details.get("call_id"):
        location.append(f"call {details['call_id']}")
    location = ", ".join(location)
    attempts = f" Requests attempted: {details['attempts']}." if details.get("attempts") is not None else ""
    if details.get("attempts") is not None:
        attempts += f" Retries used: {max(details['attempts'] - 1, 0)}."
    retained = f" Available data is retained in {folder}." if folder else ""
    return f"{scope} {location + ': ' if location else ''}{details['summary']} {details['suggested_action']}{attempts}{retained}"


def model_attempt(agent, parameters, messages):
    if agent.model == "mock":
        if parameters.call_type.startswith("state"):
            from .state import MockStateInput, mock_state_text
            if not isinstance(agent.mock_state_input, MockStateInput):
                raise ValueError("A mock state request requires this administration's own frozen input.")
            text = mock_state_text(agent.mock_state_input, initializing=parameters.call_type == "state_init")
        elif parameters.call_type == "main":
            text = f"Mock message from {agent.display_name}."
        else:
            from .survey import MockSurveyInput, mock_survey_text
            if not isinstance(agent.mock_survey_input, MockSurveyInput):
                raise ValueError("A mock survey request requires this administration's frozen own items/answers.")
            text = mock_survey_text(agent.mock_survey_input)
        return normalize_response({"choices": [{"message": {"content": text},
                                              "finish_reason": "stop"}], "model": "mock", "usage": None})
    return provider_attempt(agent, parameters, messages)


class ModelClient:
    """Execution-scoped audit/abort services; no shared agent memory or RNG."""
    def __init__(self, audit: Callable[[dict], None], abort: AbortSignal, redactor: Redactor,
                 *, request: Callable | None = None, sleep: Callable = time.sleep):
        self.audit, self.abort, self.redactor = audit, abort, redactor
        self.request = request or model_attempt
        self.sleep = sleep

    def complete(self, agent: RuntimeAgent, system: str, user: str, call_type: str,
                 max_tokens: int, meta: Mapping, *, validate: Callable[[str], ValidationOutcome]) -> CallResult:
        if call_type not in CALL_TYPES:
            raise ValueError("Unknown call type.")
        # One immutable administration. Metadata is never added to either prompt.
        meta = dict(meta)
        if call_type.startswith("state") and "items" in meta:
            raise ValueError("State calls must not receive survey items.")
        call_id = str(uuid4())
        parameters = next((p for p in agent.parameters if p.call_type == call_type), None)
        if parameters is None or parameters.requested_max_tokens != max_tokens:
            raise ValueError("Use the prevalidated frozen generation parameters.")
        for attempt in range(1, 4):
            self.abort.check()
            if attempt > 1:
                self.sleep(2 if attempt == 2 else 4)
                self.abort.check()
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            normalized = NormalizedResponse()
            cleaned = None
            category = None
            validation_error = None
            application_error = None
            try:
                normalized = self.request(agent, parameters, messages)
            except ProviderFailure as error:
                category = error.category
            # Unexpected client/mock application errors propagate to coordinator.
            if category is None:
                category = normalized.error_type
            truncated = normalized.finish_reason == "length"
            if category is None:
                try:
                    outcome = validate(normalized.text)
                    if not isinstance(outcome, ValidationOutcome):
                        raise TypeError("Validator must return ValidationOutcome.")
                    cleaned, validation_error = outcome.cleaned_text, outcome.error
                    category = "truncation" if truncated and call_type != "main" else "validation" if validation_error else None
                except Exception as error:
                    # The provider attempt completed; retain its answer/usage,
                    # signal the application fault immediately, then propagate.
                    # Do not invent a provider failure category or retry it.
                    application_error = error
                    self.abort.signal(self.redactor.value(error_details(
                        "application", agent, meta=meta, call_type=call_type,
                        call_id=call_id, attempts=attempt)))
            scope = "execution" if category in EXECUTION_ERRORS else "call" if category else None
            details = error_details(category, agent, meta=meta, call_type=call_type, call_id=call_id, attempts=attempt) if category else None
            error_text = format_error(details) if details else None
            if validation_error and error_text:
                error_text += " " + self.redactor.text(validation_error)
            if scope == "execution":
                self.abort.signal(self.redactor.value(details))
            record = {key: meta.get(key) for key in (
                "run_id", "phase", "turn", "public_message_index", "public_event_index",
                "state_version", "input_state_version", "target_state_version")}
            record.update(call_id=call_id, seat=agent.seat, agent=agent.display_name,
                          call_type=call_type, model=agent.model, returned_model=normalized.returned_model,
                          provider_response_id=normalized.response_id, attempt=attempt, timestamp=utc_now(),
                          system=system, user=user, raw_response=normalized.text, cleaned_text=cleaned,
                          error=error_text, error_type=category, failure_scope=scope,
                          usage=normalized.usage, max_tokens=max_tokens, finish_reason=normalized.finish_reason,
                          native_finish_reason=normalized.native_finish_reason, truncated=truncated)
            self.audit(self.redactor.value(record))  # a write failure is never a model retry
            if application_error is not None:
                raise application_error
            result = CallResult(normalized.text, cleaned, normalized.usage, attempt,
                                self.redactor.text(error_text) if error_text else None, category, scope,
                                normalized.finish_reason, truncated, call_id, normalized.native_finish_reason)
            if category is None or category not in RETRYABLE or attempt == 3:
                return result
        raise AssertionError("Unreachable request budget.")


def complete(agent, system, user, call_type, max_tokens, meta, *, validate):
    """Public single boundary, with explicit execution-scoped services."""
    if agent.client is None:
        raise ValueError("The runtime agent needs its execution-scoped model boundary.")
    return agent.client.complete(agent, system, user, call_type, max_tokens, meta, validate=validate)
