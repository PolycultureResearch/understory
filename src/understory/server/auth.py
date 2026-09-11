"""Connector authentication.

The MCP SDK's bearer middleware calls a TokenVerifier for every request. The
static verifier is enough for a pilot: a few long random tokens in the
tenant's environment, one per person or connector. The label is the subject,
and the subject is HMAC-hashed before it reaches the log, so the token label
never appears in the warehouse.
"""

from __future__ import annotations

import hmac

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from understory.tenant import AuthConfig


class StaticTokenVerifier:
    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = {label: tok for label, tok in tokens.items() if tok}

    async def verify_token(self, token: str) -> AccessToken | None:
        for label, expected in self._tokens.items():
            if hmac.compare_digest(token.encode(), expected.encode()):
                return AccessToken(token=token, client_id=label, scopes=[], subject=label)
        return None


def make_verifier(config: AuthConfig) -> StaticTokenVerifier | None:
    if config.mode == "none":
        return None
    if config.mode == "static":
        if not config.tokens:
            raise ValueError("auth.mode is static but auth.tokens is empty")
        return StaticTokenVerifier(config.tokens)
    raise ValueError(f"unknown auth mode {config.mode!r}")


def current_subject() -> str | None:
    """The authenticated subject for this request, or None without auth."""
    token = get_access_token()
    if token is None:
        return None
    return token.subject or token.client_id
