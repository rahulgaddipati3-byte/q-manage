from datetime import timedelta
from django.db import models
from django.utils import timezone


# IMPORTANT:
# Old migration 0005 imports this function: core.models.default_expires_at
# Keep it forever, otherwise migrations break.
def default_expires_at():
    return timezone.now() + timedelta(minutes=10)


class Counter(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100, blank=True, default="")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.code} - {self.name or self.code}"


class Token(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_USED = "used"
    STATUS_EXPIRED = "expired"

    STATUS_CHOICES = (
        (STATUS_ACTIVE, "Active"),
        (STATUS_USED, "Used"),
        (STATUS_EXPIRED, "Expired"),
    )

    counter = models.ForeignKey(
        Counter,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tokens",
    )

    number = models.CharField(max_length=20)  # unique per (service_date, number)
    service_date = models.DateField(db_index=True)
    sequence = models.PositiveIntegerField(db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)

    # Patient details (from public reserve AND staff issue)
    customer_name = models.CharField(max_length=120, blank=True, default="")
    customer_phone = models.CharField(max_length=20, blank=True, default="")
    customer_address = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    # Keep field for backwards compatibility, but we will set it safely in save()
    expires_at = models.DateTimeField(default=default_expires_at)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["service_date", "number"], name="uniq_token_number_per_day"),
            models.UniqueConstraint(fields=["service_date", "sequence"], name="uniq_token_seq_per_day"),
        ]
        indexes = [
            models.Index(fields=["service_date", "status"]),
            models.Index(fields=["service_date", "counter", "status"]),
        ]

    def save(self, *args, **kwargs):
        """
        Ensure expires_at is always set relative to created_at/now.
        You can tune TOKEN_TTL_MINUTES later without breaking old data.
        """
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=30)  # ✅ recommend 30 instead of 10
        super().save(*args, **kwargs)

    def is_expired(self) -> bool:
        # ✅ also expire tokens from previous days
        if self.service_date and self.service_date < timezone.localdate():
            return True
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

    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20)

    scheduled_time = models.DateTimeField(null=True, blank=True)
    token = models.OneToOneField(Token, null=True, blank=True, on_delete=models.SET_NULL)

    sms_sent = models.BooleanField(default=False)
    sms_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Req #{self.id} - {self.name} ({self.status})"
