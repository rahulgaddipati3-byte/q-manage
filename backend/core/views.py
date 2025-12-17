import json

from django.http import JsonResponse
from django.utils import timezone
from django.db import transaction
from django.db.models import Max
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .models import Token, Counter


# =========================
# Settings (Option 2)
# =========================
TOKEN_PREFIX = "A"     # same prefix for all counters
TOKEN_PAD = 3          # A001 format


def _json_load(request):
    """Safe JSON loader for request body."""
    try:
        raw = request.body.decode("utf-8") if request.body else ""
        return json.loads(raw or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _dt(v):
    """Return ISO string for datetime/date or None."""
    if not v:
        return None
    try:
        return v.isoformat()
    except Exception:
        return str(v)


# --------------------------------------------------
# GET token status
# --------------------------------------------------
@require_GET
def token_status(request, number):
    try:
        token = Token.objects.get(number=str(number))
    except Token.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

    # live expire correction
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
# POST consume a specific token
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
        "status": token.status,
        "used_at": _dt(token.used_at),
    })


# --------------------------------------------------
# POST issue a new token
# Option 2 TRUE: A001, A002... per day across ALL counters
# counter is optional (reception style)
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

    with transaction.atomic():
        # GLOBAL sequence per day across ALL counters
        last_seq = (
            Token.objects
            .filter(service_date=service_date)
            .aggregate(m=Max("sequence"))["m"]
        ) or 0

        next_seq = last_seq + 1
        display_number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"  # A001, A002...

        token = Token.objects.create(
            counter=counter,            # can be null if reception-issued
            service_date=service_date,
            sequence=next_seq,
            number=display_number,
        )

    return JsonResponse({
        "ok": True,
        "number": token.number,
        "status": token.status,
        "counter": token.counter.code if token.counter else None,
        "service_date": _dt(token.service_date),
        "sequence": token.sequence,
        "created_at": _dt(token.created_at),
        "expires_at": _dt(token.expires_at),
    })


# --------------------------------------------------
# POST get next token for a counter (FIFO)
# prioritizes unassigned tokens first, then counter-assigned
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
        return JsonResponse({"ok": False, "error": "Counter not found or inactive"}, status=404)

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

        # Expire old tokens at front
        while token and token.is_expired():
            token.status = "expired"
            token.save(update_fields=["status"])
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

        if not token:
            return JsonResponse({"ok": False, "error": "No active tokens"}, status=404)

        # Assign unassigned token to this counter at call-time
        if token.counter is None:
            token.counter = counter
            token.save(update_fields=["counter"])

        token.status = "used"
        token.used_at = timezone.now()
        token.save(update_fields=["status", "used_at"])

    return JsonResponse({
        "ok": True,
        "message": "Next token",
        "counter": counter.code,
        "number": token.number,
        "status": token.status,
        "used_at": _dt(token.used_at),
    })


# --------------------------------------------------
# GET queue status (waiting counts + next token)
# --------------------------------------------------
@require_GET
def queue_status(request):
    code = request.GET.get("counter")
    if not code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found or inactive"}, status=404)

    unassigned_qs = Token.objects.filter(status="active", counter__isnull=True).order_by("created_at")
    assigned_qs = Token.objects.filter(status="active", counter=counter).order_by("created_at")

    unassigned_count = unassigned_qs.count()
    assigned_count = assigned_qs.count()

    next_tok = unassigned_qs.first() or assigned_qs.first()

    return JsonResponse({
        "ok": True,
        "counter": counter.code,
        "waiting_unassigned": unassigned_count,
        "waiting_assigned_to_counter": assigned_count,
        "waiting_total_relevant": unassigned_count + assigned_count,
        "next_token": next_tok.number if next_tok else None,
    })
