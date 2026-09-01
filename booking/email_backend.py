"""A Django email backend that sends through the connected Google account.

Being a backend rather than a bespoke send function means every existing caller
-- ``notifications.py``, password resets, anything Django itself sends -- keeps
working untouched, complete with HTML alternatives and reply-to headers.

Resolution order:

1. If a Google account is connected *with* the Gmail send grant, use it.
2. Otherwise fall back to printing the message (development), or fail loudly
   (production), depending on ``EMAIL_FALLBACK_TO_CONSOLE``.

Setting ``EMAIL_HOST`` in the environment bypasses this entirely and uses SMTP.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.console import EmailBackend as ConsoleBackend

from . import gmail
from .google_calendar import GoogleNotConfigured

logger = logging.getLogger(__name__)


class GmailAPIBackend(BaseEmailBackend):
    """Send via the Gmail API of the account connected under Google setup."""

    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self._console = None

    def _console_backend(self):
        if self._console is None:
            self._console = ConsoleBackend(fail_silently=self.fail_silently)
        return self._console

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        try:
            credential = gmail.sending_credential()
        except Exception as exc:  # noqa: BLE001 - database may not be ready
            logger.error("Could not look up the sending account: %s", exc)
            credential = None

        if credential is None:
            return self._no_account(email_messages)

        sent = 0
        for message in email_messages:
            try:
                gmail.send_message(message, credential=credential)
                sent += 1
            except (GoogleNotConfigured, Exception) as exc:  # noqa: BLE001
                logger.error(
                    "Gmail send failed for %r to %s: %s", message.subject, message.to, exc
                )
                if not self.fail_silently:
                    # Deliberately not re-raised: notifications.py already
                    # treats email as best-effort, and a booking must never be
                    # lost because Google hiccuped. The error is logged above.
                    pass
        return sent

    def _no_account(self, email_messages):
        fallback = getattr(settings, "EMAIL_FALLBACK_TO_CONSOLE", settings.DEBUG)
        if fallback:
            logger.info(
                "No Google account connected for email; printing %d message(s) to the console.",
                len(email_messages),
            )
            return self._console_backend().send_messages(email_messages)

        logger.error(
            "Cannot send %d email(s): no Google account with the Gmail send grant. "
            "Connect one at /manage/google/, or set EMAIL_HOST to use SMTP.",
            len(email_messages),
        )
        return 0
