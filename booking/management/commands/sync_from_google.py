"""Read staff edits back from Google Calendar.

Staff work from Google Calendar on their phones, so that is where an
appointment actually gets moved or deleted. This pulls those edits back:

* event deleted in Google  -> the booking is cancelled and the customer told,
                              so the slot is not blocked by a ghost
* event moved in Google    -> the booking's time follows it

Run it regularly. On PythonAnywhere, put it on the *Tasks* tab -- a free
account gets one daily task, so combine it with the chat purge:

    cd ~/hair-saloon && python3.11 manage.py sync_from_google && \\
                       python3.11 manage.py purge_old_chats
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from booking.models import Appointment
from booking.services import reconcile_with_google


class Command(BaseCommand):
    help = "Apply edits made in Google Calendar back to the bookings here."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=60,
            help="How far ahead to check (default 60).",
        )
        parser.add_argument(
            "--quiet-emails", action="store_true",
            help="Apply the changes without emailing customers about them.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report differences, change nothing."
        )

    def handle(self, *args, **options):
        from datetime import timedelta

        horizon = timezone.now() + timedelta(days=options["days"])
        appointments = (
            Appointment.objects.blocking()
            .filter(start_at__gte=timezone.now(), start_at__lte=horizon)
            .exclude(google_event_id="")
            .select_related("calendar", "calendar__credential")
            .order_by("start_at")
        )

        if not appointments:
            self.stdout.write("Nothing synced to Google in that window.")
            return

        counts = {"none": 0, "deleted": 0, "moved": 0, "error": 0}
        for appointment in appointments:
            if options["dry_run"]:
                from booking.google_calendar import read_back

                change = read_back(appointment)["change"]
            else:
                change = reconcile_with_google(
                    appointment, notify=not options["quiet_emails"]
                )
            counts[change] = counts.get(change, 0) + 1

            if change == "deleted":
                self.stdout.write(
                    self.style.WARNING(
                        f"  deleted in Google -> cancelled: {appointment.customer_name}, "
                        f"{timezone.localtime(appointment.start_at):%d %b %H:%M}"
                    )
                )
            elif change == "moved":
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  moved in Google -> updated: {appointment.customer_name}, "
                        f"now {timezone.localtime(appointment.start_at):%d %b %H:%M}"
                    )
                )
            elif change == "error":
                self.stderr.write(
                    self.style.ERROR(f"  {appointment.customer_name}: {appointment.google_sync_error}")
                )

        verb = "Would apply" if options["dry_run"] else "Applied"
        self.stdout.write(
            f"\n{verb}: {counts['deleted']} cancelled, {counts['moved']} moved, "
            f"{counts['none']} unchanged, {counts['error']} error(s), "
            f"out of {len(appointments)} checked."
        )
