# backend/core/public_views.py
import json
import re

from django.db import IntegrityError, transaction
from django.db.models import IntegerField, Max
from django.db.models.functions import Cast, Substr
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Counter, Token

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
    - JSON body
    - Form POST
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
    return Counter.objects.filter(is_active=True).order_by("code").first()


# ---------- Robust field-mapping helpers ----------

_FIELD_CACHE = {}


def _token_field_names(model_cls):
    """
    Return a set of real concrete DB field names for the model.
    Cached for speed.
    """
    key = f"{model_cls._meta.label_lower}"
    if key in _FIELD_CACHE:
        return _FIELD_CACHE[key]

    names = set()
    for f in model_cls._meta.get_fields():
        if getattr(f, "concrete", False):
            names.add(f.name)

    _FIELD_CACHE[key] = names
    return names


def _set_first_existing_field(obj, candidates, value):
    """
    Set the first candidate field that exists on obj's model.
    """
    if value is None:
        return False
    field_names = _token_field_names(obj.__class__)
    for name in candidates:
        if name in field_names:
            setattr(obj, name, value)
            return True
    return False


def _store_customer_details(token: Token, name: str, phone: str, address: str):
    """
    Store customer details into ANY matching fields present in Token model.
    Also supports JSON/meta field if present.
    """
    changed = False

    # Name candidates (broad)
    changed |= _set_first_existing_field(
        token,
        [
            "customer_name", "patient_name", "client_name", "full_name", "name",
            "person_name", "visitor_name"
        ],
        name,
    )

    # Phone candidates (broad)
    changed |= _set_first_existing_field(
        token,
        [
            "customer_phone", "patient_phone", "client_phone", "phone", "mobile",
            "phone_number", "mobile_number", "contact", "contact_number"
        ],
        phone,
    )

    # Address candidates (broad)
    changed |= _set_first_existing_field(
        token,
        [
            "customer_address", "patient_address", "client_address", "address",
            "addr", "location", "customer_location"
        ],
        address,
    )

    # If you have a JSON field for arbitrary payload, store it too
    meta_field_candidates = ["meta", "payload", "data", "extra", "details"]
    for mf in meta_field_candidates:
        if mf in _token_field_names(token.__class__):
            try:
                existing = getattr(token, mf) or {}
                if not isinstance(existing, dict):
                    existing = {"value": existing}
                existing.update({"name": name, "phone": phone, "address": address})
                setattr(token, mf, existing)
                changed = True
                break
            except Exception:
                pass

    if changed:
        token.save(update_fields=None)  # safest (works even if unknown fields changed)


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

                # Create with ONLY guaranteed fields
                token = Token.objects.create(
                    counter=counter,
                    service_date=service_date,
                    sequence=next_seq,
                    number=number,
                    status=STATUS_ACTIVE,
                )

                # Store details in whatever fields your Token model actually has
                _store_customer_details(token, name=name, phone=phone, address=address)

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

    return JsonResponse(
        {
            "ok": True,
            "clinic": slug,
            "now_serving": last_used.number if last_used else None,
            "people_waiting": active_count,
            "estimated_wait_min": estimated_wait_min,
            "active_tokens": active_count,
        }
    )


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

    counter = _default_counter()
    if counter is None:
        return _json_error(
            "System not configured: no active counters.",
            status=500,
            detail="Create at least one Counter with is_active=True in production DB.",
        )

    try:
        token = _issue_token_for_today(counter=counter, name=name, phone=phone, address=address)
    except IntegrityError:
        return _json_error("Could not reserve token. Try again.", status=409)
    except Exception as e:
        return _json_error("Internal error", status=500, detail=str(e))

    return JsonResponse(
        {
            "ok": True,
            "token_id": token.id,
            "token_number": token.number,
            "track_url": f"/public/token/{token.id}/",
            "service_date": str(token.service_date),
            "status": token.status,
            "counter": token.counter.code if token.counter else None,
        }
    )


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
        service_date=service_date, status=STATUS_ACTIVE, sequence__lt=token.sequence
    ).count()

    avg_minutes = 5
    est_wait = ahead * avg_minutes

    return JsonResponse(
        {
            "ok": True,
            "your_token": token.number,
            "your_status": token.status,
            "now_serving": last_used.number if last_used else None,
            "tokens_ahead": ahead,
            "estimated_wait_minutes": est_wait,
            "service_date": str(service_date),
            "created_at": _dt(getattr(token, "created_at", None)),
            "counter": token.counter.code if token.counter else None,
        }
    )
