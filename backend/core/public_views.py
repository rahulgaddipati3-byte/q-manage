# backend/core/public_views.py
import json
import re

from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt

from .models import Token, ReservationRequest

# Accept:
#  - 10 digits: 9876543210
#  - 12 digits starting 91: 919876543210
#  - +91... with spaces
PHONE_RE = re.compile(r"^\d{10}$|^91\d{10}$")


def _normalize_phone(phone: str) -> str:
    """Normalize to either 10 digits or 91XXXXXXXXXX (no +, no spaces)."""
    if not phone:
        return ""
    p = phone.strip().replace(" ", "")
    if p.startswith("+"):
        p = p[1:]
    # Keep digits only
    p = re.sub(r"\D", "", p)

    # If 10-digit, keep as is (your regex allows it)
    if len(p) == 10:
        return p

    # If starts with 91 and 12 digits total
    if len(p) == 12 and p.startswith("91"):
        return p

    return p


# --------------------------------------------------
# Public clinic landing page
# --------------------------------------------------
@require_GET
def public_clinic_page(request, slug):
    return render(request, "public/clinic.html", {"clinic_slug": slug})


# --------------------------------------------------
# Public clinic snapshot API (for clinic.html)
# --------------------------------------------------
@require_GET
def public_clinic_snapshot(request, slug):
    service_date = timezone.localdate()

    last_used = (
        Token.objects
        .filter(service_date=service_date, status="used")
        .order_by("-used_at", "-id")
        .first()
    )

    # Consider pending + active as "waiting in system"
    pending_count = ReservationRequest.objects.filter(
        service_date=service_date, status="pending"
    ).count()

    active_count = Token.objects.filter(service_date=service_date, status="active").count()

    people_waiting = pending_count + active_count

    # Simple estimate (tune later)
    avg_minutes = 5
    estimated_wait_min = people_waiting * avg_minutes

    return JsonResponse({
        "ok": True,
        "clinic": slug,
        "now_serving": last_used.number if last_used else None,
        "people_waiting": people_waiting,
        "estimated_wait_min": estimated_wait_min,
        # keep extra fields too (useful for debugging/UI)
        "pending_requests": pending_count,
        "active_tokens": active_count,
    })


# --------------------------------------------------
# Public: reserve token (creates ReservationRequest)
# POST JSON: {"name":"..","phone":".."}
# --------------------------------------------------
@csrf_exempt
@require_POST
def public_reserve_token(request, slug):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return HttpResponseBadRequest("Invalid JSON")

    name = (payload.get("name") or "").strip()
    phone = _normalize_phone(payload.get("phone") or "")

    if not name:
        return JsonResponse({"ok": False, "error": "Name is required"}, status=400)

    if not phone or not PHONE_RE.match(phone):
        return JsonResponse(
            {"ok": False, "error": "Enter valid 10-digit mobile (or 91XXXXXXXXXX)"},
            status=400,
        )

    service_date = timezone.localdate()

    req = ReservationRequest.objects.create(
        service_date=service_date,
        status="pending",
        name=name,
        phone=phone,
    )

    return JsonResponse({
        "ok": True,
        "request_id": req.id,
        "track_url": f"/public/request/{req.id}/",
    })


# --------------------------------------------------
# Public: reservation tracking page
# --------------------------------------------------
@require_GET
def public_request_page(request, request_id):
    req = get_object_or_404(ReservationRequest, id=request_id)
    return render(request, "public/request.html", {"req": req})


# --------------------------------------------------
# Public: reservation status API (polling)
# --------------------------------------------------
@require_GET
def public_request_status(request, request_id):
    req = get_object_or_404(ReservationRequest, id=request_id)

    token_number = req.token.number if req.token else None
    token_url = f"/public/token/{req.token.id}/" if req.token else None

    scheduled = (
        timezone.localtime(req.scheduled_time).strftime("%I:%M %p").lstrip("0")
        if req.scheduled_time else None
    )

    return JsonResponse({
        "ok": True,
        "status": req.status,
        "token_number": token_number,
        "scheduled_time_display": scheduled,
        "token_track_url": token_url,
    })


# --------------------------------------------------
# Public: token tracking page (REQUIRED by urls.py)
# --------------------------------------------------
@require_GET
def public_token_page(request, token_id):
    token = get_object_or_404(Token, id=token_id)
    return render(request, "public/token.html", {"token": token})


# --------------------------------------------------
# Public: token live status API (USED BY token.html)
# --------------------------------------------------
@require_GET
def public_token_status(request, token_id):
    token = get_object_or_404(Token, id=token_id)
    service_date = token.service_date

    last_used = (
        Token.objects
        .filter(service_date=service_date, status="used")
        .order_by("-used_at", "-id")
        .first()
    )

    # Tokens ahead: active tokens with smaller sequence (same day)
    ahead = Token.objects.filter(
        service_date=service_date,
        status="active",
        sequence__lt=token.sequence
    ).count()

    avg_minutes = 5
    est_wait = ahead * avg_minutes

    return JsonResponse({
        "ok": True,
        "your_token": token.number,
        "your_status": token.status,
        "now_serving": last_used.number if last_used else None,
        "tokens_ahead": ahead,
        "estimated_wait_minutes": est_wait,
        "service_date": str(service_date),
    })
