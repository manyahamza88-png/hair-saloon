"""Slot computation.

A slot is offered when *all* of the following hold:

1. the weekday is open (calendar-specific hours win over the salon default),
2. it is not inside a vacation / closure entry,
3. it does not overlap a pending or confirmed appointment (plus buffer),
4. it does not overlap a busy block in the linked Google Calendar,
5. it respects the minimum lead time and the maximum booking horizon.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.utils import timezone

from . import google_calendar
from .models import Appointment, BusinessHours, Calendar, SalonSettings, TimeOff


@dataclass(frozen=True)
class Slot:
    start: dt.datetime  # aware, salon time zone
    end: dt.datetime

    @property
    def label(self) -> str:
        return self.start.strftime("%H:%M")

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
            "end_label": self.end.strftime("%H:%M"),
        }


# ---------------------------------------------------------------------------
# Opening hours resolution
# ---------------------------------------------------------------------------
def hours_for(calendar: Calendar, weekday: int) -> BusinessHours | None:
    """Calendar-specific hours if defined, otherwise the salon default."""
    specific = BusinessHours.objects.filter(calendar=calendar, weekday=weekday).first()
    if specific:
        return specific
    return BusinessHours.objects.filter(calendar__isnull=True, weekday=weekday).first()


def weekly_schedule(calendar: Calendar | None = None) -> list[dict]:
    """The seven-day opening-hours table used on the homepage."""
    from .models import WEEKDAYS

    rows = []
    for weekday, label in WEEKDAYS:
        hours = (
            hours_for(calendar, weekday)
            if calendar
            else BusinessHours.objects.filter(calendar__isnull=True, weekday=weekday).first()
        )
        rows.append(
            {
                "weekday": weekday,
                "label": label,
                "hours": hours,
                "closed": hours is None or hours.is_closed,
                "is_today": weekday == timezone.localdate(timezone=SalonSettings.load().tz).weekday(),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Time off
# ---------------------------------------------------------------------------
def time_off_for(calendar: Calendar, day: dt.date) -> list[TimeOff]:
    return [
        entry
        for entry in TimeOff.objects.filter(start_date__lte=day, end_date__gte=day)
        if entry.applies_to(calendar)
    ]


def upcoming_time_off(calendar: Calendar | None = None, limit: int = 5) -> list[TimeOff]:
    today = timezone.localdate(timezone=SalonSettings.load().tz)
    qs = TimeOff.objects.filter(end_date__gte=today)
    if calendar is not None:
        qs = qs.filter(calendar__in=[calendar, None]) | qs.filter(calendar__isnull=True)
        qs = qs.distinct()
    return list(qs.order_by("start_date")[:limit])


# ---------------------------------------------------------------------------
# Busy intervals
# ---------------------------------------------------------------------------
def _merge(intervals: list[tuple[dt.datetime, dt.datetime]]) -> list[tuple[dt.datetime, dt.datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def busy_intervals(
    calendar: Calendar,
    window_start: dt.datetime,
    window_end: dt.datetime,
    salon: SalonSettings | None = None,
    include_google: bool | None = None,
    exclude_appointment_id: int | None = None,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Everything that blocks a slot inside the window, merged and sorted."""
    salon = salon or SalonSettings.load()
    buffer_delta = dt.timedelta(minutes=salon.buffer_minutes)
    blocks: list[tuple[dt.datetime, dt.datetime]] = []

    local_appointments = (
        Appointment.objects.blocking()
        .filter(calendar=calendar, start_at__lt=window_end, end_at__gt=window_start)
        .exclude(pk=exclude_appointment_id)
    )
    for appointment in local_appointments:
        blocks.append((appointment.start_at - buffer_delta, appointment.end_at + buffer_delta))

    use_google = salon.respect_google_busy if include_google is None else include_google
    if use_google:
        for busy in google_calendar.safe_free_busy(calendar, window_start, window_end):
            # Skip blocks that are our own synced events, otherwise a booking
            # would be counted twice (harmless, but it costs nothing to be tidy).
            blocks.append((busy.start - buffer_delta, busy.end + buffer_delta))

    return _merge(blocks)


# ---------------------------------------------------------------------------
# Slot generation
# ---------------------------------------------------------------------------
def day_slots(
    calendar: Calendar,
    day: dt.date,
    duration_minutes: int | None = None,
    salon: SalonSettings | None = None,
    include_google: bool | None = None,
) -> list[Slot]:
    """All bookable start times for one calendar on one day."""
    salon = salon or SalonSettings.load()
    tz = salon.tz
    duration = duration_minutes or calendar.duration_minutes(salon)
    if duration <= 0:
        return []

    if not (calendar.is_active and calendar.accepts_online_booking):
        return []

    today = timezone.localdate(timezone=tz)
    if day < today or day > today + dt.timedelta(days=salon.max_advance_days):
        return []

    hours = hours_for(calendar, day.weekday())
    if hours is None or hours.is_closed:
        return []

    # Whole-day closures short-circuit everything.
    off_entries = time_off_for(calendar, day)
    if any(entry.all_day for entry in off_entries):
        return []

    open_windows = [
        (
            dt.datetime.combine(day, start, tzinfo=tz),
            dt.datetime.combine(day, end, tzinfo=tz),
        )
        for start, end in hours.intervals()
    ]
    if not open_windows:
        return []

    window_start = min(start for start, _ in open_windows)
    window_end = max(end for _, end in open_windows)

    blocked = busy_intervals(
        calendar, window_start, window_end, salon=salon, include_google=include_google
    )
    # Partial-day time off blocks slots too.
    for entry in off_entries:
        interval = entry.blocked_interval(day)
        if interval and not entry.all_day:
            blocked.append(
                (
                    dt.datetime.combine(day, interval[0], tzinfo=tz),
                    dt.datetime.combine(day, interval[1], tzinfo=tz),
                )
            )
    blocked = _merge(blocked)

    earliest = timezone.now() + dt.timedelta(hours=salon.min_lead_time_hours)
    step = dt.timedelta(minutes=max(salon.slot_interval_minutes, 5))
    length = dt.timedelta(minutes=duration)

    slots: list[Slot] = []
    for open_start, open_end in open_windows:
        cursor = open_start
        while cursor + length <= open_end:
            slot_end = cursor + length
            if cursor >= earliest and not any(
                cursor < busy_end and busy_start < slot_end for busy_start, busy_end in blocked
            ):
                slots.append(Slot(start=cursor, end=slot_end))
            cursor += step
    return slots


def slot_is_available(
    calendar: Calendar,
    start: dt.datetime,
    duration_minutes: int,
    salon: SalonSettings | None = None,
    exclude_appointment_id: int | None = None,
) -> tuple[bool, str]:
    """Re-check a slot at submit time. Returns ``(ok, reason_if_not)``.

    This is the authoritative check: the slot list a customer sees may be a few
    minutes stale, and two people can always click the same time at once.
    """
    salon = salon or SalonSettings.load()
    tz = salon.tz
    local_start = timezone.localtime(start, tz)
    day = local_start.date()
    end = start + dt.timedelta(minutes=duration_minutes)

    if not (calendar.is_active and calendar.accepts_online_booking):
        return False, "This calendar is not accepting online bookings."

    today = timezone.localdate(timezone=tz)
    if day > today + dt.timedelta(days=salon.max_advance_days):
        return False, f"Bookings open only {salon.max_advance_days} days ahead."

    if start < timezone.now() + dt.timedelta(hours=salon.min_lead_time_hours):
        return False, (
            f"Please pick a time at least {salon.min_lead_time_hours} hour(s) from now."
        )

    hours = hours_for(calendar, day.weekday())
    if hours is None or hours.is_closed:
        return False, "We are closed on that day."

    inside = any(
        dt.datetime.combine(day, window_start, tzinfo=tz) <= local_start
        and timezone.localtime(end, tz) <= dt.datetime.combine(day, window_end, tzinfo=tz)
        for window_start, window_end in hours.intervals()
    )
    if not inside:
        return False, "That time is outside our opening hours."

    for entry in time_off_for(calendar, day):
        interval = entry.blocked_interval(day)
        if not interval:
            continue
        if entry.all_day:
            return False, f"We are closed that day{f' ({entry.reason})' if entry.reason else ''}."
        off_start = dt.datetime.combine(day, interval[0], tzinfo=tz)
        off_end = dt.datetime.combine(day, interval[1], tzinfo=tz)
        if start < off_end and off_start < end:
            return False, f"That time is blocked{f' ({entry.reason})' if entry.reason else ''}."

    for busy_start, busy_end in busy_intervals(
        calendar,
        start - dt.timedelta(hours=1),
        end + dt.timedelta(hours=1),
        salon=salon,
        exclude_appointment_id=exclude_appointment_id,
    ):
        if start < busy_end and busy_start < end:
            return False, "Sorry, that slot has just been taken. Please pick another time."

    return True, ""


def days_with_availability(
    calendar: Calendar,
    first_day: dt.date,
    number_of_days: int,
    duration_minutes: int | None = None,
    salon: SalonSettings | None = None,
) -> dict[dt.date, int]:
    """``{date: slot_count}`` for a date range: powers the month picker.

    Google free/busy is queried once for the whole range instead of once per
    day, which keeps the month view to a single API round-trip.
    """
    salon = salon or SalonSettings.load()
    tz = salon.tz
    duration = duration_minutes or calendar.duration_minutes(salon)
    last_day = first_day + dt.timedelta(days=number_of_days - 1)

    google_blocks: list[tuple[dt.datetime, dt.datetime]] = []
    if salon.respect_google_busy and calendar.is_google_connected:
        buffer_delta = dt.timedelta(minutes=salon.buffer_minutes)
        range_start = dt.datetime.combine(first_day, dt.time.min, tzinfo=tz)
        range_end = dt.datetime.combine(last_day + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
        google_blocks = [
            (busy.start - buffer_delta, busy.end + buffer_delta)
            for busy in google_calendar.safe_free_busy(calendar, range_start, range_end)
        ]

    counts: dict[dt.date, int] = {}
    for offset in range(number_of_days):
        day = first_day + dt.timedelta(days=offset)
        slots = day_slots(calendar, day, duration, salon=salon, include_google=False)
        if google_blocks:
            slots = [
                slot
                for slot in slots
                if not any(
                    slot.start < busy_end and busy_start < slot.end
                    for busy_start, busy_end in google_blocks
                )
            ]
        counts[day] = len(slots)
    return counts


def next_available_slot(
    calendar: Calendar, duration_minutes: int | None = None, search_days: int = 30
) -> Slot | None:
    salon = SalonSettings.load()
    today = timezone.localdate(timezone=salon.tz)
    horizon = min(search_days, salon.max_advance_days + 1)
    for offset in range(horizon):
        slots = day_slots(calendar, today + dt.timedelta(days=offset), duration_minutes, salon=salon)
        if slots:
            return slots[0]
    return None
