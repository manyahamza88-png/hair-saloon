"""Business operations shared by the public views, the admin and the dashboard.

Keeping the state transitions in one place means a booking approved from the
email link, from the admin list, or from the staff dashboard all behave
identically: same Google sync, same emails, same audit fields.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from . import google_calendar, notifications
from .models import Appointment, SalonSettings

logger = logging.getLogger(__name__)


@transaction.atomic
def create_appointment(*, calendar, service, start_at, end_at, customer_name, customer_email,
                       customer_phone="", notes="") -> Appointment:
    """Persist a booking and kick off the sync + notifications."""
    salon = SalonSettings.load()
    appointment = Appointment.objects.create(
        calendar=calendar,
        service=service,
        start_at=start_at,
        end_at=end_at,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        notes=notes,
        status=Appointment.PENDING if salon.require_approval else Appointment.CONFIRMED,
        decided_at=None if salon.require_approval else timezone.now(),
    )
    transaction.on_commit(lambda: _after_create(appointment))
    return appointment


def _after_create(appointment: Appointment) -> None:
    # A pending request goes onto the calendar as "tentative" so the owner sees
    # the hold immediately; the customer is only invited once it is confirmed.
    google_calendar.sync_appointment(
        appointment, notify_attendees=appointment.status == Appointment.CONFIRMED
    )
    notifications.notify_owner_new_request(appointment)
    notifications.notify_customer_received(appointment)


def confirm_appointment(appointment: Appointment, note: str = "", notify: bool = True) -> Appointment:
    if appointment.status == Appointment.CONFIRMED:
        return appointment
    appointment.status = Appointment.CONFIRMED
    appointment.decided_at = timezone.now()
    if note:
        appointment.decision_note = note
    appointment.save(update_fields=["status", "decided_at", "decision_note", "updated_at"])

    google_calendar.sync_appointment(appointment, notify_attendees=True)
    if notify:
        notifications.notify_customer_confirmed(appointment)
    return appointment


def decline_appointment(appointment: Appointment, note: str = "", notify: bool = True) -> Appointment:
    if appointment.status == Appointment.DECLINED:
        return appointment
    appointment.status = Appointment.DECLINED
    appointment.decided_at = timezone.now()
    if note:
        appointment.decision_note = note
    appointment.save(update_fields=["status", "decided_at", "decision_note", "updated_at"])

    google_calendar.sync_appointment(appointment, notify_attendees=False)
    if notify:
        notifications.notify_customer_declined(appointment)
    return appointment


def cancel_appointment(appointment: Appointment, note: str = "", by_customer: bool = True,
                       notify: bool = True) -> Appointment:
    if appointment.status == Appointment.CANCELLED:
        return appointment
    appointment.status = Appointment.CANCELLED
    appointment.decided_at = timezone.now()
    if note:
        appointment.decision_note = note
    appointment.save(update_fields=["status", "decided_at", "decision_note", "updated_at"])

    google_calendar.sync_appointment(appointment, notify_attendees=True)
    if notify:
        if by_customer:
            notifications.notify_owner_cancelled(appointment)
        else:
            notifications.notify_customer_cancelled(appointment)
    return appointment


def resync_appointment(appointment: Appointment) -> bool:
    return google_calendar.sync_appointment(appointment, notify_attendees=False)


def reconcile_with_google(appointment, notify: bool = True) -> str:
    """Apply an edit a stylist made in Google Calendar back to the booking.

    Returns what happened: ``none``, ``deleted``, ``moved`` or ``error``.
    """
    result = google_calendar.read_back(appointment)
    change = result["change"]

    if change == "deleted":
        # The stylist removed it on their phone: free the slot and tell the
        # customer, rather than leaving them expecting an appointment.
        appointment.status = Appointment.CANCELLED
        appointment.decided_at = timezone.now()
        appointment.google_event_id = ""
        appointment.decision_note = (
            appointment.decision_note or "Cancelled from the salon's Google Calendar."
        )
        appointment.save(
            update_fields=[
                "status", "decided_at", "google_event_id", "decision_note", "updated_at",
            ]
        )
        if notify:
            notifications.notify_customer_cancelled(appointment)
        logger.info("Appointment %s cancelled: its Google event was deleted.", appointment.pk)

    elif change == "moved":
        old_start = appointment.start_at
        appointment.start_at = result["start"]
        appointment.end_at = result["end"]
        appointment.save(update_fields=["start_at", "end_at", "updated_at"])
        if notify and appointment.status == Appointment.CONFIRMED:
            notifications.notify_customer_confirmed(appointment)
        logger.info(
            "Appointment %s moved in Google: %s -> %s", appointment.pk, old_start, result["start"]
        )

    elif change == "accepted":
        # The stylist tapped "Yes" on the event in Google Calendar.
        appointment.google_rsvp = result["rsvp"]
        appointment.save(update_fields=["google_rsvp", "updated_at"])
        if appointment.status == Appointment.PENDING:
            confirm_appointment(appointment, notify=notify)
            logger.info("Appointment %s confirmed from a Google RSVP.", appointment.pk)

    elif change == "declined":
        appointment.google_rsvp = result["rsvp"]
        appointment.save(update_fields=["google_rsvp", "updated_at"])
        if appointment.status in (Appointment.PENDING, Appointment.CONFIRMED):
            decline_appointment(appointment, notify=notify)
            logger.info("Appointment %s declined from a Google RSVP.", appointment.pk)

    elif change == "error":
        appointment.google_sync_error = result["reason"][:2000]
        appointment.save(update_fields=["google_sync_error", "updated_at"])

    return change


def provision_google_calendar(calendar, share_role: str = "writer", notify: bool = True) -> str:
    """Create a Google calendar for this entry and hand the owner access.

    The credential creates the calendar, so it owns it outright and no sharing
    has to be arranged from inside anyone's Google account. The owner's address
    is then granted access through the API, which makes the calendar appear in
    their Google Calendar by itself.

    Raises ``ValueError`` for a misconfigured entry and
    ``google_calendar.GoogleError`` if Google refuses.
    """
    from .models import Calendar, SalonSettings

    if calendar.google_calendar_id:
        raise ValueError(
            f"'{calendar.name}' already points at {calendar.google_calendar_id}. "
            "Clear the calendar ID first if you really want a new one."
        )
    if not calendar.credential or not calendar.credential.is_active:
        raise ValueError(f"'{calendar.name}' has no active Google credential attached.")

    salon = SalonSettings.load()
    calendar_id = google_calendar.create_calendar(
        calendar.credential,
        name=f"{salon.name} - {calendar.name}",
        timezone_name=salon.timezone_name,
        description=calendar.description or f"Online bookings for {calendar.name}.",
    )

    calendar.google_calendar_id = calendar_id
    calendar.last_sync_error = ""
    Calendar.objects.filter(pk=calendar.pk).update(
        google_calendar_id=calendar_id, last_sync_error=""
    )

    # Sharing it with the owner is a nicety, not the point: the booking system
    # already works without it. Do not lose the calendar we just made if this
    # second step fails.
    if calendar.owner_email:
        try:
            google_calendar.grant_calendar_access(
                calendar.credential, calendar_id, calendar.owner_email, role=share_role, notify=notify
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            logger.warning("Could not share calendar %s with the owner: %s", calendar.pk, exc)
            message = f"Calendar created, but sharing it with {calendar.owner_email} failed: {exc}"
            calendar.last_sync_error = message
            Calendar.objects.filter(pk=calendar.pk).update(last_sync_error=message[:2000])

    return calendar_id
