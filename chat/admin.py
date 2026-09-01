from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html

from .models import ChatSettings, Conversation, Message


@admin.register(ChatSettings)
class ChatSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        (
            "Availability",
            {
                "fields": ("enabled", "follow_business_hours", "show_when_offline"),
                "description": (
                    "<b>Switched on</b> is the master switch: off means no chat at all. "
                    "With <b>follow business hours</b> ticked, chat is additionally limited to "
                    "the salon's <a href='/admin/booking/businesshours/'>opening hours</a> and "
                    "respects <a href='/admin/booking/timeoff/'>vacation</a>."
                ),
            },
        ),
        ("Behaviour", {"fields": ("require_name", "poll_seconds")}),
        (
            "Wording",
            {"fields": ("welcome_heading", "welcome_text", "offline_text", "greeting", "busy_text")},
        ),
        ("Privacy", {"fields": ("retention_days",)}),
    )

    def has_add_permission(self, request):
        return not ChatSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = ChatSettings.load()
        return redirect(reverse("admin:chat_chatsettings_change", args=[obj.pk]))


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    readonly_fields = ("sender_type", "sender_name", "text", "created_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("display_name", "status_badge", "message_total", "opened_from", "created_at", "updated_at")
    list_filter = ("status", "created_at")
    search_fields = ("guest_name", "guest_email", "session_key", "messages__text")
    date_hierarchy = "created_at"
    readonly_fields = (
        "user", "session_key", "guest_name", "guest_email", "opened_from",
        "accepted_by", "accepted_at", "closed_at", "created_at", "updated_at",
    )
    inlines = [MessageInline]

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        colours = {
            Conversation.STATUS_PENDING: "#b45309",
            Conversation.STATUS_ACCEPTED: "#15803d",
            Conversation.STATUS_CLOSED: "#6b7280",
            Conversation.STATUS_REJECTED: "#b91c1c",
        }
        return format_html(
            '<b style="color:{}">{}</b>', colours.get(obj.status, "#000"), obj.get_status_display()
        )

    @admin.display(description="Messages")
    def message_total(self, obj):
        return obj.messages.count()
