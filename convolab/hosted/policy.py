"""Approved official endpoints and independent ephemeral model credentials."""
from dataclasses import dataclass, field
import re

import httpx
from pydantic import SecretStr

from convolab.config import ConfigError, Study, freeze_execution, study_to_dict, validate_study
from convolab.llm import ProviderFailure, Redactor, provider_attempt


OFFICIAL_BASES = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com",
                  "gemini": "https://generativelanguage.googleapis.com"}


def official_endpoint(model):
    route, _, name = model.partition("/")
    if route == "gemini":
        # The pinned Gemini adapter appends models/<model>:generateContent to
        # api_base; include the API version in the server-chosen base URL.
        return OFFICIAL_BASES[route] + "/v1beta"
    return OFFICIAL_BASES[route]


def validate_hosted_policy(study: Study, *, runtime=False):
    for seat, agent in enumerate(study.agents, 1):
        if agent.api_key_env is not None:
            raise ConfigError(f"seat {seat}: environment-variable key references are available only in the local app. Enter your key in the website's password field.")
        if agent.model == "mock":
            if agent.api_base is not None:
                raise ConfigError(f"seat {seat}: mock models do not use an API endpoint.")
            continue
        route, _, name = agent.model.partition("/")
        if route not in OFFICIAL_BASES:
            raise ConfigError(f"seat {seat}: the website supports mock, OpenAI, Anthropic and Gemini. Use the local app for other endpoints.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", name) or name.lower().startswith(("http:", "https:", "file:", "ftp:", "data:")):
            raise ConfigError(f"seat {seat}: enter a provider model identifier without a URL or path.")
        allowed = {official_endpoint(agent.model)} if runtime else {None, OFFICIAL_BASES[route]}
        if agent.api_base not in allowed:
            raise ConfigError(f"seat {seat}: custom endpoints are available only in the local app. The website uses the official provider endpoint.")


@dataclass(frozen=True, repr=False)
class FrozenHostedInputs:
    owner_id: str
    study: Study
    credentials: tuple[SecretStr | None, ...] = field(repr=False)
    redaction_secrets: tuple[SecretStr, ...] = field(repr=False)

    def __repr__(self):
        return "FrozenHostedInputs(credentials=<private>)"

    def execution_study(self):
        data = study_to_dict(self.study)
        for agent, key in zip(data["agents"], self.credentials, strict=True):
            agent["api_key"] = key
            if agent["model"] != "mock":
                agent["api_base"] = official_endpoint(agent["model"])
        return validate_study(data)

    def safe_value(self, value):
        return Redactor(self.redaction_secrets).value(value)


def freeze_hosted_inputs(identity, study, keys):
    from .auth import VerifiedIdentity
    if not isinstance(identity, VerifiedIdentity):
        raise ConfigError("Connect your private workspace before starting a study.")
    validate_hosted_policy(study)  # Must run before any credential resolution.
    if any(agent.api_key is not None for agent in study.agents):
        raise ConfigError("Enter model keys in the website's password fields, not in study settings.")
    credentials, secrets = [], []
    for seat, agent in enumerate(study.agents, 1):
        key = keys.get(seat)
        if key is not None and not isinstance(key, SecretStr):
            raise ConfigError(f"seat {seat}: enter a text API key in the password field.")
        if key and key.get_secret_value().strip():
            secrets.append(SecretStr(key.get_secret_value()))
        if agent.model == "mock":
            credentials.append(None)
        elif key and key.get_secret_value().strip():
            credentials.append(SecretStr(key.get_secret_value()))
        else:
            raise ConfigError(f"seat {seat}: enter your API key before starting. The website never uses server environment keys.")
    return FrozenHostedInputs(identity.owner_id, freeze_execution(study).study, tuple(credentials), tuple(secrets))


class OfficialTransport(httpx.BaseTransport):
    """Reject redirects, endpoint rewrites and unintended service requests."""
    def __init__(self, route, transport):
        self.route, self.transport = route, transport

    def handle_request(self, request):
        base = httpx.URL(OFFICIAL_BASES[self.route])
        url = request.url
        valid = (url.scheme == "https" and url.host == base.host and url.port in {None, 443}
                 and not url.userinfo and not url.fragment and request.method == "POST")
        if self.route == "openai":
            valid = valid and url.path == "/v1/chat/completions" and not url.query
        elif self.route == "anthropic":
            valid = valid and url.path == "/v1/messages" and not url.query
        else:
            valid = valid and bool(re.fullmatch(r"/v1(?:beta)?/models/[A-Za-z0-9._:-]+:generateContent", url.path))
            valid = valid and all(key == "key" for key in url.params.keys())
        if not valid:
            raise ProviderFailure("invalid_configuration")
        return self.transport.handle_request(request)

    def close(self):
        self.transport.close()


def hosted_provider_attempt(agent, parameters, messages, *, transport=None):
    # All actual HTTP attempts get a final outbound boundary, even if a future
    # provider adapter changes its routing. The engine's attempt budget is kept.
    validate_hosted_policy(validate_study_agent(agent.config), runtime=True)
    route = agent.model.split("/", 1)[0]
    if route not in OFFICIAL_BASES or agent.credential is None:
        raise ProviderFailure("invalid_configuration")
    return provider_attempt(agent, parameters, messages,
                            transport=OfficialTransport(route, transport or httpx.HTTPTransport(retries=0)))


def validate_study_agent(agent):
    # Policy validation needs only the immutable per-seat configuration; avoid
    # constructing a fake research Study or validating/allocating personas.
    class OneSeat:
        agents = (agent,)
    return OneSeat()
