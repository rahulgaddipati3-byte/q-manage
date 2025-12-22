# backend/core/views.py
import json
import os
import re
from datetime import datetime

import requests
from django.http import JsonResponse
from django.utils import timezone
from django.db import transaction, IntegrityError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required

from django.db.models import (
    Max, IntegerField, Count, Avg, F,
    ExpressionWrapper, DurationField, Q
)
from django.db.models.functions import Substr, Cast

from .models import Token, Counter

TOKEN_PREFIX = "A"
TOKEN_PAD = 3

# Statuses expected in Token.status (update your Token.STATUS_CHOICES accordingly)
STATUS_PENDING = "pending"   # customer requested, staff not yet approved
STATUS_ACTIVE  = "active"    # approved & waiting/serving
STATUS_USED    = "used"
STATUS_EXPIRED = "expired"


# --------------------------------------------------
# Helpers
# --------------------------------------------------
def _json_load(request):
    try:
        raw = request.body.decode("utf-8") if request.body else ""
        return json.loads(raw or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _dt(v):
    if not v:
        return None
    try:
        return v.isoformat()
    except Exception:
        return str(v)


def _normalize_mobile(mobile: str) -> str:
    """
    Returns E.164-like number without '+' (Meta WhatsApp accepts phone number in international format).
    Examples:
      "9876543210" -> "919876543210"
      "+91 98765 43210" -> "919876543210"
    """
    if not mobile:
        return ""
    m = re.sub(r"[^\d+]", "", mobile.strip())
    if m.startswith("+"):
        m = m[1:]
    # If user enters 10-digit Indian number
    if len(m) == 10 and m.isdigit():
        return "91" + m
    return m


def _parse_arrival_time(s: str):
    """
    Accepts "10:30" or "10:30 AM" etc and returns a timezone-aware datetime for today.
    """
    if not s:
        return None
    s = s.strip()
    today = timezone.localdate()
    candidates = ["%H:%M", "%I:%M %p", "%I:%M%p", "%H:%M:%S"]
    for fmt in candidates:
        try:
            t = datetime.strptime(s, fmt).time()
            dt = datetime.combine(today, t)
            return timezone.make_aware(dt, timezone.get_current_timezone())
        except Exception:
            continue
    return None


def _send_whatsapp_text(to_mobile_e164_no_plus: str, text: str):
    """
    Sends a WhatsApp Cloud API text message.
    Requires env vars:
      WHATSAPP_ACCESS_TOKEN
      WHATSAPP_PHONE_NUMBER_ID
    Note: In Meta dev mode, you can message only numbers added as test recipients.
    """
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()

    if not access_token or not phone_number_id:
        # Don’t crash your flow if env isn’t set yet
        return False, "WhatsApp env vars missing (WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID)"

    url = f"https://graph.facebook.com/v22.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_mobile_e164_no_plus,
        "type": "text",
        "text": {"body": text},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code >= 200 and r.status_code < 300:
            return True, "sent"
        return False, f"{r.status_code}: {r.text}"
    except Exception as e:
        return False, str(e)


def _get_token_by_number_for_today(number: str):
    """
    Your DB constraint is per-day unique (service_date, number).
    So always lookup for today first; if not found, fallback to latest by created_at.
    """
    today = timezone.localdate()
    tok = (
        Token.objects.filter(service_date=today, number=number)
        .order_by("-created_at")
        .first()
    )
    if tok:
        return tok
    return Token.objects.filter(number=number).order_by("-created_at").first()


# --------------------------------------------------
# API: Token status (GET)
# --------------------------------------------------
@require_GET
def token_status(request, number):
    token = _get_token_by_number_for_today(str(number).strip())
    if not token:
        return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

    # auto-expire active tokens
    if token.status == STATUS_ACTIVE and token.is_expired():
        token.status = STATUS_EXPIRED
        token.save(update_fields=["status"])

    return JsonResponse({
        "ok": True,
        "id": token.id,
        "number": token.number,
        "status": token.status,
        "counter": token.counter.code if token.counter else None,
        "service_date": _dt(token.service_date),
        "sequence": token.sequence,
        "created_at": _dt(token.created_at),
        "expires_at": _dt(token.expires_at),
        "used_at": _dt(token.used_at),
        # If you add these fields in Token model later, this will show them (safe):
        "customer_name": getattr(token, "customer_name", None),
        "customer_mobile": getattr(token, "customer_mobile", None),
        "arrival_time": _dt(getattr(token, "arrival_time", None)),
    })


# --------------------------------------------------
# API: Customer requests a token (POST)
# Customer must provide name + mobile
# Creates a token in PENDING state (staff must approve)
# --------------------------------------------------
@csrf_exempt
def request_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _json_load(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    name = str(body.get("name", "")).strip()
    mobile = _normalize_mobile(str(body.get("mobile", "")).strip())
    if not name:
        return JsonResponse({"ok": False, "error": "name is required"}, status=400)
    if not mobile or len(mobile) < 10:
        return JsonResponse({"ok": False, "error": "valid mobile is required"}, status=400)

    service_date = timezone.localdate()
    prefix_len = len(TOKEN_PREFIX)

    for _ in range(10):
        try:
            with transaction.atomic():
                qs = Token.objects.filter(service_date=service_date)

                last_seq = qs.aggregate(m=Max("sequence"))["m"] or 0
                last_num = (
                    qs.filter(number__startswith=TOKEN_PREFIX)
                    .annotate(num=Cast(Substr("number", prefix_len + 1), IntegerField()))
                    .aggregate(m=Max("num"))["m"]
                ) or 0

                next_seq = max(last_seq, last_num) + 1
                number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"

                token = Token.objects.create(
                    counter=None,  # not assigned until staff approves
                    service_date=service_date,
                    sequence=next_seq,
                    number=number,
                    status=STATUS_PENDING,
                    # These fields must exist in your Token model:
                    customer_name=name,
                    customer_mobile=mobile,
                )

            return JsonResponse({
                "ok": True,
                "message": "Token request submitted. Waiting for staff approval.",
                "id": token.id,
                "number": token.number,
                "status": token.status,
                "created_at": _dt(token.created_at),
            })

        except IntegrityError:
            continue

    return JsonResponse({"ok": False, "error": "Could not create token request"}, status=409)


# --------------------------------------------------
# API: Staff approves a token + sets arrival time (POST)
# Sends WhatsApp message to customer after approval
# Body: {"token_id": 12, "counter": "A1", "arrival_time": "10:45"}
# --------------------------------------------------
@csrf_exempt
def approve_token(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    body = _json_load(request)
    if body is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    token_id = body.get("token_id")
    counter_code = str(body.get("counter", "")).strip()
    arrival_time_str = str(body.get("arrival_time", "")).strip()

    if not token_id:
        return JsonResponse({"ok": False, "error": "token_id is required"}, status=400)
    if not counter_code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)
    if not arrival_time_str:
        return JsonResponse({"ok": False, "error": "arrival_time is required (e.g. 10:45)"}, status=400)

    arrival_dt = _parse_arrival_time(arrival_time_str)
    if not arrival_dt:
        return JsonResponse({"ok": False, "error": "arrival_time format invalid. Use 10:45 or 10:45 AM"}, status=400)

    try:
        counter = Counter.objects.get(code=counter_code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found"}, status=404)

    with transaction.atomic():
        try:
            token = Token.objects.select_for_update().get(id=token_id)
        except Token.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

        if token.status == STATUS_USED:
            return JsonResponse({"ok": False, "error": "Token already used"}, status=400)
        if token.status == STATUS_EXPIRED:
            return JsonResponse({"ok": False, "error": "Token expired"}, status=400)

        # Move from pending -> active (approved)
        token.counter = counter
        token.status = STATUS_ACTIVE
        token.arrival_time = arrival_dt  # field must exist in Token model
        token.save(update_fields=["counter", "status", "arrival_time"])

    # Send WhatsApp message
    cust_name = getattr(token, "customer_name", "")
    cust_mobile = getattr(token, "customer_mobile", "")
    msg = (
        f"Hi {cust_name}, your appointment is scheduled.\n"
        f"Token: {token.number}\n"
        f"Please come at: {arrival_dt.strftime('%I:%M %p').lstrip('0')}\n"
        f"Be on time. Thank you."
    )
    ok, info = _send_whatsapp_text(cust_mobile, msg)

    return JsonResponse({
        "ok": True,
        "message": "Token approved",
        "whatsapp_sent": ok,
        "whatsapp_info": info,
        "id": token.id,
        "number": token.number,
        "counter": token.counter.code if token.counter else None,
        "status": token.status,
        "arrival_time": _dt(getattr(token, "arrival_time", None)),
    })


# --------------------------------------------------
# API: Consume token (POST)  (marks USED)
# Body: {"number":"A001"}
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
        token = _get_token_by_number_for_today(number)
        if not token:
            return JsonResponse({"ok": False, "error": "Token not found"}, status=404)

        token = Token.objects.select_for_update().get(id=token.id)

        if token.status == STATUS_ACTIVE and token.is_expired():
            token.status = STATUS_EXPIRED
            token.save(update_fields=["status"])

        if token.status == STATUS_PENDING:
            return JsonResponse({"ok": False, "error": "Token not approved yet"}, status=400)
        if token.status == STATUS_EXPIRED:
            return JsonResponse({"ok": False, "error": "Token expired"}, status=400)
        if token.status == STATUS_USED:
            return JsonResponse({"ok": False, "error": "Token already used"}, status=400)

        token.status = STATUS_USED
        token.used_at = timezone.now()
        token.save(update_fields=["status", "used_at"])

    return JsonResponse({
        "ok": True,
        "message": "Token consumed",
        "number": token.number,
        "used_at": _dt(token.used_at),
    })


# --------------------------------------------------
# API: Issue token (POST)  (admin/staff shortcut; creates ACTIVE directly)
# Body: {"counter":"A1"} optional
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
    prefix_len = len(TOKEN_PREFIX)

    for _ in range(10):
        try:
            with transaction.atomic():
                qs = Token.objects.filter(service_date=service_date)

                last_seq = qs.aggregate(m=Max("sequence"))["m"] or 0
                last_num = (
                    qs.filter(number__startswith=TOKEN_PREFIX)
                    .annotate(num=Cast(Substr("number", prefix_len + 1), IntegerField()))
                    .aggregate(m=Max("num"))["m"]
                ) or 0

                next_seq = max(last_seq, last_num) + 1
                number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"

                token = Token.objects.create(
                    counter=counter,
                    service_date=service_date,
                    sequence=next_seq,
                    number=number,
                    status=STATUS_ACTIVE,
                )

            return JsonResponse({
                "ok": True,
                "id": token.id,
                "number": token.number,
                "counter": token.counter.code if token.counter else None,
                "sequence": token.sequence,
                "status": token.status,
                "created_at": _dt(token.created_at),
            })

        except IntegrityError:
            continue

    return JsonResponse({"ok": False, "error": "Could not issue token"}, status=409)


# --------------------------------------------------
# API: Next token (POST)
# IMPORTANT: This should NOT mark USED immediately.
# It should "serve" an ACTIVE token and return it.
# Then you call consume_token when service is completed.
# Body: {"counter":"A1"}
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
        return JsonResponse({"ok": False, "error": "Counter not found"}, status=404)

    with transaction.atomic():
        # Only ACTIVE tokens can be served
        token = (
            Token.objects.select_for_update()
            .filter(status=STATUS_ACTIVE, counter__isnull=True)
            .order_by("created_at")
            .first()
        ) or (
            Token.objects.select_for_update()
            .filter(status=STATUS_ACTIVE, counter=counter)
            .order_by("created_at")
            .first()
        )

        while token and token.is_expired():
            token.status = STATUS_EXPIRED
            token.save(update_fields=["status"])
            token = (
                Token.objects.select_for_update()
                .filter(status=STATUS_ACTIVE, counter__isnull=True)
                .order_by("created_at")
                .first()
            )

        if not token:
            return JsonResponse({"ok": False, "error": "No active (approved) tokens"}, status=404)

        # Assign the token to this counter if not already assigned
        if token.counter_id is None:
            token.counter = counter
            token.save(update_fields=["counter"])

    return JsonResponse({
        "ok": True,
        "counter": counter.code,
        "id": token.id,
        "number": token.number,
        "status": token.status,
        "customer_name": getattr(token, "customer_name", None),
        "customer_mobile": getattr(token, "customer_mobile", None),
        "arrival_time": _dt(getattr(token, "arrival_time", None)),
    })


# --------------------------------------------------
# API: Queue status (GET)
# shows ACTIVE + PENDING counts separately
# --------------------------------------------------
@require_GET
def queue_status(request):
    code = request.GET.get("counter")
    if not code:
        return JsonResponse({"ok": False, "error": "counter is required"}, status=400)

    try:
        counter = Counter.objects.get(code=code, is_active=True)
    except Counter.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Counter not found"}, status=404)

    pending = Token.objects.filter(status=STATUS_PENDING)
    unassigned_active = Token.objects.filter(status=STATUS_ACTIVE, counter__isnull=True)
    assigned_active = Token.objects.filter(status=STATUS_ACTIVE, counter=counter)

    next_tok = (
        unassigned_active.order_by("created_at").first()
        or assigned_active.order_by("created_at").first()
    )

    return JsonResponse({
        "ok": True,
        "counter": counter.code,
        "pending_requests": pending.count(),
        "waiting_active": unassigned_active.count() + assigned_active.count(),
        "next_token": next_tok.number if next_tok else None,
    })


# --------------------------------------------------
# UI: Custom Admin Dashboard
# --------------------------------------------------
@login_required
def admin_dashboard(request):
    if not request.user.is_staff:
        return redirect("/login/")

    today = timezone.localdate()
    tokens = Token.objects.filter(service_date=today)

    issued = tokens.count()
    served = tokens.filter(status=STATUS_USED).count()
    waiting_active = tokens.filter(status=STATUS_ACTIVE).count()
    pending = tokens.filter(status=STATUS_PENDING).count()

    avg_wait = (
        tokens.filter(used_at__isnull=False)
        .annotate(
            wait=ExpressionWrapper(
                F("used_at") - F("created_at"),
                output_field=DurationField(),
            )
        )
        .aggregate(avg=Avg("wait"))["avg"]
    )

    per_counter = (
        tokens.values("counter__code")
        .annotate(
            issued=Count("id"),
            served=Count("id", filter=Q(status=STATUS_USED)),
            waiting=Count("id", filter=Q(status=STATUS_ACTIVE)),
            pending=Count("id", filter=Q(status=STATUS_PENDING)),
        )
        .order_by("counter__code")
    )

    return render(
        request,
        "core/admin_dashboard.html",
        {
            "issued": issued,
            "served": served,
            "waiting": waiting_active,
            "pending": pending,
            "avg_wait": avg_wait,
            "per_counter": per_counter,
            "today": today,
        },
    )
