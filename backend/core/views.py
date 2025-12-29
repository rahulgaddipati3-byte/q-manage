# backend/core/views.py
import json
from django.http import JsonResponse
from django.utils import timezone
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required

from django.db.models import Max, IntegerField, Count, Avg, F, ExpressionWrapper, DurationField, Q
from django.db.models.functions import Substr, Cast

from .models import Token, Counter

TOKEN_PREFIX = "A"
TOKEN_PAD = 3

STATUS_ACTIVE = "active"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"


# -----------------------------
# Helpers
# -----------------------------
def _json_load(request):
    try:
        raw = request.body.decode("utf-8") if request.body else ""
        return json.loads(raw or "{}")
    except Exception:
        return None


def _dt(v):
    if not v:
        return None
    try:
        return v.isoformat()
    except Exception:
        return str(v)


def _has_field(model, field_name: str) -> bool:
    return any(f.name == field_name for f in model._meta.fields)


def _is_expired(token) -> bool:
    if _has_field(Token, "expires_at") and getattr(token, "expires_at", None):
        return timezone.now() >= token.expires_at
    return False


def _mark_expired_if_needed(token):
    if token.status == STATUS_ACTIVE and _is_expired(token):
        token.status = STATUS_EXPIRED
        token.save(update_fields=["status"])
        return True
    return False


def _get_or_create_counter(counter_code: str):
    counter_code = (counter_code or "").strip()
    if not counter_code:
        return None, "counter is required"

    counter, _ = Counter.objects.get_or_create(code=counter_code, defaults={"name": counter_code})
    if not counter.is_active:
        return None, "Counter not active"
    return counter, None


def _next_number_for_today(service_date):
    prefix_len = len(TOKEN_PREFIX)

    qs = Token.objects.filter(service_date=service_date)
    last_seq = qs.aggregate(m=Max("sequence"))["m"] or 0

    last_num = (
        qs.filter(number__startswith=TOKEN_PREFIX)
        .annotate(num=Cast(Substr("number", prefix_len + 1), IntegerField()))
        .aggregate(m=Max("num"))["m"]
    ) or 0

    next_seq = max(int(last_seq), int(last_num)) + 1
    number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"
    return next_seq, number


# -----------------------------
# API: Token status (GET)
# -----------------------------
@require_GET
def token_status(request, number):
    number = str(number).strip()
    today = timezone.localdate()

    token = Token.objects.filter(service_date=today, number=number).first()
    if not token:
        token = Token.objects.filter(number=number).order_by("-service_date", "-id").first()

    if not token:
        return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

    _mark_expired_if_needed(token)

    return JsonResponse({
        "ok": True,
        "number": token.number,
        "status": token.status,
        "counter": token.counter.code if token.counter else None,
        "service_date": _dt(getattr(token, "service_date", None)),
        "sequence": getattr(token, "sequence", None),
        "created_at": _dt(getattr(token, "created_at", None)),
        "expires_at": _dt(getattr(token, "expires_at", None)),
        "used_at": _dt(getattr(token, "used_at", None)),
    })


# -----------------------------
# API: Consume token (POST) -> marks USED
# Body: {"number":"A001"}
# -----------------------------
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

    today = timezone.localdate()

    with transaction.atomic():
        token = Token.objects.select_for_update().filter(service_date=today, number=number).first()
        if not token:
            # fallback if someone tries older token number
            token = Token.objects.select_for_update().filter(number=number).order_by("-service_date", "-id").first()

        if not token:
            return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

        _mark_expired_if_needed(token)

        if token.status == STATUS_EXPIRED:
            return JsonResponse({"ok": False, "error": "Token expired"}, status=400)
        if token.status == STATUS_USED:
            return JsonResponse({"ok": False, "error": "Token already used"}, status=400)
        if token.status != STATUS_ACTIVE:
            return JsonResponse({"ok": False, "error": f"Token not active (status={token.status})"}, status=400)

        token.status = STATUS_USED
        if _has_field(Token, "used_at"):
            token.used_at = timezone.now()
            token.save(update_fields=["status", "used_at"])
        else:
            token.save(update_fields=["status"])

    return JsonResponse({
        "ok": True,
        "message": "Token consumed",
        "number": token.number,
        "used_at": _dt(getattr(token, "used_at", None)),
    })


# -----------------------------
# API: Issue token (POST) -> creates ACTIVE directly
# Body: {"counter":"A1"} optional
# -----------------------------
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
        counter, err = _get_or_create_counter(counter_code)
        if err:
            return JsonResponse({"ok": False, "error": err}, status=400)

    service_date = timezone.localdate()

    with transaction.atomic():
        seq, number = _next_number_for_today(service_date)
        token = Token.objects.create(
            counter=counter,
            service_date=service_date,
            sequence=seq,
            number=number,
            status=STATUS_ACTIVE,
        )

    return JsonResponse({
        "ok": True,
        "number": token.number,
        "counter": token.counter.code if token.counter else None,
        "sequence": token.sequence,
        "status": token.status,
        "created_at": _dt(getattr(token, "created_at", None)),
    })


# -----------------------------
# API: Call Next token (POST)
# This marks USED because your UI expects "Now serving" to advance.
# Body: {"counter":"A1"}
# -----------------------------
@csrf_exempt
def next_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _json_load(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    counter_code = str(body.get("counter", "")).strip()
    counter, err = _get_or_create_counter(counter_code)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=400)

    today = timezone.localdate()

    with transaction.atomic():
        # Expire stale actives for today (best effort)
        if _has_field(Token, "expires_at"):
            exp_qs = Token.objects.select_for_update().filter(service_date=today, status=STATUS_ACTIVE)
            for t in exp_qs:
                if _is_expired(t):
                    t.status = STATUS_EXPIRED
                    t.save(update_fields=["status"])

        # Prefer assigned-to-this-counter first (fairness)
        token = (
            Token.objects.select_for_update()
            .filter(service_date=today, status=STATUS_ACTIVE, counter=counter)
            .order_by("sequence", "created_at")
            .first()
        )

        # Else take unassigned pool for today
        if not token:
            token = (
                Token.objects.select_for_update()
                .filter(service_date=today, status=STATUS_ACTIVE, counter__isnull=True)
                .order_by("sequence", "created_at")
                .first()
            )
            if token:
                token.counter = counter
                token.save(update_fields=["counter"])

        if not token:
            return JsonResponse({"ok": False, "error": "No active tokens"}, status=404)

        # Consume now
        token.status = STATUS_USED
        if _has_field(Token, "used_at"):
            token.used_at = timezone.now()
            token.save(update_fields=["status", "used_at"])
        else:
            token.save(update_fields=["status"])

    return JsonResponse({
        "ok": True,
        "counter": counter.code,
        "number": token.number,
        "status": token.status,
        "used_at": _dt(getattr(token, "used_at", None)),
    })


# -----------------------------
# API: Queue status (GET)
# /api/queue/status/?counter=A1
# -----------------------------
@require_GET
def queue_status(request):
    code = (request.GET.get("counter") or "").strip()
    if not code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found"}, status=404)

    today = timezone.localdate()

    waiting_assigned = Token.objects.filter(service_date=today, status=STATUS_ACTIVE, counter=counter).count()
    waiting_unassigned = Token.objects.filter(service_date=today, status=STATUS_ACTIVE, counter__isnull=True).count()

    next_tok = (
        Token.objects.filter(service_date=today, status=STATUS_ACTIVE, counter=counter)
        .order_by("sequence", "created_at")
        .first()
        or
        Token.objects.filter(service_date=today, status=STATUS_ACTIVE, counter__isnull=True)
        .order_by("sequence", "created_at")
        .first()
    )

    return JsonResponse({
        "ok": True,
        "counter": counter.code,
        "waiting_total": waiting_assigned + waiting_unassigned,
        "next_token": next_tok.number if next_tok else None,
    })


# -----------------------------
# UI: Custom Admin Dashboard
# -----------------------------
@login_required
def admin_dashboard(request):
    if not request.user.is_staff:
        return redirect("/login/")

    today = timezone.localdate()
    tokens = Token.objects.filter(service_date=today)

    issued = tokens.count()
    served = tokens.filter(status=STATUS_USED).count()
    waiting = tokens.filter(status=STATUS_ACTIVE).count()

    avg_wait = None
    if _has_field(Token, "used_at") and _has_field(Token, "created_at"):
        avg_wait = (
            tokens.filter(used_at__isnull=False)
            .annotate(wait=ExpressionWrapper(F("used_at") - F("created_at"), output_field=DurationField()))
            .aggregate(avg=Avg("wait"))["avg"]
        )

    per_counter = (
        tokens.values("counter__code")
        .annotate(
            issued=Count("id"),
            served=Count("id", filter=Q(status=STATUS_USED)),
            waiting=Count("id", filter=Q(status=STATUS_ACTIVE)),
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
