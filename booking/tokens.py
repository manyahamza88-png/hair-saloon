"""Signed, expiring tokens for the accept / decline / cancel links in emails.

Nothing is stored in the database: the token is a signed payload, so a link
cannot be forged without the Django ``SECRET_KEY`` and stops working after
``DECISION_LINK_MAX_AGE_DAYS``.
"""
from __future__ import annotations

from django.conf import settings
from django.core import signing
from django.urls import reverse

DECISION_SALT = "booking.appointment.decision"
CANCEL_SALT = "booking.appointment.cancel"

ACCEPT = "accept"
DECLINE = "decline"
VALID_ACTIONS = {ACCEPT, DECLINE}


def _max_age_seconds() -> int:
    return int(settings.DECISION_LINK_MAX_AGE_DAYS) * 24 * 3600


def make_decision_token(appointment, action: str) -> str:
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unknown action {action!r}")
    return signing.dumps(
        {"id": str(appointment.public_id), "action": action}, salt=DECISION_SALT
    )


def read_decision_token(token: str) -> tuple[str, str]:
    """Returns ``(public_id, action)``; raises ``signing.BadSignature`` if invalid."""
    data = signing.loads(token, salt=DECISION_SALT, max_age=_max_age_seconds())
    action = data.get("action")
    if action not in VALID_ACTIONS:
        raise signing.BadSignature("Unknown action in token.")
    return data["id"], action


def make_cancel_token(appointment) -> str:
    return signing.dumps({"id": str(appointment.public_id)}, salt=CANCEL_SALT)


def read_cancel_token(token: str) -> str:
    data = signing.loads(token, salt=CANCEL_SALT, max_age=_max_age_seconds())
    return data["id"]


def absolute(path: str) -> str:
    return f"{settings.SITE_BASE_URL}{path}"


def decision_url(appointment, action: str) -> str:
    token = make_decision_token(appointment, action)
    return absolute(reverse("booking:decide", args=[token]))


def cancel_url(appointment) -> str:
    token = make_cancel_token(appointment)
    return absolute(reverse("booking:cancel", args=[token]))


def appointment_url(appointment) -> str:
    return absolute(reverse("booking:appointment_detail", args=[appointment.public_id]))
