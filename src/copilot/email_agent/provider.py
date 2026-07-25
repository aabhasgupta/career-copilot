"""EmailProvider interface: one abstraction over Outlook/Gmail so the rest of
the app (digest, alerts, later inbox monitoring) never talks to a specific
API directly, keeping the provider selectable via profile.yaml's
email_integration.provider (docs/DECISIONS.md D1/genericity goal).

Outlook (Microsoft Graph, MSAL device-code flow) is the only implementation
today, since the user's primary inbox is Hotmail/Outlook.com. Gmail stays
documented as planned, not built - there's no real Gmail inbox to verify it
against, and building it now would be untested, speculative code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from copilot.config import EmailProviderName, Profile


@dataclass
class InboundMessage:
    """Provider-agnostic shape of a received email. The interface speaks
    Copilot's vocabulary, not any one provider's - "file into the Career
    Copilot area with a type label" maps to folder+category on Outlook and
    would map to labels on Gmail."""

    message_id: str
    thread_id: str
    sender: str  # bare address, lowercased (e.g. "no-reply@greenhouse.io")
    sender_name: str  # display name (e.g. "Stripe Recruiting"), may be ""
    subject: str
    body_text: str
    received_at: datetime
    # RFC 5322 Message-ID header: stable across folder moves, unlike
    # provider message ids - the idempotency key for manually-moved mail.
    internet_message_id: str = ""


class EmailProvider(ABC):
    @abstractmethod
    def send_mail(self, *, to: str, subject: str, body_html: str) -> None:
        """Send an HTML email. Raises on failure."""

    @abstractmethod
    def list_messages_since(self, since: datetime) -> list[InboundMessage]:
        """Inbox messages received after `since`, oldest first."""

    @abstractmethod
    def list_intake_messages(self) -> list[InboundMessage]:
        """Messages the user manually placed in the intake area (the
        "Career Copilot/Process" folder on Outlook; a label on Gmail) - the
        human override for anything the pipeline missed. No date bound: a
        human putting a message there IS the signal to look at it (D28)."""

    @abstractmethod
    def list_filed_headers(self) -> list[InboundMessage]:
        """Headers only (empty body_text) of every message in the filed area
        ("Career Copilot" on Outlook). Exists to catch the nearly-right
        gesture of dropping new mail on the filed folder instead of the
        intake subfolder (D29): sync dedupes these against its records and
        fetches the full body (get_message) only for strangers, so the scan
        stays cheap however large the filed area grows."""

    @abstractmethod
    def get_message(self, message_id: str) -> InboundMessage:
        """One full message by provider id."""

    @abstractmethod
    def file_message(self, message_id: str, *, category: str) -> None:
        """File a message into the Career Copilot area labeled with
        `category` (e.g. "Interview Invite"), creating whatever
        folder/label/category structure that requires on first use."""


def get_provider(profile: Profile) -> EmailProvider:
    if profile.email_integration.provider == EmailProviderName.outlook:
        import os

        from copilot.email_agent.outlook import OutlookProvider

        client_id = os.environ.get("AZURE_CLIENT_ID")
        if not client_id:
            raise RuntimeError(
                "AZURE_CLIENT_ID not set in .env - see README for the Azure app "
                "registration steps."
            )
        return OutlookProvider(client_id)

    raise NotImplementedError(
        f"Email provider '{profile.email_integration.provider.value}' is not yet "
        "implemented - only 'outlook' is built (see docs/PLAN.md)."
    )
