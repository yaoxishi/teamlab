"""Service-only durable execution admission; no model keys or study contents."""
from dataclasses import dataclass, field
import base64
import json
import re

import httpx
from pydantic import SecretStr

from .auth import VerifiedIdentity, canonical_uuid
from .settings import HostedError


@dataclass(frozen=True, repr=False)
class Admission:
    outcome: str
    execution_id: str | None


@dataclass(frozen=True, repr=False)
class Lease:
    execution_id: str
    owner_id: str
    instance_id: str
    fence: str

    def body(self):
        return {"p_execution": self.execution_id, "p_owner": self.owner_id,
                "p_instance": self.instance_id, "p_fence": self.fence}


@dataclass(frozen=True, repr=False)
class ServiceSettings:
    public: object
    secret: SecretStr = field(repr=False)

    @classmethod
    def from_environment(cls, public, environment):
        key = environment.get("CONVOLAB_SUPABASE_SECRET_KEY", "")
        valid = bool(re.fullmatch(r"sb_secret_[A-Za-z0-9_-]+", key))
        if key.count(".") == 2:
            try:
                body = key.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
                valid = isinstance(claims, dict) and claims.get("role") == "service_role"
            except (ValueError, TypeError, UnicodeError):
                valid = False
        if not valid:
            raise HostedError("Website execution is not configured yet. No study was started.")
        return cls(public, SecretStr(key))


class DurableAdmission:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    def rpc(self, name, body):
        if name not in {"admit", "renew", "dispatch", "finish", "reconcile"}:
            raise HostedError("The requested execution operation is unavailable.")
        return self._request_rpc(name, body)

    def _request_rpc(self, name, body):
        secret = self.settings.secret.get_secret_value()
        headers = {"apikey": secret, "Accept": "application/json"}
        # New Supabase secret keys authenticate with apikey, not a JWT Bearer.
        if secret.count(".") == 2:
            headers["Authorization"] = "Bearer " + secret
        try:
            with httpx.Client(transport=self.transport, timeout=15, trust_env=False,
                              follow_redirects=False) as client:
                response = client.post(self.settings.public.url + "/rest/v1/rpc/convolab_" + name,
                                       headers=headers, json=body)
            if response.status_code != 200:
                raise HostedError("Execution ownership could not be confirmed. No new model request is allowed.")
            return response.json()
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise HostedError("Execution ownership could not be confirmed. No new model request is allowed.") from None

    def admit(self, identity, nonce, lease, calls, *, available=True):
        if not isinstance(identity, VerifiedIdentity) or lease.owner_id != identity.owner_id:
            raise HostedError("The requested execution is unavailable.")
        for value in (identity.owner_id, nonce, *lease.body().values()):
            canonical_uuid(value)
        result = self.rpc("admit", lease.body() | {"p_nonce": nonce, "p_calls": calls, "p_available": available})
        if not isinstance(result, dict) or result.get("outcome") not in {"accepted", "duplicate", "existing", "busy", "limit"}:
            raise HostedError("The execution service returned an invalid response.")
        identifier = result.get("execution_id")
        if identifier is not None:
            canonical_uuid(identifier)
        if result["outcome"] == "accepted" and identifier != lease.execution_id:
            raise HostedError("The execution service returned an invalid response.")
        if result["outcome"] == "existing" and identifier is None:
            raise HostedError("The execution service returned an invalid response.")
        return Admission(result["outcome"], identifier)

    def renew(self, lease):
        return self.rpc("renew", lease.body()) is True

    def dispatch(self, lease):
        return self.rpc("dispatch", lease.body()) is True

    def finish(self, lease, status):
        return self.rpc("finish", lease.body() | {"p_status": status}) is True

    def reconcile(self):
        result = self.rpc("reconcile", {})
        if type(result) is not int or result < 0:
            raise HostedError("The execution service returned an invalid response.")
        return result
