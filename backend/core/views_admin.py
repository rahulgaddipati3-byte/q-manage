from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db.models import Count, Q

from .models import Token, Counter


@login_required
def admin_dashboard(request):
    today = timezone.localdate()

    counters = Counter.objects.filter(is_active=True).order_by("code")

    # Totals
    issued_today = Token.objects.filter(service_date=today).count()
    used_today = Token.objects.filter(service_date=today, status="used").count()
    active_now = Token.objects.filter(service_date=today, status="active").count()
    expired_today = Token.objects.filter(service_date=today, status="expired").count()

    # Per-counter stats (today)
    per_counter = []
    for c in counters:
        per_counter.append({
            "code": c.code,
            "name": c.name,
            "issued": Token.objects.filter(service_date=today, counter=c).count(),
            "used": Token.objects.filter(service_date=today, counter=c, status="used").count(),
            "active": Token.objects.filter(service_date=today, counter=c, status="active").count(),
        })

    context = {
        "today": today,
        "issued_today": issued_today,
        "used_today": used_today,
        "active_now": active_now,
        "expired_today": expired_today,
        "per_counter": per_counter,
    }
    return render(request, "core/admin_dashboard.html", context)
