"""Trusted deployment settings, never visitor-supplied configuration."""
from dataclasses import dataclass, field
import base64
import json
import re
from typing import Mapping


class HostedError(ValueError):
    """Static, non-secret explanations safe to show to a visitor."""


@dataclass(frozen=True)
class HostedSettings:
    url: str
    publishable_key: str = field(repr=False)
    turnstile_site_key: str = field(default="", repr=False)
    signup_enabled: bool = False

    def __post_init__(self):
        # This prototype uses an owner-controlled Supabase project, not arbitrary
        # URLs, redirects, a localhost service or visitor-provided credentials.
        if not isinstance(self.url, str) or not re.fullmatch(r"https://[a-z0-9]{20}\.supabase\.co", self.url):
            raise HostedError("The website's authentication service is not configured correctly.")
        key = self.publishable_key
        allowed = isinstance(key, str) and bool(re.fullmatch(r"sb_publishable_[A-Za-z0-9_-]+", key))
        if isinstance(key, str) and key.count(".") == 2:
            try:
                body = key.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
                # This inspects a PUBLIC configuration key to reject an admin
                # key. It never authenticates a visitor or trusts JWT claims.
                allowed = isinstance(claims, dict) and claims.get("role") == "anon"
            except (ValueError, TypeError, UnicodeError):
                allowed = False
        if not allowed:
            raise HostedError("Configure a public Supabase key; administrative keys must never enter the browser.")
        if type(self.signup_enabled) is not bool:
            raise HostedError("The website's workspace signup setting is invalid.")
        if self.signup_enabled and (not isinstance(self.turnstile_site_key, str)
                or not re.fullmatch(r"0x[A-Za-z0-9_-]{14,62}", self.turnstile_site_key)):
            raise HostedError("Configure the website's real bot-check site key before enabling visitor workspaces.")

    @property
    def project(self):
        return self.url.split("//", 1)[1].split(".", 1)[0]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]):
        enabled = environment.get("CONVOLAB_HOSTED_SIGNUP_ENABLED", "0")
        if enabled not in ("0", "1"):
            raise HostedError("The website's workspace signup setting is invalid.")
        return cls(environment.get("CONVOLAB_SUPABASE_URL", ""),
                   environment.get("CONVOLAB_SUPABASE_PUBLISHABLE_KEY", ""),
                   environment.get("CONVOLAB_TURNSTILE_SITE_KEY", ""), enabled == "1")

    def browser_data(self):
        data = {"url": self.url, "publishable_key": self.publishable_key,
                "signup_enabled": self.signup_enabled, "component_version": 1}
        if self.signup_enabled:
            data["turnstile_site_key"] = self.turnstile_site_key
        return data
