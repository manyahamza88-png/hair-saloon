from __future__ import annotations

import datetime as dt
import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from django.conf import settings as settings_module

from . import (
    availability,
    gmail,
    week as week_module,
    google_calendar,
    google_oauth,
    services as booking_services,
    tokens,
)
from .forms import BookingForm, CancelForm, DecisionForm, StaffAppointmentForm
from .models import (
    Appointment,
    Calendar,
    GoogleCredential,
    GoogleOAuthClientSettings,
    SalonSettings,
    Service,
    TimeOff,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
def home(request):
    """Homepage: pick a service first, then who offers it.

    A salon that has not set up any services yet falls back to the previous
    behaviour -- every calendar as a clickable card -- so booking never breaks
    just because nobody has visited the admin's Services page.
    """
    salon = SalonSettings.load()
    services = list(Service.objects.filter(is_active=True))

    cards = []
    if not services:
        for calendar in Calendar.objects.filter(is_active=True).prefetch_related("services"):
            cards.append(
                {
                    "calendar": calendar,
                    "next_slot": availability.next_available_slot(calendar),
                    "services": calendar.bookable_services()[:4],
                    "duration": calendar.duration_minutes(salon),
                }
            )

    return render(
        request,
        "booking/home.html",
        {
            "salon": salon,
            "services": services,
            "cards": cards,
            "schedule": availability.weekly_schedule(),
            "time_off": availability.upcoming_time_off(limit=3),
        },
    )


def service_calendars(request, service_id):
    """Step 2 of the service-first flow: who offers the chosen service."""
    salon = SalonSettings.load()
    service = get_object_or_404(Service, pk=service_id, is_active=True)

    cards = [
        {
            "calendar": calendar,
            "next_slot": availability.next_available_slot(calendar, service.duration_minutes),
        }
        for calendar in availability.calendars_offering(service)
    ]

    return render(
        request,
        "booking/service_calendars.html",
        {"salon": salon, "service": service, "cards": cards},
    )


def calendar_detail(request, slug):
    """Booking page for one calendar: pick a service, a day, then a slot."""
    salon = SalonSettings.load()
    calendar = get_object_or_404(Calendar, slug=slug, is_active=True)

    service_id = request.GET.get("service")
    selected_service = None
    if service_id:
        selected_service = calendar.bookable_services().filter(pk=service_id).first()

    duration = selected_service.duration_minutes if selected_service else calendar.duration_minutes(salon)

    today = timezone.localdate(timezone=salon.tz)
    try:
        selected_day = dt.date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        selected_day = None
    if selected_day is None or selected_day < today:
        selected_day = today
    horizon = today + dt.timedelta(days=salon.max_advance_days)
    if selected_day > horizon:
        selected_day = horizon

    window_days = 14
    counts = availability.days_with_availability(
        calendar, today, min(window_days + 21, salon.max_advance_days + 1), duration, salon=salon
    )
    day_strip = [
        {
            "date": day,
            "count": count,
            "is_selected": day == selected_day,
            "is_today": day == today,
        }
        for day, count in list(counts.items())[:window_days]
    ]

    slots = availability.day_slots(calendar, selected_day, duration, salon=salon)

    return render(
        request,
        "booking/calendar_detail.html",
        {
            "salon": salon,
            "calendar": calendar,
            "services": calendar.bookable_services(),
            "selected_service": selected_service,
            "duration": duration,
            "selected_day": selected_day,
            "day_strip": day_strip,
            "slots": slots,
            "today": today,
            "max_date": horizon,
            "schedule": availability.weekly_schedule(calendar),
            "time_off": availability.upcoming_time_off(calendar, limit=3),
            "form": BookingForm(
                calendar=calendar,
                initial={
                    "service": selected_service,
                    # Carried over from the chat widget, which already knows
                    # who it is talking to by the time it opens this page.
                    "customer_name": request.GET.get("name", "")[:120],
                    "customer_email": request.GET.get("email", "")[:254],
                },
            ),
        },
    )


def slots_api(request, slug):
    """JSON slots for one day: used by the date picker without a page reload."""
    salon = SalonSettings.load()
    calendar = get_object_or_404(Calendar, slug=slug, is_active=True)
    try:
        day = dt.date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        return JsonResponse({"error": "A valid ?date=YYYY-MM-DD is required."}, status=400)

    service = None
    if request.GET.get("service"):
        service = calendar.bookable_services().filter(pk=request.GET["service"]).first()
    duration = service.duration_minutes if service else calendar.duration_minutes(salon)

    slots = availability.day_slots(calendar, day, duration, salon=salon)
    return JsonResponse(
        {
            "calendar": calendar.name,
            "date": day.isoformat(),
            "duration": duration,
            "slots": [slot.as_dict() for slot in slots],
        }
    )


def month_api(request, slug):
    """Per-day slot counts for a month: greys out unavailable days."""
    salon = SalonSettings.load()
    calendar = get_object_or_404(Calendar, slug=slug, is_active=True)
    try:
        year = int(request.GET.get("year"))
        month = int(request.GET.get("month"))
        first = dt.date(year, month, 1)
    except (TypeError, ValueError):
        return JsonResponse({"error": "?year= and ?month= are required."}, status=400)

    next_month = dt.date(year + (month == 12), (month % 12) + 1, 1)
    days_in_month = (next_month - first).days

    service = None
    if request.GET.get("service"):
        service = calendar.bookable_services().filter(pk=request.GET["service"]).first()
    duration = service.duration_minutes if service else calendar.duration_minutes(salon)

    counts = availability.days_with_availability(calendar, first, days_in_month, duration, salon=salon)
    return JsonResponse({"days": {day.isoformat(): count for day, count in counts.items()}})


@require_POST
def book(request, slug):
    """Handle the booking form submission."""
    salon = SalonSettings.load()
    calendar = get_object_or_404(Calendar, slug=slug, is_active=True)
    if not calendar.accepts_online_booking:
        messages.error(request, "This calendar is not taking online bookings right now.")
        return redirect(calendar.get_absolute_url())

    form = BookingForm(request.POST, calendar=calendar)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, error)
        start_raw = request.POST.get("start", "")
        selected_day = timezone.localdate(timezone=salon.tz)
        try:
            selected_day = dt.datetime.fromisoformat(start_raw).date()
        except ValueError:
            pass
        duration = form.data.get("service")
        service = calendar.bookable_services().filter(pk=duration).first() if duration else None
        minutes = service.duration_minutes if service else calendar.duration_minutes(salon)
        return render(
            request,
            "booking/calendar_detail.html",
            {
                "salon": salon,
                "calendar": calendar,
                "services": calendar.bookable_services(),
                "selected_service": service,
                "duration": minutes,
                "selected_day": selected_day,
                "day_strip": [],
                "slots": availability.day_slots(calendar, selected_day, minutes, salon=salon),
                "today": timezone.localdate(timezone=salon.tz),
                "max_date": timezone.localdate(timezone=salon.tz) + dt.timedelta(days=salon.max_advance_days),
                "schedule": availability.weekly_schedule(calendar),
                "time_off": availability.upcoming_time_off(calendar, limit=3),
                "form": form,
            },
            status=400,
        )

    appointment = booking_services.create_appointment(
        calendar=calendar,
        service=form.cleaned_data.get("service"),
        start_at=form.cleaned_data["start"],
        end_at=form.cleaned_data["end"],
        customer_name=form.cleaned_data["customer_name"],
        customer_email=form.cleaned_data["customer_email"],
        customer_phone=form.cleaned_data.get("customer_phone", ""),
        notes=form.cleaned_data.get("notes", ""),
    )
    return redirect("booking:booking_done", public_id=appointment.public_id)


def booking_done(request, public_id):
    appointment = get_object_or_404(Appointment, public_id=public_id)
    return render(
        request,
        "booking/booking_done.html",
        {
            "appointment": appointment,
            "salon": SalonSettings.load(),
            "cancel_url": tokens.cancel_url(appointment),
        },
    )


def appointment_detail(request, public_id):
    appointment = get_object_or_404(Appointment, public_id=public_id)
    return render(
        request,
        "booking/appointment_detail.html",
        {
            "appointment": appointment,
            "salon": SalonSettings.load(),
            "cancel_url": tokens.cancel_url(appointment),
        },
    )


# ---------------------------------------------------------------------------
# Accept / decline from the notification email
# ---------------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
def decide(request, token):
    """Landing page for the accept / decline links.

    The link only opens a confirmation page; the state change happens on POST
    so that a mail client prefetching the URL cannot accept a booking.
    """
    try:
        public_id, action = tokens.read_decision_token(token)
    except signing.SignatureExpired:
        return render(request, "booking/decision_invalid.html", {"expired": True}, status=410)
    except signing.BadSignature:
        raise Http404("Invalid or tampered link.")

    appointment = get_object_or_404(Appointment, public_id=public_id)
    form = DecisionForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        note = form.cleaned_data.get("note", "")
        if action == tokens.ACCEPT:
            booking_services.confirm_appointment(appointment, note=note)
            messages.success(request, "Appointment accepted. The customer has been emailed.")
        else:
            booking_services.decline_appointment(appointment, note=note)
            messages.info(request, "Appointment declined. The customer has been emailed.")
        return redirect("booking:decision_done", public_id=appointment.public_id)

    return render(
        request,
        "booking/decide.html",
        {
            "appointment": appointment,
            "action": action,
            "is_accept": action == tokens.ACCEPT,
            "form": form,
            "salon": SalonSettings.load(),
            "already_decided": appointment.status != Appointment.PENDING,
        },
    )


def decision_done(request, public_id):
    appointment = get_object_or_404(Appointment, public_id=public_id)
    return render(
        request,
        "booking/decision_done.html",
        {"appointment": appointment, "salon": SalonSettings.load()},
    )


@require_http_methods(["GET", "POST"])
def cancel(request, token):
    """Customer-facing cancellation link (also included in owner emails)."""
    try:
        public_id = tokens.read_cancel_token(token)
    except signing.SignatureExpired:
        return render(request, "booking/decision_invalid.html", {"expired": True}, status=410)
    except signing.BadSignature:
        raise Http404("Invalid or tampered link.")

    appointment = get_object_or_404(Appointment, public_id=public_id)
    form = CancelForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        if appointment.is_open:
            booking_services.cancel_appointment(
                appointment, note=form.cleaned_data.get("reason", ""), by_customer=True
            )
            messages.success(request, "Your appointment has been cancelled.")
        return redirect("booking:appointment_detail", public_id=appointment.public_id)

    return render(
        request,
        "booking/cancel.html",
        {"appointment": appointment, "form": form, "salon": SalonSettings.load()},
    )


# ---------------------------------------------------------------------------
# Staff dashboard (Django admin handles the configuration itself)
# ---------------------------------------------------------------------------
@staff_member_required
def dashboard(request):
    salon = SalonSettings.load()
    now = timezone.now()
    today = timezone.localdate(timezone=salon.tz)

    pending = (
        Appointment.objects.filter(status=Appointment.PENDING, end_at__gte=now)
        .select_related("calendar", "service")
        .order_by("start_at")
    )
    upcoming = (
        Appointment.objects.filter(status=Appointment.CONFIRMED, end_at__gte=now)
        .select_related("calendar", "service")
        .order_by("start_at")[:25]
    )
    todays = (
        Appointment.objects.blocking()
        .filter(start_at__date=today)
        .select_related("calendar")
        .order_by("start_at")
    )

    calendars = Calendar.objects.annotate(
        pending_count=Count("appointments", filter=Q(appointments__status=Appointment.PENDING))
    )

    from chat.models import ChatSettings, Conversation

    chat_settings = ChatSettings.load()
    chat_status = chat_settings.status()
    waiting_chats = Conversation.objects.waiting().count()
    live_chats = Conversation.objects.filter(status=Conversation.STATUS_ACCEPTED).count()

    return render(
        request,
        "booking/dashboard.html",
        {
            "salon": salon,
            "pending": pending,
            "upcoming": upcoming,
            "todays": todays,
            "calendars": calendars,
            "time_off": availability.upcoming_time_off(limit=8),
            "today": today,
            "form": StaffAppointmentForm(),
            "chat_settings": chat_settings,
            "chat_status": chat_status,
            "waiting_chats": waiting_chats,
            "live_chats": live_chats,
        },
    )


@staff_member_required
@require_POST
def dashboard_decide(request, public_id):
    appointment = get_object_or_404(Appointment, public_id=public_id)
    action = request.POST.get("action")
    note = request.POST.get("note", "")

    if action == "accept":
        booking_services.confirm_appointment(appointment, note=note)
        messages.success(request, f"Accepted {appointment.customer_name}'s appointment.")
    elif action == "decline":
        booking_services.decline_appointment(appointment, note=note)
        messages.info(request, f"Declined {appointment.customer_name}'s appointment.")
    elif action == "cancel":
        booking_services.cancel_appointment(appointment, note=note, by_customer=False)
        messages.info(request, f"Cancelled {appointment.customer_name}'s appointment.")
    elif action == "resync":
        ok = booking_services.resync_appointment(appointment)
        if ok:
            messages.success(request, "Re-synced with Google Calendar.")
        else:
            messages.error(request, appointment.google_sync_error or "Google sync failed.")
    else:
        messages.error(request, "Unknown action.")

    return redirect("booking:dashboard")


@staff_member_required
def week_view(request):
    """The staff timetable: one week, every calendar, appointments + Google."""
    salon = SalonSettings.load()

    try:
        start = dt.date.fromisoformat(request.GET.get("week", ""))
    except ValueError:
        start = None

    selected_ids = [v for v in request.GET.getlist("calendar") if v.isdigit()]
    all_calendars = list(Calendar.objects.filter(is_active=True))
    chosen = [c for c in all_calendars if str(c.pk) in selected_ids] or all_calendars

    data = week_module.staff_week(
        week_start=start,
        calendars=chosen,
        include_google=request.GET.get("google") != "0",
        salon=salon,
    )
    # Keep the calendar filter when stepping to another week.
    suffix = "".join(f"&calendar={c.pk}" for c in chosen) if len(chosen) < len(all_calendars) else ""
    if request.GET.get("google") == "0":
        suffix += "&google=0"

    data.update(
        {
            "salon": salon,
            "all_calendars": all_calendars,
            "selected_ids": [c.pk for c in chosen],
            "google_off": request.GET.get("google") == "0",
            "query_suffix": suffix,
        }
    )
    return render(request, "booking/week.html", data)


# ---------------------------------------------------------------------------
# Google setup: client credentials, account linking, calendar picker
# ---------------------------------------------------------------------------
OAUTH_CREDENTIAL_NAME = "Connected Google account"


def _oauth_credentials():
    """Every Google account linked through the browser flow."""
    return GoogleCredential.objects.filter(auth_type=GoogleCredential.OAUTH).order_by(
        "-is_default_sender", "name"
    )


def _oauth_credential(create: bool = False):
    """The first linked account, for callers that just need any of them."""
    credential = _oauth_credentials().first()
    if credential is None and create:
        credential = GoogleCredential.objects.create(
            name=OAUTH_CREDENTIAL_NAME, auth_type=GoogleCredential.OAUTH, is_default_sender=True
        )
    return credential


def _credential_for_account(email: str, reconnecting=None):
    """Find or create the credential row for one Google account.

    Keyed on the address Google reports, so re-authorising the same account
    updates it in place while connecting a second account adds a row of its own
    -- which is what lets each stylist link their own calendar.
    """
    if reconnecting is not None:
        return reconnecting
    if email:
        existing = _oauth_credentials().filter(oauth_account_email__iexact=email).first()
        if existing:
            return existing
    return GoogleCredential.objects.create(
        name=email or OAUTH_CREDENTIAL_NAME,
        auth_type=GoogleCredential.OAUTH,
        oauth_account_email=email,
        # The first account linked becomes the one salon email is sent from.
        is_default_sender=not _oauth_credentials().exists(),
    )


@staff_member_required
def google_setup(request):
    """One page for the whole Google integration."""
    client = GoogleOAuthClientSettings.load()
    credentials = list(_oauth_credentials())

    linked = {
        calendar.google_calendar_id: calendar
        for calendar in Calendar.objects.exclude(google_calendar_id="")
    }

    # One block per connected Google account, so each stylist's calendars sit
    # under the account they belong to.
    accounts = []
    for credential in credentials:
        google_calendars, listing_error = [], ""
        if credential.is_connected:
            try:
                google_calendars = google_calendar.list_calendars(credential)
            except Exception as exc:  # noqa: BLE001 - shown on the page
                listing_error = str(exc)
        for item in google_calendars:
            item["linked"] = linked.get(item["id"])
            item["writable"] = item.get("access_role") in {"owner", "writer"}
        accounts.append(
            {
                "credential": credential,
                "calendars": google_calendars,
                "error": listing_error,
                "can_send_email": gmail.can_send_email(credential),
                "site_calendars": list(credential.calendars.all()),
            }
        )

    # A calendar with no account attached -- or one with an account but no
    # actual Google calendar ID picked -- writes nothing to Google. Both are
    # the same quiet failure worth shouting about on this page.
    unlinked = list(
        Calendar.objects.filter(is_active=True).filter(
            Q(credential__isnull=True) | Q(google_calendar_id="")
        )
    )

    first = credentials[0] if credentials else None
    return render(
        request,
        "booking/google_setup.html",
        {
            "salon": SalonSettings.load(),
            "client": client,
            "accounts": accounts,
            "unlinked": unlinked,
            "credential": first,
            "connected": any(a["credential"].is_connected for a in accounts),
            "can_send_email": any(a["can_send_email"] for a in accounts),
            # Django always defines EMAIL_HOST ("localhost"), so the backend in
            # use is the honest signal for whether SMTP is configured.
            "smtp_configured": "smtp" in settings_module.EMAIL_BACKEND.lower(),
            "callback_url": google_oauth.callback_url(request),
            "env_fallback": bool(
                not client.client_id and settings_module.GOOGLE_OAUTH_CLIENT_ID
            ),
        },
    )


@staff_member_required
@require_POST
def google_client_save(request):
    """Store the Client ID / Client Secret from the Cloud console."""
    client = GoogleOAuthClientSettings.load()
    client_id = request.POST.get("client_id", "").strip()
    secret = request.POST.get("client_secret", "").strip()

    client.client_id = client_id
    # An empty secret box means "keep the one already stored", so that the page
    # can be re-saved without retyping it.
    if secret:
        client.set_client_secret(secret)
    elif not client_id:
        client.set_client_secret("")
    client.save()

    if client.is_configured:
        messages.success(request, "Google client saved. You can connect an account now.")
    else:
        messages.warning(request, "Saved, but both a Client ID and a Client Secret are needed.")
    return redirect("booking:google_setup")


@staff_member_required
def google_connect(request):
    """Send the admin to Google's consent screen.

    ``?credential=<pk>`` re-authorises that account in place. Without it, a new
    account is linked alongside the existing ones, which is how a second
    stylist connects their own calendar.
    """
    client_id, client_secret = GoogleOAuthClientSettings.effective()
    reconnect = request.GET.get("credential", "")
    request.session[google_oauth.SESSION_CREDENTIAL] = (
        int(reconnect) if reconnect.isdigit() else None
    )
    try:
        auth_url = google_oauth.start(request, client_id, client_secret)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
        return redirect("booking:google_setup")
    return redirect(auth_url)


@staff_member_required
def google_callback(request):
    """Where Google sends the browser back after consent."""
    if request.GET.get("error"):
        messages.error(request, f"Google sign-in was cancelled: {request.GET['error']}")
        return redirect("booking:google_setup")

    client_id, client_secret = GoogleOAuthClientSettings.effective()
    try:
        creds = google_oauth.finish(request, client_id, client_secret)
        email = google_oauth.account_email(creds)
        reconnect_pk = request.session.pop(google_oauth.SESSION_CREDENTIAL, None)
        reconnecting = (
            GoogleCredential.objects.filter(pk=reconnect_pk).first() if reconnect_pk else None
        )
        credential = _credential_for_account(email, reconnecting=reconnecting)
        if not creds.refresh_token and not credential.get_refresh_token():
            messages.error(
                request,
                "Google did not return a refresh token. Remove this app at "
                "myaccount.google.com/permissions and connect again.",
            )
            return redirect("booking:google_setup")
        google_oauth.store(credential, creds, email)
    except Exception as exc:  # noqa: BLE001 - reported on the page
        logger.exception("Google OAuth callback failed")
        messages.error(request, f"Could not connect: {exc}")
        return redirect("booking:google_setup")

    messages.success(
        request,
        f"Connected to {email or 'your Google account'}. Now add the calendars you want to show.",
    )
    return redirect("booking:google_setup")


@staff_member_required
@require_POST
def google_disconnect(request):
    pk = request.POST.get("credential", "")
    credential = (
        _oauth_credentials().filter(pk=pk).first() if pk.isdigit() else _oauth_credential()
    )
    if credential:
        if request.POST.get("revoke"):
            google_oauth.revoke(credential)
        google_oauth.disconnect(credential)
        messages.info(
            request,
            f"Disconnected {credential.oauth_account_email or 'the Google account'}. "
            "Its calendars and bookings are untouched, but they will stop syncing.",
        )
    return redirect("booking:google_setup")


@staff_member_required
@require_POST
def google_add_calendar(request):
    """Add one of a connected account's calendars to the homepage."""
    pk = request.POST.get("credential", "")
    credential = (
        _oauth_credentials().filter(pk=pk).first() if pk.isdigit() else _oauth_credential()
    )
    if not (credential and credential.is_connected):
        messages.error(request, "Connect a Google account first.")
        return redirect("booking:google_setup")

    calendar_id = request.POST.get("calendar_id", "").strip()
    name = request.POST.get("name", "").strip()
    owner_email = request.POST.get("owner_email", "").strip() or credential.oauth_account_email

    if not calendar_id or not name:
        messages.error(request, "A calendar and a display name are both required.")
        return redirect("booking:google_setup")

    if Calendar.objects.filter(google_calendar_id=calendar_id).exists():
        messages.warning(request, f"{name}: that Google calendar is already on the site.")
        return redirect("booking:google_setup")

    calendar = Calendar.objects.create(
        name=name,
        google_calendar_id=calendar_id,
        credential=credential,
        owner_email=owner_email,
        sort_order=Calendar.objects.count(),
    )
    messages.success(
        request,
        f"Added '{calendar.name}' to the homepage. Set its opening hours and photo any time.",
    )
    return redirect("booking:google_setup")


@staff_member_required
@require_POST
def google_link_calendar(request):
    """Attach an existing site calendar to a connected Google account.

    Without this, a calendar created before any account was linked (the sample
    one, or one added by hand in the admin) silently writes nothing to Google.
    """
    calendar = get_object_or_404(Calendar, pk=request.POST.get("calendar"))
    pk = request.POST.get("credential", "")
    credential = _oauth_credentials().filter(pk=pk).first() if pk.isdigit() else None
    google_id = request.POST.get("google_calendar_id", "").strip()

    if credential is None or not credential.is_connected:
        messages.error(request, "Pick a connected Google account.")
        return redirect("booking:google_setup")
    if not google_id:
        messages.error(request, "Pick which Google calendar it should write to.")
        return redirect("booking:google_setup")

    clash = Calendar.objects.filter(google_calendar_id=google_id).exclude(pk=calendar.pk).first()
    if clash:
        messages.warning(request, f"That Google calendar is already used by '{clash.name}'.")
        return redirect("booking:google_setup")

    calendar.credential = credential
    calendar.google_calendar_id = google_id
    calendar.last_sync_error = ""
    calendar.save(update_fields=["credential", "google_calendar_id", "last_sync_error"])

    # Push anything already booked, so the stylist's phone catches up at once.
    pushed = sum(
        1
        for appointment in calendar.appointments.blocking().upcoming()
        if booking_services.resync_appointment(appointment)
    )
    messages.success(
        request,
        f"'{calendar.name}' now writes to Google."
        + (f" {pushed} existing appointment(s) pushed." if pushed else ""),
    )
    return redirect("booking:google_setup")


@staff_member_required
@require_POST
def dashboard_add(request):
    form = StaffAppointmentForm(request.POST)
    if form.is_valid():
        appointment = form.save()
        booking_services.resync_appointment(appointment)
        messages.success(request, "Appointment added.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")
    return redirect("booking:dashboard")
