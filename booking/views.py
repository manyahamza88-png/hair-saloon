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

from . import availability, services as booking_services, tokens
from .forms import BookingForm, CancelForm, DecisionForm, StaffAppointmentForm
from .models import Appointment, Calendar, SalonSettings, Service, TimeOff

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
def home(request):
    """Homepage: every configured calendar as a clickable card."""
    salon = SalonSettings.load()
    calendars = list(Calendar.objects.filter(is_active=True).prefetch_related("services"))

    cards = []
    for calendar in calendars:
        slot = availability.next_available_slot(calendar)
        cards.append(
            {
                "calendar": calendar,
                "next_slot": slot,
                "services": calendar.bookable_services()[:4],
                "duration": calendar.duration_minutes(salon),
            }
        )

    return render(
        request,
        "booking/home.html",
        {
            "salon": salon,
            "cards": cards,
            "schedule": availability.weekly_schedule(),
            "services": Service.objects.filter(is_active=True)[:12],
            "time_off": availability.upcoming_time_off(limit=3),
        },
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
            "form": BookingForm(calendar=calendar, initial={"service": selected_service}),
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
