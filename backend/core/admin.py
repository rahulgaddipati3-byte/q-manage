# backend/core/admin.py
from django.contrib import admin
from .models import Counter, Token


@admin.register(Counter)
class CounterAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(Token)
class TokenAdmin(admin.ModelAdmin):
    list_display = ("number", "service_date", "sequence", "status", "counter", "created_at", "expires_at", "used_at")
    list_filter = ("status", "service_date", "counter")
    search_fields = ("number",)
