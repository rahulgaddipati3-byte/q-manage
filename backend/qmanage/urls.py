# backend/qmanage/urls.py
from django.contrib import admin
from django.http import HttpResponseNotFound
from django.urls import path, include


def admin_disabled(request):
    return HttpResponseNotFound("Admin disabled. Use /admin-dashboard/ instead.")


urlpatterns = [
    # Block Django admin completely
    path("admin/", admin_disabled),

    # Your app routes
    path("", include("core.urls")),
]
