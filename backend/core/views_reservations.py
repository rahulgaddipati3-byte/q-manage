import json
from datetime import datetime

from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Max

from .models import ReservationRequest, Token
from .whatsapp import send_whatsapp_text  # ✅ WhatsApp helper


# --------------------------------------------------
# Helpers
# --------------------------------------------------
def _next_token_number_and_sequence(service_date):
    last_seq = (
        Token.objects
        .filter(service_date=service_date)
        .aggregate(m=Max("sequence"))["m"] or 0
    )
    next_seq = int(last_seq) + 1
    number = f"A{str(next_seq).zfill(3)}"
    return number, next_seq


def _parse_time_today(time_str: str):
    """
    Accepts 'HH:MM' (24h) or 'HH:MM AM/PM'
    Returns timezone-aware datetime for today
    """
    if not time_str:
        return None

    time_str = time_str.strip()
    formats = ["%H:%M", "%I:%M %p", "%I:%M%p"]
    for fmt in formats:
        try:
            t = datetime.strptime(time_str, fmt).time()
            dt = datetime.combine(timezone.localdate(), t)
            return timezone.make_aware(dt)
        except ValueError:
            continue
    return None


# --------------------------------------------------
# Staff: View pending requests
# --------------------------------------------------
@login_required
@require_GET
def pending_requests(request):
    service_date = timezone.localdate()
    qs = (
        ReservationRequest.objects
        .filter(service_date=service_date, status="pending")
        .order_by("created_at")
    )

    data = [{
        "id": r.id,
        "name": r.name,
        "phone": r.phone,
        "created_at": r.created_at.isoformat(),
    } for r in qs]

    return JsonResponse({"ok": True, "pending": data})


# --------------------------------------------------
# Staff: Approve request + set exact arrival time
# --------------------------------------------------
@login_required
@require_POST
def approve_request(request, request_id):
    """
    POST JSON:
    {
      "arrival_time": "10:45"   // exact time staff wants patient to come
    }
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return HttpResponseBadRequest("Invalid JSON")

    arrival_time_str = str(payload.get("arrival_time") or "").strip()
    arrival_dt = _parse_time_today(arrival_time_str)

    if not arrival_dt:
        return JsonResponse(
            {"ok": False, "error": "arrival_time required (e.g. 10:45 or 10:45 AM)"},
            status=400,
        )

    with transaction.atomic():
        req = ReservationRequest.objects.select_for_update().get(id=request_id)
        if req.status != "pending":
            return JsonResponse({"ok": False, "error": "Not pending"}, status=400)

        number, seq = _next_token_number_and_sequence(req.service_date)

        token = Token.objects.create(
            number=number,
            sequence=seq,
            service_date=req.service_date,
            status="active",
        )

        req.token = token
        req.status = "approved"
        req.decided_at = timezone.now()
        req.scheduled_time = arrival_dt
        req.save(update_fields=["token", "status", "decided_at", "scheduled_time"])

    # --------------------------------------------------
    # Send WhatsApp message (FREE tier)
    # --------------------------------------------------
    whatsapp_sent = False
    whatsapp_error = ""

    try:
        t = timezone.localtime(req.scheduled_time).strftime("%I:%M %p").lstrip("0")

        message = (
            f"Hi {req.name} 👋\n\n"
            f"Your clinic token *{token.number}* is confirmed.\n\n"
            f"🕒 Please come exactly at: *{t}*\n"
            f"📍 Kindly be on time.\n\n"
            f"- Q-Manage"
        )

        ok, info = send_whatsapp_text(req.phone, message)
        whatsapp_sent = ok
        whatsapp_error = "" if ok else info

    except Exception as e:
        whatsapp_sent = False
        whatsapp_error = str(e)[:500]

    ReservationRequest.objects.filter(id=req.id).update(
        sms_sent=whatsapp_sent,     # reuse column if you want
        sms_error=whatsapp_error,
    )

    return JsonResponse({
        "ok": True,
        "token_number": token.number,
        "scheduled_time": req.scheduled_time.isoformat(),
        "whatsapp_sent": whatsapp_sent,
    })


# --------------------------------------------------
# Staff: Reject request
# --------------------------------------------------
@login_required
@require_POST
def reject_request(request, request_id):
    req = get_object_or_404(ReservationRequest, id=request_id)
    if req.status != "pending":
        return JsonResponse({"ok": False, "error": "Not pending"}, status=400)

    req.status = "rejected"
    req.decided_at = timezone.now()
    req.save(update_fields=["status", "decided_at"])
    return JsonResponse({"ok": True})
