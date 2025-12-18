from django.urls import path
from . import views, views_ui, views_auth, views_admin, views_users

urlpatterns = [
    # Auth
    path("login/", views_auth.staff_login, name="staff_login"),
    path("logout/", views_auth.staff_logout, name="staff_logout"),

    # Admin Dashboard
    path("admin-dashboard/", views_admin.admin_dashboard, name="admin_dashboard"),

    # Staff user management
    path("users/new/", views_users.user_create, name="user_create"),

    # API
    path("api/token/issue/", views.issue_token),
    path("api/token/next/", views.next_token),
    path("api/token/consume/", views.consume_token),
    path("api/token/status/<str:number>/", views.token_status),
    path("api/queue/status/", views.queue_status),

    # UI
    path("ui/counter/", views_ui.counter_screen),
    path("ui/display/", views_ui.display_screen),
    path("ui/data/", views_ui.display_data),
    path("ui/issue/", views_ui.ui_issue_token),
    path("ui/call-next/", views_ui.ui_call_next),
]
