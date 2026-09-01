"""Tests for live chat.

The interesting cases are the ones that would embarrass a salon: a customer
seeing somebody else's conversation, a chat request vanishing when staff turn
the feature off, or a transcript outliving its retention window.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from booking.models import BusinessHours, SalonSettings, TimeOff
from chat.models import ChatSettings, Conversation, Message


class ChatTestCase(TestCase):
    def setUp(self):
        self.settings_row = ChatSettings.load()
        self.settings_row.enabled = True
        self.settings_row.require_name = True
        # These tests are about the conversation flow, so take the clock out of
        # it; AvailabilityTests turns business hours back on deliberately.
        self.settings_row.follow_business_hours = False
        self.settings_row.save()
        self.staff = User.objects.create_user("stylist", password="pw12345!", is_staff=True)

    def start_chat(self, client=None, name="Ana", text=""):
        client = client or self.client
        data = {"name": name, "page": "/book/maria/"}
        if text:
            data["text"] = text
        return client.post(reverse("chat:start"), data)


class WidgetStateTests(ChatTestCase):
    def test_widget_reports_disabled_when_switched_off(self):
        self.settings_row.enabled = False
        self.settings_row.save()
        payload = self.client.get(reverse("chat:widget")).json()
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["state"], "disabled")

    def test_offline_state_when_the_bubble_should_stay_visible(self):
        self.settings_row.enabled = False
        self.settings_row.show_when_offline = True
        self.settings_row.save()
        payload = self.client.get(reverse("chat:widget")).json()
        self.assertEqual(payload["state"], "offline")
        self.assertIn("book online", payload["offline_text"].lower())

    def test_fresh_visitor_has_no_session(self):
        payload = self.client.get(reverse("chat:widget")).json()
        self.assertEqual(payload["state"], "no_session")
        self.assertIsNone(payload["conversation_id"])

    def test_starting_a_chat_moves_to_pending(self):
        response = self.start_chat()
        self.assertEqual(response.status_code, 200)
        conversation = Conversation.objects.get()
        self.assertEqual(conversation.status, Conversation.STATUS_PENDING)
        self.assertEqual(conversation.guest_name, "Ana")
        self.assertEqual(conversation.opened_from, "/book/maria/")
        # The salon greets first, by name, so the visitor can type immediately.
        opening = conversation.messages.get()
        self.assertEqual(opening.sender_type, Message.STAFF)
        self.assertEqual(opening.text, "Hi Ana, how may I help you?")

        self.assertEqual(self.client.get(reverse("chat:widget")).json()["state"], "pending")

    def test_name_is_required_when_configured(self):
        response = self.start_chat(name="")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Conversation.objects.exists())

    def test_name_is_optional_when_configured_off(self):
        self.settings_row.require_name = False
        self.settings_row.save()
        self.assertEqual(self.start_chat(name="").status_code, 200)
        self.assertTrue(Conversation.objects.exists())

    def test_starting_twice_reuses_the_open_conversation(self):
        self.start_chat()
        self.start_chat()
        self.assertEqual(Conversation.objects.count(), 1)

    def test_cannot_start_a_chat_when_disabled(self):
        self.settings_row.enabled = False
        self.settings_row.save()
        self.assertEqual(self.start_chat().status_code, 403)
        self.assertFalse(Conversation.objects.exists())


class AvailabilityTests(ChatTestCase):
    """The master switch and the business-hours rule, and how they interact."""

    def setUp(self):
        super().setUp()
        self.settings_row.follow_business_hours = True
        self.settings_row.save()

        salon = SalonSettings.load()
        salon.timezone_name = "Europe/Berlin"
        salon.save()
        self.tz = salon.tz

        # Open Mon-Fri 09:00-17:00, closed at the weekend.
        for weekday in range(7):
            BusinessHours.objects.update_or_create(
                calendar=None,
                weekday=weekday,
                defaults={
                    "is_closed": weekday >= 5,
                    "opens_at": time(9, 0),
                    "closes_at": time(17, 0),
                },
            )

    def at(self, weekday: int, hour: int, minute: int = 0):
        """An aware datetime on the next given weekday."""
        day = timezone.localdate(timezone=self.tz) + timedelta(days=1)
        while day.weekday() != weekday:
            day += timedelta(days=1)
        return datetime.combine(day, time(hour, minute), tzinfo=self.tz)

    def test_available_during_opening_hours(self):
        self.assertTrue(self.settings_row.is_available(self.at(2, 11)))  # Wednesday 11:00

    def test_unavailable_before_opening(self):
        state = self.settings_row.status(self.at(2, 8))
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "outside_hours")
        self.assertEqual(timezone.localtime(state["next_open"], self.tz).hour, 9)

    def test_unavailable_after_closing(self):
        self.assertFalse(self.settings_row.is_available(self.at(2, 18)))

    def test_unavailable_at_the_weekend(self):
        state = self.settings_row.status(self.at(5, 11))  # Saturday
        self.assertFalse(state["available"])
        # Next opening is the Monday.
        self.assertEqual(timezone.localtime(state["next_open"], self.tz).weekday(), 0)

    def test_lunch_break_closes_chat_too(self):
        BusinessHours.objects.filter(calendar__isnull=True, weekday=2).update(
            break_start=time(12, 0), break_end=time(13, 0)
        )
        self.assertFalse(self.settings_row.is_available(self.at(2, 12, 30)))
        self.assertTrue(self.settings_row.is_available(self.at(2, 13, 30)))

    def test_salon_vacation_closes_chat(self):
        wednesday = self.at(2, 11)
        TimeOff.objects.create(
            calendar=None,
            start_date=wednesday.date(),
            end_date=wednesday.date(),
            all_day=True,
            reason="Team training",
        )
        self.assertFalse(self.settings_row.is_available(wednesday))

    def test_no_configured_hours_does_not_silently_disable_chat(self):
        """A salon with no opening hours yet should not get a dead bubble."""
        BusinessHours.objects.all().delete()
        state = self.settings_row.status(self.at(5, 3))  # Saturday, 03:00
        self.assertTrue(state["available"])
        self.assertEqual(state["reason"], "hours_not_configured")

    def test_master_switch_off_beats_open_hours(self):
        """The switch is the override: off means off even mid-morning."""
        self.settings_row.enabled = False
        self.settings_row.save()
        state = self.settings_row.status(self.at(2, 11))
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "switched_off")

    def test_ignoring_business_hours_makes_chat_always_available(self):
        self.settings_row.follow_business_hours = False
        self.settings_row.save()
        self.assertTrue(self.settings_row.is_available(self.at(5, 3)))  # Saturday, 03:00

    def test_switch_still_wins_when_hours_are_ignored(self):
        self.settings_row.follow_business_hours = False
        self.settings_row.enabled = False
        self.settings_row.save()
        self.assertFalse(self.settings_row.is_available(self.at(2, 11)))

    def test_offline_message_says_when_we_are_back(self):
        message = self.settings_row.unavailable_message(self.at(2, 7))
        self.assertIn("09:00", message)

    def test_widget_reports_offline_outside_hours(self):
        self.settings_row.show_when_offline = True
        self.settings_row.save()
        with mock.patch(
            "chat.availability._is_open_at", return_value=False
        ), mock.patch("chat.availability.next_opening", return_value=None):
            payload = self.client.get(reverse("chat:widget")).json()
        self.assertEqual(payload["state"], "offline")

    def test_cannot_start_a_chat_outside_hours(self):
        with mock.patch("chat.availability._is_open_at", return_value=False), \
             mock.patch("chat.availability.next_opening", return_value=None):
            response = self.start_chat()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Conversation.objects.exists())

    def open_chat(self):
        """Start and accept a chat as if the salon were open.

        The salon is forced open explicitly: without this the test would only
        pass while the real clock happened to sit inside business hours, and
        fail every evening.
        """
        with mock.patch("chat.availability._is_open_at", return_value=True):
            self.start_chat()
        conversation = Conversation.objects.get()
        staff_client = self.client_class()
        staff_client.force_login(self.staff)
        staff_client.post(reverse("chat:accept", args=[conversation.pk]))
        return conversation, staff_client

    def test_a_live_chat_survives_closing_time(self):
        """Closing time must not cut somebody off mid-sentence."""
        self.open_chat()

        with mock.patch("chat.availability._is_open_at", return_value=False), \
             mock.patch("chat.availability.next_opening", return_value=None):
            payload = self.client.get(reverse("chat:widget")).json()
            self.assertEqual(payload["state"], "accepted")

            sent = self.client.post(reverse("chat:send"), {"text": "one last thing"})
            self.assertEqual(sent.status_code, 200)

    def test_the_master_switch_does_end_live_chats(self):
        """Unlike closing time, the switch closes everything immediately."""
        conversation, staff_client = self.open_chat()
        staff_client.post(reverse("chat:toggle"))

        conversation.refresh_from_db()
        self.assertEqual(conversation.status, Conversation.STATUS_CLOSED)
        self.assertEqual(
            self.client.post(reverse("chat:send"), {"text": "hello?"}).status_code, 403
        )

    def test_desk_reports_the_computed_state(self):
        staff_client = self.client_class()
        staff_client.force_login(self.staff)
        with mock.patch("chat.availability._is_open_at", return_value=False), \
             mock.patch("chat.availability.next_opening", return_value=None):
            payload = staff_client.get(reverse("chat:live_data")).json()
        self.assertTrue(payload["enabled"])       # switch is on...
        self.assertFalse(payload["available"])    # ...but the salon is closed
        self.assertEqual(payload["reason"], "outside_hours")


class ConversationFlowTests(ChatTestCase):
    def test_accepting_greets_the_customer(self):
        self.start_chat()
        conversation = Conversation.objects.get()

        self.client.force_login(self.staff)
        self.client.post(reverse("chat:accept", args=[conversation.pk]))

        conversation.refresh_from_db()
        self.assertEqual(conversation.status, Conversation.STATUS_ACCEPTED)
        self.assertEqual(conversation.accepted_by, self.staff)

        # Two staff messages now: the automatic opener, then the handover.
        staff_messages = list(conversation.messages.filter(sender_type=Message.STAFF))
        self.assertEqual(len(staff_messages), 2)
        self.assertEqual(staff_messages[0].text, "Hi Ana, how may I help you?")
        self.assertIn("stylist", staff_messages[1].text.lower())
        self.assertNotIn("{staff}", staff_messages[1].text)  # rendered, not raw

    def test_customer_can_reply_to_the_greeting_before_anyone_accepts(self):
        """The whole point of greeting first: no dead wait."""
        self.start_chat()
        conversation = Conversation.objects.get()

        ok = self.client.post(reverse("chat:send"), {"text": "Do you do balayage?"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(conversation.messages.filter(text="Do you do balayage?").exists())

        # Staff see it waiting, question and all, before accepting.
        staff_client = self.client_class()
        staff_client.force_login(self.staff)
        payload = staff_client.get(reverse("chat:live_data")).json()
        self.assertEqual(payload["waiting"][0]["last_message"], "Do you do balayage?")

    def test_a_closed_chat_refuses_new_messages(self):
        self.start_chat()
        conversation = Conversation.objects.get()
        conversation.status = Conversation.STATUS_CLOSED
        conversation.save()
        self.assertEqual(
            self.client.post(reverse("chat:send"), {"text": "hello?"}).status_code, 400
        )

    def test_accepting_says_who_took_over(self):
        self.start_chat()
        conversation = Conversation.objects.get()
        staff_client = self.client_class()
        staff_client.force_login(self.staff)
        staff_client.post(reverse("chat:accept", args=[conversation.pk]))

        handover = conversation.messages.filter(sender_type=Message.STAFF).last()
        self.assertIn("stylist", handover.text.lower())
        self.assertEqual(handover.sender_name, "stylist")

    def test_empty_messages_are_rejected(self):
        self.start_chat()
        self.assertEqual(self.client.post(reverse("chat:send"), {"text": "   "}).status_code, 400)

    def test_declining_sends_the_busy_message_and_frees_the_visitor(self):
        self.start_chat()
        conversation = Conversation.objects.get()

        staff_client = self.client_class()
        staff_client.force_login(self.staff)
        staff_client.post(reverse("chat:reject", args=[conversation.pk]))

        payload = self.client.get(reverse("chat:widget")).json()
        self.assertEqual(payload["state"], "rejected")
        self.assertIn("Sorry", payload["messages"][0]["text"])

        # Dismissing the apology lets them try again rather than trapping them.
        self.client.post(reverse("chat:dismiss"))
        self.assertEqual(self.client.get(reverse("chat:widget")).json()["state"], "no_session")
        self.start_chat()
        self.assertEqual(Conversation.objects.filter(status=Conversation.STATUS_PENDING).count(), 1)

    def test_incremental_polling_only_returns_new_messages(self):
        self.start_chat()
        conversation = Conversation.objects.get()
        staff_client = self.client_class()
        staff_client.force_login(self.staff)
        staff_client.post(reverse("chat:accept", args=[conversation.pk]))

        first = self.client.get(reverse("chat:widget")).json()
        self.assertTrue(first["messages"])  # greeting + handover
        latest_id = first["messages"][-1]["id"]

        again = self.client.get(reverse("chat:widget"), {"since_id": latest_id}).json()
        self.assertEqual(again["messages"], [])

        staff_client.post(reverse("chat:staff_send", args=[conversation.pk]), {"text": "Yes we do!"})
        after = self.client.get(reverse("chat:widget"), {"since_id": latest_id}).json()
        self.assertEqual(len(after["messages"]), 1)
        self.assertEqual(after["messages"][0]["text"], "Yes we do!")

    def test_closing_resets_the_widget(self):
        self.start_chat()
        conversation = Conversation.objects.get()
        staff_client = self.client_class()
        staff_client.force_login(self.staff)
        staff_client.post(reverse("chat:accept", args=[conversation.pk]))
        staff_client.post(reverse("chat:close", args=[conversation.pk]))

        self.assertEqual(self.client.get(reverse("chat:widget")).json()["state"], "no_session")
        conversation.refresh_from_db()
        self.assertIsNotNone(conversation.closed_at)


class IsolationTests(ChatTestCase):
    """One visitor must never see another's conversation."""

    def test_two_visitors_get_separate_conversations(self):
        visitor_a = self.client_class()
        visitor_b = self.client_class()
        visitor_a.post(reverse("chat:start"), {"name": "Ana", "text": "Hi"})
        visitor_b.post(reverse("chat:start"), {"name": "Bea", "text": "Hello"})

        self.assertEqual(Conversation.objects.count(), 2)
        a_id = visitor_a.get(reverse("chat:widget")).json()["conversation_id"]
        b_id = visitor_b.get(reverse("chat:widget")).json()["conversation_id"]
        self.assertNotEqual(a_id, b_id)

    def test_a_visitor_cannot_send_into_another_conversation(self):
        visitor_a = self.client_class()
        visitor_b = self.client_class()
        visitor_a.post(reverse("chat:start"), {"name": "Ana", "text": "Hi"})
        conversation_a = Conversation.objects.get()

        staff_client = self.client_class()
        staff_client.force_login(self.staff)
        staff_client.post(reverse("chat:accept", args=[conversation_a.pk]))

        # B has no conversation of their own, so sending must fail rather than
        # landing in A's thread.
        response = visitor_b.post(reverse("chat:send"), {"text": "eavesdropping"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Message.objects.filter(text="eavesdropping").exists())

    def test_staff_endpoints_require_staff(self):
        self.start_chat()
        conversation = Conversation.objects.get()
        for name, args in [
            ("chat:desk", []),
            ("chat:live_data", []),
            ("chat:thread", [conversation.pk]),
        ]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302, name)
        self.assertEqual(
            self.client.post(reverse("chat:accept", args=[conversation.pk])).status_code, 302
        )
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, Conversation.STATUS_PENDING)


class StaffDeskTests(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def test_desk_lists_waiting_conversations(self):
        visitor = self.client_class()
        self.start_chat(client=visitor, text="Do you do balayage?")

        payload = self.client.get(reverse("chat:live_data")).json()
        self.assertEqual(len(payload["waiting"]), 1)
        self.assertEqual(payload["waiting"][0]["name"], "Ana")
        self.assertEqual(payload["waiting"][0]["last_message"], "Do you do balayage?")
        self.assertEqual(payload["waiting"][0]["opened_from"], "/book/maria/")

    def test_toggling_off_closes_open_chats(self):
        visitor = self.client_class()
        visitor.post(reverse("chat:start"), {"name": "Ana", "text": "Hi"})
        conversation = Conversation.objects.get()
        self.client.post(reverse("chat:accept", args=[conversation.pk]))

        self.client.post(reverse("chat:toggle"))

        self.settings_row.refresh_from_db()
        self.assertFalse(self.settings_row.enabled)
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, Conversation.STATUS_CLOSED)
        # The transcript survives.
        self.assertTrue(conversation.messages.exists())

    def test_dashboard_shows_waiting_chats(self):
        visitor = self.client_class()
        visitor.post(reverse("chat:start"), {"name": "Ana", "text": "Hi"})
        response = self.client.get(reverse("booking:dashboard"))
        self.assertContains(response, "waiting to chat")


class RetentionTests(ChatTestCase):
    def test_purge_deletes_only_old_conversations(self):
        self.start_chat()
        old = Conversation.objects.create(session_key="old", status=Conversation.STATUS_CLOSED)
        Message.objects.create(conversation=old, sender_type=Message.CUSTOMER, text="ancient")
        Conversation.objects.filter(pk=old.pk).update(
            updated_at=timezone.now() - timedelta(days=400)
        )

        call_command("purge_old_chats", verbosity=0)

        self.assertFalse(Conversation.objects.filter(pk=old.pk).exists())
        self.assertFalse(Message.objects.filter(text="ancient").exists())
        self.assertEqual(Conversation.objects.count(), 1)

    def test_dry_run_deletes_nothing(self):
        old = Conversation.objects.create(session_key="old", status=Conversation.STATUS_CLOSED)
        Conversation.objects.filter(pk=old.pk).update(
            updated_at=timezone.now() - timedelta(days=400)
        )
        call_command("purge_old_chats", dry_run=True, verbosity=0)
        self.assertTrue(Conversation.objects.filter(pk=old.pk).exists())

    def test_retention_window_is_configurable(self):
        self.settings_row.retention_days = 7
        self.settings_row.save()
        recent = Conversation.objects.create(session_key="x", status=Conversation.STATUS_CLOSED)
        Conversation.objects.filter(pk=recent.pk).update(
            updated_at=timezone.now() - timedelta(days=10)
        )
        call_command("purge_old_chats", verbosity=0)
        self.assertFalse(Conversation.objects.filter(pk=recent.pk).exists())


class EscapingTests(ChatTestCase):
    def test_message_text_is_returned_as_data_not_markup(self):
        """The widget renders with textContent, and the API must not pre-escape."""
        self.start_chat()
        self.client.post(reverse("chat:send"), {"text": "<script>alert(1)</script>"})
        payload = self.client.get(reverse("chat:widget")).json()
        texts = [m["text"] for m in payload["messages"]]
        self.assertIn("<script>alert(1)</script>", texts)

    def test_overlong_messages_are_truncated(self):
        self.start_chat()
        conversation = Conversation.objects.get()
        self.client.post(reverse("chat:send"), {"text": "x" * 5000})
        self.assertEqual(len(conversation.messages.filter(sender_type=Message.CUSTOMER).last().text), 2000)


class WidgetVisibilityTests(ChatTestCase):
    """The bubble must be in the page without JavaScript having to reveal it."""

    def test_bubble_is_visible_in_the_html_when_chat_is_available(self):
        response = self.client.get("/")
        page = response.content.decode()
        self.assertIn('id="chat-widget"', page)
        # No hidden attribute on the widget div: it renders straight away.
        self.assertNotIn('class="chat-widget" hidden', page)

    def test_bubble_is_hidden_in_the_html_when_chat_is_off(self):
        self.settings_row.enabled = False
        self.settings_row.save()
        page = self.client.get("/").content.decode()
        self.assertIn('class="chat-widget" hidden', page)

    def test_bubble_shows_when_offline_if_configured(self):
        self.settings_row.enabled = False
        self.settings_row.show_when_offline = True
        self.settings_row.save()
        page = self.client.get("/").content.decode()
        self.assertNotIn('class="chat-widget" hidden', page)

    def test_bubble_is_hidden_outside_business_hours(self):
        self.settings_row.follow_business_hours = True
        self.settings_row.save()
        # Hours must exist, or the "not configured yet" fallback keeps chat on.
        BusinessHours.objects.create(
            calendar=None, weekday=0, opens_at=time(9, 0), closes_at=time(17, 0)
        )
        with mock.patch("chat.availability._is_open_at", return_value=False), \
             mock.patch("chat.availability.next_opening", return_value=None):
            page = self.client.get("/").content.decode()
        self.assertIn('class="chat-widget" hidden', page)

    def test_the_widget_appears_on_every_customer_page(self):
        from booking.models import Calendar

        calendar = Calendar.objects.create(name="Maria", owner_email="m@example.com")
        for url in ["/", calendar.get_absolute_url()]:
            self.assertIn('id="chat-widget"', self.client.get(url).content.decode(), url)

    def test_javascript_is_referenced_and_collected(self):
        page = self.client.get("/").content.decode()
        self.assertIn("js/chat.js", page)
        # The file the template points at must actually exist in the app.
        from pathlib import Path

        from django.conf import settings as django_settings

        source = Path(django_settings.BASE_DIR) / "static" / "js" / "chat.js"
        self.assertTrue(source.exists(), "static/js/chat.js is missing from the project")


class TemplateHygieneTests(TestCase):
    """Guard against template mistakes that leak text onto customer pages."""

    def test_no_multiline_django_comments_in_any_template(self):
        """A {# #} comment spanning lines is not a comment: it renders."""
        from pathlib import Path

        from django.conf import settings as django_settings

        leaks = []
        for path in Path(django_settings.BASE_DIR).rglob("*.html"):
            if "staticfiles" in str(path):
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "{#" in line and "#}" not in line.split("{#", 1)[1]:
                    leaks.append(f"{path.name}:{number}")
        self.assertEqual(leaks, [], f"multi-line {{# #}} comments leak onto the page: {leaks}")

    def test_no_stray_template_syntax_reaches_the_homepage(self):
        page = self.client.get("/").content.decode()
        for marker in ["{#", "#}", "{%", "%}", "{{", "}}"]:
            self.assertNotIn(marker, page, f"unrendered template syntax {marker!r} on the page")


class HiddenAttributeTests(TestCase):
    """`hidden` must actually hide, or the chat window opens by itself."""

    def stylesheet(self):
        from pathlib import Path

        from django.conf import settings as django_settings

        return (Path(django_settings.BASE_DIR) / "static" / "css" / "style.css").read_text(
            encoding="utf-8"
        )

    def test_stylesheet_forces_hidden_elements_to_stay_hidden(self):
        """A class setting `display` outbeats the browser's [hidden] default.

        .chat-panel{display:flex} once left the chat window open on every page
        load, so the guard below is load-bearing, not decoration.
        """
        css = self.stylesheet()
        self.assertRegex(
            css.replace("\n", " "),
            r"\[hidden\]\s*\{\s*display:\s*none\s*!important",
            "style.css must force [hidden] to display:none !important",
        )

    def test_the_chat_panel_starts_closed_in_the_markup(self):
        page = self.client.get("/").content.decode()
        panel = page[page.index('id="chat-panel"'):]
        opening_tag = panel[: panel.index(">")]
        self.assertIn("hidden", opening_tag, "the chat panel must render closed")

    def test_no_element_relies_on_a_display_rule_to_be_hidden(self):
        """Any class that sets display AND is toggled with `hidden` needs the guard."""
        import re
        from pathlib import Path

        from django.conf import settings as django_settings

        css = self.stylesheet()
        with_display = {
            m.group(1)
            for m in re.finditer(r"\.([a-zA-Z0-9_-]+)\s*\{[^}]*?\bdisplay\s*:", css, re.S)
        }
        clashes = []
        for path in Path(django_settings.BASE_DIR).rglob("*.html"):
            if "staticfiles" in str(path):
                continue
            for tag in re.finditer(
                r"<[a-z]+[^>]*\bhidden\b[^>]*>", path.read_text(encoding="utf-8")
            ):
                classes = re.search(r'class="([^"]*)"', tag.group(0))
                if classes:
                    clashes += [c for c in classes.group(1).split() if c in with_display]

        if clashes:
            # Not a failure in itself: it is only safe because of the guard.
            self.assertRegex(
                css.replace("\n", " "),
                r"\[hidden\]\s*\{\s*display:\s*none\s*!important",
                f"these classes set display and are toggled with hidden: {sorted(set(clashes))}",
            )
