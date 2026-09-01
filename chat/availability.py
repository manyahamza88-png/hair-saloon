"""When is live chat actually available?

Two controls, and the switch always wins:

* the **master switch** (``ChatSettings.enabled``) -- off means off, whatever
  the clock says. This is the "we are slammed, stop the chats" button.
* **follow business hours** -- when on, chat is only offered while the salon is
  open, reusing the same ``BusinessHours`` and ``TimeOff`` rows that drive the
  booking calendar. One place to edit hours, one place to mark a holiday.
"""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

from booking.models import BusinessHours, SalonSettings, TimeOff

SEARCH_DAYS = 14


def _salon_hours(weekday: int) -> BusinessHours | None:
    """The salon-wide opening hours for a weekday (no per-calendar overrides)."""
    return BusinessHours.objects.filter(calendar__isnull=True, weekday=weekday).first()


def _salon_time_off(day: dt.date) -> list[TimeOff]:
    """Closures that apply to the whole salon on ``day``."""
    return list(
        TimeOff.objects.filter(
            calendar__isnull=True, start_date__lte=day, end_date__gte=day
        )
    )


def _is_open_at(moment: dt.datetime, tz) -> bool:
    """Is the salon open at this exact local moment?"""
    local = timezone.localtime(moment, tz)
    day, clock = local.date(), local.time()

    hours = _salon_hours(day.weekday())
    if hours is None or hours.is_closed:
        return False

    for entry in _salon_time_off(day):
        interval = entry.blocked_interval(day)
        if not interval:
            continue
        if entry.all_day or (interval[0] <= clock < interval[1]):
            return False

    return any(start <= clock < end for start, end in hours.intervals())


def next_opening(after: dt.datetime, tz) -> dt.datetime | None:
    """When the salon next opens, searching a fortnight ahead.

    Used to tell the customer "we are back at 09:00 on Tuesday" instead of a
    bare "offline", which is the difference between a useful message and a
    dead end.
    """
    local = timezone.localtime(after, tz)

    for offset in range(SEARCH_DAYS):
        day = local.date() + dt.timedelta(days=offset)
        hours = _salon_hours(day.weekday())
        if hours is None or hours.is_closed:
            continue

        off_entries = _salon_time_off(day)
        if any(entry.all_day for entry in off_entries):
            continue

        for start, end in hours.intervals():
            opens_at = dt.datetime.combine(day, start, tzinfo=tz)
            if opens_at <= local:
                continue
            blocked = any(
                (interval := entry.blocked_interval(day))
                and not entry.all_day
                and interval[0] <= start < interval[1]
                for entry in off_entries
            )
            if not blocked:
                return opens_at
    return None


def status(chat_settings, now: dt.datetime | None = None) -> dict:
    """Everything the widget and the desk need to know, in one call.

    ``available``  -- can a customer start a chat right now?
    ``reason``     -- ``switched_off`` | ``outside_hours`` | ``""``
    ``next_open``  -- aware datetime, or None
    """
    now = now or timezone.now()
    tz = SalonSettings.load().tz

    if not chat_settings.enabled:
        return {"available": False, "reason": "switched_off", "next_open": None}

    if not chat_settings.follow_business_hours:
        return {"available": True, "reason": "", "next_open": None}

    # No opening hours configured at all means "not set up yet", not "closed
    # forever". Treating it as closed would leave an admin who switched chat on
    # staring at a bubble that never appears, with nothing to explain why.
    if not BusinessHours.objects.filter(calendar__isnull=True).exists():
        return {"available": True, "reason": "hours_not_configured", "next_open": None}

    if _is_open_at(now, tz):
        return {"available": True, "reason": "", "next_open": None}

    return {
        "available": False,
        "reason": "outside_hours",
        "next_open": next_opening(now, tz),
    }


def describe(chat_settings, now: dt.datetime | None = None) -> str:
    """A sentence for the customer explaining why chat is not available."""
    state = status(chat_settings, now)
    if state["available"]:
        return ""
    if state["reason"] == "switched_off":
        return chat_settings.offline_text

    when = state["next_open"]
    if when is None:
        return chat_settings.offline_text

    tz = SalonSettings.load().tz
    local_now = timezone.localtime(now or timezone.now(), tz)
    if when.date() == local_now.date():
        back = f"later today at {when:%H:%M}"
    elif when.date() == local_now.date() + dt.timedelta(days=1):
        back = f"tomorrow at {when:%H:%M}"
    else:
        back = f"{when:%A} at {when:%H:%M}"
    return f"{chat_settings.offline_text} We are back {back}."
