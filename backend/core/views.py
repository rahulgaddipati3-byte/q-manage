# backend/core/views.py
import json

from django.contrib.auth.decorators import login_required
from django.db import transaction, IntegrityError
from django.db.models import (
    Avg,
    F,
    ExpressionWrapper,
    DurationField,
    IntegerField,
    Max,
    Q,
    Case,
    When,
    Value,
)
from django.db.models.functions import Substr, Cast
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .models import Token, Counter

TOKEN_PREFIX = "A"
TOKEN_PAD = 3

STATUS_ACTIVE = "active"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"


# -------------------------
# Helpers
# -------------------------
def _read_json(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


def _token_field_names(model_cls):
    names = set()
    for f in model_cls._meta.get_fields():
        if getattr(f, "concrete", False):
            names.add(f.name)
    return names


def _set_first_existing_field(obj, candidates, value):
    """
    Safely set name/phone/address fields even if your Token model uses
    different field names (customer_name vs patient_name etc).
    """
    if value is None:
        return False
    value = str(value).strip()
    if value == "":
        return False

    field_names = _token_field_names(obj.__class__)
    for name in candidates:
        if name in field_names:
            setattr(obj, name, value)
            return True
    return False


def _get_token_details(token):
    def first_attr(names, default=""):
        for n in names:
            if hasattr(token, n):
                v = getattr(token, n)
                if v:
                    return v
        return default

    return {
        "customer_name": first_attr(["customer_name", "patient_name", "name", "full_name"], ""),
        "customer_phone": first_attr(["customer_phone", "patient_phone", "phone", "mobile"], ""),
        "customer_address": first_attr(["customer_address", "patient_address", "address"], ""),
    }


def _set_used_at_if_exists(token):
    """Some models might not have used_at. Set only if present."""
    if "used_at" in _token_field_names(token.__class__):
        token.used_at = timezone.now()
        return True
    return False


def _today_queryset():
    """
    Base queryset for "today".

    Primary source of truth: service_date == localdate()
    Fallback: service_date is NULL but created_at's date is today
    (helps if older records were created without service_date).
    """
    service_date = timezone.localdate()
    return Token.objects.filter(
        Q(service_date=service_date) |
        Q(service_date__isnull=True, created_at__date=service_date)
    ).distinct()



def _issue_token_for_today(*, counter=None, name="", phone="", address="") -> Token:
    """
    Issues a token for today. Counter can be:
    - None => reception style (unassigned)
    - Counter instance => directly assigned
    """
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
                    counter=counter,                 # ✅ can be None (reception)
                    service_date=service_date,       # ✅ consistent day anchor
                    sequence=next_seq,
                    number=number,
                    status=STATUS_ACTIVE,
                )

                changed = False
                changed |= _set_first_existing_field(token, ["customer_name", "patient_name", "name", "full_name"], name)
                changed |= _set_first_existing_field(token, ["customer_phone", "patient_phone", "phone", "mobile"], phone)
                changed |= _set_first_existing_field(token, ["customer_address", "patient_address", "address"], address)

                if changed:
                    token.save()

                return token

        except IntegrityError:
            continue

    raise IntegrityError("Could not issue token after retries")


# -------------------------
# API: issue token
# POST {"counter":"A1"(optional), "name":"", "phone":"", "address":""}
# If counter is omitted/blank => reception-style unassigned token.
# -------------------------
@csrf_exempt
def issue_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _read_json(request)

    counter_code = str(body.get("counter", "") or "").strip()  # optional now

    name = str(body.get("name", "") or "").strip()
    phone = str(body.get("phone", "") or "").strip()
    address = str(body.get("address", "") or "").strip()

    counter = None
    if counter_code:
        try:
            counter = Counter.objects.get(code=counter_code, is_active=True)
        except Counter.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Counter not found or inactive"}, status=404)

    try:
        token = _issue_token_for_today(counter=counter, name=name, phone=phone, address=address)
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "Could not issue token. Try again."}, status=409)

    details = _get_token_details(token)

    return JsonResponse({
        "ok": True,
        "counter": counter.code if counter else None,
        "number": token.number,
        "token_id": token.id,
        "status": token.status,
        "service_date": str(token.service_date),
        **details,
    })


# -------------------------
# API: next token (call next)
# POST {"counter":"A1"}
#
# ✅ FIX: counter should pull from:
#   - tokens already assigned to this counter
#   - OR unassigned tokens (reception flow)
#
# ✅ When it picks an unassigned token, it ASSIGNS it to this counter.
# -------------------------
@csrf_exempt
def next_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _read_json(request)
    counter_code = str(body.get("counter", "")).strip()
    if not counter_code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=counter_code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found or inactive"}, status=404)

    service_date = timezone.localdate()

    with transaction.atomic():
        # ✅ Build one base queryset with an annotation so ordering works
        base = (
            Token.objects.select_for_update()
            .filter(service_date=service_date, status=STATUS_ACTIVE)
            .filter(Q(counter__isnull=True) | Q(counter=counter))
            .annotate(
                unassigned_first=Case(
                    When(counter__isnull=True, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            )
            .order_by("-unassigned_first", "sequence", "id")
        )

        token = base.first()

        # Expire any expired tokens (if model supports is_expired())
        while token and hasattr(token, "is_expired") and callable(getattr(token, "is_expired")) and token.is_expired():
            token.status = STATUS_EXPIRED
            token.save(update_fields=["status"])
            token = base.first()

        if not token:
            return JsonResponse({"ok": False, "error": "No active tokens"}, status=404)

        # ✅ If it was unassigned, assign it to this counter now
        if token.counter_id is None:
            token.counter = counter

        token.status = STATUS_USED
        used_at_changed = _set_used_at_if_exists(token)

        fields = ["status", "counter"]
        if used_at_changed:
            fields.append("used_at")

        token.save(update_fields=fields)

    details = _get_token_details(token)

    return JsonResponse({
        "ok": True,
        "message": "Next token called",
        "counter": counter.code,
        "number": token.number,
        "token_id": token.id,
        "status": token.status,
        "used_at": token.used_at.isoformat() if hasattr(token, "used_at") and token.used_at else None,
        **details,
    })


# -------------------------
# API: token status by number
# GET /api/token/status/<number>/
# -------------------------
@require_GET
def token_status(request, number):
    token = Token.objects.filter(number=number).first()
    if not token:
        return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

    details = _get_token_details(token)

    return JsonResponse({
        "ok": True,
        "number": token.number,
        "status": token.status,
        "service_date": str(token.service_date) if token.service_date else None,
        "counter": token.counter.code if token.counter else None,
        **details,
    })


# -------------------------
# API: queue status
# GET /api/queue/status/?counter=A1
# -------------------------
@require_GET
def queue_status(request):
    service_date = timezone.localdate()
    counter_code = (request.GET.get("counter") or "").strip()

    base = Token.objects.filter(service_date=service_date)

    counter = None
    if counter_code:
        try:
            counter = Counter.objects.get(code=counter_code, is_active=True)
        except Counter.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Counter not found"}, status=404)

        # counter can consume unassigned too
        base = base.filter(Q(counter=counter) | Q(counter__isnull=True))

    active = base.filter(status=STATUS_ACTIVE).order_by("sequence", "id")

    waiting_list = []
    for t in active[:50]:
        d = _get_token_details(t)
        waiting_list.append({
            "token_id": t.id,
            "number": t.number,
            "customer_name": d["customer_name"],
            "customer_phone": d["customer_phone"],
            "customer_address": d["customer_address"],
            "status": t.status,
            "counter": t.counter.code if t.counter else None,
        })

    # ✅ safer "last used"
    used = base.filter(status=STATUS_USED)
    if "used_at" in _token_field_names(Token):
        last_used = used.filter(used_at__isnull=False).order_by("-used_at", "-id").first()
    else:
        last_used = used.order_by("-id").first()

    return JsonResponse({
        "ok": True,
        "service_date": str(service_date),
        "counter": counter.code if counter else None,
        "now_serving": last_used.number if last_used else None,
        "waiting_count": active.count(),
        "next_token": waiting_list[0]["number"] if waiting_list else None,
        "waiting_list": waiting_list,
    })


# -------------------------
# Admin dashboard
# -------------------------
@login_required
def admin_dashboard(request):
    service_date = timezone.localdate()

    today = _today_queryset()

    total_tokens = today.count()
    served_tokens = today.filter(status=STATUS_USED).count()
    waiting_tokens = today.filter(status=STATUS_ACTIVE).count()

    avg_wait = None
    if "used_at" in _token_field_names(Token) and "created_at" in _token_field_names(Token):
        used_qs = today.filter(
            status=STATUS_USED,
            used_at__isnull=False,
            created_at__isnull=False,
        ).annotate(
            wait=ExpressionWrapper(F("used_at") - F("created_at"), output_field=DurationField())
        )
        if used_qs.exists():
            avg = used_qs.aggregate(a=Avg("wait"))["a"]
            if avg:
                avg_wait = int(avg.total_seconds() // 60)

    counters = Counter.objects.filter(is_active=True).order_by("code")

    per_counter = []
    per_counter.append({
        "code": None,
        "name": "(unassigned)",
        "issued": today.filter(counter__isnull=True).count(),
        "served": today.filter(counter__isnull=True, status=STATUS_USED).count(),
        "waiting": today.filter(counter__isnull=True, status=STATUS_ACTIVE).count(),
    })

    for c in counters:
        per_counter.append({
            "code": c.code,
            "name": c.name,
            "issued": today.filter(counter=c).count(),
            "served": today.filter(counter=c, status=STATUS_USED).count(),
            "waiting": today.filter(counter=c, status=STATUS_ACTIVE).count(),
        })

    return render(request, "core/admin_dashboard.html", {
        "service_date": service_date,
        "total_tokens": total_tokens,
        "served_tokens": served_tokens,
        "waiting_tokens": waiting_tokens,
        "avg_wait_minutes": avg_wait,
        "per_counter": per_counter,
    })
