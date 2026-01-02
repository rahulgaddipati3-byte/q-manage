# backend/core/views.py
import json

from django.contrib.auth.decorators import login_required
from django.db import transaction, IntegrityError
from django.db.models import Avg, F, ExpressionWrapper, DurationField, IntegerField, Max
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


def _issue_token_for_today(*, counter, name="", phone="", address="") -> Token:
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
# POST {"counter":"A1", "name":"", "phone":"", "address":""}
# -------------------------
@csrf_exempt
def issue_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _read_json(request)
    counter_code = str(body.get("counter", "")).strip()
    if not counter_code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    name = str(body.get("name", "") or "").strip()
    phone = str(body.get("phone", "") or "").strip()
    address = str(body.get("address", "") or "").strip()

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
        "counter": counter.code,
        "number": token.number,
        "token_id": token.id,
        "status": token.status,
        "service_date": str(token.service_date),
        **details,
    })


# -------------------------
# API: next token (call next)
# POST {"counter":"A1"}
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

    with transaction.atomic():
        token = (
            Token.objects.select_for_update()
            .filter(counter=counter, status=STATUS_ACTIVE)
            .order_by("created_at", "id")
            .first()
        )

        while token and hasattr(token, "is_expired") and token.is_expired():
            token.status = STATUS_EXPIRED
            token.save(update_fields=["status"])
            token = (
                Token.objects.select_for_update()
                .filter(counter=counter, status=STATUS_ACTIVE)
                .order_by("created_at", "id")
                .first()
            )

        if not token:
            return JsonResponse({"ok": False, "error": "No active tokens"}, status=404)

        token.status = STATUS_USED
        token.used_at = timezone.now()
        token.save(update_fields=["status", "used_at"])

    details = _get_token_details(token)

    return JsonResponse({
        "ok": True,
        "message": "Next token called",
        "counter": counter.code,
        "number": token.number,
        "token_id": token.id,
        "status": token.status,
        "used_at": token.used_at.isoformat() if token.used_at else None,
        **details,
    })


# -------------------------
# API: token status by number  ✅ REQUIRED BY URLCONF
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
        "service_date": str(token.service_date),
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

    if counter_code:
        try:
            counter = Counter.objects.get(code=counter_code, is_active=True)
        except Counter.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Counter not found"}, status=404)
        base = base.filter(counter=counter)
    else:
        counter = None

    active = base.filter(status=STATUS_ACTIVE).order_by("created_at", "id")
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
        })

    last_used = (
        base.filter(status=STATUS_USED, used_at__isnull=False)
        .order_by("-used_at", "-id")
        .first()
    )

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
    today = Token.objects.filter(service_date=service_date)

    total_tokens = today.count()
    served_tokens = today.filter(status=STATUS_USED).count()
    waiting_tokens = today.filter(status=STATUS_ACTIVE).count()

    avg_wait = None
    used_qs = today.filter(status=STATUS_USED, used_at__isnull=False, created_at__isnull=False).annotate(
        wait=ExpressionWrapper(F("used_at") - F("created_at"), output_field=DurationField())
    )
    if used_qs.exists():
        avg = used_qs.aggregate(a=Avg("wait"))["a"]
        if avg:
            avg_wait = int(avg.total_seconds() // 60)

    counters = Counter.objects.filter(is_active=True).order_by("code")
    per_counter = []
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
