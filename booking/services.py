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
