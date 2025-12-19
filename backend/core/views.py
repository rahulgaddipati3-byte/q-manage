# backend/core/views.py
import json

from django.http import JsonResponse
from django.utils import timezone
from django.db import transaction, IntegrityError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required

from django.db.models import (
    Max, IntegerField, Count, Avg, F,
    ExpressionWrapper, DurationField, Q
)
from django.db.models.functions import Substr, Cast

from .models import Token, Counter

TOKEN_PREFIX = "A"
TOKEN_PAD = 3


# --------------------------------------------------
# Helpers
# --------------------------------------------------
def _json_load(request):
    try:
        raw = request.body.decode("utf-8") if request.body else ""
        return json.loads(raw or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _dt(v):
    if not v:
        return None
    try:
        return v.isoformat()
    except Exception:
        return str(v)


# --------------------------------------------------
# API: Token status (GET)
# --------------------------------------------------
@require_GET
def token_status(request, number):
    try:
        token = Token.objects.get(number=str(number))
    except Token.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

    if token.status == "active" and token.is_expired():
        token.status = "expired"
        token.save(update_fields=["status"])

    return JsonResponse({
        "ok": True,
        "number": token.number,
        "status": token.status,
        "counter": token.counter.code if token.counter else None,
        "service_date": _dt(token.service_date),
        "sequence": token.sequence,
        "created_at": _dt(token.created_at),
        "expires_at": _dt(token.expires_at),
        "used_at": _dt(token.used_at),
    })


# --------------------------------------------------
# API: Consume token (POST)
# --------------------------------------------------
@csrf_exempt
def consume_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _json_load(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    number = str(body.get("number", "")).strip()
    if not number:
        return JsonResponse({"ok": False, "error": "number is required"}, status=400)

    with transaction.atomic():
        try:
            token = Token.objects.select_for_update().get(number=number)
        except Token.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

        if token.status == "active" and token.is_expired():
            token.status = "expired"
            token.save(update_fields=["status"])

        if token.status == "expired":
            return JsonResponse({"ok": False, "error": "Token expired"}, status=400)

        if token.status == "used":
            return JsonResponse({"ok": False, "error": "Token already used"}, status=400)

        token.status = "used"
        token.used_at = timezone.now()
        token.save(update_fields=["status", "used_at"])

    return JsonResponse({
        "ok": True,
        "message": "Token consumed",
        "number": token.number,
        "used_at": _dt(token.used_at),
    })


# --------------------------------------------------
# API: Issue token (POST)
# --------------------------------------------------
@csrf_exempt
def issue_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _json_load(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    counter_code = str(body.get("counter", "")).strip()

    counter = None
    if counter_code:
        counter, _ = Counter.objects.get_or_create(
            code=counter_code,
            defaults={"name": counter_code}
        )
        if not counter.is_active:
            return JsonResponse({"ok": False, "error": "Counter not active"}, status=400)

    service_date = timezone.localdate()
    prefix_len = len(TOKEN_PREFIX)

    for _ in range(10):
        try:
            with transaction.atomic():
                qs = Token.objects.filter(service_date=service_date)

                last_seq = qs.aggregate(m=Max("sequence"))["m"] or 0
                last_num = (
                    qs.filter(number__startswith=TOKEN_PREFIX)
                    .annotate(num=Cast(Substr("number", prefix_len + 1), IntegerField()))
                    .aggregate(m=Max("num"))["m"]
                ) or 0

                next_seq = max(last_seq, last_num) + 1
                number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"

                token = Token.objects.create(
                    counter=counter,
                    service_date=service_date,
                    sequence=next_seq,
                    number=number,
                )

            return JsonResponse({
                "ok": True,
                "number": token.number,
                "counter": token.counter.code if token.counter else None,
                "sequence": token.sequence,
                "created_at": _dt(token.created_at),
            })

        except IntegrityError:
            continue

    return JsonResponse({"ok": False, "error": "Could not issue token"}, status=409)


# --------------------------------------------------
# API: Next token (POST)
# --------------------------------------------------
@csrf_exempt
def next_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _json_load(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    counter_code = str(body.get("counter", "")).strip()
    if not counter_code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=counter_code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found"}, status=404)

    with transaction.atomic():
        token = (
            Token.objects.select_for_update()
            .filter(status="active", counter__isnull=True)
            .order_by("created_at")
            .first()
        ) or (
            Token.objects.select_for_update()
            .filter(status="active", counter=counter)
            .order_by("created_at")
            .first()
        )

        while token and token.is_expired():
            token.status = "expired"
            token.save(update_fields=["status"])
            token = (
                Token.objects.select_for_update()
                .filter(status="active", counter__isnull=True)
                .order_by("created_at")
                .first()
            )

        if not token:
            return JsonResponse({"ok": False, "error": "No active tokens"}, status=404)

        token.counter = counter
        token.status = "used"
        token.used_at = timezone.now()
        token.save(update_fields=["counter", "status", "used_at"])

    return JsonResponse({
        "ok": True,
        "counter": counter.code,
        "number": token.number,
        "used_at": _dt(token.used_at),
    })


# --------------------------------------------------
# API: Queue status (GET)
# --------------------------------------------------
@require_GET
def queue_status(request):
    code = request.GET.get("counter")
    if not code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found"}, status=404)

    unassigned = Token.objects.filter(status="active", counter__isnull=True)
    assigned = Token.objects.filter(status="active", counter=counter)

    next_tok = unassigned.order_by("created_at").first() or assigned.order_by("created_at").first()

    return JsonResponse({
        "ok": True,
        "counter": counter.code,
        "waiting_total": unassigned.count() + assigned.count(),
        "next_token": next_tok.number if next_tok else None,
    })


# --------------------------------------------------
# UI: Custom Admin Dashboard
# --------------------------------------------------
@login_required
def admin_dashboard(request):
    if not request.user.is_staff:
        return redirect("/login/")

    today = timezone.localdate()
    tokens = Token.objects.filter(service_date=today)

    issued = tokens.count()
    served = tokens.filter(status="used").count()
    waiting = tokens.filter(status="active").count()

    avg_wait = (
        tokens.filter(used_at__isnull=False)
        .annotate(
            wait=ExpressionWrapper(
                F("used_at") - F("created_at"),
                output_field=DurationField(),
            )
        )
        .aggregate(avg=Avg("wait"))["avg"]
    )

    per_counter = (
        tokens.values("counter__code")
        .annotate(
            issued=Count("id"),
            served=Count("id", filter=Q(status="used")),
            waiting=Count("id", filter=Q(status="active")),
        )
        .order_by("counter__code")
    )

    return render(
        request,
        "core/admin_dashboard.html",
        {
            "issued": issued,
            "served": served,
            "waiting": waiting,
            "avg_wait": avg_wait,
            "per_counter": per_counter,
            "today": today,
        },
    )
