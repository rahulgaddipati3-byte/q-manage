# backend/core/public_views.py
import json
import re

from django.db import transaction, IntegrityError
from django.db.models import Max, IntegerField
from django.db.models.functions import Substr, Cast
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Token, Counter

TOKEN_PREFIX = "A"
TOKEN_PAD = 3

STATUS_ACTIVE = "active"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"

PHONE_RE = re.compile(r"^\d{10}$|^91\d{10}$")


def _dt(v):
    if not v:
        return None
    try:
        return v.isoformat()
    except Exception:
        return str(v)


def _json_error(msg, status=400, detail=None):
    data = {"ok": False, "error": msg}
    if detail:
        data["detail"] = detail
    return JsonResponse(data, status=status)


def _normalize_phone(phone):
    if not phone:
        return ""
    p = str(phone).strip().replace(" ", "")
    if p.startswith("+"):
        p = p[1:]
    return re.sub(r"\D", "", p)


def _token_field_names():
    # model field names only (safe)
    return {f.name for f in Token._meta.get_fields() if hasattr(f, "attname")}


def _build_customer_kwargs(name, phone, address):
    """
    Detect which customer/patient fields exist in Token model and return kwargs.
    This avoids 'unexpected keyword argument' crashes across environments.
    """
    fields = _token_field_names()

    candidates = [
        ("customer_name", "customer_phone", "customer_address"),
        ("patient_name", "patient_phone", "patient_address"),
        ("client_name", "client_phone", "client_address"),
        ("name", "phone", "address"),
    ]

    for fn, fp, fa in candidates:
        if fn in fields or fp in fields or fa in fields:
            kwargs = {}
            if fn in fields:
                kwargs[fn] = name
            if fp in fields:
                kwargs[fp] = phone
            if fa in fields:
                kwargs[fa] = address
            return kwargs

    # If none of the typical fields exist, don't pass any of these (still allow token issue)
    return {}


def _default_counter():
    return Counter.objects.filter(is_active=True).order_by("code").first()


def _issue_token_for_today(*, counter, name, phone, address) -> Token:
    if not counter:
        raise ValueError("No active counter")

    service_date = timezone.localdate()
    prefix_len = len(TOKEN_PREFIX)

    cust_kwargs = _build_customer_kwargs(name, phone, address)

    for _ in range(10):
        try:
            with transaction.atomic():
                qs = Token.objects.select_for_update().filter(service_date=service_date)

                last_seq = qs.aggregate(m=Max("sequence"))["m"] or 0
                last_num = (
                    qs.filter(number__startswith=TOKEN_PREFIX)
                      .annotate(num=Cast(Substr("number", prefix_len + 1), IntegerField()))
                      .aggregate(m=Max("num"))["m"]
                ) or 0

                next_seq = max(int(last_seq), int(last_num)) + 1
                number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"

                token = Token.objects.create(
                    counter=counter,
                    service_date=service_date,
                    sequence=next_seq,
                    number=number,
                    status=STATUS_ACTIVE,
                    **cust_kwargs,
                )
                return token
        except IntegrityError:
            continue

    raise IntegrityError("Could not issue token after retries")


# ---------------------------
# Public clinic landing page
# ---------------------------
@require_GET
def public_clinic_page(request, slug):
    return render(request, "public/clinic.html", {"clinic_slug": slug})


# ---------------------------
# Public clinic snapshot API
# ---------------------------
@require_GET
def public_clinic_snapshot(request, slug):
    service_date = timezone.localdate()

    last_used = (
        Token.objects.filter(service_date=service_date, status=STATUS_USED)
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


# ---------------------------
# Public reserve -> issue token (JSON)
# ---------------------------
@csrf_exempt
@require_POST
def public_reserve_token(request, slug):
    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except Exception:
        return _json_error("Invalid JSON", status=400)

    name = (payload.get("name") or "").strip()
    phone_raw = payload.get("phone") or payload.get("mobile") or ""
    phone = _normalize_phone(phone_raw)
    address = (payload.get("address") or "").strip()

    if not name:
        return _json_error("Name is required", status=400)

    if not phone or not PHONE_RE.match(phone):
        return _json_error("Enter valid 10-digit mobile (or 91XXXXXXXXXX)", status=400)

    counter = _default_counter()
    if counter is None:
        return _json_error(
            "System not configured: no active counters.",
            status=500,
            detail="Create at least one Counter with is_active=True in production DB."
        )

    try:
        token = _issue_token_for_today(counter=counter, name=name, phone=phone, address=address)
    except IntegrityError:
        return _json_error("Could not reserve token. Try again.", status=409)
    except Exception as e:
        return _json_error("Internal error", status=500, detail=str(e))

    return JsonResponse({
        "ok": True,
        "token_id": token.id,
        "token_number": token.number,
        "track_url": f"/public/token/{token.id}/",
        "service_date": str(token.service_date),
        "status": token.status,
        "counter": token.counter.code if token.counter else None,
    })


# ---------------------------
# Public token tracking page
# ---------------------------
@require_GET
def public_token_page(request, token_id):
    token = get_object_or_404(Token, id=token_id)
    return render(request, "public/token.html", {"token": token})


# ---------------------------
# Public token status API
# ---------------------------
@require_GET
def public_token_status(request, token_id):
    token = get_object_or_404(Token, id=token_id)
    service_date = token.service_date

    last_used = (
        Token.objects.filter(service_date=service_date, status=STATUS_USED)
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
