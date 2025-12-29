# backend/core/views_ui.py
import json
from django.shortcuts import render
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.db import transaction
from django.contrib.auth.decorators import login_required

from .models import Counter, Token, ReservationRequest


def _is_expired(token) -> bool:
    # Token has expires_at in your model
    return bool(getattr(token, "expires_at", None) and timezone.now() >= token.expires_at)


@login_required
def counter_screen(request):
    counters = Counter.objects.filter(is_active=True).order_by("code")
    return render(request, "core/counter.html", {"counters": counters})


def display_screen(request):
    counters = Counter.objects.filter(is_active=True).order_by("code")
    return render(request, "core/display.html", {"counters": counters})


@require_GET
def display_data(request):
    """
    UI display data:
    now_serving = last USED token for that counter
    waiting_count = ACTIVE tokens assigned to that counter
    next_token = oldest ACTIVE token assigned to that counter
    """
    rows = []
    counters = Counter.objects.filter(is_active=True).order_by("code")

    for c in counters:
        active_qs = Token.objects.filter(counter=c, status="active").order_by("created_at")
        waiting_count = active_qs.count()
        next_token = active_qs.first().number if waiting_count else None

        last_used = (
            Token.objects.filter(counter=c, status="used", used_at__isnull=False)
            .order_by("-used_at", "-id")
            .first()
        )
        now_serving = last_used.number if last_used else None

        rows.append({
            "code": c.code,
            "name": c.name,
            "now_serving": now_serving,
            "waiting_count": waiting_count,
            "next_token": next_token,
        })

    return JsonResponse({"ok": True, "counters": rows})


@require_GET
@login_required
def reservations_data(request):
    """
    Latest reservations (today) for staff view.
    ReservationRequest is created when customer reserves (and token is issued immediately).
    """
    today = timezone.localdate()
    qs = (
        ReservationRequest.objects
        .filter(service_date=today)
        .select_related("token")
        .order_by("-id")[:30]
    )

    out = []
    for r in qs:
        out.append({
            "id": r.id,
            "name": r.name,
            "phone": r.phone,
            "status": r.status,
            "token": r.token.number if r.token else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    return JsonResponse({"ok": True, "results": out})


@csrf_exempt
@login_required
def ui_issue_token(request):
    """
    Keep disabled here. Use /api/token/issue/ from the UI JS.
    """
    return JsonResponse({"ok": False, "error": "Use /api/token/issue/ via counter.html"}, status=400)


@csrf_exempt
@login_required
def ui_call_next(request):
    """
    STAFF Call Next (THE REAL ONE):
    - Prefer oldest ACTIVE token with counter=NULL (customer reserved / reception)
    - Else oldest ACTIVE token already assigned to this counter
    - Assign to this counter if unassigned
    - Mark USED + used_at
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    counter_code = str(body.get("counter", "")).strip()
    if not counter_code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=counter_code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found or inactive"}, status=404)

    with transaction.atomic():
        # 1) unassigned tokens first
        token = (
            Token.objects.select_for_update()
            .filter(status="active", counter__isnull=True)
            .order_by("created_at", "id")
            .first()
        )

        # 2) else tokens already assigned to this counter
        if not token:
            token = (
                Token.objects.select_for_update()
                .filter(status="active", counter=counter)
                .order_by("created_at", "id")
                .first()
            )

        # expire old ones (if any)
        while token and _is_expired(token):
            token.status = "expired"
            token.save(update_fields=["status"])
            token = (
                Token.objects.select_for_update()
                .filter(status="active", counter__isnull=True)
                .order_by("created_at", "id")
                .first()
            )

        if not token:
            return JsonResponse({"ok": False, "error": "No active tokens"}, status=404)

        # assign to this counter if unassigned
        if token.counter_id is None:
            token.counter = counter

        # mark used (this is the IMPORTANT missing piece)
        token.status = "used"
        token.used_at = timezone.now()
        token.save(update_fields=["counter", "status", "used_at"])

    return JsonResponse({
        "ok": True,
        "message": "Next token called",
        "counter": counter.code,
        "number": token.number,
        "status": token.status,
        "used_at": token.used_at.isoformat() if token.used_at else None,
    })
