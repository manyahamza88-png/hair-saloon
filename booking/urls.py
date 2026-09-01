from django.urls import path

from . import views

app_name = "booking"

urlpatterns = [
    path("", views.home, name="home"),
    path("book/<slug:slug>/", views.calendar_detail, name="calendar_detail"),
    path("book/<slug:slug>/submit/", views.book, name="book"),
    path("api/<slug:slug>/slots/", views.slots_api, name="slots_api"),
    path("api/<slug:slug>/month/", views.month_api, name="month_api"),
    path("booking/<uuid:public_id>/done/", views.booking_done, name="booking_done"),
    path("booking/<uuid:public_id>/", views.appointment_detail, name="appointment_detail"),
    path("decide/<str:token>/", views.decide, name="decide"),
    path("decide/<uuid:public_id>/done/", views.decision_done, name="decision_done"),
    path("cancel/<str:token>/", views.cancel, name="cancel"),
    path("manage/", views.dashboard, name="dashboard"),
    path("manage/week/", views.week_view, name="week"),
    path("manage/google/", views.google_setup, name="google_setup"),
    path("manage/google/client/", views.google_client_save, name="google_client_save"),
    path("manage/google/connect/", views.google_connect, name="google_connect"),
    path("manage/google/callback/", views.google_callback, name="google_callback"),
    path("manage/google/disconnect/", views.google_disconnect, name="google_disconnect"),
    path("manage/google/add-calendar/", views.google_add_calendar, name="google_add_calendar"),
    path("manage/<uuid:public_id>/action/", views.dashboard_decide, name="dashboard_decide"),
    path("manage/add/", views.dashboard_add, name="dashboard_add"),
]
