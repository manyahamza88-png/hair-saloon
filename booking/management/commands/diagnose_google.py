"""Say exactly why bookings are or are not reaching Google Calendar.

Every failure in this chain is silent by design -- a booking must never be lost
because Google is unhappy -- which makes it hard to tell *which* link is broken.
This walks the whole chain and reports a verdict for each step.

    python manage.py diagnose_google
    python manage.py diagnose_google --write   # also create and delete a test event
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from booking import gmail, google_calendar
from booking.models import (
    Appointment,
    Calendar,
    GoogleCredential,
    GoogleOAuthClientSettings,
    SalonSettings,
)

OK = "  [ok]   "
BAD = "  [FAIL] "
WARN = "  [warn] "


class Command(BaseCommand):
    help = "Diagnose the whole Google Calendar chain, step by step."

    def add_arguments(self, parser):
        parser.add_argument(
            "--write",
            action="store_true",
            help="Prove write access by creating a test event and deleting it again.",
        )

    def ok(self, text):
        self.stdout.write(self.style.SUCCESS(OK + text))

    def bad(self, text):
        self.stdout.write(self.style.ERROR(BAD + text))

    def warn(self, text):
        self.stdout.write(self.style.WARNING(WARN + text))

    def handle(self, *args, **options):
        problems = []

        # 1. libraries -----------------------------------------------------
        self.stdout.write("\n1. Google client libraries")
        if google_calendar.libraries_available():
            self.ok("installed")
        else:
            self.bad("not installed -- run: pip install --user -r requirements.txt")
            problems.append("libraries missing")
            self.finish(problems)
            return

        # 2. OAuth client --------------------------------------------------
        self.stdout.write("\n2. Google Cloud OAuth client")
        client_id, client_secret = GoogleOAuthClientSettings.effective()
        if client_id and client_secret:
            self.ok(f"client id ...{client_id[-28:]}")
        else:
            self.bad("no Client ID / Secret. Add them at /manage/google/")
            problems.append("no OAuth client")

        # 3. connected accounts -------------------------------------------
        self.stdout.write("\n3. Connected Google accounts")
        credentials = list(GoogleCredential.objects.filter(is_active=True))
        if not credentials:
            self.bad("none. Connect one at /manage/google/")
            problems.append("no account connected")
        for credential in credentials:
            label = credential.oauth_account_email or credential.name
            if credential.auth_type == GoogleCredential.OAUTH:
                if credential.oauth_refresh_token:
                    self.ok(f"{label}: connected")
                else:
                    self.bad(f"{label}: no refresh token -- reconnect at /manage/google/")
                    problems.append(f"{label} not connected")
                if gmail.can_send_email(credential):
                    self.ok(f"{label}: may send email")
                else:
                    self.warn(f"{label}: no gmail.send grant (reconnect to enable email)")
            else:
                self.ok(f"{label}: service account {credential.service_account_email}")

        # 4. calendars -----------------------------------------------------
        self.stdout.write("\n4. Calendars on the site")
        calendars = list(Calendar.objects.all())
        if not calendars:
            self.bad("none configured")
            problems.append("no calendars")

        for calendar in calendars:
            self.stdout.write(f"\n   {calendar.name!r} (active={calendar.is_active})")
            if not calendar.credential:
                self.bad("no Google account attached -- NOTHING is written to Google")
                self.stdout.write(
                    "           Fix: /manage/google/ -> 'These calendars do not reach Google'"
                )
                problems.append(f"{calendar.name}: no credential")
                continue
            self.ok(f"account: {calendar.credential.oauth_account_email or calendar.credential.name}")

            if not calendar.google_calendar_id:
                self.bad("no calendar ID -- nothing to write into")
                problems.append(f"{calendar.name}: no calendar id")
                continue
            self.ok(f"calendar id: {calendar.google_calendar_id}")

            if not calendar.is_google_connected:
                self.bad("is_google_connected is False -- sync is skipped")
                problems.append(f"{calendar.name}: not connected")
                continue

            try:
                self.ok(google_calendar.check_calendar(calendar).replace("OK - ", ""))
            except Exception as exc:  # noqa: BLE001 - the point of the command
                self.bad(f"cannot reach it: {exc}")
                problems.append(f"{calendar.name}: {exc}")
                continue

            if options["write"]:
                self.check_write(calendar, problems)

        # 5. recent appointments -------------------------------------------
        self.stdout.write("\n5. Recent bookings")
        recent = Appointment.objects.order_by("-created_at")[:10]
        if not recent:
            self.warn("no bookings yet -- make one and run this again")
        for appointment in recent:
            when = timezone.localtime(appointment.start_at, SalonSettings.load().tz)
            label = f"{appointment.customer_name} {when:%d %b %H:%M}"
            if appointment.google_sync_error:
                self.bad(f"{label}: {appointment.google_sync_error[:120]}")
                problems.append(f"sync error on {label}")
            elif appointment.google_event_id:
                self.ok(f"{label}: in Google as {appointment.google_event_id[:20]}")
            elif not appointment.calendar.is_google_connected:
                self.bad(f"{label}: calendar not connected, never sent")
            else:
                self.warn(f"{label}: no event id -- try the Re-sync action")

        self.finish(problems)

    def check_write(self, calendar, problems):
        """Actually create and delete an event, proving write access."""
        import datetime as dt

        salon = SalonSettings.load()
        start = timezone.now() + dt.timedelta(days=365)
        body = {
            "summary": "Salon booking system - connection test (safe to delete)",
            "start": {"dateTime": start.isoformat(), "timeZone": salon.timezone_name},
            "end": {
                "dateTime": (start + dt.timedelta(minutes=15)).isoformat(),
                "timeZone": salon.timezone_name,
            },
            "status": "tentative",
        }
        try:
            service = google_calendar.build_service(calendar.credential)
            event = (
                service.events()
                .insert(calendarId=calendar.google_calendar_id, body=body, sendUpdates="none")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            self.bad(f"WRITE FAILED: {google_calendar._wrap(exc)}")
            problems.append(f"{calendar.name}: cannot write")
            return

        self.ok(f"write works (created {event.get('id', '?')[:20]})")
        try:
            service.events().delete(
                calendarId=calendar.google_calendar_id, eventId=event["id"]
            ).execute()
            self.ok("test event removed again")
        except Exception as exc:  # noqa: BLE001
            self.warn(f"could not delete the test event, remove it by hand: {exc}")

    def finish(self, problems):
        self.stdout.write("\n" + "=" * 62)
        if problems:
            self.stdout.write(self.style.ERROR(f"{len(problems)} problem(s) found:"))
            for problem in problems:
                self.stdout.write(f"   - {problem}")
        else:
            self.stdout.write(
                self.style.SUCCESS("Everything checks out: bookings should reach Google.")
            )
