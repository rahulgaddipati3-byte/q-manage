from django.contrib import admin
from .models import Token, Counter


@admin.register(Counter)
class CounterAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    ordering = ("code",)


@admin.register(Token)
class TokenAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "counter", "created_at", "expires_at", "used_at")
    list_filter = ("status", "counter")
    search_fields = ("number",)
    ordering = ("-created_at",)
