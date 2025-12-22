# backend/core/urls.py
from django.urls import path
from . import views, views_ui, views_auth, views_users
from . import public_views, views_reservations

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
    # Reservation flow (public -> staff approve/reject)
    # -------------------------
    # Public request pages/APIs (by request_id)
    path("public/request/<int:request_id>/", public_views.public_request_page, name="public_request_page"),
    path("api/public/request/<int:request_id>/status/", public_views.public_request_status, name="public_request_status"),

    # Staff approve/reject reservation requests
    path("api/staff/requests/pending/", views_reservations.pending_requests, name="pending_requests"),
    path("api/staff/requests/<int:request_id>/approve/", views_reservations.approve_request, name="approve_request"),
    path("api/staff/requests/<int:request_id>/reject/", views_reservations.reject_request, name="reject_request"),

    # -------------------------
    # Existing Internal API (staff/reception)
    # -------------------------
    path("api/token/issue/", views.issue_token, name="api_issue_token"),
    path("api/token/next/", views.next_token, name="api_next_token"),
    path("api/token/consume/", views.consume_token, name="api_consume_token"),
    path("api/token/status/<str:number>/", views.token_status, name="api_token_status"),
    path("api/queue/status/", views.queue_status, name="api_queue_status"),

    # -------------------------
    # OPTIONAL: New “pending -> approve -> WhatsApp” endpoints from views.py
    # Use these only if you are not using views_reservations for the same job.
    # -------------------------
    # path("api/token/request/", views.request_token, name="api_request_token"),
    # path("api/token/approve/", views.approve_token, name="api_approve_token"),

    # -------------------------
    # UI (staff screens)
    # -------------------------
    path("ui/counter/", views_ui.counter_screen, name="ui_counter"),
    path("ui/display/", views_ui.display_screen, name="ui_display"),
    path("ui/data/", views_ui.display_data, name="ui_display_data"),

    # UI actions (POST)
    path("ui/issue/", views_ui.ui_issue_token, name="ui_issue_token"),
    path("ui/call-next/", views_ui.ui_call_next, name="ui_call_next"),

    # -------------------------
    # Public customer flow (no login) (by clinic slug / token_id)
    # -------------------------
    path("public/clinic/<slug:slug>/", public_views.public_clinic_page, name="public_clinic_page"),
    path("public/token/<int:token_id>/", public_views.public_token_page, name="public_token_page"),

    path("api/public/clinic/<slug:slug>/snapshot/", public_views.public_clinic_snapshot, name="public_clinic_snapshot"),
    path("api/public/clinic/<slug:slug>/reserve/", public_views.public_reserve_token, name="public_reserve_token"),
    path("api/public/token/<int:token_id>/status/", public_views.public_token_status, name="public_token_status"),
]
