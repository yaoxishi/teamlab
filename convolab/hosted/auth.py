"""Authoritative identity checks with isolated user-scoped HTTP requests."""
from dataclasses import dataclass, field
from uuid import UUID

import httpx
from pydantic import SecretStr

from .settings import HostedError, HostedSettings


def canonical_uuid(value):
    if not isinstance(value, str):
        raise HostedError("The requested private item is unavailable.")
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise HostedError("The requested private item is unavailable.") from None
    if str(parsed) != value or parsed.version is None:
        raise HostedError("The requested private item is unavailable.")
    return value


@dataclass(frozen=True)
class VerifiedIdentity:
    """Created by the server verifier, never from a browser's claimed user ID."""
    owner_id: str
    access_token: SecretStr = field(repr=False, compare=False)


class SupabaseBoundary:
    def __init__(self, settings: HostedSettings, *, transport=None):
        self.settings = settings
        self._transport = transport

    def request(self, method, path, token, *, params=None, json=None):
        # Paths are constants owned by the adapters, not visitor URLs. Never
        # share a mutable client's Authorization header across visitors.
        if (method, path) not in {("GET", "/auth/v1/user"), ("POST", "/rest/v1/rpc/convolab_session_active"),
                                ("POST", "/rest/v1/rpc/convolab_touch"), ("GET", "/rest/v1/convolab_executions")}:
            raise HostedError("The requested website operation is unavailable.")
        if not isinstance(token, SecretStr) or not token.get_secret_value().strip():
            raise HostedError("Your private workspace needs a valid browser session.")
        headers = {"apikey": self.settings.publishable_key,
                   "Authorization": "Bearer " + token.get_secret_value(), "Accept": "application/json"}
        try:
            with httpx.Client(transport=self._transport, timeout=15, trust_env=False,
                              follow_redirects=False) as client:
                response = client.request(method, self.settings.url + path, headers=headers,
                                          params=params, json=json)
        except httpx.HTTPError:
            raise HostedError("The private workspace service is unavailable. Try again; no study was started.") from None
        # Do not echo response bodies, URLs, exception details or auth headers.
        if response.status_code in {401, 403}:
            raise HostedError("Your browser session is no longer authorized. Reconnect your private workspace.")
        if response.status_code != 200:
            raise HostedError("The private workspace service is unavailable. Try again; no study was started.")
        try:
            return response.json()
        except (ValueError, UnicodeError):
            raise HostedError("The private workspace service returned an invalid response.") from None


class AuthVerifier(SupabaseBoundary):
    def verify(self, browser_message):
        if not isinstance(browser_message, dict) or browser_message.get("status") != "ready":
            raise HostedError("Connect your private workspace before viewing or editing private data.")
        token = browser_message.get("access_token")
        if not isinstance(token, str) or not token.strip() or len(token) > 16384 or any(c.isspace() for c in token):
            raise HostedError("Your private workspace needs a valid browser session.")
        secret = SecretStr(token)
        user = self.request("GET", "/auth/v1/user", secret)
        if not isinstance(user, dict) or user.get("role") != "authenticated" or user.get("is_anonymous") is not True:
            raise HostedError("This website requires its own anonymous private workspace.")
        owner = canonical_uuid(user.get("id"))
        # Auth getUser alone does not prove a signed-out JWT's session is live.
        # PostgREST verifies this same JWT; the restricted RPC checks auth.sessions.
        if self.request("POST", "/rest/v1/rpc/convolab_session_active", secret, json={}) is not True:
            raise HostedError("Your browser session is no longer authorized. Reconnect your private workspace.")
        # Separate POST records server-time activity. RLS reads remain read-only.
        # It also rechecks live ownership after concurrent identity cleanup.
        if self.request("POST", "/rest/v1/rpc/convolab_touch", secret, json={}) is not True:
            raise HostedError("Your browser session is no longer authorized. Reconnect your private workspace.")
        return VerifiedIdentity(owner, secret)
