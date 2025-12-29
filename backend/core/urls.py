from django.urls import path
from . import views, views_ui, views_auth, views_users
from . import public_views

urlpatterns = [
    # --------------------------------------------------
    # Home / Auth
    # --------------------------------------------------
    path("", views_auth.staff_login, name="home"),
    path("login/", views_auth.staff_login, name="staff_login"),
    path("logout/", views_auth.staff_logout, name="staff_logout"),

    # --------------------------------------------------
    # Admin Dashboard
    # --------------------------------------------------
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),

    # --------------------------------------------------
    # Staff User Management
    # --------------------------------------------------
    path("users/new/", views_users.user_create, name="user_create"),

    # --------------------------------------------------
    # Internal Token APIs (USED BY STAFF UI)
    # --------------------------------------------------
    path("api/token/issue/", views.issue_token, name="api_issue_token"),
    path("api/token/next/", views.next_token, name="api_next_token"),
    path("api/token/consume/", views.consume_token, name="api_consume_token"),
    path("api/token/status/<str:number>/", views.token_status, name="api_token_status"),
    path("api/queue/status/", views.queue_status, name="api_queue_status"),

    # --------------------------------------------------
    # STAFF UI
    # --------------------------------------------------
    path("ui/counter/", views_ui.counter_screen, name="ui_counter"),
    path("ui/display/", views_ui.display_screen, name="ui_display"),
    path("ui/data/", views_ui.display_data, name="ui_display_data"),

    # --------------------------------------------------
    # PUBLIC CUSTOMER FLOW (AUTO-ISSUE TOKEN)
    # --------------------------------------------------
    path(
        "public/clinic/<slug:slug>/",
        public_views.public_clinic_page,
        name="public_clinic_page",
    ),

    path(
        "api/public/clinic/<slug:slug>/snapshot/",
        public_views.public_clinic_snapshot,
        name="public_clinic_snapshot",
    ),

    path(
        "api/public/clinic/<slug:slug>/reserve/",
        public_views.public_reserve_token,
        name="public_reserve_token",
    ),

    path(
        "public/token/<int:token_id>/",
        public_views.public_token_page,
        name="public_token_page",
    ),

    path(
        "api/public/token/<int:token_id>/status/",
        public_views.public_token_status,
        name="public_token_status",
    ),
]
