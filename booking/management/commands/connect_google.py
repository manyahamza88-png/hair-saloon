"""One-off OAuth flow for salons that would rather not share a calendar with a
service account.

Run it on a machine with a browser (your laptop, not PythonAnywhere):

    python manage.py connect_google --name "Maria's Google" --client-secrets client_secret.json

It opens Google's consent screen, then stores the resulting refresh token as a
``GoogleCredential`` you can attach to a calendar. Copy the credential across
with ``dumpdata``/``loaddata``, or simply paste the printed refresh token into
the same field in the admin on the server.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from booking.google_calendar import SCOPES
from booking.models import GoogleCredential


class Command(BaseCommand):
    help = "Connect a Google account through OAuth and store its refresh token."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Label for the stored credential.")
        parser.add_argument(
            "--client-secrets",
            required=True,
            help="Path to the OAuth client secrets JSON downloaded from Google Cloud "
            "(application type: Desktop app).",
        )
        parser.add_argument("--port", type=int, default=8765, help="Local callback port.")

    def handle(self, *args, **options):
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:  # pragma: no cover
            raise CommandError(
                "google-auth-oauthlib is not installed. Run: pip install -r requirements.txt"
            ) from exc

        path = options["client_secrets"]
        try:
            with open(path, encoding="utf-8") as handle:
                secrets = json.load(handle)
        except OSError as exc:
            raise CommandError(f"Cannot read {path}: {exc}") from exc

        flow = InstalledAppFlow.from_client_secrets_file(path, scopes=SCOPES)
        creds = flow.run_local_server(port=options["port"], prompt="consent", access_type="offline")

        if not creds.refresh_token:
            raise CommandError(
                "Google did not return a refresh token. Revoke the app's access at "
                "https://myaccount.google.com/permissions and try again."
            )

        section = secrets.get("installed") or secrets.get("web") or {}
        account_email = ""
        try:
            from googleapiclient.discovery import build

            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            primary = service.calendars().get(calendarId="primary").execute()
            account_email = primary.get("id", "")
        except Exception:  # noqa: BLE001 - purely informational
            pass

        credential, created = GoogleCredential.objects.update_or_create(
            name=options["name"],
            defaults={
                "auth_type": GoogleCredential.OAUTH,
                "oauth_client_id": section.get("client_id", ""),
                "oauth_client_secret": section.get("client_secret", ""),
                "oauth_refresh_token": creds.refresh_token,
                "oauth_account_email": account_email,
                "is_active": True,
            },
        )
        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} credential '{credential.name}'."))
        if account_email:
            self.stdout.write(f"Connected account: {account_email}")
        self.stdout.write("\nRefresh token (paste this into the admin on your server):\n")
        self.stdout.write(creds.refresh_token)
