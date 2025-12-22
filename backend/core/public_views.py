# backend/core/public_views.py
import json

from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.db.models import Max

from .models import Token


# --------------------------------------------------
# Helpers
# --------------------------------------------------
def _today_service_date():
    return timezone.localdate()


def _next_token_number_and_sequence(service_date):
    """
    Option 2 (your system):
    Single prefix A001, A002... per day across ALL counters.
    Uses Token.sequence as the source of truth (unique per day).
    """
    last_seq = (
        Token.objects
        .filter(service_date=service_date)
        .aggregate(m=Max("sequence"))
    )["m"] or 0

    next_seq = int(last_seq) + 1
    token_number = f"A{str(next_seq).zfill(3)}"
    return token_number, next_seq


def _avg_minutes_per_token():
    """
    MVP: keep it simple. Later you can make this configurable in settings or DB.
    """
    return 5


# --------------------------------------------------
# Public Pages (NO LOGIN)
# --------------------------------------------------
@require_GET
def public_clinic_page(request, slug):
    """
    Public landing page for clinic queue.
    We keep <slug> in URL to match your routes, but for now it can be 'main'.
    (No Clinic model required for MVP.)
    """
    return render(request, "public/clinic.html", {"clinic_slug": slug})


@require_GET
def public_token_page(request, token_id):
    """
    Public tracking page after reservation.
    """
    token = get_object_or_404(Token, id=token_id)
    return render(request, "public/token.html", {"token": token})


# --------------------------------------------------
# Public APIs
# --------------------------------------------------
@require_GET
def public_clinic_snapshot(request, slug):
    """
    Returns:
    - current running token (last USED today)
    - waiting count (ACTIVE today)
    - estimated wait minutes
    """
    service_date = _today_service_date()

    last_used = (
        Token.objects
        .filter(service_date=service_date, status="used")
        .order_by("-sequence")   # sequence is the right ordering
        .first()
    )

    waiting_count = Token.objects.filter(service_date=service_date, status="active").count()

    est_minutes = waiting_count * _avg_minutes_per_token()

    return JsonResponse({
        "ok": True,
        "service_date": str(service_date),
        "now_serving": last_used.number if last_used else None,
        "waiting_count": waiting_count,
        "estimated_wait_minutes": est_minutes,
    })


@csrf_exempt
@require_POST
def public_reserve_token(request, slug):
    """
    Reserve token from home.
    Creates a normal ACTIVE token so your existing staff/counter flow works unchanged.
    """
    service_date = _today_service_date()

    # Optional payload (not used in MVP, but safe to accept)
    if request.body:
        try:
            json.loads(request.body.decode("utf-8"))
        except Exception:
            return HttpResponseBadRequest("Invalid JSON")

    with transaction.atomic():
        number, seq = _next_token_number_and_sequence(service_date)

        token = Token.objects.create(
            number=number,
            sequence=seq,
            service_date=service_date,
            status="active",
            # counter remains NULL for online-issued token (reception-style)
        )

    return JsonResponse({
        "ok": True,
        "token_id": token.id,
        "token_number": token.number,
        "track_url": f"/public/token/{token.id}/",
    })


@require_GET
def public_token_status(request, token_id):
    """
    Returns live tracking:
    - now serving
    - tokens ahead
    - estimated wait minutes
    - your status
    """
    token = get_object_or_404(Token, id=token_id)
    service_date = token.service_date

    last_used = (
        Token.objects
        .filter(service_date=service_date, status="used")
        .order_by("-sequence")
        .first()
    )

    tokens_ahead = Token.objects.filter(
        service_date=service_date,
        status="active",
        sequence__lt=token.sequence
    ).count()

    est_minutes = tokens_ahead * _avg_minutes_per_token()

    return JsonResponse({
        "ok": True,
        "token_id": token.id,
        "your_token": token.number,
        "your_status": token.status,
        "now_serving": last_used.number if last_used else None,
        "tokens_ahead": tokens_ahead,
        "estimated_wait_minutes": est_minutes,
        "service_date": str(service_date),
    })
