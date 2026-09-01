"""Browser-based OAuth: link a Google account from the admin panel.

The superuser pastes a **Client ID** and **Client Secret** from the Google Cloud
console once, then clicks *Connect a Google account*. Google's consent screen
grants this app full read/write access to that account's calendars, and the
resulting refresh token is stored encrypted. No JSON key files, no calendar
sharing, nothing to configure inside the Google account itself.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import secrets

from django.urls import reverse
from django.utils import timezone

from .google_calendar import SCOPES, GoogleError, GoogleNotConfigured, _wrap

logger = logging.getLogger(__name__)

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

SESSION_STATE = "google_oauth_state"
SESSION_VERIFIER = "google_oauth_code_verifier"
SESSION_CREDENTIAL = "google_oauth_credential_pk"


def force_https(url: str) -> str:
    """Force an https:// scheme.

    Behind the PythonAnywhere proxy Django can build ``http://`` URLs even
    though the browser used https. Google compares the redirect URI *exactly*,
    so without this the callback fails with ``redirect_uri_mismatch``.
    """
    if url.startswith("http://") and "127.0.0.1" not in url and "localhost" not in url:
        return "https://" + url[len("http://"):]
    return url


def callback_url(request) -> str:
    """The redirect URI to register in the Google Cloud console."""
    return force_https(request.build_absolute_uri(reverse("booking:google_callback")))


def _client_config(client_id: str, client_secret: str) -> dict:
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
        }
    }


def _build_flow(request, client_id: str, client_secret: str, state: str | None = None):
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:  # pragma: no cover
        raise GoogleNotConfigured(
            "google-auth-oauthlib is not installed. Run: pip install -r requirements.txt"
        ) from exc

    return Flow.from_client_config(
        _client_config(client_id, client_secret),
        scopes=SCOPES,
        state=state,
        redirect_uri=callback_url(request),
    )


def start(request, client_id: str, client_secret: str) -> str:
    """Begin the flow: returns the Google URL to send the browser to."""
    if not (client_id and client_secret):
        raise GoogleNotConfigured("Enter a Google Client ID and Client Secret first.")

    # Explicit PKCE. Google requires it for web application clients, and
    # google-auth-oauthlib will not add it for us here.
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )

    flow = _build_flow(request, client_id, client_secret)
    auth_url, state = flow.authorization_url(
        access_type="offline",      # ask for a refresh token
        prompt="consent",           # ...and force one even on a repeat connect
        include_granted_scopes="true",
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )
    request.session[SESSION_STATE] = state
    request.session[SESSION_VERIFIER] = code_verifier
    return auth_url


def finish(request, client_id: str, client_secret: str):
    """Exchange the callback for tokens. Returns the google-auth credentials."""
    flow = _build_flow(request, client_id, client_secret, state=request.session.get(SESSION_STATE))

    fetch_kwargs = {"authorization_response": force_https(request.build_absolute_uri())}
    code_verifier = request.session.get(SESSION_VERIFIER)
    if code_verifier:
        fetch_kwargs["code_verifier"] = code_verifier

    try:
        flow.fetch_token(**fetch_kwargs)
    except Exception as exc:  # noqa: BLE001
        raise GoogleError(f"Google refused the sign-in: {exc}") from exc
    finally:
        request.session.pop(SESSION_STATE, None)
        request.session.pop(SESSION_VERIFIER, None)

    return flow.credentials


def account_email(creds) -> str:
    """The address of the account that just connected (best effort)."""
    try:
        from googleapiclient.discovery import build

        service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        return service.userinfo().get().execute().get("email", "")
    except Exception:  # noqa: BLE001 - fall back to the calendar API
        pass
    try:
        from googleapiclient.discovery import build

        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return service.calendars().get(calendarId="primary").execute().get("id", "")
    except Exception:  # noqa: BLE001 - purely informational
        return ""


def store(credential, creds, email: str = "") -> None:
    """Persist the tokens on a ``GoogleCredential``, encrypted."""
    from .models import GoogleCredential

    expiry = creds.expiry
    if expiry is not None and timezone.is_naive(expiry):
        expiry = expiry.replace(tzinfo=dt.timezone.utc)

    credential.auth_type = GoogleCredential.OAUTH
    credential.set_access_token(creds.token or "")
    # Google only returns a refresh token on the first consent; keep the old one
    # if this was a re-authorisation that did not include a new one.
    if creds.refresh_token:
        credential.set_refresh_token(creds.refresh_token)
    credential.oauth_token_expiry = expiry
    credential.oauth_scopes = json.dumps(list(creds.scopes or []))
    credential.oauth_account_email = email or credential.oauth_account_email
    credential.connected_at = timezone.now()
    credential.is_active = True
    credential.last_checked_at = timezone.now()
    credential.last_check_result = f"Connected as {email}." if email else "Connected."
    credential.save()


def disconnect(credential) -> None:
    """Forget the tokens but keep the credential row and its calendars."""
    credential.oauth_refresh_token = ""
    credential.oauth_access_token = ""
    credential.oauth_token_expiry = None
    credential.oauth_scopes = ""
    credential.connected_at = None
    credential.last_check_result = "Disconnected."
    credential.save()


def revoke(credential) -> bool:
    """Also tell Google to drop the grant. Best effort."""
    token = credential.get_refresh_token()
    if not token:
        return False
    try:
        import urllib.parse
        import urllib.request

        data = urllib.parse.urlencode({"token": token}).encode()
        request_obj = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            return response.status == 200
    except Exception as exc:  # noqa: BLE001 - revocation is a courtesy
        logger.warning("Could not revoke Google token: %s", exc)
        return False
