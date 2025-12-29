# backend/core/views.py
import json

from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.db import transaction, IntegrityError
from django.db.models import Max, IntegerField, Count
from django.db.models.functions import Substr, Cast

from .models import Token, Counter

# -------------------------
# Config (Option 2 numbering: A001 per day across ALL counters)
# -------------------------
TOKEN_PREFIX = "A"
TOKEN_PAD = 3

STATUS_ACTIVE = "active"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"


def _json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return {}


def _dt(v):
    if not v:
        return None
    try:
        return timezone.localtime(v).isoformat()
    except Exception:
        try:
            return v.isoformat()
        except Exception:
            return str(v)


def _model_has_field(model_cls, field_name: str) -> bool:
    try:
        return any(f.name == field_name for f in model_cls._meta.get_fields())
    except Exception:
        return False


def _expire_if_needed(token: Token) -> bool:
    """
    If token has is_expired() and it's expired -> mark expired and return True.
    Else return False.
    """
    try:
        if hasattr(token, "is_expired") and callable(token.is_expired) and token.is_expired():
            token.status = STATUS_EXPIRED
            fields = ["status"]
            if _model_has_field(Token, "expired_at"):
                token.expired_at = timezone.now()
                fields.append("expired_at")
            token.save(update_fields=fields)
            return True
    except Exception:
        # If is_expired() is buggy, we do NOT crash production queue.
        return False
    return False


def _issue_token_for_today(counter=None) -> Token:
    """
    Creates an ACTIVE token for today.
    counter=None means "unassigned/public".
    Safe with retries in case unique constraints exist on number/sequence.
    """
    service_date = timezone.localdate()
    prefix_len = len(TOKEN_PREFIX)

    for _ in range(15):
        try:
            with transaction.atomic():
                qs = Token.objects.select_for_update().filter(service_date=service_date)

                # Prefer explicit integer sequence if available
                last_seq = qs.aggregate(m=Max("sequence"))["m"] or 0

                # Fallback parse from number (A001 -> 1)
                last_num = (
                    qs.filter(number__startswith=TOKEN_PREFIX)
                    .annotate(num=Cast(Substr("number", prefix_len + 1), IntegerField()))
                    .aggregate(m=Max("num"))["m"]
                ) or 0

                next_seq = max(int(last_seq), int(last_num)) + 1
                number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"

                create_kwargs = dict(
                    counter=counter,
                    service_date=service_date,
                    sequence=next_seq,
                    number=number,
                    status=STATUS_ACTIVE,
                )
                token = Token.objects.create(**create_kwargs)
                return token
        except IntegrityError:
            # collision on unique fields -> retry
            continue

    raise IntegrityError("Could not issue token after retries")


# ==========================================================
# API: Issue token (staff / reception)
# POST JSON: {"counter":"A1"}  (required)
# ==========================================================
@csrf_exempt
@require_POST
def issue_token(request):
    body = _json_body(request)
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
        "message": "Token issued",
        "counter": counter.code,
        "number": token.number,
        "token_id": token.id,
        "status": token.status,
        "service_date": str(token.service_date),
        "created_at": _dt(getattr(token, "created_at", None)),
    })


# ==========================================================
# API: Next token (staff)
# POST JSON: {"counter":"A1"}  (required)
# Behavior:
# 1) Serve oldest ACTIVE token assigned to that counter
# 2) If none, pull oldest ACTIVE token where counter IS NULL (public reserve),
#    assign it to this counter, then serve it
# ==========================================================
@csrf_exempt
@require_POST
def next_token(request):
    body = _json_body(request)
    counter_code = str(body.get("counter", "")).strip()
    if not counter_code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=counter_code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found or inactive"}, status=404)

    with transaction.atomic():
        # 1) Counter-specific queue (oldest first)
        token = (
            Token.objects.select_for_update()
            .filter(counter=counter, status=STATUS_ACTIVE)
            .order_by("created_at", "id")
            .first()
        )

        while token and _expire_if_needed(token):
            token = (
                Token.objects.select_for_update()
                .filter(counter=counter, status=STATUS_ACTIVE)
                .order_by("created_at", "id")
                .first()
            )

        # 2) If none, pull from public/unassigned queue
        if not token:
            token = (
                Token.objects.select_for_update()
                .filter(counter__isnull=True, status=STATUS_ACTIVE)
                .order_by("created_at", "id")
                .first()
            )

            while token and _expire_if_needed(token):
                token = (
                    Token.objects.select_for_update()
                    .filter(counter__isnull=True, status=STATUS_ACTIVE)
                    .order_by("created_at", "id")
                    .first()
                )

            if token:
                token.counter = counter
                token.save(update_fields=["counter"])

        if not token:
            return JsonResponse({"ok": False, "error": "No active tokens"}, status=404)

        token.status = STATUS_USED
        if _model_has_field(Token, "used_at"):
            token.used_at = timezone.now()

        fields = ["status"]
        if _model_has_field(Token, "used_at"):
            fields.append("used_at")
        token.save(update_fields=fields)

    return JsonResponse({
        "ok": True,
        "message": "Next token called",
        "counter": counter.code,
        "number": token.number,
        "token_id": token.id,
        "status": token.status,
        "used_at": _dt(getattr(token, "used_at", None)),
    })


# ==========================================================
# API: Consume token directly
# POST JSON: {"number":"A001"}  OR {"token_id": 123}
# Marks USED (for reception/scan style flows)
# ==========================================================
@csrf_exempt
@require_POST
def consume_token(request):
    body = _json_body(request)
    number = str(body.get("number", "")).strip()
    token_id = body.get("token_id", None)

    if not number and not token_id:
        return JsonResponse({"ok": False, "error": "number or token_id is required"}, status=400)

    with transaction.atomic():
        qs = Token.objects.select_for_update()

        if token_id:
            try:
                token = qs.get(id=int(token_id))
            except (Token.DoesNotExist, ValueError, TypeError):
                return JsonResponse({"ok": False, "error": "Token not found"}, status=404)
        else:
            try:
                token = qs.get(number=number)
            except Token.DoesNotExist:
                return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

        if token.status == STATUS_USED:
            return JsonResponse({
                "ok": True,
                "message": "Already used",
                "number": token.number,
                "status": token.status,
                "used_at": _dt(getattr(token, "used_at", None)),
            })

        if token.status == STATUS_EXPIRED:
            return JsonResponse({"ok": False, "error": "Token is expired"}, status=409)

        # expire if needed
        if _expire_if_needed(token):
            return JsonResponse({"ok": False, "error": "Token is expired"}, status=409)

        token.status = STATUS_USED
        if _model_has_field(Token, "used_at"):
            token.used_at = timezone.now()

        fields = ["status"]
        if _model_has_field(Token, "used_at"):
            fields.append("used_at")
        token.save(update_fields=fields)

    return JsonResponse({
        "ok": True,
        "message": "Consumed",
        "number": token.number,
        "token_id": token.id,
        "status": token.status,
        "used_at": _dt(getattr(token, "used_at", None)),
    })


# ==========================================================
# API: Token status
# GET /api/token/status/A001/
# ==========================================================
@require_GET
def token_status(request, number):
    try:
        token = Token.objects.get(number=number)
    except Token.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

    return JsonResponse({
        "ok": True,
        "number": token.number,
        "status": token.status,
        "service_date": str(token.service_date),
        "counter": token.counter.code if token.counter else None,
        "created_at": _dt(getattr(token, "created_at", None)),
        "used_at": _dt(getattr(token, "used_at", None)),
    })


# ==========================================================
# API: Queue status
# GET /api/queue/status/?counter=A1  -> single counter stats
# GET /api/queue/status/            -> all counters list + public waiting
# ==========================================================
@require_GET
def queue_status(request):
    service_date = timezone.localdate()
    counter_code = str(request.GET.get("counter", "")).strip()

    def _counter_stats(c: Counter):
        active_qs = Token.objects.filter(
            service_date=service_date,
            status=STATUS_ACTIVE,
            counter=c
        ).order_by("created_at", "id")

        waiting_count = active_qs.count()
        next_tok = active_qs.first()

        last_used = (
            Token.objects.filter(
                service_date=service_date,
                status=STATUS_USED,
                counter=c
            )
            .order_by("-used_at", "-id")
            .first()
        )

        return {
            "code": c.code,
            "name": c.name,
            "now_serving": last_used.number if last_used else None,
            "waiting_count": waiting_count,
            "next_token": next_tok.number if next_tok else None,
        }

    # Single counter mode
    if counter_code:
        try:
            c = Counter.objects.get(code=counter_code, is_active=True)
        except Counter.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Counter not found or inactive"}, status=404)

        stats = _counter_stats(c)

        # Include public/unassigned waiting too (useful debug)
        public_waiting = Token.objects.filter(
            service_date=service_date,
            status=STATUS_ACTIVE,
            counter__isnull=True
        ).count()

        stats["public_waiting"] = public_waiting
        stats["service_date"] = str(service_date)

        return JsonResponse({"ok": True, **stats})

    # All counters mode
    counters = Counter.objects.filter(is_active=True).order_by("code")
    rows = [_counter_stats(c) for c in counters]

    public_waiting = Token.objects.filter(
        service_date=service_date,
        status=STATUS_ACTIVE,
        counter__isnull=True
    ).count()

    return JsonResponse({
        "ok": True,
        "service_date": str(service_date),
        "counters": rows,
        "public_waiting": public_waiting,
    })


# ==========================================================
# Custom Admin Dashboard (NOT Django admin)
# /admin-dashboard/
# ==========================================================
@require_GET
def admin_dashboard(request):
    service_date = timezone.localdate()

    totals = Token.objects.filter(service_date=service_date).aggregate(
        total=Count("id"),
        active=Count("id", filter=None),
    )

    total = Token.objects.filter(service_date=service_date).count()
    active = Token.objects.filter(service_date=service_date, status=STATUS_ACTIVE).count()
    used = Token.objects.filter(service_date=service_date, status=STATUS_USED).count()
    expired = Token.objects.filter(service_date=service_date, status=STATUS_EXPIRED).count()
    public_waiting = Token.objects.filter(service_date=service_date, status=STATUS_ACTIVE, counter__isnull=True).count()

    counters = Counter.objects.filter(is_active=True).order_by("code")
    per_counter = []
    for c in counters:
        per_counter.append({
            "code": c.code,
            "name": c.name,
            "active": Token.objects.filter(service_date=service_date, status=STATUS_ACTIVE, counter=c).count(),
            "used": Token.objects.filter(service_date=service_date, status=STATUS_USED, counter=c).count(),
        })

    return render(request, "core/admin_dashboard.html", {
        "service_date": service_date,
        "total": total,
        "active": active,
        "used": used,
        "expired": expired,
        "public_waiting": public_waiting,
        "per_counter": per_counter,
    })
