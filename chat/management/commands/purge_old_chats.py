"""Delete chat conversations (and their messages) past the retention window.

The window is admin-configurable (Chat settings -> retention days, default one
year); ``--days`` overrides it for a one-off run.

Chat transcripts are personal data, so this is not optional housekeeping: run it
daily. On PythonAnywhere, add it under the *Tasks* tab:

    cd ~/hair-saloon && python3.11 manage.py purge_old_chats
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from chat.models import ChatSettings, Conversation


class Command(BaseCommand):
    help = "Delete chat conversations older than the retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Retention window in days. Defaults to the admin-configured value.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would go, delete nothing."
        )

    def handle(self, *args, **options):
        days = options["days"] or ChatSettings.load().retention_days
        cutoff = timezone.now() - timedelta(days=days)

        old = Conversation.objects.filter(updated_at__lt=cutoff)
        conversations = old.count()
        if not conversations:
            self.stdout.write(f"Nothing older than {days} days. Nothing to do.")
            return

        messages = sum(conversation.messages.count() for conversation in old)

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Would delete {conversations} conversation(s) and {messages} message(s) "
                    f"last updated before {cutoff:%Y-%m-%d}."
                )
            )
            return

        old.delete()  # messages cascade
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {conversations} conversation(s) and {messages} message(s) "
                f"last updated before {cutoff:%Y-%m-%d}."
            )
        )
