"""Chat endpoints: a small JSON API for the customer widget and the staff desk.

Every customer endpoint is safe to call anonymously and identifies the visitor
by their Django session, so no login is needed to talk to the salon.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Max, Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from booking.models import SalonSettings

from .models import ChatSettings, Conversation, Message

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 2000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _session_key(request) -> str:
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def _current_conversation(request, include_rejected: bool = True):
    """The visitor's current conversation, or None.

    ``include_rejected`` is True for the widget poll, so the customer can read
    the "we are busy" reply and dismiss it. It is False when starting a new
    chat, so a previous decline does not block a fresh attempt.
    """
    statuses = [Conversation.STATUS_PENDING, Conversation.STATUS_ACCEPTED]
    if include_rejected:
        statuses.append(Conversation.STATUS_REJECTED)

    qs = Conversation.objects.filter(status__in=statuses)
    if request.user.is_authenticated:
        found = qs.filter(user=request.user).order_by("-updated_at").first()
        if found:
            return found
    return qs.filter(session_key=_session_key(request)).order_by("-updated_at").first()


def _owns(request, conversation: Conversation) -> bool:
    """Guard so one visitor cannot read another's chat by guessing an id."""
    if request.user.is_authenticated and conversation.user_id == request.user.pk:
        return True
    return bool(conversation.session_key) and conversation.session_key == request.session.session_key


def _message_json(message: Message) -> dict:
    return {
        "id": message.pk,
        "sender_type": message.sender_type,
        "sender_name": message.sender_name,
        "text": message.text,
        "time": timezone.localtime(message.created_at).strftime("%H:%M"),
    }


def _messages_since(conversation: Conversation, since_id) -> list[dict]:
    qs = conversation.messages.all()
    if since_id:
        try:
            qs = qs.filter(pk__gt=int(since_id))
        except (TypeError, ValueError):
            pass
    return [_message_json(m) for m in qs]


# ---------------------------------------------------------------------------
# Customer side
# ---------------------------------------------------------------------------
def widget(request):
    """Poll endpoint driving the customer widget.

    States: ``disabled``, ``offline``, ``no_session``, ``pending``,
    ``accepted``, ``rejected``, ``closed``.
    """
    chat_settings = ChatSettings.load()
    conversation = _current_conversation(request)

    if not chat_settings.is_available():
        # A chat already in progress is never cut off mid-sentence just because
        # the salon hit closing time: it plays out, and only new ones are
        # refused. The master switch does close everything, in toggle().
        if conversation is None or conversation.status != Conversation.STATUS_ACCEPTED:
            state = "offline" if chat_settings.show_when_offline else "disabled"
            return JsonResponse(
                {
                    "enabled": False,
                    "state": state,
                    "offline_text": chat_settings.unavailable_message(),
                    "heading": chat_settings.welcome_heading,
                    "poll_seconds": 30,
                }
            )

    payload = {
        "enabled": True,
        "heading": chat_settings.welcome_heading,
        "welcome_text": chat_settings.welcome_text,
        "require_name": chat_settings.require_name,
        "poll_seconds": chat_settings.poll_seconds,
    }

    if conversation is None:
        return JsonResponse({**payload, "state": "no_session", "conversation_id": None, "messages": []})

    payload["conversation_id"] = conversation.pk

    if conversation.status == Conversation.STATUS_PENDING:
        return JsonResponse(
            {
                **payload,
                "state": "pending",
                "waiting_seconds": conversation.waiting_seconds(),
                "messages": _messages_since(conversation, request.GET.get("since_id")),
            }
        )

    if conversation.status == Conversation.STATUS_REJECTED:
        system = [_message_json(m) for m in conversation.messages.filter(sender_type=Message.SYSTEM)]
        return JsonResponse({**payload, "state": "rejected", "messages": system})

    return JsonResponse(
        {
            **payload,
            "state": "accepted",
            "messages": _messages_since(conversation, request.GET.get("since_id")),
        }
    )


def bot_menu(request):
    """First step of the in-chat booking helper: a greeting plus the services
    on offer, so a visitor can find a slot without ever waiting for a member
    of staff. No ``Conversation`` exists yet at this point -- that only
    happens if they go on to request live chat."""
    chat_settings = ChatSettings.load()
    if not chat_settings.is_available():
        return JsonResponse(
            {"error": chat_settings.unavailable_message() or "Live chat is switched off."},
            status=403,
        )

    from booking.models import Service

    name = request.GET.get("name", "").strip()[:80]
    services = [
        {
            "id": service.pk,
            "name": service.name,
            "duration_minutes": service.duration_minutes,
            "price": str(service.price) if service.price is not None else "",
        }
        for service in Service.objects.filter(is_active=True)
    ]
    return JsonResponse({"greeting": chat_settings.render_auto_greeting(name), "services": services})


def bot_calendars(request, service_id):
    """Second step: which calendars offer the service the visitor picked."""
    from booking import availability as booking_availability
    from booking.models import Service

    service = get_object_or_404(Service, pk=service_id, is_active=True)
    calendars = [
        {
            "id": calendar.pk,
            "name": calendar.name,
            "url": f"{calendar.get_absolute_url()}?service={service.pk}",
        }
        for calendar in booking_availability.calendars_offering(service)
        if calendar.accepts_online_booking
    ]
    return JsonResponse(
        {
            "service": {
                "id": service.pk,
                "name": service.name,
                "duration_minutes": service.duration_minutes,
            },
            "calendars": calendars,
        }
    )


@require_POST
def start(request):
    """Customer asks to chat: creates a pending conversation."""
    chat_settings = ChatSettings.load()
    if not chat_settings.is_available():
        return JsonResponse(
            {"error": chat_settings.unavailable_message() or "Live chat is switched off."},
            status=403,
        )

    existing = _current_conversation(request, include_rejected=False)
    if existing:
        return JsonResponse(
            {"status": "ok", "conversation_id": existing.pk, "state": existing.status}
        )

    name = request.POST.get("name", "").strip()[:80]
    if chat_settings.require_name and not name:
        return JsonResponse({"error": "Please tell us your name first."}, status=400)

    conversation = Conversation.objects.create(
        user=request.user if request.user.is_authenticated else None,
        session_key=_session_key(request),
        guest_name=name,
        guest_email=request.POST.get("email", "").strip()[:254],
        opened_from=request.POST.get("page", "")[:255],
        status=Conversation.STATUS_PENDING,
    )

    # The salon speaks first. With no user accounts there is nothing to greet
    # by default, so the opening line names whoever just introduced themselves
    # and invites the question -- the visitor can type immediately instead of
    # staring at a spinner until somebody accepts.
    Message.objects.create(
        conversation=conversation,
        sender_type=Message.STAFF,
        sender_name=SalonSettings.load().name,
        text=chat_settings.render_auto_greeting(conversation.display_name),
    )

    first_message = request.POST.get("text", "").strip()
    if first_message:
        Message.objects.create(
            conversation=conversation,
            sender_type=Message.CUSTOMER,
            sender_name=conversation.display_name,
            text=first_message[:MAX_MESSAGE_LENGTH],
        )

    return JsonResponse({"status": "ok", "conversation_id": conversation.pk, "state": "pending"})


@require_POST
def send(request):
    """Customer sends a message into an accepted conversation."""
    # Deliberately checks the master switch, not the opening hours: a chat that
    # is already live should be able to finish after closing time.
    if not ChatSettings.load().enabled:
        return JsonResponse({"error": "Live chat is switched off."}, status=403)

    text = request.POST.get("text", "").strip()
    if not text:
        return JsonResponse({"error": "Nothing to send."}, status=400)

    conversation = _current_conversation(request, include_rejected=False)
    # Pending counts: the visitor answers the automatic greeting before any
    # member of staff has picked the chat up.
    if conversation is None or not conversation.is_open:
        return JsonResponse({"error": "This chat is no longer active."}, status=400)

    message = Message.objects.create(
        conversation=conversation,
        sender_type=Message.CUSTOMER,
        sender_name=conversation.display_name,
        text=text[:MAX_MESSAGE_LENGTH],
    )
    conversation.touch()
    return JsonResponse({"status": "ok", "message": _message_json(message)})


@require_POST
def dismiss(request):
    """Customer acknowledges a decline, or ends the chat themselves."""
    conversation = _current_conversation(request)
    if conversation and _owns(request, conversation):
        conversation.status = Conversation.STATUS_CLOSED
        conversation.closed_at = timezone.now()
        conversation.save(update_fields=["status", "closed_at", "updated_at"])
    return JsonResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Staff side
# ---------------------------------------------------------------------------
@staff_member_required
def desk(request):
    """The staff chat desk: everything waiting and everything live."""
    chat_settings = ChatSettings.load()
    availability = chat_settings.status()
    waiting = Conversation.objects.waiting().select_related("user").order_by("created_at")
    live = (
        Conversation.objects.filter(status=Conversation.STATUS_ACCEPTED)
        .select_related("user", "accepted_by")
        .annotate(message_count=Count("messages"), last_message_at=Max("messages__created_at"))
        .order_by("-updated_at")
    )
    recent = (
        Conversation.objects.filter(
            status__in=[Conversation.STATUS_CLOSED, Conversation.STATUS_REJECTED]
        )
        .annotate(message_count=Count("messages"))
        .order_by("-updated_at")[:20]
    )
    # A missing collected chat.js is invisible to the customer and to us: the
    # bubble simply never works. Say so here rather than leaving it a mystery.
    static_missing = False
    if not settings.DEBUG:
        try:
            from django.contrib.staticfiles.storage import staticfiles_storage

            static_missing = not staticfiles_storage.exists("js/chat.js")
        except Exception:  # noqa: BLE001 - a diagnostic must not break the page
            static_missing = False

    return render(
        request,
        "chat/desk.html",
        {
            "chat_settings": chat_settings,
            "availability": availability,
            "static_missing": static_missing,
            "waiting": waiting,
            "live": live,
            "recent": recent,
        },
    )


@staff_member_required
def live_data(request):
    """Poll endpoint for the staff desk."""
    chat_settings = ChatSettings.load()
    conversations = (
        Conversation.objects.open()
        .select_related("user")
        .prefetch_related(Prefetch("messages", queryset=Message.objects.order_by("created_at")))[:30]
    )

    waiting, live = [], []
    for conversation in conversations:
        messages = list(conversation.messages.all())
        entry = {
            "id": conversation.pk,
            "name": conversation.display_name,
            "opened_from": conversation.opened_from,
            "waiting_seconds": conversation.waiting_seconds(),
            "message_count": len(messages),
            "last_message": messages[-1].text[:120] if messages else "",
            "messages": [_message_json(m) for m in messages],
        }
        (waiting if conversation.status == Conversation.STATUS_PENDING else live).append(entry)

    state = chat_settings.status()
    return JsonResponse(
        {
            "enabled": chat_settings.enabled,
            "available": state["available"],
            "reason": state["reason"],
            "waiting": waiting,
            "live": live,
            "poll_seconds": chat_settings.poll_seconds,
        }
    )


@staff_member_required
def thread(request, conversation_id):
    conversation = get_object_or_404(Conversation, pk=conversation_id)
    return JsonResponse(
        {
            "id": conversation.pk,
            "name": conversation.display_name,
            "status": conversation.status,
            "messages": _messages_since(conversation, request.GET.get("since_id")),
        }
    )


@staff_member_required
@require_POST
def accept(request, conversation_id):
    chat_settings = ChatSettings.load()
    conversation = get_object_or_404(
        Conversation, pk=conversation_id, status=Conversation.STATUS_PENDING
    )
    conversation.status = Conversation.STATUS_ACCEPTED
    conversation.accepted_by = request.user
    conversation.accepted_at = timezone.now()
    conversation.save(update_fields=["status", "accepted_by", "accepted_at", "updated_at"])

    staff_name = request.user.get_short_name() or request.user.get_username()
    Message.objects.create(
        conversation=conversation,
        sender_type=Message.STAFF,
        sender_name=staff_name,
        text=chat_settings.render_greeting(staff_name),
    )
    return _ok_or_redirect(request, {"status": "ok", "conversation_id": conversation.pk})


@staff_member_required
@require_POST
def reject(request, conversation_id):
    chat_settings = ChatSettings.load()
    conversation = get_object_or_404(
        Conversation, pk=conversation_id, status=Conversation.STATUS_PENDING
    )
    conversation.status = Conversation.STATUS_REJECTED
    conversation.save(update_fields=["status", "updated_at"])
    Message.objects.create(
        conversation=conversation, sender_type=Message.SYSTEM, text=chat_settings.busy_text
    )
    return _ok_or_redirect(request, {"status": "ok"})


@staff_member_required
@require_POST
def close(request, conversation_id):
    conversation = get_object_or_404(Conversation, pk=conversation_id)
    conversation.status = Conversation.STATUS_CLOSED
    conversation.closed_at = timezone.now()
    conversation.save(update_fields=["status", "closed_at", "updated_at"])
    return _ok_or_redirect(request, {"status": "ok"})


@staff_member_required
@require_POST
def staff_send(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, pk=conversation_id, status=Conversation.STATUS_ACCEPTED
    )
    text = request.POST.get("text", "").strip()
    if not text:
        return JsonResponse({"error": "Nothing to send."}, status=400)

    message = Message.objects.create(
        conversation=conversation,
        sender_type=Message.STAFF,
        sender_name=request.user.get_short_name() or request.user.get_username(),
        text=text[:MAX_MESSAGE_LENGTH],
    )
    conversation.touch()
    return _ok_or_redirect(request, {"status": "ok", "message": _message_json(message)})


@staff_member_required
@require_POST
def toggle(request):
    """Turn live chat on or off.

    Switching off closes anything open so no customer is left waiting for a
    reply that will never come. History is kept.
    """
    chat_settings = ChatSettings.load()
    was_enabled = chat_settings.enabled
    chat_settings.enabled = not was_enabled
    chat_settings.save(update_fields=["enabled"])

    if was_enabled:
        Conversation.objects.open().update(
            status=Conversation.STATUS_CLOSED, closed_at=timezone.now(), updated_at=timezone.now()
        )
    return redirect(request.POST.get("next") or "chat:desk")


def _ok_or_redirect(request, payload):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(payload)
    return redirect("chat:desk")
