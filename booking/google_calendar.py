"""Thin wrapper around the Google Calendar API.

Everything here is defensive on purpose: if Google is misconfigured, offline or
rate limiting us, the salon must still be able to take bookings. Failures are
logged, recorded on the object, and surfaced in the admin -- they never raise
into a customer-facing view.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
READONLY_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class GoogleNotConfigured(Exception):
    """Raised when a calendar has no usable credentials."""


class GoogleError(Exception):
    """Any failure while talking to the Google Calendar API."""


@dataclass(frozen=True)
class BusyBlock:
    start: dt.datetime
    end: dt.datetime


# ---------------------------------------------------------------------------
# Library availability
# ---------------------------------------------------------------------------
def libraries_available() -> bool:
    try:
        import googleapiclient.discovery  # noqa: F401
        import google.oauth2.service_account  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Credentials -> API client
# ---------------------------------------------------------------------------
def _credentials_for(credential, readonly: bool = False):
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials as UserCredentials

    scopes = READONLY_SCOPES if readonly else SCOPES

    if credential.auth_type == credential.SERVICE_ACCOUNT:
        if not credential.service_account_json.strip():
            raise GoogleNotConfigured("No service account JSON on this credential.")
        try:
            info = json.loads(credential.service_account_json)
        except ValueError as exc:
            raise GoogleNotConfigured(f"Service account JSON is not valid JSON: {exc}") from exc
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        if credential.delegated_user:
            creds = creds.with_subject(credential.delegated_user)
        return creds

    if credential.auth_type == credential.OAUTH:
        if not credential.oauth_refresh_token:
            raise GoogleNotConfigured("This credential has no refresh token yet.")
        client_id = credential.oauth_client_id or settings.GOOGLE_OAUTH_CLIENT_ID
        client_secret = credential.oauth_client_secret or settings.GOOGLE_OAUTH_CLIENT_SECRET
        if not client_id or not client_secret:
            raise GoogleNotConfigured("OAuth client id / secret missing.")
        return UserCredentials(
            token=None,
            refresh_token=credential.oauth_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
        )

    raise GoogleNotConfigured(f"Unknown auth type {credential.auth_type!r}.")


def build_service(credential, readonly: bool = False):
    """Return an authorised Google Calendar API client."""
    if credential is None or not credential.is_active:
        raise GoogleNotConfigured("No active Google credential attached.")
    if not libraries_available():
        raise GoogleNotConfigured(
            "google-api-python-client is not installed (pip install -r requirements.txt)."
        )
    from googleapiclient.discovery import build

    creds = _credentials_for(credential, readonly=readonly)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _wrap(exc: Exception) -> GoogleError:
    """Turn a Google client error into something readable in the admin."""
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = getattr(exc.resp, "status", "?")
            try:
                detail = json.loads(exc.content.decode()).get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001 - best effort only
                detail = exc.content.decode(errors="replace")[:300] if exc.content else ""
            hint = ""
            if str(status) == "404":
                hint = " Check the calendar ID, and that it is shared with the credential."
            elif str(status) in {"401", "403"}:
                hint = (
                    " Share the calendar with the service account address and give it "
                    "'Make changes to events'."
                )
            return GoogleError(f"Google API error {status}: {detail}{hint}")
    except ImportError:  # pragma: no cover
        pass
    return GoogleError(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------
def check_calendar(calendar) -> str:
    """Verify we can reach a calendar. Returns a human readable summary."""
    service = build_service(calendar.credential, readonly=True)
    try:
        info = service.calendars().get(calendarId=calendar.google_calendar_id).execute()
    except Exception as exc:  # noqa: BLE001 - normalised below
        raise _wrap(exc) from exc
    return f"OK - connected to '{info.get('summary', calendar.google_calendar_id)}' ({info.get('timeZone', '?')})."


def check_credential(credential) -> str:
    """Verify the credential itself by listing the calendars it can see."""
    service = build_service(credential, readonly=True)
    try:
        result = service.calendarList().list(maxResults=50).execute()
    except Exception as exc:  # noqa: BLE001
        # A service account with only shared calendars may have an empty list;
        # that is fine, but a bad key raises here.
        raise _wrap(exc) from exc
    items = result.get("items", [])
    if not items:
        return (
            "Authenticated, but this credential currently sees no calendars. "
            "Share the calendar with it, then set the calendar ID manually."
        )
    names = ", ".join(f"{i.get('summary')} [{i.get('id')}]" for i in items[:10])
    return f"OK - {len(items)} calendar(s) visible: {names}"


def list_calendars(credential) -> list[dict]:
    """All calendars this credential can see, for the admin picker."""
    service = build_service(credential, readonly=True)
    try:
        result = service.calendarList().list(maxResults=250).execute()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return [
        {
            "id": item.get("id"),
            "summary": item.get("summary", ""),
            "access_role": item.get("accessRole", ""),
            "primary": item.get("primary", False),
        }
        for item in result.get("items", [])
    ]


def free_busy(calendar, start: dt.datetime, end: dt.datetime) -> list[BusyBlock]:
    """Busy blocks for one calendar between two aware datetimes."""
    service = build_service(calendar.credential, readonly=True)
    body = {
        "timeMin": start.astimezone(dt.timezone.utc).isoformat(),
        "timeMax": end.astimezone(dt.timezone.utc).isoformat(),
        "items": [{"id": calendar.google_calendar_id}],
    }
    try:
        result = service.freebusy().query(body=body).execute()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

    entry = result.get("calendars", {}).get(calendar.google_calendar_id, {})
    errors = entry.get("errors")
    if errors:
        reasons = ", ".join(e.get("reason", "?") for e in errors)
        raise GoogleError(f"Free/busy lookup failed ({reasons}). Is the calendar shared with the credential?")

    blocks = []
    for period in entry.get("busy", []):
        try:
            blocks.append(
                BusyBlock(
                    start=dt.datetime.fromisoformat(period["start"].replace("Z", "+00:00")),
                    end=dt.datetime.fromisoformat(period["end"].replace("Z", "+00:00")),
                )
            )
        except (KeyError, ValueError):  # pragma: no cover - malformed payload
            continue
    return blocks


def safe_free_busy(calendar, start: dt.datetime, end: dt.datetime) -> list[BusyBlock]:
    """``free_busy`` that never raises: booking keeps working if Google is down."""
    if not calendar.is_google_connected:
        return []
    try:
        blocks = free_busy(calendar, start, end)
    except Exception as exc:  # noqa: BLE001 - availability must not break
        logger.warning("Free/busy lookup failed for calendar %s: %s", calendar.pk, exc)
        _record_calendar_error(calendar, str(exc))
        return []
    _record_calendar_error(calendar, "")
    return blocks


def _record_calendar_error(calendar, message: str) -> None:
    if calendar.last_sync_error != message:
        calendar.last_sync_error = message
        type(calendar).objects.filter(pk=calendar.pk).update(last_sync_error=message)


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------
def _event_body(appointment, salon) -> dict:
    from .models import Appointment

    status_word = {
        Appointment.PENDING: "REQUEST",
        Appointment.CONFIRMED: "",
        Appointment.DECLINED: "DECLINED",
        Appointment.CANCELLED: "CANCELLED",
    }.get(appointment.status, "")
    prefix = f"[{status_word}] " if status_word else ""

    lines = [
        f"Service: {appointment.service_name}",
        f"Customer: {appointment.customer_name}",
        f"Email: {appointment.customer_email}",
    ]
    if appointment.customer_phone:
        lines.append(f"Phone: {appointment.customer_phone}")
    if appointment.notes:
        lines.append("")
        lines.append(f"Notes: {appointment.notes}")
    lines.append("")
    lines.append(f"Manage this booking: {appointment_url(appointment)}")

    body = {
        "summary": f"{prefix}{appointment.service_name} - {appointment.customer_name}",
        "description": "\n".join(lines),
        "start": {"dateTime": appointment.start_at.isoformat(), "timeZone": salon.timezone_name},
        "end": {"dateTime": appointment.end_at.isoformat(), "timeZone": salon.timezone_name},
        "status": "tentative" if appointment.status == Appointment.PENDING else "confirmed",
        "extendedProperties": {"private": {"salonAppointmentId": str(appointment.public_id)}},
        "reminders": {"useDefault": True},
    }
    if appointment.customer_email:
        body["attendees"] = [
            {"email": appointment.customer_email, "displayName": appointment.customer_name}
        ]
    return body


def appointment_url(appointment) -> str:
    from django.urls import reverse

    return f"{settings.SITE_BASE_URL}{reverse('booking:appointment_detail', args=[appointment.public_id])}"


def push_appointment(appointment, notify_attendees: bool = False) -> str:
    """Create or update the Google event for an appointment. Returns its id."""
    from .models import SalonSettings

    calendar = appointment.calendar
    if not calendar.is_google_connected:
        raise GoogleNotConfigured("This calendar is not connected to Google.")

    salon = SalonSettings.load()
    service = build_service(calendar.credential)
    body = _event_body(appointment, salon)
    send_updates = "all" if notify_attendees else "none"

    try:
        if appointment.google_event_id:
            event = (
                service.events()
                .update(
                    calendarId=calendar.google_calendar_id,
                    eventId=appointment.google_event_id,
                    body=body,
                    sendUpdates=send_updates,
                )
                .execute()
            )
        else:
            event = (
                service.events()
                .insert(
                    calendarId=calendar.google_calendar_id,
                    body=body,
                    sendUpdates=send_updates,
                )
                .execute()
            )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return event.get("id", "")


def delete_appointment_event(appointment, notify_attendees: bool = False) -> None:
    calendar = appointment.calendar
    if not (calendar.is_google_connected and appointment.google_event_id):
        return
    service = build_service(calendar.credential)
    try:
        service.events().delete(
            calendarId=calendar.google_calendar_id,
            eventId=appointment.google_event_id,
            sendUpdates="all" if notify_attendees else "none",
        ).execute()
    except Exception as exc:  # noqa: BLE001
        try:
            from googleapiclient.errors import HttpError

            if isinstance(exc, HttpError) and getattr(exc.resp, "status", None) in (404, 410):
                return  # already gone: nothing to do
        except ImportError:  # pragma: no cover
            pass
        raise _wrap(exc) from exc


def sync_appointment(appointment, notify_attendees: bool = False) -> bool:
    """Best-effort sync. Records the outcome on the appointment, never raises."""
    from .models import Appointment

    if not appointment.calendar.is_google_connected:
        return False

    try:
        if appointment.status in (Appointment.DECLINED, Appointment.CANCELLED):
            if appointment.google_event_id:
                delete_appointment_event(appointment, notify_attendees=notify_attendees)
                appointment.google_event_id = ""
        else:
            appointment.google_event_id = push_appointment(
                appointment, notify_attendees=notify_attendees
            )
    except Exception as exc:  # noqa: BLE001 - stored, not raised
        logger.warning("Google sync failed for appointment %s: %s", appointment.pk, exc)
        appointment.google_sync_error = str(exc)[:2000]
        appointment.save(update_fields=["google_sync_error", "updated_at"])
        return False

    appointment.google_sync_error = ""
    appointment.google_synced_at = timezone.now()
    appointment.save(update_fields=["google_event_id", "google_synced_at", "google_sync_error", "updated_at"])
    return True
