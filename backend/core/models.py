from django.db import models
from django.utils import timezone
from datetime import timedelta
import random

# IMPORTANT:
# This function MUST exist because an old migration references:
# core.models.default_expires_at
def default_expires_at():
    return timezone.now() + timedelta(minutes=10)


class Counter(models.Model):
    """
    A counter/service desk (clinic room, salon chair, cashier, etc.)
    """
    code = models.CharField(max_length=20, unique=True)   # e.g. "c1", "C1"
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} - {self.name}"


class Token(models.Model):
    STATUS_CHOICES = (
        ("active", "Active"),
        ("used", "Used"),
        ("expired", "Expired"),
    )

    counter = models.ForeignKey(
        Counter,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tokens",
    )

    # What customer sees (A001, A002...)
    number = models.CharField(max_length=10, unique=True, blank=True)

    # Product-ready sequencing (per day per counter)
    service_date = models.DateField(null=True, blank=True, db_index=True)
    sequence = models.PositiveIntegerField(default=0, db_index=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="active")

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_expires_at)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["counter", "service_date", "sequence"],
                name="uniq_counter_day_sequence",
            )
        ]

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def save(self, *args, **kwargs):
        # Keep compatibility: if something creates Token without number, still generate something
        if not self.number:
            # fallback random (should not happen if you use issue_token API)
            self.number = str(random.randint(100000, 999999))

        # live expiry correction
        if self.status == "active" and self.is_expired():
            self.status = "expired"

        super().save(*args, **kwargs)

    def __str__(self):
        return self.number
