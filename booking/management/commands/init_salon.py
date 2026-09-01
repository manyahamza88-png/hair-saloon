"""Create the salon settings row and a sensible default opening-hours week.

    python manage.py init_salon --name "Studio Lumiere" --demo
"""
from datetime import time

from django.core.management.base import BaseCommand

from booking.models import BusinessHours, SalonSettings, Service

DEFAULT_WEEK = {
    0: (time(9, 0), time(18, 0), False),
    1: (time(9, 0), time(18, 0), False),
    2: (time(9, 0), time(18, 0), False),
    3: (time(9, 0), time(20, 0), False),
    4: (time(9, 0), time(20, 0), False),
    5: (time(9, 0), time(16, 0), False),
    6: (time(10, 0), time(16, 0), True),  # Sunday closed
}

DEMO_SERVICES = [
    ("Cut & finish", "Wash, cut and blow dry", 45, 45),
    ("Cut & colour", "Full colour with a cut", 120, 110),
    ("Blow dry", "Wash and style", 30, 28),
    ("Highlights", "Foils and toner", 150, 140),
    ("Beard trim", "Shape and hot towel", 20, 18),
]


class Command(BaseCommand):
    help = "Create the salon settings row and default opening hours."

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None, help="Salon name.")
        parser.add_argument("--timezone", default=None, help="IANA time zone, e.g. Europe/Berlin.")
        parser.add_argument("--demo", action="store_true", help="Also create a few example services.")
        parser.add_argument("--force", action="store_true", help="Overwrite existing opening hours.")

    def handle(self, *args, **options):
        salon = SalonSettings.load()
        if options["name"]:
            salon.name = options["name"]
        if options["timezone"]:
            salon.timezone_name = options["timezone"]
        salon.save()
        self.stdout.write(self.style.SUCCESS(f"Salon settings ready: {salon.name} ({salon.timezone_name})"))

        created = 0
        for weekday, (opens, closes, closed) in DEFAULT_WEEK.items():
            defaults = {"opens_at": opens, "closes_at": closes, "is_closed": closed}
            obj, was_created = BusinessHours.objects.get_or_create(
                calendar=None, weekday=weekday, defaults=defaults
            )
            if was_created:
                created += 1
            elif options["force"]:
                for field, value in defaults.items():
                    setattr(obj, field, value)
                obj.save()
        self.stdout.write(self.style.SUCCESS(f"Default opening hours: {created} day(s) created."))

        if options["demo"]:
            for order, (name, description, minutes, price) in enumerate(DEMO_SERVICES):
                Service.objects.get_or_create(
                    name=name,
                    defaults={
                        "description": description,
                        "duration_minutes": minutes,
                        "price": price,
                        "sort_order": order,
                    },
                )
            self.stdout.write(self.style.SUCCESS(f"{len(DEMO_SERVICES)} example services ready."))

        self.stdout.write(
            "\nNext: create a superuser, then add a Google credential and a calendar in /admin/."
        )
