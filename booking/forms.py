from __future__ import annotations

import datetime as dt

from django import forms
from django.utils import timezone

from .availability import slot_is_available
from .models import Appointment, Calendar, SalonSettings, Service


class BookingForm(forms.Form):
    """The customer-facing booking form.

    The slot the customer clicked arrives as an ISO timestamp in a hidden
    field; it is re-validated here against live availability, because the slot
    list they are looking at may be minutes old.
    """

    service = forms.ModelChoiceField(queryset=Service.objects.none(), required=False, label="Service")
    start = forms.CharField(widget=forms.HiddenInput)
    customer_name = forms.CharField(max_length=120, label="Your name")
    customer_email = forms.EmailField(label="Email")
    customer_phone = forms.CharField(max_length=50, required=False, label="Phone (optional)")
    notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        label="Anything we should know? (optional)",
    )
    # Honeypot: real people leave it empty, bots fill everything in.
    website = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, calendar: Calendar, **kwargs):
        super().__init__(*args, **kwargs)
        self.calendar = calendar
        self.salon = SalonSettings.load()
        services = calendar.bookable_services()
        self.fields["service"].queryset = services
        self.fields["service"].empty_label = (
            f"Standard appointment ({calendar.duration_minutes(self.salon)} min)"
        )
        if not services.exists():
            self.fields["service"].widget = forms.HiddenInput()
        for name, field in self.fields.items():
            if name == "website":
                continue
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " field-input").strip()

    def clean_website(self):
        if self.cleaned_data.get("website"):
            raise forms.ValidationError("Spam detected.")
        return ""

    def clean_start(self):
        raw = self.cleaned_data["start"]
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except ValueError:
            raise forms.ValidationError("Invalid time. Please pick a slot again.")
        if timezone.is_naive(parsed):
            parsed = parsed.replace(tzinfo=self.salon.tz)
        return parsed

    @property
    def duration_minutes(self) -> int:
        service = self.cleaned_data.get("service") if hasattr(self, "cleaned_data") else None
        if service:
            return service.duration_minutes
        return self.calendar.duration_minutes(self.salon)

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start")
        if not start:
            return cleaned

        ok, reason = slot_is_available(
            self.calendar, start, self.duration_minutes, salon=self.salon
        )
        if not ok:
            raise forms.ValidationError(reason)
        cleaned["end"] = start + dt.timedelta(minutes=self.duration_minutes)
        return cleaned


class DecisionForm(forms.Form):
    """Confirmation step behind an accept / decline link from the email."""

    note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "class": "field-input"}),
        required=False,
        label="Message for the customer (optional)",
    )


class CancelForm(forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "class": "field-input"}),
        required=False,
        label="Reason (optional)",
    )


class StaffAppointmentForm(forms.ModelForm):
    """Used on the staff dashboard to block time or add a phone booking."""

    class Meta:
        model = Appointment
        fields = [
            "calendar",
            "service",
            "customer_name",
            "customer_email",
            "customer_phone",
            "start_at",
            "end_at",
            "notes",
            "status",
        ]
        widgets = {
            "start_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "end_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["calendar"].queryset = Calendar.objects.filter(is_active=True)
        self.fields["start_at"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
        self.fields["end_at"].input_formats = self.fields["start_at"].input_formats
        for field in self.fields.values():
            css = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = (css + " field-input").strip()
