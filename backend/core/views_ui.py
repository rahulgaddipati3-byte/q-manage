from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.utils import timezone
from django.db import transaction

from .models import Counter, Token


def counter_screen(request):
    counters = Counter.objects.filter(is_active=True).order_by("code")
    return render(request, "core/counter.html", {"counters": counters})


def display_screen(request):
    counters = Counter.objects.filter(is_active=True).order_by("code")
    return render(request, "core/display.html", {"counters": counters})


@require_GET
def display_data(request):
    """
    Returns status for all active counters:
    - now_serving: last USED token number
    - waiting_count: ACTIVE tokens count
    - next_token: oldest ACTIVE token number
    """
    rows = []
    counters = Counter.objects.filter(is_active=True).order_by("code")

    for c in counters:
        # Waiting
        active_qs = Token.objects.filter(counter=c, status="active").order_by("created_at")
        waiting_count = active_qs.count()
        next_token = active_qs.first().number if waiting_count else None

        # Now serving (latest used)
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
def ui_issue_token(request):
    """
    POST /ui/issue/
    body: {"counter":"c1"}   (required)
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    import json
    body = json.loads(request.body.decode("utf-8") or "{}")
    counter_code = str(body.get("counter", "")).strip()

    if not counter_code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    counter, _ = Counter.objects.get_or_create(code=counter_code, defaults={"name": counter_code})

    token = Token.objects.create(counter=counter)

    return JsonResponse({
        "ok": True,
        "message": "Token issued",
        "counter": counter.code,
        "number": token.number,
        "status": token.status,
        "created_at": token.created_at,
        "expires_at": token.expires_at,
    })


@csrf_exempt
def ui_call_next(request):
    """
    POST /ui/call-next/
    body: {"counter":"c1"}   (required)
    Picks the oldest ACTIVE token for that counter (FIFO), marks USED.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    import json
    body = json.loads(request.body.decode("utf-8") or "{}")
    counter_code = str(body.get("counter", "")).strip()

    if not counter_code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=counter_code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found or inactive"}, status=404)

    with transaction.atomic():
        # lock + pick oldest active
        token = (
            Token.objects.select_for_update()
            .filter(counter=counter, status="active")
            .order_by("created_at")
            .first()
        )

        # expire expired ones and keep searching
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
        "used_at": token.used_at,
    })
