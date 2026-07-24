"""Microsoft Graph email provider (Outlook/Hotmail), MSAL device-code auth.

Device-code flow: a public client app (no client secret - `AZURE_CLIENT_ID`
alone identifies the app registration). The user visits a Microsoft URL on
any device and enters a short code once; the resulting token (with a refresh
token) is cached to data/email_token.json, so this is a one-time interactive
step. Every later call reuses/silently refreshes the cached token - no
secret ever lives in this codebase or .env, consistent with device-code flow
being designed for public clients that can't keep a secret.

See docs/APIS.md for the Azure app registration steps and required scopes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import msal

from copilot.config import DATA_DIR
from copilot.email_agent.provider import EmailProvider

TOKEN_CACHE_PATH = DATA_DIR / "email_token.json"
# "consumers" (not "common"/"organizations") is the correct authority for a
# personal Microsoft account (Hotmail/Outlook.com/Live) - confirmed live.
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.Send", "Mail.Read", "Mail.ReadWrite", "MailboxSettings.ReadWrite"]
SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


def _load_cache(cache_path: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())
    return cache


def _save_cache(cache: msal.SerializableTokenCache, cache_path: Path) -> None:
    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize())


def _app(
    client_id: str, cache_path: Path
) -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    cache = _load_cache(cache_path)
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
    return app, cache


def login(
    client_id: str,
    cache_path: Path = TOKEN_CACHE_PATH,
    on_prompt: Callable[[str], None] = print,
) -> None:
    """One-time interactive device-code login. Calls on_prompt with the
    URL+code the user needs to visit, then blocks until they complete it (or
    it times out) and persists the resulting token cache."""
    app, cache = _app(client_id, cache_path)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Failed to start device flow: {flow.get('error_description', flow)}")
    on_prompt(flow["message"])
    result = app.acquire_token_by_device_flow(flow)  # blocks until done or timeout
    if "access_token" not in result:
        raise RuntimeError(f"Login failed: {result.get('error_description', result)}")
    _save_cache(cache, cache_path)


def _get_token(client_id: str, cache_path: Path) -> str:
    app, cache = _app(client_id, cache_path)
    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError("Not logged in. Run 'copilot email login' first.")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise RuntimeError("Cached login expired or invalid. Run 'copilot email login' again.")
    _save_cache(cache, cache_path)
    return result["access_token"]


class OutlookProvider(EmailProvider):
    def __init__(self, client_id: str, cache_path: Path = TOKEN_CACHE_PATH):
        self.client_id = client_id
        self.cache_path = cache_path

    def send_mail(self, *, to: str, subject: str, body_html: str) -> None:
        token = _get_token(self.client_id, self.cache_path)
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }
        resp = httpx.post(
            SEND_MAIL_URL,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
