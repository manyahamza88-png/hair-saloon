"""Transactional email.

Every message is rendered from a template pair (``.txt`` and ``.html``) and
sent best-effort: a broken SMTP configuration must never lose a booking, so
failures are logged and reported, not raised.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from . import tokens
from .models import Appointment, SalonSettings

logger = logging.getLogger(__name__)


def _send(subject: str, template: str, context: dict, to: list[str], reply_to: list[str] | None = None) -> bool:
    recipients = [address for address in to if address]
    if not recipients:
        return False
    try:
        text_body = render_to_string(f"emails/{template}.txt", context)
        try:
            html_body = render_to_string(f"emails/{template}.html", context)
        except Exception:  # noqa: BLE001 - html part is optional
            html_body = None

        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
            reply_to=[address for address in (reply_to or []) if address] or None,
        )
        if html_body:
            message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - email must not break the flow
        logger.error("Could not send %r to %s: %s", template, recipients, exc)
        return False
    logger.info("Sent %r to %s", template, recipients)
    return True


def _base_context(appointment: Appointment) -> dict:
    salon = SalonSettings.load()
    return {
        "appointment": appointment,
        "calendar": appointment.calendar,
        "salon": salon,
        "start": timezone.localtime(appointment.start_at, salon.tz),
        "end": timezone.localtime(appointment.end_at, salon.tz),
        "appointment_url": tokens.appointment_url(appointment),
        "cancel_url": tokens.cancel_url(appointment),
        "site_url": settings.SITE_BASE_URL,
    }


# ---------------------------------------------------------------------------
# To the calendar owner
# ---------------------------------------------------------------------------
def notify_owner_new_request(appointment: Appointment) -> bool:
    """The accept / decline email that goes to the calendar's Google account."""
    salon = SalonSettings.load()
    context = _base_context(appointment)
    context.update(
        {
            "accept_url": tokens.decision_url(appointment, tokens.ACCEPT),
            "decline_url": tokens.decision_url(appointment, tokens.DECLINE),
            "needs_approval": salon.require_approval,
            "link_valid_days": settings.DECISION_LINK_MAX_AGE_DAYS,
        }
    )
    verb = "New booking request" if salon.require_approval else "New booking"
    subject = (
        f"{verb}: {appointment.customer_name}, "
        f"{context['start']:%a %d %b at %H:%M} ({appointment.calendar.name})"
    )
    recipients = [appointment.calendar.owner_email]
    if salon.notify_email and salon.notify_email not in recipients:
        recipients.append(salon.notify_email)
    return _send(
        subject,
        "owner_new_request",
        context,
        recipients,
        reply_to=[appointment.customer_email],
    )


def notify_owner_cancelled(appointment: Appointment) -> bool:
    context = _base_context(appointment)
    subject = (
        f"Cancelled: {appointment.customer_name}, "
        f"{context['start']:%a %d %b at %H:%M} ({appointment.calendar.name})"
    )
    recipients = [appointment.calendar.owner_email]
    salon = SalonSettings.load()
    if salon.notify_email and salon.notify_email not in recipients:
        recipients.append(salon.notify_email)
    return _send(subject, "owner_cancelled", context, recipients)


# ---------------------------------------------------------------------------
# To the customer
# ---------------------------------------------------------------------------
def notify_customer_received(appointment: Appointment) -> bool:
    salon = SalonSettings.load()
    context = _base_context(appointment)
    context["needs_approval"] = salon.require_approval
    if salon.require_approval:
        subject = f"We received your booking request - {salon.name}"
        template = "customer_received"
    else:
        subject = f"Your appointment is confirmed - {salon.name}"
        template = "customer_confirmed"
    return _send(subject, template, context, [appointment.customer_email], reply_to=[appointment.calendar.owner_email])


def notify_customer_confirmed(appointment: Appointment) -> bool:
    salon = SalonSettings.load()
    context = _base_context(appointment)
    return _send(
        f"Your appointment is confirmed - {salon.name}",
        "customer_confirmed",
        context,
        [appointment.customer_email],
        reply_to=[appointment.calendar.owner_email],
    )


def notify_customer_declined(appointment: Appointment) -> bool:
    salon = SalonSettings.load()
    context = _base_context(appointment)
    return _send(
        f"About your booking request - {salon.name}",
        "customer_declined",
        context,
        [appointment.customer_email],
        reply_to=[appointment.calendar.owner_email],
    )


def notify_customer_cancelled(appointment: Appointment) -> bool:
    salon = SalonSettings.load()
    context = _base_context(appointment)
    return _send(
        f"Your appointment was cancelled - {salon.name}",
        "customer_cancelled",
        context,
        [appointment.customer_email],
    )
