# backend/core/views_reservations.py
import json
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_GET
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.db import transaction, IntegrityError
from django.db.models import Max

from .models import ReservationRequest, Token

TOKEN_PREFIX = "A"
TOKEN_PAD = 3

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

TOKEN_ACTIVE = "active"


# ----------------------------
# Helpers
# ----------------------------
def _json_load(request):
    try:
        raw = request.body.decode("utf-8") if request.body else ""
        return json.loads(raw or "{}")
    except Exception:
        return None


def _model_has_field(model, field_name: str) -> bool:
    return any(f.name == field_name for f in model._meta.fields)


def _get_service_date_from_request(request):
    """
    Default = today (localdate)
    Allows: ?date=YYYY-MM-DD
    """
    d = request.GET.get("date")
    if d:
        parsed = parse_date(d)
        if parsed:
            return parsed
    return timezone.localdate()


def _next_token_number(service_date):
    """
    Generates next A001, A002... per day across ALL counters.
    Uses Token.sequence constraint per day.
    """
    last_seq = (
        Token.objects
        .filter(service_date=service_date)
        .aggregate(m=Max("sequence"))["m"]
        or 0
    )
    next_seq = last_seq + 1
    number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"
    return next_seq, number


def _parse_scheduled_time(value):
    """
    Accepts ISO datetime string from frontend.
    Returns aware datetime (if possible) or None.
    """
    if not value:
        return None
    dt = parse_datetime(value)
    if not dt:
        return None
    if timezone.is_naive(dt):
        # interpret naive as local time
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


# --------------------------------------------------
# UI page: Staff Requests Screen
# GET /ui/requests/
# --------------------------------------------------
@login_required
def staff_requests_page(request):
    if not request.user.is_staff:
        return render(request, "registration/login.html", status=403)

    service_date = _get_service_date_from_request(request)

    pending = (
        ReservationRequest.objects
        .filter(service_date=service_date, status=STATUS_PENDING)
        .order_by("id")
    )

    return render(
        request,
        "core/staff_requests.html",
        {"pending": pending, "today": service_date},
    )


# --------------------------------------------------
# API: list pending reservation requests (JSON)
# GET /api/staff/requests/pending/?date=YYYY-MM-DD
# --------------------------------------------------
@require_GET
@login_required
def pending_requests(request):
    if not request.user.is_staff:
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    service_date = _get_service_date_from_request(request)

    qs = (
        ReservationRequest.objects
        .filter(service_date=service_date, status=STATUS_PENDING)
        .order_by("id")
    )

    results = []
    for r in qs:
        results.append({
            "id": r.id,
            "name": r.name,
            "phone": r.phone,
            "status": r.status,
            "service_date": str(r.service_date),
            "created_at": r.created_at.isoformat() if getattr(r, "created_at", None) else None,
        })

    return JsonResponse({"ok": True, "service_date": str(service_date), "results": results})


# --------------------------------------------------
# API: approve reservation request
# POST /api/staff/requests/<id>/approve/
# Body: {"scheduled_time": "2025-12-29T10:30:00+05:30"} (optional)
#
# Creates ACTIVE token with counter=NULL (so Call Next can pick it)
# --------------------------------------------------
@csrf_exempt
@login_required
def approve_request(request, request_id):
    if not request.user.is_staff:
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _json_load(request)
    if body is None:
        body = {}

    scheduled_time_raw = body.get("scheduled_time")
    scheduled_time = _parse_scheduled_time(scheduled_time_raw)

    with transaction.atomic():
        req = get_object_or_404(
            ReservationRequest.objects.select_for_update(),
            id=request_id
        )

        if req.status != STATUS_PENDING:
            return JsonResponse({"ok": False, "error": "Request is not pending"}, status=400)

        # If token already exists, just mark approved
        if req.token_id:
            req.status = STATUS_APPROVED
            if _model_has_field(ReservationRequest, "decided_at"):
                req.decided_at = timezone.now()

            update_fields = ["status"]
            if _model_has_field(ReservationRequest, "decided_at"):
                update_fields.append("decided_at")

            if _model_has_field(ReservationRequest, "scheduled_time") and scheduled_time:
                req.scheduled_time = scheduled_time
                update_fields.append("scheduled_time")

            req.save(update_fields=update_fields)
            return JsonResponse({"ok": True, "message": "Already approved", "token_id": req.token_id})

        # IMPORTANT: token must use req.service_date (NOT always today)
        service_date = req.service_date

        for _ in range(10):
            try:
                seq, number = _next_token_number(service_date)

                token_kwargs = {
                    "service_date": service_date,
                    "sequence": seq,
                    "number": number,
                    "status": TOKEN_ACTIVE,
                    "counter": None,   # critical: unassigned so /api/token/next picks it
                }

                token = Token.objects.create(**token_kwargs)

                req.token = token
                req.status = STATUS_APPROVED

                if _model_has_field(ReservationRequest, "decided_at"):
                    req.decided_at = timezone.now()

                if _model_has_field(ReservationRequest, "scheduled_time") and scheduled_time:
                    req.scheduled_time = scheduled_time

                req.save()

                return JsonResponse({
                    "ok": True,
                    "message": "Approved",
                    "request_id": req.id,
                    "token_id": token.id,
                    "token_number": token.number,
                    "service_date": str(service_date),
                })

            except IntegrityError:
                continue

    return JsonResponse({"ok": False, "error": "Could not approve (conflict)"}, status=409)


# --------------------------------------------------
# API: reject reservation request
# POST /api/staff/requests/<id>/reject/
# --------------------------------------------------
@csrf_exempt
@login_required
def reject_request(request, request_id):
    if not request.user.is_staff:
        return JsonResponse({"ok": False, "error": "Forbidden"}, status=403)

    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    with transaction.atomic():
        req = get_object_or_404(
            ReservationRequest.objects.select_for_update(),
            id=request_id
        )

        if req.status != STATUS_PENDING:
            return JsonResponse({"ok": False, "error": "Request is not pending"}, status=400)

        req.status = STATUS_REJECTED
        if _model_has_field(ReservationRequest, "decided_at"):
            req.decided_at = timezone.now()

        update_fields = ["status"]
        if _model_has_field(ReservationRequest, "decided_at"):
            update_fields.append("decided_at")

        req.save(update_fields=update_fields)

    return JsonResponse({"ok": True, "message": "Rejected", "request_id": req.id})
