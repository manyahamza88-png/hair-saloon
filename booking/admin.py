"""The salon owner's control panel.

Everything the brief asks for is reachable here: add and remove calendars,
change business days and hours, mark vacation, and approve or decline
reservations.
"""
from __future__ import annotations

from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from . import google_calendar, services as booking_services
from .models import (
    Appointment,
    BusinessHours,
    Calendar,
    GoogleCredential,
    GoogleOAuthClientSettings,
    SalonSettings,
    Service,
    TimeOff,
)


# ---------------------------------------------------------------------------
# Salon settings (singleton)
# ---------------------------------------------------------------------------
@admin.register(SalonSettings)
class SalonSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Shop", {"fields": ("name", "tagline", "about", "address", "phone", "contact_email")}),
        ("Time", {"fields": ("timezone_name",)}),
        (
            "Booking rules",
            {
                "fields": (
                    "slot_interval_minutes",
                    "default_duration_minutes",
                    "buffer_minutes",
                    "min_lead_time_hours",
                    "max_advance_days",
                    "require_approval",
                    "respect_google_busy",
                    "booking_terms",
                )
            },
        ),
        ("Notifications", {"fields": ("notify_email",)}),
    )

    def has_add_permission(self, request):
        return not SalonSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # There is only ever one row: go straight to it.
        from django.shortcuts import redirect
        from django.urls import reverse

        obj = SalonSettings.load()
        return redirect(reverse("admin:booking_salonsettings_change", args=[obj.pk]))


# ---------------------------------------------------------------------------
# Google OAuth client
# ---------------------------------------------------------------------------
@admin.register(GoogleOAuthClientSettings)
class GoogleOAuthClientSettingsAdmin(admin.ModelAdmin):
    """Read-only pointer: the real editing happens on the Google setup page."""

    list_display = ("__str__", "updated_at")
    readonly_fields = ("client_id", "secret_state", "updated_at", "go_to_setup")
    fields = ("client_id", "secret_state", "updated_at", "go_to_setup")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Client secret")
    def secret_state(self, obj):
        return "Stored (encrypted)" if obj.has_secret else "Not set"

    @admin.display(description="Where to change this")
    def go_to_setup(self, obj):
        return format_html(
            '<a class="button" href="{}">Open Google setup</a>', reverse("booking:google_setup")
        )

    def changelist_view(self, request, extra_context=None):
        return redirect(reverse("booking:google_setup"))


# ---------------------------------------------------------------------------
# Google credentials
# ---------------------------------------------------------------------------
@admin.register(GoogleCredential)
class GoogleCredentialAdmin(admin.ModelAdmin):
    list_display = ("name", "auth_type", "share_with", "is_active", "calendar_count", "last_checked_at")
    list_filter = ("auth_type", "is_active")
    search_fields = ("name", "oauth_account_email")
    readonly_fields = (
        "share_with",
        "connection_state",
        "last_checked_at",
        "last_check_result",
        "setup_help",
    )
    actions = ["test_connection", "show_calendars"]
    fieldsets = (
        (None, {"fields": ("name", "auth_type", "is_active", "setup_help")}),
        (
            "Service account (recommended)",
            {
                "fields": ("service_account_json", "share_with", "delegated_user"),
                "description": (
                    "Paste the JSON key you downloaded from Google Cloud, save, then share each "
                    "Google Calendar with the address shown below and give it "
                    "<b>Make changes to events</b>."
                ),
            },
        ),
        (
            "OAuth 2.0 (connected Google account)",
            {
                "classes": ("collapse",),
                "fields": ("oauth_account_email", "connection_state"),
                "description": (
                    "Connect and disconnect accounts on the "
                    "<a href='/manage/google/'>Google setup</a> page. Tokens are stored "
                    "encrypted and are never shown here."
                ),
            },
        ),
        ("Read-only API key (optional)", {"classes": ("collapse",), "fields": ("api_key",)}),
        ("Last check", {"fields": ("last_checked_at", "last_check_result")}),
    )

    @admin.display(description="Share calendars with")
    def share_with(self, obj):
        email = obj.service_account_email if obj.pk else ""
        if not email:
            return "-"
        return format_html("<code>{}</code>", email)

    @admin.display(description="Calendars")
    def calendar_count(self, obj):
        return obj.calendars.count()

    @admin.display(description="Connection")
    def connection_state(self, obj):
        if not obj.pk or obj.auth_type != GoogleCredential.OAUTH:
            return "-"
        if obj.oauth_refresh_token:
            return format_html(
                'Connected{} since {} &middot; <a href="/manage/google/">manage</a>',
                f" as {obj.oauth_account_email}" if obj.oauth_account_email else "",
                obj.connected_at.strftime("%d %b %Y") if obj.connected_at else "?",
            )
        return format_html('Not connected &middot; <a href="/manage/google/">connect now</a>')

    @admin.display(description="How to set this up")
    def setup_help(self, obj):
        return mark_safe(
            "<ol style='margin:0;padding-left:1.2em'>"
            "<li>Google Cloud console &rarr; create a project &rarr; enable the "
            "<b>Google Calendar API</b>.</li>"
            "<li>Credentials &rarr; <b>Create credentials</b> &rarr; <b>Service account</b> &rarr; "
            "Keys &rarr; Add key &rarr; JSON. Paste the file below and save.</li>"
            "<li>Go to <b>Calendars</b>, add one with a name and an owner email, leave the "
            "<b>Calendar ID</b> empty, and run the action "
            "<b>Create a new Google calendar</b>.</li>"
            "</ol>"
            "<p style='margin:.6em 0 0'>That is the whole setup: this credential creates and owns "
            "the calendar, then shares it out to the owner, so nothing needs configuring inside "
            "anyone's Google account.</p>"
            "<p style='margin:.4em 0 0;color:#666'>To use a calendar that already exists instead, "
            "share it with the <b>Share calendars with</b> address below "
            "(<b>Make changes to events</b>) and paste its Calendar ID into the calendar.</p>"
        )

    @admin.action(description="Test connection to Google")
    def test_connection(self, request, queryset):
        for credential in queryset:
            try:
                result = google_calendar.check_credential(credential)
                level = messages.SUCCESS
            except Exception as exc:  # noqa: BLE001 - reported to the admin
                result = str(exc)
                level = messages.ERROR
            GoogleCredential.objects.filter(pk=credential.pk).update(
                last_checked_at=timezone.now(), last_check_result=result
            )
            self.message_user(request, f"{credential.name}: {result}", level=level)

    @admin.action(description="List calendars this credential can see")
    def show_calendars(self, request, queryset):
        for credential in queryset:
            try:
                items = google_calendar.list_calendars(credential)
            except Exception as exc:  # noqa: BLE001
                self.message_user(request, f"{credential.name}: {exc}", level=messages.ERROR)
                continue
            if not items:
                self.message_user(
                    request,
                    f"{credential.name}: no calendars visible yet. Share one with it first.",
                    level=messages.WARNING,
                )
                continue
            for item in items:
                self.message_user(
                    request,
                    format_html(
                        "{}: <b>{}</b> - ID <code>{}</code> ({})",
                        credential.name,
                        item["summary"],
                        item["id"],
                        item["access_role"],
                    ),
                )


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------
class BusinessHoursInline(admin.TabularInline):
    model = BusinessHours
    extra = 0
    fields = ("weekday", "is_closed", "opens_at", "closes_at", "break_start", "break_end")
    ordering = ("weekday",)
    verbose_name = "opening hours override"
    verbose_name_plural = "opening hours for this calendar (leave empty to use the salon default)"


class TimeOffInline(admin.TabularInline):
    model = TimeOff
    extra = 0
    fields = ("start_date", "end_date", "all_day", "start_time", "end_time", "reason")
    ordering = ("start_date",)


@admin.register(Calendar)
class CalendarAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "owner_email",
        "google_status",
        "is_active",
        "accepts_online_booking",
        "pending_badge",
        "sort_order",
    )
    list_editable = ("is_active", "accepts_online_booking", "sort_order")
    list_filter = ("is_active", "accepts_online_booking", "credential")
    search_fields = ("name", "owner_email", "google_calendar_id")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [BusinessHoursInline, TimeOffInline]
    actions = [
        "provision_calendar",
        "share_with_owner",
        "test_calendar",
        "activate",
        "deactivate",
        "resync_upcoming",
    ]
    readonly_fields = ("last_sync_error", "share_hint")
    fieldsets = (
        ("Shown to customers", {"fields": ("name", "slug", "description", "photo", "colour", "sort_order")}),
        (
            "Google Calendar",
            {
                "fields": (
                    "credential",
                    "google_calendar_id",
                    "owner_email",
                    "share_hint",
                    "last_sync_error",
                ),
                "description": (
                    "The <b>Calendar ID</b> comes from Google Calendar &rarr; Settings for that "
                    "calendar &rarr; Integrate calendar. <b>Owner email</b> is the Google account "
                    "that receives the accept / decline emails."
                ),
            },
        ),
        ("Booking", {"fields": ("default_duration_minutes", "is_active", "accepts_online_booking")}),
    )

    @admin.display(description="Google")
    def google_status(self, obj):
        if not obj.credential:
            return format_html('<span style="color:#888">local only</span>')
        if obj.last_sync_error:
            return format_html('<span style="color:#b91c1c" title="{}">error</span>', obj.last_sync_error[:200])
        return format_html('<span style="color:#15803d">connected</span>')

    @admin.display(description="Pending")
    def pending_badge(self, obj):
        count = obj.appointments.filter(status=Appointment.PENDING).count()
        if not count:
            return "-"
        return format_html('<b style="color:#b45309">{}</b>', count)

    @admin.display(description="Reminder")
    def share_hint(self, obj):
        if obj.pk and not obj.google_calendar_id:
            return mark_safe(
                "No calendar yet. Save, then pick this row in the list and run "
                "<b>Create a new Google calendar</b> &mdash; it will be created and shared "
                "with the owner email automatically."
            )
        if obj.credential and obj.credential.service_account_email:
            return format_html(
                "Using an existing calendar? Share it with <code>{}</code> and give it "
                "<b>Make changes to events</b>.",
                obj.credential.service_account_email,
            )
        return "Pick a credential above (and save) to see the address to share with."

    @admin.action(description="Create a new Google calendar (no sharing needed)")
    def provision_calendar(self, request, queryset):
        for calendar in queryset:
            try:
                calendar_id = booking_services.provision_google_calendar(calendar)
            except ValueError as exc:
                self.message_user(request, str(exc), level=messages.WARNING)
                continue
            except Exception as exc:  # noqa: BLE001 - reported to the admin
                self.message_user(request, f"{calendar.name}: {exc}", level=messages.ERROR)
                continue

            calendar.refresh_from_db()
            if calendar.last_sync_error:
                self.message_user(
                    request,
                    f"{calendar.name}: calendar created ({calendar_id}), but {calendar.last_sync_error}",
                    level=messages.WARNING,
                )
            else:
                self.message_user(
                    request,
                    format_html(
                        "{}: created <code>{}</code> and shared it with {}. "
                        "It will appear in their Google Calendar shortly.",
                        calendar.name,
                        calendar_id,
                        calendar.owner_email or "nobody (no owner email set)",
                    ),
                    level=messages.SUCCESS,
                )

    @admin.action(description="Re-send the calendar invitation to the owner")
    def share_with_owner(self, request, queryset):
        for calendar in queryset:
            if not (calendar.is_google_connected and calendar.owner_email):
                self.message_user(
                    request,
                    f"{calendar.name}: needs a credential, a calendar ID and an owner email.",
                    level=messages.WARNING,
                )
                continue
            try:
                google_calendar.grant_calendar_access(
                    calendar.credential, calendar.google_calendar_id, calendar.owner_email
                )
            except Exception as exc:  # noqa: BLE001
                self.message_user(request, f"{calendar.name}: {exc}", level=messages.ERROR)
                continue
            self.message_user(
                request,
                f"{calendar.name}: shared with {calendar.owner_email}.",
                level=messages.SUCCESS,
            )

    @admin.action(description="Test connection to this calendar")
    def test_calendar(self, request, queryset):
        for calendar in queryset:
            try:
                result = google_calendar.check_calendar(calendar)
                Calendar.objects.filter(pk=calendar.pk).update(last_sync_error="")
                self.message_user(request, f"{calendar.name}: {result}", level=messages.SUCCESS)
            except Exception as exc:  # noqa: BLE001
                Calendar.objects.filter(pk=calendar.pk).update(last_sync_error=str(exc)[:2000])
                self.message_user(request, f"{calendar.name}: {exc}", level=messages.ERROR)

    @admin.action(description="Show on the homepage")
    def activate(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} calendar(s) activated.")

    @admin.action(description="Hide from the homepage")
    def deactivate(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} calendar(s) hidden.")

    @admin.action(description="Re-sync upcoming appointments to Google")
    def resync_upcoming(self, request, queryset):
        total = failed = 0
        for calendar in queryset:
            for appointment in calendar.appointments.blocking().upcoming():
                total += 1
                if not booking_services.resync_appointment(appointment):
                    failed += 1
        self.message_user(
            request,
            f"Re-synced {total - failed}/{total} appointment(s)."
            + (" See each appointment for the error." if failed else ""),
            level=messages.WARNING if failed else messages.SUCCESS,
        )


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "duration_minutes", "price", "calendar_list", "is_active", "sort_order")
    list_editable = ("duration_minutes", "price", "is_active", "sort_order")
    list_filter = ("is_active", "calendars")
    search_fields = ("name",)
    filter_horizontal = ("calendars",)

    @admin.display(description="Offered on")
    def calendar_list(self, obj):
        names = list(obj.calendars.values_list("name", flat=True))
        return ", ".join(names) if names else "all calendars"


# ---------------------------------------------------------------------------
# Opening hours and vacation
# ---------------------------------------------------------------------------
@admin.register(BusinessHours)
class BusinessHoursAdmin(admin.ModelAdmin):
    list_display = ("scope", "weekday", "is_closed", "opens_at", "closes_at", "break_start", "break_end")
    list_editable = ("is_closed", "opens_at", "closes_at", "break_start", "break_end")
    list_filter = ("calendar", "weekday", "is_closed")
    ordering = ("calendar__sort_order", "weekday")

    @admin.display(description="Applies to", ordering="calendar__name")
    def scope(self, obj):
        return obj.calendar.name if obj.calendar else "Whole salon (default)"


@admin.register(TimeOff)
class TimeOffAdmin(admin.ModelAdmin):
    list_display = ("scope", "start_date", "end_date", "all_day", "start_time", "end_time", "reason")
    list_filter = ("calendar", "all_day")
    date_hierarchy = "start_date"
    search_fields = ("reason",)

    @admin.display(description="Applies to", ordering="calendar__name")
    def scope(self, obj):
        return obj.calendar.name if obj.calendar else "Whole salon"


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------
@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = (
        "start_local",
        "customer_name",
        "calendar",
        "service",
        "status_badge",
        "google_state",
        "created_at",
    )
    list_filter = ("status", "calendar", "service")
    search_fields = ("customer_name", "customer_email", "customer_phone", "notes")
    date_hierarchy = "start_at"
    readonly_fields = (
        "public_id",
        "google_event_id",
        "google_synced_at",
        "google_sync_error",
        "created_at",
        "updated_at",
        "links",
    )
    actions = ["accept_selected", "decline_selected", "cancel_selected", "resync_selected"]
    fieldsets = (
        ("Booking", {"fields": ("calendar", "service", "start_at", "end_at", "status", "decision_note")}),
        ("Customer", {"fields": ("customer_name", "customer_email", "customer_phone", "notes")}),
        (
            "Google sync",
            {"fields": ("google_event_id", "google_synced_at", "google_sync_error")},
        ),
        ("Meta", {"fields": ("public_id", "links", "decided_at", "created_at", "updated_at")}),
    )

    @admin.display(description="When", ordering="start_at")
    def start_local(self, obj):
        return timezone.localtime(obj.start_at, SalonSettings.load().tz).strftime("%a %d %b %Y, %H:%M")

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        colours = {
            Appointment.PENDING: "#b45309",
            Appointment.CONFIRMED: "#15803d",
            Appointment.DECLINED: "#b91c1c",
            Appointment.CANCELLED: "#6b7280",
        }
        return format_html(
            '<b style="color:{}">{}</b>', colours.get(obj.status, "#000"), obj.get_status_display()
        )

    @admin.display(description="Google")
    def google_state(self, obj):
        if not obj.calendar.is_google_connected:
            return "-"
        if obj.google_sync_error:
            return format_html('<span style="color:#b91c1c" title="{}">error</span>', obj.google_sync_error[:200])
        if obj.google_event_id:
            return format_html('<span style="color:#15803d">synced</span>')
        return format_html('<span style="color:#888">not synced</span>')

    @admin.display(description="Links")
    def links(self, obj):
        from . import tokens

        if not obj.pk:
            return "-"
        return format_html(
            '<a href="{}" target="_blank">Customer page</a> &middot; '
            '<a href="{}" target="_blank">Accept</a> &middot; '
            '<a href="{}" target="_blank">Decline</a>',
            tokens.appointment_url(obj),
            tokens.decision_url(obj, tokens.ACCEPT),
            tokens.decision_url(obj, tokens.DECLINE),
        )

    @admin.action(description="Accept (sync to Google + email the customer)")
    def accept_selected(self, request, queryset):
        for appointment in queryset:
            booking_services.confirm_appointment(appointment)
        self.message_user(request, f"{queryset.count()} appointment(s) accepted.")

    @admin.action(description="Decline (remove from Google + email the customer)")
    def decline_selected(self, request, queryset):
        for appointment in queryset:
            booking_services.decline_appointment(appointment)
        self.message_user(request, f"{queryset.count()} appointment(s) declined.")

    @admin.action(description="Cancel (email the customer)")
    def cancel_selected(self, request, queryset):
        for appointment in queryset:
            booking_services.cancel_appointment(appointment, by_customer=False)
        self.message_user(request, f"{queryset.count()} appointment(s) cancelled.")

    @admin.action(description="Re-sync with Google Calendar")
    def resync_selected(self, request, queryset):
        ok = sum(1 for appointment in queryset if booking_services.resync_appointment(appointment))
        total = queryset.count()
        self.message_user(
            request,
            f"Re-synced {ok}/{total}.",
            level=messages.WARNING if ok < total else messages.SUCCESS,
        )
