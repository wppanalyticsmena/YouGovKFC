"""
OAuth2 token provider for the YouGov BrandIndex API.

Built for unattended / scheduled runs: no browser, no human. Credentials are
supplied at runtime — either as environment variables (so a scheduler can inject
them as secrets) or as CLI arguments — and the script fetches its own bearer
token on each run.

Supported grant types
---------------------
  client_credentials   Pure machine-to-machine. Preferred for scheduling — the
                       script sends client_id + client_secret and gets a token.
                       Nothing interactive, ever.
  refresh_token        If YouGov only issues authorization_code credentials, do
                       the one-time browser login by hand to obtain a
                       refresh_token, then the scheduler refreshes access tokens
                       automatically from then on.

Token endpoint (default): https://login.yougov.com/oauth/token
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("yougov.auth")

DEFAULT_TOKEN_URL = "https://login.yougov.com/oauth/token"
DEFAULT_SCOPE = "brandindex"


@dataclass
class OAuth2Settings:
    client_id: str
    client_secret: str
    token_url: str = DEFAULT_TOKEN_URL
    scope: str | None = DEFAULT_SCOPE
    grant_type: str = "client_credentials"   # or "refresh_token"
    refresh_token: str | None = None
    client_auth: str = "body"                # how to send client creds: "body" or "basic"

    @classmethod
    def resolve(cls, args=None) -> "OAuth2Settings | None":
        """Build settings from CLI args first, then environment. Returns None if no
        client_id/client_secret are available (caller falls back to another mode)."""
        from dotenv import load_dotenv
        load_dotenv(interpolate=False)  # never expand $ in secrets

        def pick(arg_name: str, env_name: str, default: str = "") -> str:
            if args is not None:
                val = getattr(args, arg_name, None)
                if val:
                    return str(val).strip()
            return os.getenv(env_name, default).strip()

        client_id = pick("client_id", "YOUGOV_CLIENT_ID")
        client_secret = pick("client_secret", "YOUGOV_CLIENT_SECRET")
        if not client_id or not client_secret:
            return None

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            token_url=pick("token_url", "YOUGOV_TOKEN_URL", DEFAULT_TOKEN_URL),
            scope=pick("scope", "YOUGOV_SCOPE", DEFAULT_SCOPE) or None,
            grant_type=pick("grant_type", "YOUGOV_GRANT_TYPE", "client_credentials"),
            refresh_token=pick("refresh_token", "YOUGOV_REFRESH_TOKEN") or None,
            client_auth=pick("client_auth", "YOUGOV_CLIENT_AUTH", "body"),
        )


class OAuth2TokenProvider:
    """Fetches and caches a bearer token, refreshing it before it expires."""

    def __init__(self, settings: OAuth2Settings, timeout: int = 60):
        self.s = settings
        self.timeout = timeout
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        # Reuse the cached token until 60s before expiry.
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        return self._fetch()

    def _fetch(self) -> str:
        data = {"grant_type": self.s.grant_type}
        if self.s.scope:
            data["scope"] = self.s.scope
        if self.s.grant_type == "refresh_token":
            if not self.s.refresh_token:
                raise ValueError("grant_type=refresh_token requires a refresh_token "
                                 "(set YOUGOV_REFRESH_TOKEN or --refresh-token).")
            data["refresh_token"] = self.s.refresh_token

        auth = None
        if self.s.client_auth == "basic":
            auth = (self.s.client_id, self.s.client_secret)  # HTTP Basic
        else:
            data["client_id"] = self.s.client_id
            data["client_secret"] = self.s.client_secret

        resp = requests.post(self.s.token_url, data=data, auth=auth,
                             headers={"Accept": "application/json"}, timeout=self.timeout)
        if not resp.ok:
            raise RuntimeError(
                f"OAuth token request failed: HTTP {resp.status_code} from "
                f"{self.s.token_url}: {resp.text[:300]}")

        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError(f"No access_token in OAuth response: {body}")

        self._token = token
        self._expires_at = time.time() + float(body.get("expires_in", 3600))
        if body.get("refresh_token"):           # capture a rotated refresh token
            self.s.refresh_token = body["refresh_token"]
        log.info("Obtained access token (grant=%s, expires_in=%ss).",
                 self.s.grant_type, body.get("expires_in"))
        return token
