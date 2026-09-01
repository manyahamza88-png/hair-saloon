"""Tests for the parts that would silently ruin a salon's day:
slot generation, double booking, vacation, and the accept / decline flow.

Google is never contacted here: ``respect_google_busy`` is switched off, or the
calendar has no credential attached, so ``is_google_connected`` is False.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail, signing
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from booking import availability, tokens
from booking.models import (
    Appointment,
    BusinessHours,
    Calendar,
    GoogleCredential,
    SalonSettings,
    Service,
    TimeOff,
)
from booking.services import confirm_appointment, create_appointment, decline_appointment


def next_weekday(target: int) -> dt.date:
    """The next occurrence of ``target`` weekday that is at least 3 days out."""
    day = timezone.localdate() + dt.timedelta(days=3)
    while day.weekday() != target:
        day += dt.timedelta(days=1)
    return day


class BaseSalonTest(TestCase):
    def setUp(self):
        self.salon = SalonSettings.load()
        self.salon.timezone_name = "Europe/Berlin"
        self.salon.slot_interval_minutes = 30
        self.salon.default_duration_minutes = 60
        self.salon.buffer_minutes = 0
        self.salon.min_lead_time_hours = 0
        self.salon.max_advance_days = 60
        self.salon.require_approval = True
        self.salon.respect_google_busy = False
        self.salon.save()

        for weekday in range(7):
            BusinessHours.objects.create(
                calendar=None,
                weekday=weekday,
                is_closed=weekday == 6,  # closed on Sunday
                opens_at=dt.time(9, 0),
                closes_at=dt.time(17, 0),
            )

        self.calendar = Calendar.objects.create(
            name="Maria",
            google_calendar_id="maria@example.com",
            owner_email="maria@example.com",
        )

    def aware(self, day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=self.salon.tz)

    def make_appointment(self, hour: int = 10, duration: int = 60, day: dt.date | None = None,
                         **overrides) -> Appointment:
        """Book through the real service layer.

        ``captureOnCommitCallbacks`` is required because the Google sync and the
        emails are deferred to ``transaction.on_commit``, which a ``TestCase``
        would otherwise roll back without ever running.
        """
        day = day or next_weekday(0)
        start = self.aware(day, hour)
        params = {
            "calendar": self.calendar,
            "service": None,
            "start_at": start,
            "end_at": start + dt.timedelta(minutes=duration),
            "customer_name": "Ana",
            "customer_email": "ana@example.com",
        }
        params.update(overrides)
        with self.captureOnCommitCallbacks(execute=True):
            return create_appointment(**params)


class SlotGenerationTests(BaseSalonTest):
    def test_slots_span_the_opening_hours(self):
        monday = next_weekday(0)
        slots = availability.day_slots(self.calendar, monday)
        # 09:00 to 17:00, 60 min appointments every 30 min -> last start 16:00
        self.assertEqual(slots[0].start.hour, 9)
        self.assertEqual(slots[-1].start.strftime("%H:%M"), "16:00")
        self.assertEqual(len(slots), 15)

    def test_closed_day_has_no_slots(self):
        sunday = next_weekday(6)
        self.assertEqual(availability.day_slots(self.calendar, sunday), [])

    def test_lunch_break_is_carved_out(self):
        BusinessHours.objects.filter(calendar=None, weekday=1).update(
            break_start=dt.time(12, 0), break_end=dt.time(13, 0)
        )
        tuesday = next_weekday(1)
        labels = [slot.label for slot in availability.day_slots(self.calendar, tuesday)]
        self.assertNotIn("12:00", labels)
        self.assertNotIn("12:30", labels)
        self.assertIn("13:00", labels)

    def test_existing_appointment_blocks_overlapping_slots(self):
        monday = next_weekday(0)
        Appointment.objects.create(
            calendar=self.calendar,
            customer_name="Ana",
            customer_email="ana@example.com",
            start_at=self.aware(monday, 10),
            end_at=self.aware(monday, 11),
            status=Appointment.CONFIRMED,
        )
        labels = [slot.label for slot in availability.day_slots(self.calendar, monday)]
        for blocked in ("09:30", "10:00", "10:30"):
            self.assertNotIn(blocked, labels)
        self.assertIn("11:00", labels)
        self.assertIn("09:00", labels)

    def test_pending_appointment_also_holds_the_slot(self):
        monday = next_weekday(0)
        Appointment.objects.create(
            calendar=self.calendar,
            customer_name="Ana",
            customer_email="ana@example.com",
            start_at=self.aware(monday, 10),
            end_at=self.aware(monday, 11),
            status=Appointment.PENDING,
        )
        labels = [slot.label for slot in availability.day_slots(self.calendar, monday)]
        self.assertNotIn("10:00", labels)

    def test_declined_appointment_frees_the_slot(self):
        monday = next_weekday(0)
        Appointment.objects.create(
            calendar=self.calendar,
            customer_name="Ana",
            customer_email="ana@example.com",
            start_at=self.aware(monday, 10),
            end_at=self.aware(monday, 11),
            status=Appointment.DECLINED,
        )
        labels = [slot.label for slot in availability.day_slots(self.calendar, monday)]
        self.assertIn("10:00", labels)

    def test_buffer_extends_the_block(self):
        self.salon.buffer_minutes = 30
        self.salon.save()
        monday = next_weekday(0)
        Appointment.objects.create(
            calendar=self.calendar,
            customer_name="Ana",
            customer_email="ana@example.com",
            start_at=self.aware(monday, 10),
            end_at=self.aware(monday, 11),
            status=Appointment.CONFIRMED,
        )
        labels = [slot.label for slot in availability.day_slots(self.calendar, monday)]
        self.assertNotIn("11:00", labels)
        self.assertIn("11:30", labels)

    def test_all_day_vacation_closes_the_day(self):
        monday = next_weekday(0)
        TimeOff.objects.create(
            calendar=None, start_date=monday, end_date=monday, all_day=True, reason="Holiday"
        )
        self.assertEqual(availability.day_slots(self.calendar, monday), [])

    def test_vacation_for_one_calendar_leaves_the_others_open(self):
        other = Calendar.objects.create(
            name="Ben", google_calendar_id="ben@example.com", owner_email="ben@example.com"
        )
        monday = next_weekday(0)
        TimeOff.objects.create(calendar=self.calendar, start_date=monday, end_date=monday, all_day=True)
        self.assertEqual(availability.day_slots(self.calendar, monday), [])
        self.assertTrue(availability.day_slots(other, monday))

    def test_partial_day_time_off(self):
        monday = next_weekday(0)
        TimeOff.objects.create(
            calendar=self.calendar,
            start_date=monday,
            end_date=monday,
            all_day=False,
            start_time=dt.time(12, 0),
            end_time=dt.time(15, 0),
            reason="Dentist",
        )
        labels = [slot.label for slot in availability.day_slots(self.calendar, monday)]
        self.assertNotIn("12:00", labels)
        self.assertNotIn("14:30", labels)
        self.assertIn("15:00", labels)

    def test_calendar_hours_override_the_salon_default(self):
        monday = next_weekday(0)
        BusinessHours.objects.create(
            calendar=self.calendar, weekday=0, opens_at=dt.time(13, 0), closes_at=dt.time(16, 0)
        )
        slots = availability.day_slots(self.calendar, monday)
        self.assertEqual(slots[0].label, "13:00")
        self.assertEqual(slots[-1].label, "15:00")

    def test_min_lead_time_hides_imminent_slots(self):
        self.salon.min_lead_time_hours = 48
        self.salon.save()
        tomorrow = timezone.localdate(timezone=self.salon.tz) + dt.timedelta(days=1)
        if tomorrow.weekday() != 6:
            self.assertEqual(availability.day_slots(self.calendar, tomorrow), [])

    def test_beyond_the_horizon_is_empty(self):
        far = timezone.localdate(timezone=self.salon.tz) + dt.timedelta(days=200)
        self.assertEqual(availability.day_slots(self.calendar, far), [])

    def test_service_duration_changes_the_slot_count(self):
        monday = next_weekday(0)
        long_service = Service.objects.create(name="Colour", duration_minutes=120)
        slots = availability.day_slots(self.calendar, monday, long_service.duration_minutes)
        self.assertEqual(slots[-1].label, "15:00")

    def test_inactive_calendar_offers_nothing(self):
        self.calendar.accepts_online_booking = False
        self.calendar.save()
        self.assertEqual(availability.day_slots(self.calendar, next_weekday(0)), [])


class SlotValidationTests(BaseSalonTest):
    def test_valid_slot_passes(self):
        ok, reason = availability.slot_is_available(self.calendar, self.aware(next_weekday(0), 10), 60)
        self.assertTrue(ok, reason)

    def test_outside_opening_hours_is_rejected(self):
        ok, reason = availability.slot_is_available(self.calendar, self.aware(next_weekday(0), 20), 60)
        self.assertFalse(ok)
        self.assertIn("opening hours", reason)

    def test_closed_day_is_rejected(self):
        ok, reason = availability.slot_is_available(self.calendar, self.aware(next_weekday(6), 10), 60)
        self.assertFalse(ok)
        self.assertIn("closed", reason.lower())

    def test_taken_slot_is_rejected(self):
        monday = next_weekday(0)
        Appointment.objects.create(
            calendar=self.calendar,
            customer_name="Ana",
            customer_email="ana@example.com",
            start_at=self.aware(monday, 10),
            end_at=self.aware(monday, 11),
            status=Appointment.CONFIRMED,
        )
        ok, reason = availability.slot_is_available(self.calendar, self.aware(monday, 10, 30), 60)
        self.assertFalse(ok)
        self.assertIn("taken", reason)


class BookingFlowTests(BaseSalonTest):
    def test_homepage_lists_active_calendars(self):
        response = self.client.get(reverse("booking:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maria")

    def test_homepage_hides_inactive_calendars(self):
        self.calendar.is_active = False
        self.calendar.save()
        response = self.client.get(reverse("booking:home"))
        self.assertNotContains(response, "Maria")

    def test_booking_page_shows_slots(self):
        monday = next_weekday(0)
        response = self.client.get(
            reverse("booking:calendar_detail", args=[self.calendar.slug]), {"date": monday.isoformat()}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "09:00")

    def test_slots_api(self):
        monday = next_weekday(0)
        response = self.client.get(
            reverse("booking:slots_api", args=[self.calendar.slug]), {"date": monday.isoformat()}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["duration"], 60)
        self.assertEqual(len(payload["slots"]), 15)

    def test_slots_api_rejects_a_bad_date(self):
        response = self.client.get(
            reverse("booking:slots_api", args=[self.calendar.slug]), {"date": "not-a-date"}
        )
        self.assertEqual(response.status_code, 400)

    def test_booking_creates_a_pending_appointment_and_emails_everyone(self):
        monday = next_weekday(0)
        start = self.aware(monday, 10)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("booking:book", args=[self.calendar.slug]),
                {
                    "start": start.isoformat(),
                    "customer_name": "Ana Vidal",
                    "customer_email": "ana@example.com",
                    "customer_phone": "0123456789",
                    "notes": "Please keep the length",
                },
            )
        appointment = Appointment.objects.get()
        self.assertRedirects(
            response, reverse("booking:booking_done", args=[appointment.public_id])
        )
        self.assertEqual(appointment.status, Appointment.PENDING)
        self.assertEqual(appointment.duration_minutes, 60)

        # One email to the calendar owner, one to the customer.
        self.assertEqual(len(mail.outbox), 2)
        owner_email = next(m for m in mail.outbox if "maria@example.com" in m.to)
        self.assertIn("New booking request", owner_email.subject)
        self.assertIn(tokens.decision_url(appointment, tokens.ACCEPT), owner_email.body)
        self.assertIn(tokens.decision_url(appointment, tokens.DECLINE), owner_email.body)

    def test_booking_is_confirmed_immediately_when_approval_is_off(self):
        self.salon.require_approval = False
        self.salon.save()
        monday = next_weekday(0)
        self.client.post(
            reverse("booking:book", args=[self.calendar.slug]),
            {
                "start": self.aware(monday, 10).isoformat(),
                "customer_name": "Ana",
                "customer_email": "ana@example.com",
            },
        )
        self.assertEqual(Appointment.objects.get().status, Appointment.CONFIRMED)

    def test_double_booking_is_refused(self):
        monday = next_weekday(0)
        start = self.aware(monday, 10)
        payload = {
            "start": start.isoformat(),
            "customer_name": "Ana",
            "customer_email": "ana@example.com",
        }
        self.client.post(reverse("booking:book", args=[self.calendar.slug]), payload)
        response = self.client.post(
            reverse("booking:book", args=[self.calendar.slug]),
            dict(payload, customer_name="Bea", customer_email="bea@example.com"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_honeypot_blocks_a_bot(self):
        monday = next_weekday(0)
        response = self.client.post(
            reverse("booking:book", args=[self.calendar.slug]),
            {
                "start": self.aware(monday, 10).isoformat(),
                "customer_name": "Bot",
                "customer_email": "bot@example.com",
                "website": "http://spam.example",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Appointment.objects.count(), 0)

    def test_booking_a_closed_day_is_refused(self):
        sunday = next_weekday(6)
        response = self.client.post(
            reverse("booking:book", args=[self.calendar.slug]),
            {
                "start": self.aware(sunday, 10).isoformat(),
                "customer_name": "Ana",
                "customer_email": "ana@example.com",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Appointment.objects.count(), 0)


class DecisionLinkTests(BaseSalonTest):
    def test_accept_link_needs_a_post_to_take_effect(self):
        appointment = self.make_appointment()
        url = reverse("booking:decide", args=[tokens.make_decision_token(appointment, tokens.ACCEPT)])

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.PENDING)  # GET must not decide

        mail.outbox.clear()
        response = self.client.post(url, {"note": "See you then"})
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.CONFIRMED)
        self.assertEqual(appointment.decision_note, "See you then")
        self.assertRedirects(
            response, reverse("booking:decision_done", args=[appointment.public_id])
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("confirmed", mail.outbox[0].subject)
        self.assertIn("See you then", mail.outbox[0].body)

    def test_decline_link_frees_the_slot(self):
        appointment = self.make_appointment()
        url = reverse("booking:decide", args=[tokens.make_decision_token(appointment, tokens.DECLINE)])
        mail.outbox.clear()
        self.client.post(url, {"note": "Fully booked, sorry"})

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.DECLINED)
        labels = [slot.label for slot in availability.day_slots(self.calendar, appointment.local_start().date())]
        self.assertIn("10:00", labels)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ana@example.com", mail.outbox[0].to)

    def test_tampered_token_is_rejected(self):
        appointment = self.make_appointment()
        token = tokens.make_decision_token(appointment, tokens.DECLINE)
        response = self.client.get(reverse("booking:decide", args=[token[:-4] + "0000"]))
        self.assertEqual(response.status_code, 404)

    def test_expired_token_is_reported(self):
        appointment = self.make_appointment()
        token = tokens.make_decision_token(appointment, tokens.ACCEPT)
        with mock.patch(
            "booking.tokens.signing.loads", side_effect=signing.SignatureExpired("too old")
        ):
            response = self.client.get(reverse("booking:decide", args=[token]))
        self.assertEqual(response.status_code, 410)

    def test_customer_can_cancel(self):
        appointment = self.make_appointment()
        confirm_appointment(appointment)
        mail.outbox.clear()

        url = reverse("booking:cancel", args=[tokens.make_cancel_token(appointment)])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"reason": "Something came up"})

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.CANCELLED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("maria@example.com", mail.outbox[0].to)  # the owner is told


class DashboardTests(BaseSalonTest):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user("owner", password="pw12345!", is_staff=True)

    def test_dashboard_requires_staff(self):
        response = self.client.get(reverse("booking:dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_staff_can_accept_from_the_dashboard(self):
        appointment = self.make_appointment()
        self.client.force_login(self.staff)

        response = self.client.get(reverse("booking:dashboard"))
        self.assertContains(response, "Ana")

        self.client.post(
            reverse("booking:dashboard_decide", args=[appointment.public_id]), {"action": "accept"}
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.CONFIRMED)


class GoogleOAuthSetupTests(BaseSalonTest):
    """The admin-panel flow: client credentials, linking, naming calendars."""

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user("owner", password="pw12345!", is_staff=True)
        self.client.force_login(self.staff)

    # -- client id / secret ------------------------------------------------
    def test_setup_page_requires_staff(self):
        self.client.logout()
        response = self.client.get(reverse("booking:google_setup"))
        self.assertEqual(response.status_code, 302)

    def test_saving_the_client_encrypts_the_secret(self):
        from booking.models import GoogleOAuthClientSettings

        self.client.post(
            reverse("booking:google_client_save"),
            {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-topsecret"},
        )
        client = GoogleOAuthClientSettings.load()
        self.assertEqual(client.client_id, "123-abc.apps.googleusercontent.com")
        self.assertTrue(client.is_configured)
        # Round-trips, but is not readable in the database.
        self.assertEqual(client.get_client_secret(), "GOCSPX-topsecret")
        self.assertNotIn("GOCSPX-topsecret", client.client_secret_encrypted)

    def test_blank_secret_keeps_the_stored_one(self):
        from booking.models import GoogleOAuthClientSettings

        self.client.post(
            reverse("booking:google_client_save"),
            {"client_id": "id-1", "client_secret": "secret-1"},
        )
        self.client.post(
            reverse("booking:google_client_save"), {"client_id": "id-2", "client_secret": ""}
        )
        client = GoogleOAuthClientSettings.load()
        self.assertEqual(client.client_id, "id-2")
        self.assertEqual(client.get_client_secret(), "secret-1")

    def test_effective_never_mixes_database_and_env(self):
        from booking.models import GoogleOAuthClientSettings

        with self.settings(GOOGLE_OAUTH_CLIENT_ID="env-id", GOOGLE_OAUTH_CLIENT_SECRET="env-secret"):
            self.assertEqual(GoogleOAuthClientSettings.effective(), ("env-id", "env-secret"))

            client = GoogleOAuthClientSettings.load()
            client.client_id = "db-id"
            client.set_client_secret("db-secret")
            client.save()
            self.assertEqual(GoogleOAuthClientSettings.effective(), ("db-id", "db-secret"))

    def test_connect_is_refused_without_a_client(self):
        response = self.client.get(reverse("booking:google_connect"), follow=True)
        self.assertContains(response, "Client ID")
        self.assertRedirects(response, reverse("booking:google_setup"))

    def test_connect_redirects_to_google(self):
        from booking.models import GoogleOAuthClientSettings

        client = GoogleOAuthClientSettings.load()
        client.client_id = "123-abc.apps.googleusercontent.com"
        client.set_client_secret("GOCSPX-x")
        client.save()

        response = self.client.get(reverse("booking:google_connect"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("https://accounts.google.com/o/oauth2/auth"))
        # A refresh token requires offline access, and PKCE is mandatory here.
        for expected in ("access_type=offline", "prompt=consent", "code_challenge"):
            self.assertIn(expected, response["Location"])
        self.assertIn("google_oauth_code_verifier", self.client.session)

    # -- callback ----------------------------------------------------------
    def _fake_creds(self, refresh="refresh-abc"):
        return mock.Mock(
            token="access-xyz",
            refresh_token=refresh,
            expiry=dt.datetime(2030, 1, 1, 12, 0),
            scopes=["https://www.googleapis.com/auth/calendar"],
        )

    def test_callback_stores_an_encrypted_refresh_token(self):
        with mock.patch("booking.google_oauth.finish", return_value=self._fake_creds()), \
             mock.patch("booking.google_oauth.account_email", return_value="salon@gmail.com"):
            response = self.client.get(reverse("booking:google_callback"), {"code": "x", "state": "y"})

        self.assertRedirects(response, reverse("booking:google_setup"))
        credential = GoogleCredential.objects.get(auth_type=GoogleCredential.OAUTH)
        self.assertTrue(credential.is_connected)
        self.assertEqual(credential.oauth_account_email, "salon@gmail.com")
        self.assertEqual(credential.get_refresh_token(), "refresh-abc")
        self.assertNotIn("refresh-abc", credential.oauth_refresh_token)
        self.assertIsNotNone(credential.oauth_token_expiry)
        self.assertTrue(timezone.is_aware(credential.oauth_token_expiry))

    def test_cancelled_consent_is_reported(self):
        response = self.client.get(
            reverse("booking:google_callback"), {"error": "access_denied"}, follow=True
        )
        self.assertContains(response, "cancelled")
        self.assertFalse(GoogleCredential.objects.exists())

    def test_reconnect_without_a_new_refresh_token_keeps_the_old_one(self):
        with mock.patch("booking.google_oauth.finish", return_value=self._fake_creds()), \
             mock.patch("booking.google_oauth.account_email", return_value="salon@gmail.com"):
            self.client.get(reverse("booking:google_callback"), {"code": "x"})

        # Google omits the refresh token on a repeat authorisation.
        with mock.patch("booking.google_oauth.finish", return_value=self._fake_creds(refresh=None)), \
             mock.patch("booking.google_oauth.account_email", return_value="salon@gmail.com"):
            self.client.get(reverse("booking:google_callback"), {"code": "x"})

        credential = GoogleCredential.objects.get(auth_type=GoogleCredential.OAUTH)
        self.assertEqual(credential.get_refresh_token(), "refresh-abc")

    def test_disconnect_clears_the_tokens_but_keeps_calendars(self):
        with mock.patch("booking.google_oauth.finish", return_value=self._fake_creds()), \
             mock.patch("booking.google_oauth.account_email", return_value="salon@gmail.com"):
            self.client.get(reverse("booking:google_callback"), {"code": "x"})
        credential = GoogleCredential.objects.get(auth_type=GoogleCredential.OAUTH)
        self.calendar.credential = credential
        self.calendar.save()

        self.client.post(reverse("booking:google_disconnect"))

        credential.refresh_from_db()
        self.assertFalse(credential.is_connected)
        self.assertTrue(Calendar.objects.filter(pk=self.calendar.pk).exists())

    # -- adding named calendars -------------------------------------------
    def _connect(self):
        with mock.patch("booking.google_oauth.finish", return_value=self._fake_creds()), \
             mock.patch("booking.google_oauth.account_email", return_value="salon@gmail.com"):
            self.client.get(reverse("booking:google_callback"), {"code": "x"})
        return GoogleCredential.objects.get(auth_type=GoogleCredential.OAUTH)

    def test_setup_page_lists_the_accounts_calendars(self):
        self._connect()
        listing = [
            {"id": "a@group.calendar.google.com", "summary": "Maria", "access_role": "owner", "primary": False},
            {"id": "b@group.calendar.google.com", "summary": "Ben", "access_role": "reader", "primary": False},
        ]
        with mock.patch("booking.google_calendar.list_calendars", return_value=listing):
            response = self.client.get(reverse("booking:google_setup"))

        self.assertContains(response, "Maria")
        self.assertContains(response, "Ben")
        # A read-only calendar cannot take bookings, so it offers no Add button.
        self.assertContains(response, "Read-only access")

    def test_adding_a_calendar_puts_it_on_the_homepage(self):
        credential = self._connect()
        response = self.client.post(
            reverse("booking:google_add_calendar"),
            {
                "calendar_id": "a@group.calendar.google.com",
                "name": "Maria - Colour & Cuts",
                "owner_email": "maria@example.com",
            },
        )
        self.assertRedirects(response, reverse("booking:google_setup"))

        calendar = Calendar.objects.get(google_calendar_id="a@group.calendar.google.com")
        self.assertEqual(calendar.name, "Maria - Colour & Cuts")
        self.assertEqual(calendar.owner_email, "maria@example.com")
        self.assertEqual(calendar.credential, credential)
        self.assertTrue(calendar.is_google_connected)

        self.client.logout()
        home = self.client.get(reverse("booking:home"))
        self.assertContains(home, "Maria - Colour &amp; Cuts")

    def test_owner_email_defaults_to_the_connected_account(self):
        self._connect()
        self.client.post(
            reverse("booking:google_add_calendar"),
            {"calendar_id": "a@group.calendar.google.com", "name": "Chair 1", "owner_email": ""},
        )
        calendar = Calendar.objects.get(google_calendar_id="a@group.calendar.google.com")
        self.assertEqual(calendar.owner_email, "salon@gmail.com")

    def test_the_same_calendar_cannot_be_added_twice(self):
        self._connect()
        payload = {
            "calendar_id": "a@group.calendar.google.com",
            "name": "Maria",
            "owner_email": "maria@example.com",
        }
        self.client.post(reverse("booking:google_add_calendar"), payload)
        self.client.post(reverse("booking:google_add_calendar"), dict(payload, name="Duplicate"))
        self.assertEqual(
            Calendar.objects.filter(google_calendar_id="a@group.calendar.google.com").count(), 1
        )

    def test_adding_requires_a_connected_account(self):
        response = self.client.post(
            reverse("booking:google_add_calendar"),
            {"calendar_id": "a@x", "name": "Maria", "owner_email": "m@example.com"},
            follow=True,
        )
        self.assertContains(response, "Connect a Google account first")
        self.assertFalse(Calendar.objects.filter(google_calendar_id="a@x").exists())

    # -- the PythonAnywhere proxy quirk ------------------------------------
    def test_callback_url_is_forced_to_https(self):
        from booking.google_oauth import force_https

        self.assertEqual(force_https("http://salon.pythonanywhere.com/x/"),
                         "https://salon.pythonanywhere.com/x/")
        self.assertEqual(force_https("https://salon.pythonanywhere.com/x/"),
                         "https://salon.pythonanywhere.com/x/")
        # ...but local development stays on http.
        self.assertEqual(force_https("http://127.0.0.1:8000/x/"), "http://127.0.0.1:8000/x/")


class GoogleIntegrationTests(BaseSalonTest):
    """The Google layer is exercised with a stubbed API client."""

    def setUp(self):
        super().setUp()
        self.credential = GoogleCredential.objects.create(
            name="Salon account",
            auth_type=GoogleCredential.SERVICE_ACCOUNT,
            service_account_json='{"client_email": "bot@x.iam.gserviceaccount.com", '
            '"private_key": "-----BEGIN PRIVATE KEY-----", "token_uri": "https://oauth2.googleapis.com/token"}',
        )
        self.calendar.credential = self.credential
        self.calendar.save()

    def test_service_account_email_is_exposed_for_sharing(self):
        self.assertEqual(self.credential.service_account_email, "bot@x.iam.gserviceaccount.com")

    def test_booking_pushes_a_tentative_event(self):
        with mock.patch("booking.google_calendar.push_appointment", return_value="evt-1") as push:
            appointment = self.make_appointment()
        appointment.refresh_from_db()
        self.assertEqual(appointment.google_event_id, "evt-1")
        self.assertEqual(push.call_args.kwargs["notify_attendees"], False)

    def test_accepting_invites_the_customer(self):
        with mock.patch("booking.google_calendar.push_appointment", return_value="evt-1"):
            appointment = self.make_appointment()
        with mock.patch("booking.google_calendar.push_appointment", return_value="evt-1") as push:
            confirm_appointment(appointment)
        self.assertEqual(push.call_args.kwargs["notify_attendees"], True)

    def test_declining_removes_the_event(self):
        with mock.patch("booking.google_calendar.push_appointment", return_value="evt-1"):
            appointment = self.make_appointment()
        with mock.patch("booking.google_calendar.delete_appointment_event") as delete:
            decline_appointment(appointment)
        delete.assert_called_once()
        appointment.refresh_from_db()
        self.assertEqual(appointment.google_event_id, "")

    def test_a_google_outage_does_not_break_booking(self):
        with mock.patch(
            "booking.google_calendar.push_appointment", side_effect=RuntimeError("Google is down")
        ):
            appointment = self.make_appointment()
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.PENDING)
        self.assertIn("Google is down", appointment.google_sync_error)
        self.assertEqual(len(mail.outbox), 2)  # the humans were still told

    def test_service_account_does_not_attach_attendees(self):
        """Google rejects attendees from a plain service account."""
        from booking.google_calendar import _event_body, can_invite_attendees

        self.assertFalse(can_invite_attendees(self.credential))
        appointment = self.make_appointment()
        body = _event_body(
            appointment, self.salon, allow_attendees=can_invite_attendees(self.credential)
        )
        self.assertNotIn("attendees", body)
        # The customer is still recorded, just in the description.
        self.assertIn("ana@example.com", body["description"])

    def test_delegated_service_account_may_invite(self):
        from booking.google_calendar import can_invite_attendees

        self.credential.delegated_user = "boss@salon.example"
        self.assertTrue(can_invite_attendees(self.credential))

    def test_oauth_credential_may_invite(self):
        from booking.google_calendar import can_invite_attendees

        oauth = GoogleCredential(
            name="Personal", auth_type=GoogleCredential.OAUTH, oauth_refresh_token="x"
        )
        self.assertTrue(can_invite_attendees(oauth))

    def test_provisioning_creates_and_shares_a_calendar(self):
        from booking.services import provision_google_calendar

        fresh = Calendar.objects.create(
            name="Ben",
            credential=self.credential,
            owner_email="ben@example.com",
        )
        self.assertEqual(fresh.google_calendar_id, "")

        with mock.patch(
            "booking.google_calendar.create_calendar", return_value="new-cal-id"
        ) as create, mock.patch("booking.google_calendar.grant_calendar_access") as grant:
            calendar_id = provision_google_calendar(fresh)

        self.assertEqual(calendar_id, "new-cal-id")
        fresh.refresh_from_db()
        self.assertEqual(fresh.google_calendar_id, "new-cal-id")
        self.assertTrue(fresh.is_google_connected)
        self.assertEqual(fresh.last_sync_error, "")

        # Named after the salon, and shared with the owner.
        self.assertIn("Ben", create.call_args.kwargs["name"])
        self.assertEqual(grant.call_args.args[2], "ben@example.com")

    def test_provisioning_refuses_to_replace_an_existing_calendar(self):
        from booking.services import provision_google_calendar

        with self.assertRaises(ValueError):
            provision_google_calendar(self.calendar)  # already has an ID

    def test_provisioning_needs_a_credential(self):
        from booking.services import provision_google_calendar

        orphan = Calendar.objects.create(name="No creds", owner_email="x@example.com")
        with self.assertRaises(ValueError):
            provision_google_calendar(orphan)

    def test_a_failed_share_keeps_the_new_calendar(self):
        from booking.services import provision_google_calendar

        fresh = Calendar.objects.create(
            name="Ben", credential=self.credential, owner_email="ben@example.com"
        )
        with mock.patch("booking.google_calendar.create_calendar", return_value="new-cal-id"), \
             mock.patch(
                 "booking.google_calendar.grant_calendar_access",
                 side_effect=RuntimeError("quota exceeded"),
             ):
            calendar_id = provision_google_calendar(fresh)

        self.assertEqual(calendar_id, "new-cal-id")
        fresh.refresh_from_db()
        self.assertEqual(fresh.google_calendar_id, "new-cal-id")  # not lost
        self.assertIn("quota exceeded", fresh.last_sync_error)

    def test_google_busy_blocks_hide_slots(self):
        self.salon.respect_google_busy = True
        self.salon.save()
        monday = next_weekday(0)
        busy = [
            availability.google_calendar.BusyBlock(
                start=self.aware(monday, 11), end=self.aware(monday, 13)
            )
        ]
        with mock.patch("booking.google_calendar.safe_free_busy", return_value=busy):
            labels = [slot.label for slot in availability.day_slots(self.calendar, monday)]
        self.assertNotIn("11:00", labels)
        self.assertNotIn("12:30", labels)
        self.assertIn("13:00", labels)
        self.assertIn("09:00", labels)
