from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect
from django.urls import path


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", lambda request: redirect("/ui/issue/")),

    path("", include("core.urls")),  # <-- pulls all api routes from core/urls.py
]
