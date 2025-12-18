# backend/core/views_ui.py
import json
from django.shortcuts import render
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.db import transaction
from django.contrib.auth.decorators import login_required

from .models import Counter, Token


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
            .order_by("-used_at")
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


@csrf_exempt
@login_required
def ui_issue_token(request):
    """
    Uses API endpoint internally by creating ACTIVE token for selected counter.
    Note: number/sequence/service_date are handled in core/views.py issue_token (API).
    But if you are issuing directly here, you MUST create properly.
    Best: call /api/token/issue/ from counter.html (recommended) and not create here.
    """
    return JsonResponse({"ok": False, "error": "Use /api/token/issue/ via counter.html"}, status=400)


@csrf_exempt
@login_required
def ui_call_next(request):
    """
    Calls the next token via API logic:
    Picks oldest ACTIVE token for this counter, marks USED.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = json.loads(request.body.decode("utf-8") or "{}")
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
            .filter(counter=counter, status="active")
            .order_by("created_at")
            .first()
        )

        while token and token.is_expired():
            token.status = "expired"
            token.save(update_fields=["status"])
            token = (
                Token.objects.select_for_update()
                .filter(counter=counter, status="active")
                .order_by("created_at")
                .first()
            )

        if not token:
            return JsonResponse({"ok": False, "error": "No active tokens"}, status=404)

        token.status = "used"
        token.used_at = timezone.now()
        token.save(update_fields=["status", "used_at"])

    return JsonResponse({
        "ok": True,
        "message": "Next token called",
        "counter": counter.code,
        "number": token.number,
        "status": token.status,
        "used_at": token.used_at.isoformat() if token.used_at else None,
    })
