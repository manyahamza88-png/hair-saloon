"""Create the Google calendar for a salon calendar that does not have one yet.

    python manage.py provision_calendar --all
    python manage.py provision_calendar --slug maria --role owner

The credential creates and owns the new calendar, so nothing has to be set up
inside anybody's Google account. The owner's address is then granted access
through the API and the calendar turns up in their Google Calendar.
"""
from django.core.management.base import BaseCommand, CommandError

from booking.models import Calendar
from booking.services import provision_google_calendar


class Command(BaseCommand):
    help = "Create and share a Google calendar for calendars that have no calendar ID."

    def add_arguments(self, parser):
        parser.add_argument("--slug", help="Only this calendar (by slug).")
        parser.add_argument("--all", action="store_true", help="Every calendar without an ID.")
        parser.add_argument(
            "--role",
            default="writer",
            choices=["writer", "owner", "reader"],
            help="Access the owner email is granted (default: writer).",
        )
        parser.add_argument(
            "--no-notify", action="store_true", help="Do not email the owner about the new calendar."
        )

    def handle(self, *args, **options):
        if options["slug"]:
            calendars = Calendar.objects.filter(slug=options["slug"])
            if not calendars:
                raise CommandError(f"No calendar with slug {options['slug']!r}.")
        elif options["all"]:
            calendars = Calendar.objects.filter(google_calendar_id="")
            if not calendars:
                self.stdout.write("Every calendar already has a Google calendar ID.")
                return
        else:
            raise CommandError("Pass --slug <slug> or --all.")

        failures = 0
        for calendar in calendars:
            try:
                calendar_id = provision_google_calendar(
                    calendar, share_role=options["role"], notify=not options["no_notify"]
                )
            except ValueError as exc:
                self.stdout.write(self.style.WARNING(f"{calendar.name}: {exc}"))
                continue
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                failures += 1
                self.stderr.write(self.style.ERROR(f"{calendar.name}: {exc}"))
                continue

            self.stdout.write(self.style.SUCCESS(f"{calendar.name}: created {calendar_id}"))
            calendar.refresh_from_db()
            if calendar.last_sync_error:
                self.stdout.write(self.style.WARNING(f"  {calendar.last_sync_error}"))
            elif calendar.owner_email:
                self.stdout.write(f"  shared with {calendar.owner_email} as {options['role']}")

        if failures:
            raise CommandError(f"{failures} calendar(s) could not be created.")
