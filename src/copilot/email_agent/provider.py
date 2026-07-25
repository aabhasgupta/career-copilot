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


class EmailProvider(ABC):
    @abstractmethod
    def send_mail(self, *, to: str, subject: str, body_html: str) -> None:
        """Send an HTML email. Raises on failure."""

    @abstractmethod
    def list_messages_since(self, since: datetime) -> list[InboundMessage]:
        """Inbox messages received after `since`, oldest first."""

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
