"""Verify every Google credential and calendar from the command line.

    python manage.py check_google

Handy on PythonAnywhere: run it in a Bash console right after pasting a
service-account key to see exactly what Google says.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from booking import gmail, google_calendar
from booking.models import Calendar, GoogleCredential


class Command(BaseCommand):
    help = "Test the connection to Google Calendar for every credential and calendar."

    def handle(self, *args, **options):
        if not google_calendar.libraries_available():
            self.stderr.write(
                self.style.ERROR(
                    "The Google client libraries are not installed. "
                    "Run: pip install -r requirements.txt"
                )
            )
            return

        credentials = GoogleCredential.objects.all()
        if not credentials:
            self.stdout.write(self.style.WARNING("No Google credentials configured yet."))

        for credential in credentials:
            self.stdout.write(f"\nCredential: {credential.name} [{credential.auth_type}]")
            if credential.service_account_email:
                self.stdout.write(f"  Share calendars with: {credential.service_account_email}")
            try:
                result = google_calendar.check_credential(credential)
                self.stdout.write(self.style.SUCCESS(f"  {result}"))
            except Exception as exc:  # noqa: BLE001 - this command exists to report them
                result = str(exc)
                self.stderr.write(self.style.ERROR(f"  {result}"))
            GoogleCredential.objects.filter(pk=credential.pk).update(
                last_checked_at=timezone.now(), last_check_result=result
            )

        self.stdout.write("\nSending email:")
        try:
            self.stdout.write(self.style.SUCCESS(f"  {gmail.check_sending()}"))
        except Exception as exc:  # noqa: BLE001 - this command exists to report them
            self.stderr.write(self.style.ERROR(f"  {exc}"))

        self.stdout.write("\nCalendars:")
        calendars = Calendar.objects.all()
        if not calendars:
            self.stdout.write(self.style.WARNING("  None configured yet."))
        for calendar in calendars:
            if not calendar.is_google_connected:
                self.stdout.write(f"  {calendar.name}: local only (no credential)")
                continue
            try:
                result = google_calendar.check_calendar(calendar)
                Calendar.objects.filter(pk=calendar.pk).update(last_sync_error="")
                self.stdout.write(self.style.SUCCESS(f"  {calendar.name}: {result}"))
            except Exception as exc:  # noqa: BLE001
                Calendar.objects.filter(pk=calendar.pk).update(last_sync_error=str(exc)[:2000])
                self.stderr.write(self.style.ERROR(f"  {calendar.name}: {exc}"))
