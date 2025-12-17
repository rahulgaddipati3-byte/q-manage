from django.urls import path
from . import views
from . import views_ui

urlpatterns = [
    # ------- API (POST actions FIRST) -------
    path("api/token/issue/", views.issue_token),
    path("api/token/consume/", views.consume_token),
    path("api/token/next/", views.next_token),
    path("api/queue/status/", views.queue_status),

    # ------- API (parameterized LAST) -------
    path("api/token/<str:number>/", views.token_status),

    # ------- UI pages -------
    path("ui/counter/", views_ui.counter_screen),
    path("ui/display/", views_ui.display_screen),

    # ------- UI ajax endpoints -------
    path("ui/data/", views_ui.display_data),
    path("ui/issue/", views_ui.ui_issue_token),
    path("ui/call-next/", views_ui.ui_call_next),
]
