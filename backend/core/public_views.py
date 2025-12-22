import json
import re
from datetime import timedelta

from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt

from .models import Token, ReservationRequest

PHONE_RE = re.compile(r"^\d{10}$|^91\d{10}$")


@require_GET
def public_clinic_page(request, slug):
    return render(request, "public/clinic.html", {"clinic_slug": slug})


@csrf_exempt
@require_POST
def public_reserve_token(request, slug):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return HttpResponseBadRequest("Invalid JSON")

    name = (payload.get("name") or "").strip()
    phone = (payload.get("phone") or "").strip().replace(" ", "")

    if not name:
        return JsonResponse({"ok": False, "error": "Name is required"}, status=400)
    if not phone or not PHONE_RE.match(phone):
        return JsonResponse({"ok": False, "error": "Enter valid 10-digit mobile (or 91XXXXXXXXXX)"}, status=400)

    service_date = timezone.localdate()

    req = ReservationRequest.objects.create(
        service_date=service_date,
        status="pending",
        name=name,
        phone=phone,
    )

    return JsonResponse({
        "ok": True,
        "request_id": req.id,
        "track_url": f"/public/request/{req.id}/",
    })


@require_GET
def public_request_page(request, request_id):
    req = get_object_or_404(ReservationRequest, id=request_id)
    return render(request, "public/request.html", {"req": req})


@require_GET
def public_request_status(request, request_id):
    req = get_object_or_404(ReservationRequest, id=request_id)
    token_number = req.token.number if req.token else None
    token_url = f"/public/token/{req.token.id}/" if req.token else None

    scheduled = timezone.localtime(req.scheduled_time).strftime("%I:%M %p").lstrip("0") if req.scheduled_time else None

    return JsonResponse({
        "ok": True,
        "status": req.status,
        "token_number": token_number,
        "scheduled_time_display": scheduled,
        "token_track_url": token_url,
    })
