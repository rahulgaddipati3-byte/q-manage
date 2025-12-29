# backend/core/models.py
from datetime import timedelta
from django.db import models
from django.utils import timezone


# IMPORTANT:
# Old migration 0005 imports this function: core.models.default_expires_at
# Keep it forever (even if you don't use it in new code), otherwise migrations break.
def default_expires_at():
    return timezone.now() + timedelta(minutes=10)


class Counter(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100, blank=True, default="")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.code} - {self.name or self.code}"


class Token(models.Model):
    STATUS_CHOICES = (
        ("active", "Active"),
        ("used", "Used"),
        ("expired", "Expired"),
    )

    counter = models.ForeignKey(Counter, null=True, blank=True, on_delete=models.SET_NULL)

    number = models.CharField(max_length=20)  # NOT unique; unique per (service_date, number)
    service_date = models.DateField(db_index=True)
    sequence = models.PositiveIntegerField(db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(default=default_expires_at)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["service_date", "number"], name="uniq_token_number_per_day"),
            models.UniqueConstraint(fields=["service_date", "sequence"], name="uniq_token_seq_per_day"),
        ]

    def is_expired(self) -> bool:
        return bool(self.expires_at and timezone.now() >= self.expires_at)

    def __str__(self):
        return self.number


class ReservationRequest(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    )

    service_date = models.DateField(db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")

    # Required from customer
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=15)  # store 10-digit or 91XXXXXXXXXX

    # Staff confirms this time
    scheduled_time = models.DateTimeField(null=True, blank=True)

    # Token created only after approval
    token = models.OneToOneField(Token, null=True, blank=True, on_delete=models.SET_NULL)

    # SMS status (or WhatsApp)
    sms_sent = models.BooleanField(default=False)
    sms_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Req {self.id} ({self.status}) - {self.name}"
