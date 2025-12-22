# backend/core/views_reservations.py
import json
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
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


def _json_load(request):
    try:
        raw = request.body.decode("utf-8") if request.body else ""
        return json.loads(raw or "{}")
    except Exception:
        return None


def _model_has_field(model, field_name: str) -> bool:
    return any(f.name == field_name for f in model._meta.fields)


def _next_token_number(service_date):
    """
    Generates next A001, A002... per day across ALL counters.
    Uses Token.sequence if present, otherwise just max(sequence) anyway.
    """
    last_seq = Token.objects.filter(service_date=service_date).aggregate(m=Max("sequence"))["m"] or 0
    next_seq = last_seq + 1
    number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"
    return next_seq, number


# --------------------------------------------------
# UI page: Staff Requests Screen (THIS FIXES YOUR ERROR)
# --------------------------------------------------
@login_required
def staff_requests_page(request):
    if not request.user.is_staff:
        # your login url is /login/
        return render(request, "registration/login.html", status=403)

    today = timezone.localdate()
    pending = (
        ReservationRequest.objects
        .filter(service_date=today, status=STATUS_PENDING)
        .order_by("id")
    )

    return render(request, "core/staff_requests.html", {"pending": pending, "today": today})


# --------------------------------------------------
# API: list pending reservation requests (JSON)
# GET /api/staff/requests/pending/
# --------------------------------------------------
@require_GET
def pending_requests(request):
    today = timezone.localdate()
    qs = ReservationRequest.objects.filter(service_date=today, status=STATUS_PENDING).order_by("id")

    data = []
    for r in qs:
        data.append({
            "id": r.id,
            "name": r.name,
            "phone": r.phone,
            "status": r.status,
            "service_date": str(r.service_date),
        })

    return JsonResponse({"ok": True, "results": data})


# --------------------------------------------------
# API: approve reservation request
# POST /api/staff/requests/<id>/approve/
# Creates ACTIVE token and links to request
# --------------------------------------------------
@csrf_exempt
def approve_request(request, request_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _json_load(request)
    if body is None:
        body = {}

    scheduled_time = body.get("scheduled_time")  # optional (string)

    today = timezone.localdate()

    with transaction.atomic():
        req = get_object_or_404(ReservationRequest.objects.select_for_update(), id=request_id)

        if req.status != STATUS_PENDING:
            return JsonResponse({"ok": False, "error": "Request is not pending"}, status=400)

        # If already linked token exists, don't create new
        if getattr(req, "token_id", None):
            req.status = STATUS_APPROVED
            if _model_has_field(ReservationRequest, "scheduled_time") and scheduled_time:
                # if you store scheduled_time as DateTimeField, parse on frontend or keep null.
                # keeping safe: do not parse unknown format here.
                pass
            req.save(update_fields=["status"])
            return JsonResponse({"ok": True, "message": "Already approved", "token_id": req.token_id})

        # Create token
        for _ in range(10):
            try:
                seq, number = _next_token_number(today)

                token_kwargs = {
                    "service_date": today,
                    "sequence": seq,
                    "number": number,
                    "status": TOKEN_ACTIVE,
                }

                # Optional customer fields if they exist on Token model
                if _model_has_field(Token, "customer_name"):
                    token_kwargs["customer_name"] = req.name
                if _model_has_field(Token, "customer_mobile"):
                    token_kwargs["customer_mobile"] = req.phone

                token = Token.objects.create(**token_kwargs)

                # Link request -> token (field names depend on your model; most likely FK named "token")
                if _model_has_field(ReservationRequest, "token"):
                    req.token = token

                req.status = STATUS_APPROVED

                # Optional scheduled_time field (only if it exists)
                if _model_has_field(ReservationRequest, "scheduled_time") and scheduled_time:
                    # safest: ignore parsing here unless you confirm format
                    pass

                req.save()
                return JsonResponse({
                    "ok": True,
                    "message": "Approved",
                    "request_id": req.id,
                    "token_id": token.id,
                    "token_number": token.number,
                })

            except IntegrityError:
                continue

    return JsonResponse({"ok": False, "error": "Could not approve (conflict)"}, status=409)


# --------------------------------------------------
# API: reject reservation request
# POST /api/staff/requests/<id>/reject/
# --------------------------------------------------
@csrf_exempt
def reject_request(request, request_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    with transaction.atomic():
        req = get_object_or_404(ReservationRequest.objects.select_for_update(), id=request_id)
        if req.status != STATUS_PENDING:
            return JsonResponse({"ok": False, "error": "Request is not pending"}, status=400)

        req.status = STATUS_REJECTED
        req.save(update_fields=["status"])

    return JsonResponse({"ok": True, "message": "Rejected", "request_id": req.id})
