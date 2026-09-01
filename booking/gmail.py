"""Send email through the connected Google account's Gmail API.

Why this exists instead of SMTP:

* **No password.** The salon already connects a Google account for the calendar;
  the same OAuth token sends the mail. There is no App Password to create, store
  or rotate.
* **It works where SMTP does not.** Free PythonAnywhere accounts cannot open
  outbound SMTP connections, but they can reach ``googleapis.com`` over HTTPS --
  which they must anyway, or the calendar sync would not work either.
* **Better deliverability.** The mail genuinely comes from the salon's Gmail
  account, rather than being relayed by a server Gmail has never heard of.

The grant used is ``gmail.send``, which can only send. It cannot read the inbox.
"""
from __future__ import annotations

import base64
import json
import logging

from .google_calendar import GMAIL_SEND_SCOPE, GoogleNotConfigured, _wrap

logger = logging.getLogger(__name__)


def granted_scopes(credential) -> list[str]:
    if not credential or not credential.oauth_scopes:
        return []
    try:
        return list(json.loads(credential.oauth_scopes))
    except (ValueError, TypeError):
        return []


def can_send_email(credential) -> bool:
    """Whether this credential may send mail.

    A calendar connected before email sending existed will not carry the Gmail
    grant, so the setup page offers a reconnect rather than failing silently at
    the moment a customer books.
    """
    from .models import GoogleCredential

    if credential is None or not credential.is_active:
        return False
    if credential.auth_type != GoogleCredential.OAUTH:
        return False  # service accounts cannot send as a human
    if not credential.oauth_refresh_token:
        return False
    scopes = granted_scopes(credential)
    # An empty scope list means an older record that predates scope storage;
    # assume no grant rather than guessing, so the UI prompts a reconnect.
    return GMAIL_SEND_SCOPE in scopes


def sending_credential():
    """The connected Google account used for outgoing mail, or None."""
    from .models import GoogleCredential

    # With one account per stylist, the salon nominates which one sends mail;
    # otherwise fall back to whichever connected account can.
    candidates = GoogleCredential.objects.filter(
        auth_type=GoogleCredential.OAUTH, is_active=True
    ).order_by("-is_default_sender", "pk")
    for credential in candidates:
        if can_send_email(credential):
            return credential
    return None


def _service(credential):
    from googleapiclient.discovery import build

    from .google_calendar import _credentials_for

    creds = _credentials_for(credential)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def sender_address(credential) -> str:
    """The address mail will appear to come from.

    Gmail rewrites the From header to the authenticated account regardless, so
    this uses the address stored at connect time and avoids a per-email API call.
    """
    return credential.oauth_account_email or ""


def send_message(message, credential=None) -> bool:
    """Send a ``django.core.mail.EmailMessage`` through Gmail.

    Returns True if Google accepted it. Raises ``GoogleNotConfigured`` when no
    account is connected, so the caller can decide whether that is fatal.
    """
    credential = credential or sending_credential()
    if credential is None:
        raise GoogleNotConfigured(
            "No Google account with permission to send email. Connect one under "
            "Google setup, or set EMAIL_HOST to use SMTP instead."
        )

    mime = message.message()  # Django builds the full MIME document for us

    # Gmail sends as the authenticated account whatever the From header says;
    # matching it avoids a confusing mismatch in the recipient's client.
    address = sender_address(credential)
    if address and address not in str(mime.get("From", "")):
        from email.utils import formataddr, parseaddr

        display_name = parseaddr(str(mime.get("From", "")))[0]
        del mime["From"]
        mime["From"] = formataddr((display_name, address)) if display_name else address

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    try:
        _service(credential).users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
    except Exception as exc:  # noqa: BLE001 - normalised for the admin
        raise _wrap(exc) from exc

    logger.info("Sent %r via Gmail to %s", message.subject, message.to)
    return True


def check_sending(credential=None) -> str:
    """Verify the Gmail grant without sending anything. Returns a summary."""
    from .models import GoogleCredential

    credential = credential or GoogleCredential.objects.filter(
        auth_type=GoogleCredential.OAUTH, is_active=True
    ).first()
    if credential is None:
        raise GoogleNotConfigured("No Google account connected.")
    if not can_send_email(credential):
        raise GoogleNotConfigured(
            "This connection does not include permission to send email. "
            "Reconnect the account under Google setup to grant it."
        )
    try:
        profile = _service(credential).users().getProfile(userId="me").execute()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return f"OK - will send as {profile.get('emailAddress', sender_address(credential))}."
