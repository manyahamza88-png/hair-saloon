"""Live chat: conversations and messages.

Deliberately poll-based rather than WebSocket-based, because PythonAnywhere
does not offer WebSockets on the plans a small salon is likely to use. The
customer's browser asks for new messages every few seconds; the poll interval
is admin-configurable so a busy shop can trade responsiveness against CPU
seconds.
"""
from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class ChatSettings(models.Model):
    """Singleton: live chat on/off, wording, retention."""

    enabled = models.BooleanField(
        "live chat switched on",
        default=False,
        help_text=(
            "The master switch. Off means no chat at all, whatever the opening hours say -- "
            "use it when nobody can answer."
        ),
    )
    follow_business_hours = models.BooleanField(
        default=True,
        help_text=(
            "Only offer chat while the salon is open, using the same business hours and "
            "vacation entries as the booking calendar. Turn off to offer chat around the clock."
        ),
    )
    welcome_heading = models.CharField(
        max_length=120,
        default="Chat with the salon",
        help_text="Title at the top of the chat window.",
    )
    welcome_text = models.TextField(
        default="Ask us anything about services, prices or availability. We usually reply within a few minutes.",
        help_text="Shown before the customer starts a chat.",
    )
    offline_text = models.TextField(
        default="Nobody is available to chat right now. Please book online or send us an email.",
        help_text="Shown instead of the chat form when live chat is switched off but you still want the bubble visible.",
    )
    show_when_offline = models.BooleanField(
        default=False,
        help_text="Keep the bubble visible when chat is off, showing the message above instead of hiding entirely.",
    )
    auto_greeting = models.CharField(
        max_length=200,
        default="Hi {name}, how may I help you?",
        help_text=(
            "Posted automatically the moment a visitor starts a chat, so they can type "
            "straight away. Use {name} for the name they gave."
        ),
    )
    greeting = models.CharField(
        max_length=200,
        default="{staff} here — I am with you now.",
        help_text=(
            "Posted when a member of staff picks the chat up. Use {staff} for their name."
        ),
    )
    busy_text = models.TextField(
        default=(
            "Sorry, we are all with clients at the moment and cannot take the chat. "
            "Please try again a little later, or book online."
        ),
        help_text="Sent automatically when a member of staff declines a chat request.",
    )

    poll_seconds = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(2), MaxValueValidator(60)],
        help_text="How often an open chat window checks for new messages. Higher = less server load.",
    )
    require_name = models.BooleanField(
        default=True, help_text="Ask the customer for a first name before starting the chat."
    )
    retention_days = models.PositiveIntegerField(
        default=365,
        validators=[MinValueValidator(1)],
        help_text="Conversations untouched for longer than this are deleted by 'manage.py purge_old_chats'.",
    )

    class Meta:
        verbose_name = "chat settings"
        verbose_name_plural = "chat settings"

    def __str__(self) -> str:
        if not self.enabled:
            return "Live chat (switched off)"
        return "Live chat (on, business hours)" if self.follow_business_hours else "Live chat (always on)"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "ChatSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    # -- availability -----------------------------------------------------
    def render_auto_greeting(self, name: str) -> str:
        """The opening line. Falls back gracefully if the template is odd."""
        try:
            return self.auto_greeting.format(name=name or "there")
        except (KeyError, IndexError, ValueError):
            return self.auto_greeting

    def render_greeting(self, staff_name: str) -> str:
        try:
            return self.greeting.format(staff=staff_name or "A stylist")
        except (KeyError, IndexError, ValueError):
            return self.greeting

    def status(self, now=None) -> dict:
        from .availability import status

        return status(self, now)

    def is_available(self, now=None) -> bool:
        return self.status(now)["available"]

    def unavailable_message(self, now=None) -> str:
        from .availability import describe

        return describe(self, now)


class ConversationQuerySet(models.QuerySet):
    def open(self):
        return self.filter(
            status__in=[Conversation.STATUS_PENDING, Conversation.STATUS_ACCEPTED]
        )

    def waiting(self):
        return self.filter(status=Conversation.STATUS_PENDING)


class Conversation(models.Model):
    """One chat with one visitor."""

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_CLOSED = "closed"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Waiting"),
        (STATUS_ACCEPTED, "Live"),
        (STATUS_CLOSED, "Ended"),
        (STATUS_REJECTED, "Declined"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
    )
    session_key = models.CharField(max_length=40, blank=True, db_index=True)
    guest_name = models.CharField(max_length=80, blank=True)
    guest_email = models.EmailField(blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    # Where the visitor was when they opened the chat: lets staff answer
    # "is Maria free on Saturday?" without asking which page they mean.
    opened_from = models.CharField(max_length=255, blank=True)

    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_conversations",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ConversationQuerySet.as_manager()

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["status", "-updated_at"])]

    def __str__(self) -> str:
        return f"Chat with {self.display_name} ({self.get_status_display()})"

    @property
    def display_name(self) -> str:
        if self.guest_name:
            return self.guest_name
        if self.user:
            return self.user.get_full_name() or self.user.get_username()
        return f"Guest {self.session_key[:6]}" if self.session_key else "Guest"

    @property
    def is_open(self) -> bool:
        return self.status in (self.STATUS_PENDING, self.STATUS_ACCEPTED)

    def touch(self) -> None:
        Conversation.objects.filter(pk=self.pk).update(updated_at=timezone.now())

    def waiting_seconds(self) -> int:
        return int((timezone.now() - self.created_at).total_seconds())


class Message(models.Model):
    CUSTOMER = "customer"
    STAFF = "staff"
    SYSTEM = "system"
    SENDER_CHOICES = [
        (CUSTOMER, "Customer"),
        (STAFF, "Salon"),
        (SYSTEM, "System"),
    ]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender_type = models.CharField(max_length=10, choices=SENDER_CHOICES)
    sender_name = models.CharField(max_length=80, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.get_sender_type_display()}: {self.text[:40]}"
