"""Data model for the salon booking system.

The whole configuration surface lives here so that a non-technical salon owner
can run the shop entirely from the Django admin:

* ``GoogleCredential``  -- the Google API credentials (one per Google account)
* ``Calendar``          -- a named calendar shown on the homepage (a stylist,
                           a chair, a room ... whatever the salon wants)
* ``Service``           -- what can be booked and how long it takes
* ``BusinessHours``     -- opening hours, salon wide or per calendar
* ``TimeOff``           -- holidays / vacation / one-off closures
* ``Appointment``       -- a reservation, pending until the calendar owner
                           accepts it from the notification email
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

WEEKDAYS = [
    (0, "Monday"),
    (1, "Tuesday"),
    (2, "Wednesday"),
    (3, "Thursday"),
    (4, "Friday"),
    (5, "Saturday"),
    (6, "Sunday"),
]


# ---------------------------------------------------------------------------
# Salon-wide settings (singleton)
# ---------------------------------------------------------------------------
class SalonSettings(models.Model):
    """One editable row holding everything about the shop itself."""

    name = models.CharField(max_length=120, default="Hair Salon")
    tagline = models.CharField(max_length=200, blank=True, default="Book your next appointment online")
    about = models.TextField(blank=True, help_text="Shown on the homepage, plain text or simple HTML.")

    address = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    contact_email = models.EmailField(blank=True, help_text="Shown to customers and used as reply-to.")

    timezone_name = models.CharField(
        "time zone",
        max_length=64,
        default=settings.TIME_ZONE,
        help_text="IANA name, e.g. Europe/Berlin. All opening hours are interpreted in this zone.",
    )

    slot_interval_minutes = models.PositiveIntegerField(
        default=15, help_text="Appointments can start every N minutes."
    )
    default_duration_minutes = models.PositiveIntegerField(
        default=45, help_text="Used when a calendar has no services attached."
    )
    buffer_minutes = models.PositiveIntegerField(
        default=0, help_text="Cleaning / turnaround time kept free after every appointment."
    )
    min_lead_time_hours = models.PositiveIntegerField(
        default=2, help_text="Customers cannot book anything starting sooner than this."
    )
    max_advance_days = models.PositiveIntegerField(
        default=60, help_text="How far into the future the booking calendar goes."
    )
    require_approval = models.BooleanField(
        default=True,
        help_text=(
            "On: bookings arrive as 'pending' and the calendar owner accepts or declines "
            "them by email. Off: bookings are confirmed immediately."
        ),
    )
    respect_google_busy = models.BooleanField(
        default=True,
        help_text="Also hide slots that are busy in the linked Google Calendar (free/busy lookup).",
    )
    notify_email = models.EmailField(
        blank=True,
        help_text="Optional extra address that gets a copy of every booking notification.",
    )
    booking_terms = models.TextField(
        blank=True, help_text="Small print shown under the booking form (cancellation policy, etc.)."
    )

    class Meta:
        verbose_name = "salon settings"
        verbose_name_plural = "salon settings"

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce the singleton
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # pragma: no cover - guarded in admin too
        raise ValidationError("The salon settings row cannot be deleted.")

    @classmethod
    def load(cls) -> "SalonSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(settings.TIME_ZONE)

    def clean(self):
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError({"timezone_name": "Unknown time zone name."})
        if self.slot_interval_minutes < 5:
            raise ValidationError({"slot_interval_minutes": "Use at least 5 minutes."})


# ---------------------------------------------------------------------------
# Google credentials
# ---------------------------------------------------------------------------
class GoogleCredential(models.Model):
    """Google API credentials used to talk to one or more calendars.

    Two ways to authenticate, both supported:

    ``service_account``
        Paste the JSON key file of a Google Cloud service account. The salon
        then shares each Google Calendar with the service account address
        (``...@....iam.gserviceaccount.com``) giving it "Make changes to
        events". This is the recommended option on PythonAnywhere: no browser
        round-trip, nothing expires.

    ``oauth``
        A refresh token obtained once by the calendar owner through the
        "Connect Google account" button in the admin. Use this when the
        calendar lives in a personal Gmail account that you would rather not
        share with a service account.
    """

    SERVICE_ACCOUNT = "service_account"
    OAUTH = "oauth"
    AUTH_CHOICES = [
        (SERVICE_ACCOUNT, "Service account (JSON key)"),
        (OAUTH, "OAuth 2.0 (connected Google account)"),
    ]

    name = models.CharField(
        max_length=120, unique=True, help_text="Your label for this credential, e.g. 'Salon Google account'."
    )
    auth_type = models.CharField(max_length=20, choices=AUTH_CHOICES, default=SERVICE_ACCOUNT)

    service_account_json = models.TextField(
        blank=True,
        help_text="Paste the whole downloaded service-account JSON key file here.",
    )
    delegated_user = models.EmailField(
        blank=True,
        help_text=(
            "Google Workspace only: impersonate this user with domain-wide delegation. "
            "Leave empty for normal shared-calendar setups."
        ),
    )

    oauth_client_id = models.CharField(max_length=255, blank=True)
    oauth_client_secret = models.CharField(max_length=255, blank=True)
    oauth_refresh_token = models.TextField(blank=True)
    oauth_account_email = models.EmailField(blank=True, help_text="Filled in automatically after connecting.")

    api_key = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional Google API key. Only usable for reading public calendars.",
    )

    is_active = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_check_result = models.TextField(blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Google credential"

    def __str__(self) -> str:
        return f"{self.name} ({self.get_auth_type_display()})"

    @property
    def service_account_email(self) -> str:
        """The address the salon has to share its calendars with."""
        if self.auth_type != self.SERVICE_ACCOUNT or not self.service_account_json:
            return ""
        try:
            return json.loads(self.service_account_json).get("client_email", "")
        except (ValueError, AttributeError):
            return ""

    def clean(self):
        if self.auth_type == self.SERVICE_ACCOUNT:
            if not self.service_account_json.strip():
                raise ValidationError({"service_account_json": "Paste the service account JSON key."})
            try:
                data = json.loads(self.service_account_json)
            except ValueError as exc:
                raise ValidationError({"service_account_json": f"Not valid JSON: {exc}"})
            missing = [k for k in ("client_email", "private_key", "token_uri") if not data.get(k)]
            if missing:
                raise ValidationError(
                    {"service_account_json": "Missing key(s) in JSON: " + ", ".join(missing)}
                )
        elif self.auth_type == self.OAUTH:
            if not self.oauth_refresh_token.strip():
                raise ValidationError(
                    {"oauth_refresh_token": "Connect a Google account (or paste a refresh token)."}
                )


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------
class Calendar(models.Model):
    """A named Google Calendar the customer can book against."""

    name = models.CharField(max_length=120, help_text="Shown on the homepage, e.g. 'Maria - Colour & Cuts'.")
    slug = models.SlugField(max_length=140, unique=True, blank=True, help_text="Leave blank to auto-fill.")
    description = models.TextField(blank=True, help_text="Short blurb shown on the calendar card.")
    photo = models.ImageField(upload_to="calendars/", blank=True, null=True)
    colour = models.CharField(
        max_length=7, default="#b0855b", help_text="Accent colour for this calendar's card (hex)."
    )

    google_calendar_id = models.CharField(
        max_length=255,
        help_text=(
            "The Google Calendar ID: 'primary', an address like name@gmail.com, or the long "
            "...@group.calendar.google.com id from Calendar settings."
        ),
    )
    credential = models.ForeignKey(
        GoogleCredential,
        on_delete=models.PROTECT,
        related_name="calendars",
        null=True,
        blank=True,
        help_text="Leave empty to keep this calendar local only (no Google sync).",
    )
    owner_email = models.EmailField(
        help_text="The Google account that owns this calendar. Accept / decline emails go here."
    )

    default_duration_minutes = models.PositiveIntegerField(
        null=True, blank=True, help_text="Overrides the salon default for this calendar."
    )
    is_active = models.BooleanField(default=True, help_text="Inactive calendars disappear from the homepage.")
    accepts_online_booking = models.BooleanField(
        default=True, help_text="Show the calendar but turn its booking button off."
    )
    sort_order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    last_sync_error = models.TextField(blank=True, editable=False)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "calendar"
            slug, counter = base, 2
            while Calendar.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("booking:calendar_detail", args=[self.slug])

    @property
    def is_google_connected(self) -> bool:
        return bool(self.credential and self.credential.is_active and self.google_calendar_id)

    def duration_minutes(self, salon: SalonSettings | None = None) -> int:
        salon = salon or SalonSettings.load()
        return self.default_duration_minutes or salon.default_duration_minutes

    def bookable_services(self):
        qs = Service.objects.filter(is_active=True)
        own = qs.filter(calendars=self)
        return own if own.exists() else qs.filter(calendars__isnull=True)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
class Service(models.Model):
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    duration_minutes = models.PositiveIntegerField(default=45)
    price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    calendars = models.ManyToManyField(
        Calendar,
        blank=True,
        related_name="services",
        help_text="Leave empty to offer this service on every calendar.",
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.duration_minutes} min)"

    @property
    def label(self) -> str:
        bits = [self.name, f"{self.duration_minutes} min"]
        if self.price is not None:
            bits.append(f"{self.price:.2f}")
        return " / ".join(bits)


# ---------------------------------------------------------------------------
# Opening hours
# ---------------------------------------------------------------------------
class BusinessHours(models.Model):
    """Opening hours for one weekday.

    A row with ``calendar = None`` is the salon-wide default. A row with a
    calendar overrides the default for that calendar only, which is how a
    single stylist can work Saturdays while the rest of the shop does not.
    """

    calendar = models.ForeignKey(
        Calendar,
        on_delete=models.CASCADE,
        related_name="business_hours",
        null=True,
        blank=True,
        help_text="Leave empty for the salon-wide default.",
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    is_closed = models.BooleanField(default=False, help_text="Tick to close this weekday entirely.")
    opens_at = models.TimeField(default=time(9, 0))
    closes_at = models.TimeField(default=time(18, 0))
    break_start = models.TimeField(null=True, blank=True, help_text="Optional lunch break start.")
    break_end = models.TimeField(null=True, blank=True)

    class Meta:
        ordering = ["calendar__sort_order", "weekday"]
        constraints = [
            models.UniqueConstraint(
                fields=["calendar", "weekday"],
                name="unique_hours_per_calendar_weekday",
            ),
        ]
        verbose_name = "business hours"
        verbose_name_plural = "business hours"

    def __str__(self) -> str:
        who = self.calendar.name if self.calendar else "Salon"
        if self.is_closed:
            return f"{who} / {self.get_weekday_display()}: closed"
        return f"{who} / {self.get_weekday_display()}: {self.opens_at:%H:%M}-{self.closes_at:%H:%M}"

    def clean(self):
        if not self.is_closed and self.opens_at >= self.closes_at:
            raise ValidationError({"closes_at": "Closing time must be after opening time."})
        if bool(self.break_start) != bool(self.break_end):
            raise ValidationError({"break_end": "Set both break start and break end, or neither."})
        if self.break_start and self.break_end:
            if self.break_start >= self.break_end:
                raise ValidationError({"break_end": "Break end must be after break start."})
            if self.break_start < self.opens_at or self.break_end > self.closes_at:
                raise ValidationError({"break_start": "The break must sit inside the opening hours."})

    def intervals(self) -> list[tuple[time, time]]:
        """Opening hours split around the optional break."""
        if self.is_closed:
            return []
        if self.break_start and self.break_end:
            return [(self.opens_at, self.break_start), (self.break_end, self.closes_at)]
        return [(self.opens_at, self.closes_at)]


# ---------------------------------------------------------------------------
# Vacation / closures
# ---------------------------------------------------------------------------
class TimeOff(models.Model):
    """Vacation, public holidays or a one-off afternoon away."""

    calendar = models.ForeignKey(
        Calendar,
        on_delete=models.CASCADE,
        related_name="time_off",
        null=True,
        blank=True,
        help_text="Leave empty to close the whole salon.",
    )
    reason = models.CharField(max_length=200, blank=True, help_text="Shown to customers, e.g. 'Summer holiday'.")
    start_date = models.DateField()
    end_date = models.DateField(help_text="Inclusive: same date as start for a single day.")
    all_day = models.BooleanField(default=True)
    start_time = models.TimeField(null=True, blank=True, help_text="Only when 'all day' is off.")
    end_time = models.TimeField(null=True, blank=True)

    class Meta:
        ordering = ["start_date"]
        verbose_name = "time off / vacation"
        verbose_name_plural = "time off / vacation"

    def __str__(self) -> str:
        who = self.calendar.name if self.calendar else "Whole salon"
        span = f"{self.start_date:%d %b %Y}"
        if self.end_date != self.start_date:
            span += f" to {self.end_date:%d %b %Y}"
        return f"{who}: {span}" + (f" ({self.reason})" if self.reason else "")

    def clean(self):
        if self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before the start date."})
        if not self.all_day:
            if not self.start_time or not self.end_time:
                raise ValidationError({"start_time": "Set both times, or tick 'all day'."})
            if self.start_time >= self.end_time:
                raise ValidationError({"end_time": "End time must be after start time."})

    def covers_date(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date

    def applies_to(self, calendar: Calendar) -> bool:
        return self.calendar_id is None or self.calendar_id == calendar.pk

    def blocked_interval(self, day: date) -> tuple[time, time] | None:
        """The blocked part of ``day``: ``None`` means the day is not affected."""
        if not self.covers_date(day):
            return None
        if self.all_day:
            return (time.min, time.max)
        return (self.start_time, self.end_time)


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------
class AppointmentQuerySet(models.QuerySet):
    def blocking(self):
        """Appointments that occupy a slot (pending counts: it is reserved)."""
        return self.filter(status__in=[Appointment.PENDING, Appointment.CONFIRMED])

    def upcoming(self):
        return self.filter(start_at__gte=timezone.now()).order_by("start_at")


class Appointment(models.Model):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (PENDING, "Pending approval"),
        (CONFIRMED, "Confirmed"),
        (DECLINED, "Declined"),
        (CANCELLED, "Cancelled"),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    calendar = models.ForeignKey(Calendar, on_delete=models.PROTECT, related_name="appointments")
    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True)

    customer_name = models.CharField(max_length=120)
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)

    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    decision_note = models.TextField(blank=True, help_text="Optional message sent to the customer.")
    decided_at = models.DateTimeField(null=True, blank=True)

    google_event_id = models.CharField(max_length=255, blank=True, editable=False)
    google_synced_at = models.DateTimeField(null=True, blank=True, editable=False)
    google_sync_error = models.TextField(blank=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AppointmentQuerySet.as_manager()

    class Meta:
        ordering = ["-start_at"]
        indexes = [
            models.Index(fields=["calendar", "start_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        local = timezone.localtime(self.start_at, SalonSettings.load().tz)
        return f"{self.customer_name} / {self.calendar.name} / {local:%d %b %Y %H:%M}"

    # -- helpers ----------------------------------------------------------
    @property
    def duration_minutes(self) -> int:
        return int((self.end_at - self.start_at).total_seconds() // 60)

    @property
    def is_open(self) -> bool:
        return self.status in (self.PENDING, self.CONFIRMED)

    @property
    def is_past(self) -> bool:
        return self.end_at < timezone.now()

    @property
    def service_name(self) -> str:
        return self.service.name if self.service else "Appointment"

    def local_start(self) -> datetime:
        return timezone.localtime(self.start_at, SalonSettings.load().tz)

    def local_end(self) -> datetime:
        return timezone.localtime(self.end_at, SalonSettings.load().tz)

    def get_absolute_url(self) -> str:
        return reverse("booking:appointment_detail", args=[self.public_id])

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        return self.start_at < other_end and other_start < self.end_at

    def clean(self):
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError({"end_at": "The end time must be after the start time."})
        if self.start_at and self.end_at and self.calendar_id:
            clash = (
                Appointment.objects.blocking()
                .filter(calendar_id=self.calendar_id, start_at__lt=self.end_at, end_at__gt=self.start_at)
                .exclude(pk=self.pk)
                .first()
            )
            if clash:
                raise ValidationError(
                    f"Overlaps an existing appointment ({clash.customer_name}, "
                    f"{timezone.localtime(clash.start_at):%d %b %H:%M})."
                )

    def blocked_window(self, buffer_minutes: int = 0) -> tuple[datetime, datetime]:
        """The busy window including the salon's turnaround buffer."""
        return self.start_at, self.end_at + timedelta(minutes=buffer_minutes)
