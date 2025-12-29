# backend/core/public_views.py
import json
import re

from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction, IntegrityError
from django.db.models import Max

from .models import Token, Counter


# -------------------------
# Token numbering
# -------------------------
TOKEN_PREFIX = "A"
TOKEN_PAD = 3

STATUS_ACTIVE = "active"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"

# Accept:
#  - 10 digits: 9876543210
#  - 12 digits starting 91: 919876543210
PHONE_RE = re.compile(r"^\d{10}$|^91\d{10}$")


def _dt(v):
    if not v:
        return None
    try:
        return v.isoformat()
    except Exception:
        return str(v)


def _normalize_phone(phone: str) -> str:
    """Normalize to digits only. Keeps 10-digit or 91XXXXXXXXXX formats."""
    if not phone:
        return ""
    p = phone.strip().replace(" ", "")
    if p.startswith("+"):
        p = p[1:]
    p = re.sub(r"\D", "", p)
    return p


def _get_default_counter():
    """
    Public reserve must be visible on staff screen.
    So assign to the first active counter (A1 typically).
    """
    return Counter.objects.filter(is_active=True).order_by("code").first()


def _issue_token_for_today(counter: Counter) -> Token:
    """
    Creates an ACTIVE token for today and assigns it to a counter
    so it appears on staff queue for that counter.
    Safe against collisions via retry on IntegrityError.
    """
    if counter is None:
        raise ValueError("No active counter available")

    service_date = timezone.localdate()

    for _ in range(10):
        try:
            with transaction.atomic():
                qs = Token.objects.select_for_update().filter(service_date=service_date)

                last_seq = qs.aggregate(m=Max("sequence"))["m"] or 0
                next_seq = int(last_seq) + 1
                number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"

                token = Token.objects.create(
                    counter=counter,
                    service_date=service_date,
                    sequence=next_seq,
                    number=number,
                    status=STATUS_ACTIVE,
                )
                return token
        except IntegrityError:
            continue

    raise IntegrityError("Could not issue token after retries")


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
        .filter(service_date=service_date, status=STATUS_USED)
        .order_by("-used_at", "-id")
        .first()
    )

    active_count = Token.objects.filter(service_date=service_date, status=STATUS_ACTIVE).count()

    avg_minutes = 5
    estimated_wait_min = active_count * avg_minutes

    return JsonResponse({
        "ok": True,
        "clinic": slug,
        "now_serving": last_used.number if last_used else None,
        "people_waiting": active_count,
        "estimated_wait_min": estimated_wait_min,
        "active_tokens": active_count,
    })


# --------------------------------------------------
# Public: reserve token (AUTO issues Token immediately)
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

    # Assign to an active counter so it shows on staff screen
    counter = _get_default_counter()
    if not counter:
        return JsonResponse({"ok": False, "error": "No counters available. Create counters first."}, status=500)

    try:
        token = _issue_token_for_today(counter=counter)
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "Could not reserve token. Try again."}, status=409)

    return JsonResponse({
        "ok": True,
        "token_id": token.id,
        "token_number": token.number,
        "track_url": f"/public/token/{token.id}/",
        "service_date": str(token.service_date),
        "status": token.status,
        "counter": counter.code,
    })


# --------------------------------------------------
# Public: token tracking page
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
        .filter(service_date=service_date, status=STATUS_USED)
        .order_by("-used_at", "-id")
        .first()
    )

    ahead = Token.objects.filter(
        service_date=service_date,
        status=STATUS_ACTIVE,
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
        "created_at": _dt(getattr(token, "created_at", None)),
        "counter": token.counter.code if token.counter else None,
    })
