from django.db.utils import DatabaseError, OperationalError, ProgrammingError
from django.utils.functional import SimpleLazyObject

from .models import SalonSettings


def _chat_visibility():
    """Whether the chat bubble should be in the page at all, and why.

    Worked out server-side so the button is present in the HTML from the first
    byte. It used to be hidden in markup and revealed only by JavaScript, which
    meant a stale or missing chat.js produced no bubble at all, silently.
    """
    try:
        from chat.models import ChatSettings

        chat_settings = ChatSettings.load()
        state = chat_settings.status()
    except (OperationalError, ProgrammingError, DatabaseError, ImportError):
        return {"show": False, "available": False}

    return {
        "show": state["available"] or chat_settings.show_when_offline,
        "available": state["available"],
    }


def salon(request):
    """Make the salon settings and chat visibility available in every template."""
    try:
        settings_row = SalonSettings.load()
    except (OperationalError, ProgrammingError, DatabaseError):
        # Before the first migrate there is no table yet.
        settings_row = SalonSettings()

    # Lazy: pages that never render the widget (the admin) pay no query.
    visibility = SimpleLazyObject(_chat_visibility)
    return {
        "salon": settings_row,
        "chat_visible": SimpleLazyObject(lambda: visibility["show"]),
        "chat_available": SimpleLazyObject(lambda: visibility["available"]),
    }
