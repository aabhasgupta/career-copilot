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
from datetime import datetime
from pathlib import Path

import httpx
import msal

from copilot.config import DATA_DIR
from copilot.email_agent.provider import EmailProvider, InboundMessage

TOKEN_CACHE_PATH = DATA_DIR / "email_token.json"
# "consumers" (not "common"/"organizations") is the correct authority for a
# personal Microsoft account (Hotmail/Outlook.com/Live) - confirmed live.
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.Send", "Mail.Read", "Mail.ReadWrite", "MailboxSettings.ReadWrite"]
GRAPH = "https://graph.microsoft.com/v1.0"
SEND_MAIL_URL = f"{GRAPH}/me/sendMail"
COPILOT_FOLDER = "Career Copilot"
# Child of COPILOT_FOLDER. The human override lane: drag any email here and
# the next sync processes it, bypassing the watermark and pre-filter (D28).
INTAKE_FOLDER = "Process"
# Outlook category preset colors; assignment order is arbitrary, just stable.
CATEGORY_COLOR = "preset9"

_MESSAGE_SELECT = "$select=id,conversationId,internetMessageId,from,subject,body,receivedDateTime"
# Same minus the body - for the filed-area scan, where almost every message
# is already known and downloading bodies hourly would be pure waste.
_HEADER_SELECT = "$select=id,conversationId,internetMessageId,from,subject,receivedDateTime"


def _parse_message(m: dict) -> InboundMessage:
    addr = (m.get("from") or {}).get("emailAddress") or {}
    return InboundMessage(
        message_id=m["id"],
        thread_id=m.get("conversationId") or m["id"],
        sender=(addr.get("address") or "").lower(),
        sender_name=addr.get("name") or "",
        subject=m.get("subject") or "",
        body_text=(m.get("body") or {}).get("content") or "",
        received_at=datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")),
        internet_message_id=m.get("internetMessageId") or "",
    )


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
        # Resolved lazily and memoized per process: the Career Copilot folder
        # id, the intake subfolder id, and the category names known to exist.
        self._folder_id: str | None = None
        self._intake_folder_id: str | None = None
        self._known_categories: set[str] | None = None

    def _headers(self, **extra: str) -> dict[str, str]:
        token = _get_token(self.client_id, self.cache_path)
        return {"Authorization": f"Bearer {token}", **extra}

    def send_mail(self, *, to: str, subject: str, body_html: str) -> None:
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }
        resp = httpx.post(SEND_MAIL_URL, headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()

    def list_messages_since(self, since: datetime) -> list[InboundMessage]:
        # Prefer header makes Graph return plain-text bodies, sparing us HTML
        # stripping. Paged via @odata.nextLink; inbox folder only (not Sent,
        # not the Career Copilot folder itself - already-filed mail is done).
        url = (
            f"{GRAPH}/me/mailFolders/inbox/messages"
            f"?$filter=receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&$orderby=receivedDateTime asc&{_MESSAGE_SELECT}&$top=50"
        )
        return self._fetch_messages(url)

    def list_intake_messages(self) -> list[InboundMessage]:
        # No receivedDateTime filter: the folder's contents are the request,
        # however old the mail is.
        intake_id = self._ensure_intake_folder()
        url = f"{GRAPH}/me/mailFolders/{intake_id}/messages?{_MESSAGE_SELECT}&$top=50"
        return self._fetch_messages(url)

    def list_filed_headers(self) -> list[InboundMessage]:
        folder_id = self._ensure_folder()
        url = f"{GRAPH}/me/mailFolders/{folder_id}/messages?{_HEADER_SELECT}&$top=50"
        return self._fetch_messages(url)

    def get_message(self, message_id: str) -> InboundMessage:
        headers = self._headers(Prefer='outlook.body-content-type="text"')
        resp = httpx.get(
            f"{GRAPH}/me/messages/{message_id}?{_MESSAGE_SELECT}", headers=headers, timeout=30
        )
        resp.raise_for_status()
        return _parse_message(resp.json())

    def _fetch_messages(self, url: str) -> list[InboundMessage]:
        headers = self._headers(Prefer='outlook.body-content-type="text"')
        messages: list[InboundMessage] = []
        while url:
            resp = httpx.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            messages.extend(_parse_message(m) for m in data.get("value", []))
            url = data.get("@odata.nextLink")
        return messages

    def _ensure_folder(self) -> str:
        if self._folder_id:
            return self._folder_id
        headers = self._headers()
        resp = httpx.get(
            f"{GRAPH}/me/mailFolders?$filter=displayName eq '{COPILOT_FOLDER}'",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        existing = resp.json().get("value", [])
        if existing:
            self._folder_id = existing[0]["id"]
        else:
            resp = httpx.post(
                f"{GRAPH}/me/mailFolders",
                headers=headers,
                json={"displayName": COPILOT_FOLDER},
                timeout=30,
            )
            resp.raise_for_status()
            self._folder_id = resp.json()["id"]
        return self._folder_id

    def _ensure_intake_folder(self) -> str:
        if self._intake_folder_id:
            return self._intake_folder_id
        parent_id = self._ensure_folder()
        headers = self._headers()
        resp = httpx.get(
            f"{GRAPH}/me/mailFolders/{parent_id}/childFolders"
            f"?$filter=displayName eq '{INTAKE_FOLDER}'",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        existing = resp.json().get("value", [])
        if existing:
            self._intake_folder_id = existing[0]["id"]
        else:
            resp = httpx.post(
                f"{GRAPH}/me/mailFolders/{parent_id}/childFolders",
                headers=headers,
                json={"displayName": INTAKE_FOLDER},
                timeout=30,
            )
            resp.raise_for_status()
            self._intake_folder_id = resp.json()["id"]
        return self._intake_folder_id

    def _ensure_category(self, category: str) -> None:
        headers = self._headers()
        if self._known_categories is None:
            resp = httpx.get(f"{GRAPH}/me/outlook/masterCategories", headers=headers, timeout=30)
            resp.raise_for_status()
            self._known_categories = {
                c["displayName"] for c in resp.json().get("value", [])
            }
        if category in self._known_categories:
            return
        resp = httpx.post(
            f"{GRAPH}/me/outlook/masterCategories",
            headers=headers,
            json={"displayName": category, "color": CATEGORY_COLOR},
            timeout=30,
        )
        # 409 = created concurrently / already exists under different casing.
        if resp.status_code != 409:
            resp.raise_for_status()
        self._known_categories.add(category)

    def file_message(self, message_id: str, *, category: str) -> None:
        """Tag with the category first, then move: the move gives the message
        a new id in the target folder, so category-then-move keeps the id we
        hold valid for both calls."""
        self._ensure_category(category)
        folder_id = self._ensure_folder()
        headers = self._headers()
        resp = httpx.patch(
            f"{GRAPH}/me/messages/{message_id}",
            headers=headers,
            json={"categories": [category]},
            timeout=30,
        )
        resp.raise_for_status()
        resp = httpx.post(
            f"{GRAPH}/me/messages/{message_id}/move",
            headers=headers,
            json={"destinationId": folder_id},
            timeout=30,
        )
        resp.raise_for_status()
