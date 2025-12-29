# backend/core/public_views.py
import json
import re

from django.db import transaction, IntegrityError
from django.db import models
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

# Accept 10-digit OR 91XXXXXXXXXX (country code without +)
PHONE_RE = re.compile(r"^\d{10}$|^91\d{10}$")


def _dt(v):
    if not v:
        return None
    try:
        return v.isoformat()
    except Exception:
        return str(v)


def _normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    p = str(phone).strip().replace(" ", "")
    if p.startswith("+"):
        p = p[1:]
    return re.sub(r"\D", "", p)


def _json_error(message: str, *, status: int = 400, detail: str | None = None):
    payload = {"ok": False, "error": message}
    if detail:
        payload["detail"] = detail
    return JsonResponse(payload, status=status)


def _read_payload(request):
    """
    Accept BOTH:
    - JSON body: {"name": "...", "phone"/"mobile": "...", "address": "..."}
    - Form POST (FormData): name, phone/mobile, address
    Returns dict-like payload.
    """
    ctype = (request.content_type or "").lower()

    if "application/json" in ctype:
        try:
            return json.loads((request.body or b"{}").decode("utf-8"))
        except Exception:
            return None

    if request.POST:
        return request.POST

    try:
        return json.loads((request.body or b"{}").decode("utf-8"))
    except Exception:
        return None


def _default_counter():
    """
    Returns (counter_or_none, error_detail_or_none)

    - If an active counter exists => return it
    - Else => attempt to create a default counter "c1" using smart defaults
      for required (NOT NULL, no default) fields.
    - If Counter has a REQUIRED ForeignKey or other complex required field,
      creation may still fail; in that case error_detail contains the exact reason.
    """
    # 1) existing active
    c = Counter.objects.filter(is_active=True).order_by("code").first()
    if c:
        return c, None

    # 2) smart defaults for required fields
    defaults: dict[str, object] = {"is_active": True}

    try:
        for f in Counter._meta.fields:
            # skip PK/auto fields
            if getattr(f, "primary_key", False):
                continue
            if isinstance(f, (models.AutoField, models.BigAutoField)):
                continue

            fname = f.name

            # code will be provided in get_or_create(code="c1")
            if fname == "code":
                continue

            # if already provided, skip
            if fname in defaults:
                continue

            # fill only required: NOT NULL + no default
            has_default = f.default is not models.NOT_PROVIDED
            if f.null or has_default:
                continue

            # if choices exist, choose first option
            if getattr(f, "choices", None):
                try:
                    defaults[fname] = f.choices[0][0]
                    continue
                except Exception:
                    pass

            # fill by field type
            if isinstance(f, (models.CharField, models.TextField, models.SlugField)):
                defaults[fname] = "Counter 1"
            elif isinstance(f, (models.IntegerField, models.BigIntegerField, models.SmallIntegerField)):
                defaults[fname] = 1
            elif isinstance(f, models.BooleanField):
                defaults[fname] = True
            elif isinstance(f, models.DateTimeField):
                defaults[fname] = timezone.now()
            elif isinstance(f, models.DateField):
                defaults[fname] = timezone.localdate()
            elif isinstance(f, models.TimeField):
                defaults[fname] = timezone.localtime().time()
            elif isinstance(f, models.ForeignKey):
                # Cannot safely guess a required FK target without knowing the model.
                # Let it fail with a clear error detail; we'll fix based on the exception text.
                pass

        # create or fetch
        c, _ = Counter.objects.get_or_create(code="c1", defaults=defaults)

        # ensure active
        if hasattr(c, "is_active") and not c.is_active:
            c.is_active = True
            c.save(update_fields=["is_active"])

        return c, None

    except Exception as e:
        return None, str(e)


def _issue_token_for_today(*, counter, name="", phone="", address="") -> Token:
    """
    Create ACTIVE token for today with collision-safe retry.
    """
    if counter is None:
        raise ValueError("No active counter available")

    service_date = timezone.localdate()
    prefix_len = len(TOKEN_PREFIX)

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
                    customer_name=name,
                    customer_phone=phone,
                    customer_address=address,
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
# Public reserve -> auto issue token
# ---------------------------
@csrf_exempt
@require_POST
def public_reserve_token(request, slug):
    payload = _read_payload(request)
    if payload is None:
        return _json_error("Invalid request payload", status=400)

    name = (payload.get("name") or "").strip()
    phone_raw = payload.get("phone") or payload.get("mobile") or ""
    phone = _normalize_phone(phone_raw)
    address = (payload.get("address") or "").strip()

    if not name:
        return _json_error("Name is required", status=400)

    if not phone or not PHONE_RE.match(phone):
        return _json_error("Enter valid 10-digit mobile (or 91XXXXXXXXXX)", status=400)

    counter, counter_err = _default_counter()
    if counter is None:
        return _json_error(
            "System not configured: could not create/find an active counter.",
            status=500,
            detail=counter_err or "No active counter found."
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
