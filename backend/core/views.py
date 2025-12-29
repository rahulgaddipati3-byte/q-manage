# backend/core/views.py
import json

from django.contrib.auth.decorators import login_required
from django.db import transaction, IntegrityError
from django.db.models import Count, Avg, F, ExpressionWrapper, DurationField, Q, IntegerField, Max
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


def _issue_token_for_today(*, counter) -> Token:
    """
    Staff issue token for today (assigned to counter).
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


# -------------------------
# API: issue token
# POST {"counter":"A1"}
# -------------------------
@csrf_exempt
def issue_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = {}

    counter_code = str(body.get("counter", "")).strip()
    if not counter_code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=counter_code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found or inactive"}, status=404)

    try:
        token = _issue_token_for_today(counter=counter)
    except IntegrityError:
        return JsonResponse({"ok": False, "error": "Could not issue token. Try again."}, status=409)

    return JsonResponse({
        "ok": True,
        "counter": counter.code,
        "number": token.number,
        "token_id": token.id,
        "status": token.status,
        "service_date": str(token.service_date),
    })


# -------------------------
# API: next token (call next)
# POST {"counter":"A1"}
# marks oldest ACTIVE token as USED
# -------------------------
@csrf_exempt
def next_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = {}

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

    return JsonResponse({
        "ok": True,
        "message": "Next token called",
        "counter": counter.code,
        "number": token.number,
        "token_id": token.id,
        "status": token.status,
        "used_at": token.used_at.isoformat() if token.used_at else None,
        "customer_name": getattr(token, "customer_name", ""),
        "customer_phone": getattr(token, "customer_phone", ""),
        "customer_address": getattr(token, "customer_address", ""),
    })


# -------------------------
# API: consume token by number
# -------------------------
@csrf_exempt
def consume_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = {}

    number = str(body.get("number", "")).strip()
    if not number:
        return JsonResponse({"ok": False, "error": "number is required"}, status=400)

    with transaction.atomic():
        token = Token.objects.select_for_update().filter(number=number).first()
        if not token:
            return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

        token.status = STATUS_USED
        token.used_at = timezone.now()
        token.save(update_fields=["status", "used_at"])

    return JsonResponse({"ok": True, "number": token.number, "status": token.status})


# -------------------------
# API: token status by number
# -------------------------
def token_status(request, number):
    token = Token.objects.filter(number=number).first()
    if not token:
        return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

    return JsonResponse({
        "ok": True,
        "number": token.number,
        "status": token.status,
        "service_date": str(token.service_date),
        "counter": token.counter.code if token.counter else None,
    })


# -------------------------
# API: queue status (optionally per counter)
# GET /api/queue/status/?counter=A1
# returns waiting list with patient info
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
    waiting_count = active.count()

    waiting_list = []
    for t in active[:20]:
        waiting_list.append({
            "token_id": t.id,
            "number": t.number,
            "created_at": t.created_at.isoformat() if getattr(t, "created_at", None) else None,
            "customer_name": getattr(t, "customer_name", ""),
            "customer_phone": getattr(t, "customer_phone", ""),
            "customer_address": getattr(t, "customer_address", ""),
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
        "waiting_count": waiting_count,
        "next_token": waiting_list[0]["number"] if waiting_list else None,
        "waiting_list": waiting_list,
    })


# -------------------------
# Admin dashboard (custom)
# FIX: counts all tokens for today
# -------------------------
@login_required
def admin_dashboard(request):
    service_date = timezone.localdate()

    today = Token.objects.filter(service_date=service_date)

    total_tokens = today.count()
    served_tokens = today.filter(status=STATUS_USED).count()
    waiting_tokens = today.filter(status=STATUS_ACTIVE).count()

    avg_wait = None
    # average wait = used_at - created_at, only if both exist
    if total_tokens:
        used_qs = today.filter(status=STATUS_USED, used_at__isnull=False, created_at__isnull=False).annotate(
            wait=ExpressionWrapper(F("used_at") - F("created_at"), output_field=DurationField())
        )
        if used_qs.exists():
            avg = used_qs.aggregate(a=Avg("wait"))["a"]
            if avg:
                avg_wait = int(avg.total_seconds() // 60)

    # per counter summary
    counters = Counter.objects.filter(is_active=True).order_by("code")
    per_counter = []
    for c in counters:
        issued = today.filter(counter=c).count()
        served = today.filter(counter=c, status=STATUS_USED).count()
        waiting = today.filter(counter=c, status=STATUS_ACTIVE).count()
        per_counter.append({
            "code": c.code,
            "name": c.name,
            "issued": issued,
            "served": served,
            "waiting": waiting,
        })

    # include "unassigned" too (just in case)
    unassigned_issued = today.filter(counter__isnull=True).count()
    unassigned_served = today.filter(counter__isnull=True, status=STATUS_USED).count()
    unassigned_waiting = today.filter(counter__isnull=True, status=STATUS_ACTIVE).count()

    per_counter.append({
        "code": "(unassigned)",
        "name": "(unassigned)",
        "issued": unassigned_issued,
        "served": unassigned_served,
        "waiting": unassigned_waiting,
    })

    return render(request, "core/admin_dashboard.html", {
        "service_date": service_date,
        "total_tokens": total_tokens,
        "served_tokens": served_tokens,
        "waiting_tokens": waiting_tokens,
        "avg_wait_minutes": avg_wait,
        "per_counter": per_counter,
    })
