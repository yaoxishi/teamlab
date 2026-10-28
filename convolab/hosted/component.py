"""Pinned local auth adapter; approved Turnstile loads only for new workspaces."""
from functools import lru_cache
import hashlib
from importlib.resources import files
import json

from .settings import HostedError


@lru_cache(maxsize=1)
def assets():
    root = files("convolab.hosted").joinpath("assets")
    manifest = json.loads(root.joinpath("manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or set(manifest.get("sha256", {})) != {"auth.js", "auth.css", "auth.html"}:
        raise HostedError("The website's authentication component failed its integrity check.")
    result = {}
    for name, expected in manifest["sha256"].items():
        data = root.joinpath(name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise HostedError("The website's authentication component failed its integrity check.")
        result[name] = data.decode("utf-8")
    return result


@lru_cache(maxsize=1)
def renderer():
    import streamlit as st
    data = assets()
    return st.components.v2.component("convolab_private_auth_v1", html=data["auth.html"],
                                      js=data["auth.js"], css=data["auth.css"])


def browser_auth(settings):
    result = renderer()(key="convolab_auth_v1", data=settings.browser_data(),
                        default={"auth": {"status": "connecting"}}, on_auth_change=lambda: None)
    return result.auth
