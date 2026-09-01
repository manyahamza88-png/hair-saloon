from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    # Customer widget
    path("widget/", views.widget, name="widget"),
    path("start/", views.start, name="start"),
    path("send/", views.send, name="send"),
    path("dismiss/", views.dismiss, name="dismiss"),
    # Staff desk
    path("desk/", views.desk, name="desk"),
    path("desk/live/", views.live_data, name="live_data"),
    path("desk/toggle/", views.toggle, name="toggle"),
    path("desk/<int:conversation_id>/", views.thread, name="thread"),
    path("desk/<int:conversation_id>/send/", views.staff_send, name="staff_send"),
    path("desk/<int:conversation_id>/accept/", views.accept, name="accept"),
    path("desk/<int:conversation_id>/reject/", views.reject, name="reject"),
    path("desk/<int:conversation_id>/close/", views.close, name="close"),
]
