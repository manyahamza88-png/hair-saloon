"""Week data for the staff timetable.

Mirrors the shape of the week view in the sister project: one call returns a
whole week with previous/next navigation, so the template stays dumb.

What it merges into one grid:

* appointments from this database (pending and confirmed),
* events that live in the stylist's own Google Calendar and were not created
  here -- the dentist appointment they added by hand,
* opening hours, so closed time is shaded rather than blank,
* salon-wide and per-stylist time off.

Google is queried once per calendar for the whole week, not once per day.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from django.utils import timezone

from . import google_calendar
from .availability import hours_for, time_off_for
from .models import Appointment, Calendar, SalonSettings

# The grid always spans at least this, so a quiet week still looks like a day.
DEFAULT_GRID_START = dt.time(8, 0)
DEFAULT_GRID_END = dt.time(20, 0)


def week_start_for(day: dt.date) -> dt.date:
    """The Monday of the week containing ``day``."""
    return day - dt.timedelta(days=day.weekday())


@dataclass
class Entry:
    """One block drawn on the grid."""

    kind: str  # "appointment" | "external" | "timeoff"
    label: str
    sublabel: str
    start: dt.datetime
    end: dt.datetime
    calendar_id: int | None = None
    calendar_name: str = ""
    colour: str = "#b0855b"
    status: str = ""
    public_id: str = ""

    # Position within the grid, as percentages, so the template needs no maths.
    top_pct: float = 0.0
    height_pct: float = 0.0

    @property
    def time_label(self) -> str:
        return f"{timezone.localtime(self.start):%H:%M}"

    @property
    def is_pending(self) -> bool:
        return self.status == Appointment.PENDING


@dataclass
class Day:
    date: dt.date
    is_today: bool
    is_closed: bool
    closed_reason: str = ""
    opens_at: dt.time | None = None
    closes_at: dt.time | None = None
    entries: list[Entry] = field(default_factory=list)

    @property
    def weekday_label(self) -> str:
        return self.date.strftime("%a")

    @property
    def appointment_count(self) -> int:
        return sum(1 for e in self.entries if e.kind == "appointment")


def _minutes(value: dt.time) -> int:
    return value.hour * 60 + value.minute


def staff_week(
    week_start: dt.date | None = None,
    calendars=None,
    include_google: bool = True,
    salon: SalonSettings | None = None,
) -> dict:
    """Everything the weekly timetable needs."""
    salon = salon or SalonSettings.load()
    tz = salon.tz
    today = timezone.localdate(timezone=tz)
    week_start = week_start or week_start_for(today)
    week_start = week_start_for(week_start)
    week_end = week_start + dt.timedelta(days=7)

    if calendars is None:
        calendars = list(Calendar.objects.filter(is_active=True))
    else:
        calendars = list(calendars)
    calendar_ids = [c.pk for c in calendars]

    range_start = dt.datetime.combine(week_start, dt.time.min, tzinfo=tz)
    range_end = dt.datetime.combine(week_end, dt.time.min, tzinfo=tz)

    # --- appointments, one query for the whole week ------------------------
    appointments = (
        Appointment.objects.blocking()
        .filter(calendar_id__in=calendar_ids, start_at__lt=range_end, end_at__gt=range_start)
        .select_related("calendar", "service")
        .order_by("start_at")
    )

    by_day: dict[dt.date, list[Entry]] = {
        week_start + dt.timedelta(days=i): [] for i in range(7)
    }

    for appointment in appointments:
        local_start = timezone.localtime(appointment.start_at, tz)
        day = local_start.date()
        if day not in by_day:
            continue
        by_day[day].append(
            Entry(
                kind="appointment",
                label=appointment.customer_name,
                sublabel=appointment.service_name,
                start=appointment.start_at,
                end=appointment.end_at,
                calendar_id=appointment.calendar_id,
                calendar_name=appointment.calendar.name,
                colour=appointment.calendar.colour,
                status=appointment.status,
                public_id=str(appointment.public_id),
            )
        )

    # --- events the stylist put in Google themselves -----------------------
    google_errors = []
    if include_google:
        for calendar in calendars:
            if not calendar.is_google_connected:
                continue
            for event in google_calendar.safe_list_events(calendar, range_start, range_end):
                if event.all_day:
                    continue  # all-day entries are shown as a day banner, not a block
                day = timezone.localtime(event.start, tz).date()
                if day not in by_day:
                    continue
                by_day[day].append(
                    Entry(
                        kind="external",
                        label=event.summary,
                        sublabel=f"in {calendar.name}'s Google Calendar",
                        start=event.start,
                        end=event.end,
                        calendar_id=calendar.pk,
                        calendar_name=calendar.name,
                        colour=calendar.colour,
                    )
                )
            if calendar.last_sync_error:
                google_errors.append((calendar.name, calendar.last_sync_error))

    # --- grid bounds: widen to fit the opening hours and everything booked --
    grid_start = _minutes(DEFAULT_GRID_START)
    grid_end = _minutes(DEFAULT_GRID_END)
    for offset in range(7):
        day = week_start + dt.timedelta(days=offset)
        for calendar in calendars:
            hours = hours_for(calendar, day.weekday())
            if hours and not hours.is_closed:
                grid_start = min(grid_start, _minutes(hours.opens_at))
                grid_end = max(grid_end, _minutes(hours.closes_at))
    for entries in by_day.values():
        for entry in entries:
            grid_start = min(grid_start, _minutes(timezone.localtime(entry.start, tz).time()))
            local_end = timezone.localtime(entry.end, tz)
            end_minutes = _minutes(local_end.time()) or 24 * 60
            grid_end = max(grid_end, end_minutes)
    grid_start = max(0, (grid_start // 60) * 60)
    grid_end = min(24 * 60, -(-grid_end // 60) * 60)
    span = max(grid_end - grid_start, 60)

    # --- position every entry, and describe each day -----------------------
    days = []
    for offset in range(7):
        date_ = week_start + dt.timedelta(days=offset)
        entries = sorted(by_day[date_], key=lambda e: e.start)
        for entry in entries:
            local_start = timezone.localtime(entry.start, tz)
            local_end = timezone.localtime(entry.end, tz)
            start_min = max(_minutes(local_start.time()), grid_start)
            end_min = _minutes(local_end.time()) or 24 * 60
            if local_end.date() > date_:
                end_min = 24 * 60
            end_min = min(max(end_min, start_min + 15), grid_end)
            entry.top_pct = round((start_min - grid_start) / span * 100, 3)
            entry.height_pct = round((end_min - start_min) / span * 100, 3)

        # A day counts as closed when no shown calendar is open on it.
        open_windows = [
            hours_for(calendar, date_.weekday())
            for calendar in calendars
        ]
        open_windows = [h for h in open_windows if h and not h.is_closed]
        closed_reason = ""
        if open_windows:
            off = []
            for calendar in calendars:
                off.extend(entry for entry in time_off_for(calendar, date_) if entry.all_day)
            if off and len(off) >= len(calendars):
                open_windows = []
                closed_reason = off[0].reason or "Closed"

        days.append(
            Day(
                date=date_,
                is_today=date_ == today,
                is_closed=not open_windows,
                closed_reason=closed_reason,
                opens_at=min((h.opens_at for h in open_windows), default=None),
                closes_at=max((h.closes_at for h in open_windows), default=None),
                entries=entries,
            )
        )

    hour_marks = [
        {
            "label": f"{hour:02d}:00",
            "top_pct": round((hour * 60 - grid_start) / span * 100, 3),
        }
        for hour in range(grid_start // 60, grid_end // 60 + 1)
        if grid_start <= hour * 60 <= grid_end
    ]

    now_pct = None
    if week_start <= today < week_start + dt.timedelta(days=7):
        now_local = timezone.localtime(timezone.now(), tz)
        now_minutes = _minutes(now_local.time())
        if grid_start <= now_minutes <= grid_end:
            now_pct = round((now_minutes - grid_start) / span * 100, 3)

    return {
        "week_start": week_start,
        "week_end": week_start + dt.timedelta(days=6),
        "prev_week": week_start - dt.timedelta(days=7),
        "next_week": week_start + dt.timedelta(days=7),
        "this_week": week_start_for(today),
        "is_current_week": week_start == week_start_for(today),
        "days": days,
        "calendars": calendars,
        "hour_marks": hour_marks,
        "now_pct": now_pct,
        "today": today,
        "google_errors": google_errors,
        "total_appointments": sum(day.appointment_count for day in days),
    }
