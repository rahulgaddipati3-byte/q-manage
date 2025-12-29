# backend/core/urls.py
from django.urls import path
from . import views, views_ui, views_auth, views_users
from . import public_views

urlpatterns = [
    # Home -> Login
    path("", views_auth.staff_login, name="home"),

    # Auth
    path("login/", views_auth.staff_login, name="staff_login"),
    path("logout/", views_auth.staff_logout, name="staff_logout"),

    # Custom admin dashboard (NOT Django admin)
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),

    # Staff user management
    path("users/new/", views_users.user_create, name="user_create"),

    # -------------------------
    # Internal API (staff/reception)
    # -------------------------
    path("api/token/issue/", views.issue_token, name="api_issue_token"),
    path("api/token/next/", views.next_token, name="api_next_token"),
    path("api/token/consume/", views.consume_token, name="api_consume_token"),
    path("api/token/status/<str:number>/", views.token_status, name="api_token_status"),
    path("api/queue/status/", views.queue_status, name="api_queue_status"),

    # -------------------------
    # UI (staff screens)
    # -------------------------
    path("ui/counter/", views_ui.counter_screen, name="ui_counter"),
    path("ui/display/", views_ui.display_screen, name="ui_display"),
    path("ui/data/", views_ui.display_data, name="ui_display_data"),
    path("ui/issue/", views_ui.ui_issue_token, name="ui_issue_token"),
    path("ui/call-next/", views_ui.ui_call_next, name="ui_call_next"),

    # -------------------------
    # Public customer flow (no login)
    # -------------------------
    path("public/clinic/<slug:slug>/", public_views.public_clinic_page, name="public_clinic_page"),
    path("api/public/clinic/<slug:slug>/snapshot/", public_views.public_clinic_snapshot, name="public_clinic_snapshot"),
    path("api/public/clinic/<slug:slug>/reserve/", public_views.public_reserve_token, name="public_reserve_token"),

    path("public/token/<int:token_id>/", public_views.public_token_page, name="public_token_page"),
    path("api/public/token/<int:token_id>/status/", public_views.public_token_status, name="public_token_status"),
]
