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

from copilot.config import EmailProviderName, Profile


class EmailProvider(ABC):
    @abstractmethod
    def send_mail(self, *, to: str, subject: str, body_html: str) -> None:
        """Send an HTML email. Raises on failure."""


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
