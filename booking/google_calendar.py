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

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
# Least privilege: gmail.send can only send, it cannot read the mailbox.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

SCOPES = [CALENDAR_SCOPE]
READONLY_SCOPES = [CALENDAR_READONLY_SCOPE]

# What the browser flow asks a Google account to grant: calendar access, plus
# permission to send the salon's booking emails from that account. Sending this
# way needs no SMTP password and works on hosts that block outbound SMTP.
OAUTH_SCOPES = [CALENDAR_SCOPE, GMAIL_SEND_SCOPE]


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
def _naive_utc(value: dt.datetime | None) -> dt.datetime | None:
    """google-auth compares ``expiry`` against a naive UTC ``utcnow()``."""
    if value is None:
        return None
    if timezone.is_aware(value):
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


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
        # An OAuth token may also carry the Gmail send grant; declare the full
        # set so a refresh does not narrow it.
        scopes = READONLY_SCOPES if readonly else OAUTH_SCOPES
        refresh_token = credential.get_refresh_token()
        if not refresh_token:
            raise GoogleNotConfigured(
                "This Google account is not connected yet. Open Google setup and "
                "click 'Connect a Google account'."
            )

        from .models import GoogleOAuthClientSettings

        # A credential may carry its own client (e.g. one imported from the
        # command line); otherwise use the app-wide client from the admin.
        client_id = credential.oauth_client_id
        client_secret = credential.get_oauth_client_secret()
        if not (client_id and client_secret):
            client_id, client_secret = GoogleOAuthClientSettings.effective()
        if not (client_id and client_secret):
            raise GoogleNotConfigured(
                "No Google Client ID / Client Secret configured. Add them under Google setup."
            )

        return UserCredentials(
            token=credential.get_access_token() or None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            expiry=_naive_utc(credential.oauth_token_expiry),
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


# ---------------------------------------------------------------------------
# Provisioning: let the credential own the calendar
# ---------------------------------------------------------------------------
def create_calendar(credential, name: str, timezone_name: str, description: str = "") -> str:
    """Create a brand-new Google calendar owned by this credential.

    This is the route that needs nothing configured inside anybody's Google
    account: the service account creates the calendar, so it already has full
    access to it. Returns the new calendar ID.
    """
    service = build_service(credential)
    body = {"summary": name, "timeZone": timezone_name}
    if description:
        body["description"] = description
    try:
        created = service.calendars().insert(body=body).execute()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc
    return created.get("id", "")


def grant_calendar_access(credential, calendar_id: str, email: str, role: str = "writer",
                          notify: bool = True) -> None:
    """Share a calendar the credential owns with a human.

    Note the direction of travel: the app grants access outwards, through the
    API. The person receiving it does not configure anything -- the calendar
    simply turns up in their Google Calendar.

    ``role`` is ``writer`` (see and edit events) or ``owner`` (also rename or
    delete the calendar).
    """
    if not email:
        return
    service = build_service(credential)
    rule = {"scope": {"type": "user", "value": email}, "role": role}
    try:
        service.acl().insert(
            calendarId=calendar_id, body=rule, sendNotifications=notify
        ).execute()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


def delete_calendar(credential, calendar_id: str) -> None:
    """Permanently delete a calendar the credential owns."""
    service = build_service(credential)
    try:
        service.calendars().delete(calendarId=calendar_id).execute()
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc


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


@dataclass(frozen=True)
class ExternalEvent:
    """An event that lives in Google but was not created by this site."""

    summary: str
    start: dt.datetime
    end: dt.datetime
    all_day: bool
    event_id: str


def list_events(calendar, start: dt.datetime, end: dt.datetime,
                skip_own: bool = True) -> list[ExternalEvent]:
    """Real events from a Google calendar, with their titles.

    ``free_busy`` only reports opaque busy blocks; the staff week view wants to
    show *what* the stylist has in their own calendar. Events this site created
    are skipped by default, since they are already drawn from the database.
    """
    service = build_service(calendar.credential, readonly=True)
    try:
        result = (
            service.events()
            .list(
                calendarId=calendar.google_calendar_id,
                timeMin=start.astimezone(dt.timezone.utc).isoformat(),
                timeMax=end.astimezone(dt.timezone.utc).isoformat(),
                singleEvents=True,      # expand recurring events into instances
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc) from exc

    events = []
    for item in result.get("items", []):
        if item.get("status") == "cancelled":
            continue
        if skip_own and item.get("extendedProperties", {}).get("private", {}).get(
            "salonAppointmentId"
        ):
            continue

        raw_start = item.get("start", {})
        raw_end = item.get("end", {})
        all_day = "date" in raw_start
        try:
            if all_day:
                begin = dt.datetime.fromisoformat(raw_start["date"]).replace(
                    tzinfo=dt.timezone.utc
                )
                finish = dt.datetime.fromisoformat(raw_end["date"]).replace(
                    tzinfo=dt.timezone.utc
                )
            else:
                begin = dt.datetime.fromisoformat(raw_start["dateTime"].replace("Z", "+00:00"))
                finish = dt.datetime.fromisoformat(raw_end["dateTime"].replace("Z", "+00:00"))
        except (KeyError, ValueError):  # pragma: no cover - malformed payload
            continue

        events.append(
            ExternalEvent(
                summary=item.get("summary", "(no title)"),
                start=begin,
                end=finish,
                all_day=all_day,
                event_id=item.get("id", ""),
            )
        )
    return events


def safe_list_events(calendar, start: dt.datetime, end: dt.datetime) -> list[ExternalEvent]:
    """``list_events`` that never raises: the week view must still render."""
    if not calendar.is_google_connected:
        return []
    try:
        events = list_events(calendar, start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list events for calendar %s: %s", calendar.pk, exc)
        _record_calendar_error(calendar, str(exc))
        return []
    return events


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
def can_invite_attendees(credential) -> bool:
    """Whether this credential may add attendees to an event.

    Google refuses ``attendees`` from a plain service account with
    ``forbiddenForServiceAccounts``: only a real user (OAuth) or a service
    account using domain-wide delegation may invite people. That is not a
    problem for us -- the salon sends its own confirmation emails -- so we just
    leave the attendee list off and put the customer in the description.
    """
    if credential is None:
        return False
    if credential.auth_type == credential.OAUTH:
        return True
    return bool(credential.delegated_user)


def _event_body(appointment, salon, allow_attendees: bool = False) -> dict:
    from .models import Appointment

    status_word = {
        Appointment.PENDING: "REQUEST",
        Appointment.CONFIRMED: "",
        Appointment.DECLINED: "DECLINED",
        Appointment.CANCELLED: "CANCELLED",
    }.get(appointment.status, "")
    prefix = f"[{status_word}] " if status_word else ""

    from . import tokens

    # Staff work from Google Calendar on a phone, not from this site's admin, so
    # the event itself has to be actionable: the accept and decline links go in
    # the description where the mobile app turns them into tappable links.
    lines = []
    if appointment.status == Appointment.PENDING:
        lines += [
            "*** NOT CONFIRMED YET — tap a link below ***",
            "",
            f"ACCEPT:  {tokens.decision_url(appointment, tokens.ACCEPT)}",
            f"DECLINE: {tokens.decision_url(appointment, tokens.DECLINE)}",
            "",
            "Each opens a confirmation page first, so nothing changes by accident.",
            "",
        ]

    lines += [
        f"Service: {appointment.service_name} ({appointment.duration_minutes} min)",
        f"Customer: {appointment.customer_name}",
        f"Email: {appointment.customer_email}",
    ]
    if appointment.customer_phone:
        lines.append(f"Phone: {appointment.customer_phone}")
    if appointment.notes:
        lines.append("")
        lines.append(f"Notes: {appointment.notes}")

    lines.append("")
    if appointment.status == Appointment.CONFIRMED:
        lines.append(f"Cancel this booking: {tokens.cancel_url(appointment)}")
    lines.append(f"Booking details: {appointment_url(appointment)}")

    body = {
        "summary": f"{prefix}{appointment.service_name} - {appointment.customer_name}",
        "description": "\n".join(lines),
        "start": {"dateTime": appointment.start_at.isoformat(), "timeZone": salon.timezone_name},
        "end": {"dateTime": appointment.end_at.isoformat(), "timeZone": salon.timezone_name},
        "status": "tentative" if appointment.status == Appointment.PENDING else "confirmed",
        "extendedProperties": {"private": {"salonAppointmentId": str(appointment.public_id)}},
        "reminders": {"useDefault": True},
    }
    if allow_attendees and appointment.customer_email:
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
    attendees_allowed = can_invite_attendees(calendar.credential)
    body = _event_body(appointment, salon, allow_attendees=attendees_allowed)
    send_updates = "all" if (notify_attendees and attendees_allowed) else "none"

    def _write(event_body, updates):
        if appointment.google_event_id:
            return (
                service.events()
                .update(
                    calendarId=calendar.google_calendar_id,
                    eventId=appointment.google_event_id,
                    body=event_body,
                    sendUpdates=updates,
                )
                .execute()
            )
        return (
            service.events()
            .insert(calendarId=calendar.google_calendar_id, body=event_body, sendUpdates=updates)
            .execute()
        )

    try:
        event = _write(body, send_updates)
    except Exception as exc:  # noqa: BLE001
        # Belt and braces: if Google still objects to the attendee list (for
        # instance delegation was configured but has since been revoked), write
        # the event without it rather than losing the booking entirely.
        if attendees_allowed and _is_attendee_refusal(exc):
            logger.warning(
                "Calendar %s may not invite attendees; retrying without them.", calendar.pk
            )
            event = _write(_event_body(appointment, salon, allow_attendees=False), "none")
        else:
            raise _wrap(exc) from exc
    return event.get("id", "")


def _is_attendee_refusal(exc: Exception) -> bool:
    text = str(exc)
    if "forbiddenForServiceAccounts" in text:
        return True
    return "attendees" in text and "Service accounts cannot invite attendees" in text


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


def fetch_event(calendar, event_id: str) -> dict | None:
    """One event by id. ``None`` means it is gone from Google."""
    service = build_service(calendar.credential, readonly=True)
    try:
        event = service.events().get(
            calendarId=calendar.google_calendar_id, eventId=event_id
        ).execute()
    except Exception as exc:  # noqa: BLE001
        try:
            from googleapiclient.errors import HttpError

            if isinstance(exc, HttpError) and getattr(exc.resp, "status", None) in (404, 410):
                return None
        except ImportError:  # pragma: no cover
            pass
        raise _wrap(exc) from exc

    if event.get("status") == "cancelled":
        return None
    return event


def read_back(appointment) -> dict:
    """Compare Google against our record for one appointment.

    Staff live in Google Calendar on their phones, so that is where an
    appointment actually gets moved or dropped. Without reading those edits back
    the slot stays blocked here for a booking that no longer exists.

    Returns ``{"change": ...}`` where change is one of ``none``, ``deleted``,
    ``moved`` or ``error``.
    """
    calendar = appointment.calendar
    if not (calendar.is_google_connected and appointment.google_event_id):
        return {"change": "none", "reason": "not synced"}

    try:
        event = fetch_event(calendar, appointment.google_event_id)
    except Exception as exc:  # noqa: BLE001 - reported, never raised at a customer
        return {"change": "error", "reason": str(exc)}

    if event is None:
        return {"change": "deleted"}

    start_raw = event.get("start", {}).get("dateTime")
    end_raw = event.get("end", {}).get("dateTime")
    if not (start_raw and end_raw):
        return {"change": "none", "reason": "all-day event, left alone"}

    try:
        new_start = dt.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        new_end = dt.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
    except ValueError:
        return {"change": "none", "reason": "unreadable times"}

    if new_start != appointment.start_at or new_end != appointment.end_at:
        return {"change": "moved", "start": new_start, "end": new_end}

    return {"change": "none"}


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
