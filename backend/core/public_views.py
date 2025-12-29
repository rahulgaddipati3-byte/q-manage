import json
import re

from django.db import transaction, IntegrityError
from django.db.models import Max, IntegerField
from django.db.models.functions import Substr, Cast
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Token, Counter

TOKEN_PREFIX = "A"
TOKEN_PAD = 3

STATUS_ACTIVE = "active"
STATUS_USED = "used"

PHONE_RE = re.compile(r"^\d{10}$|^91\d{10}$")


def _json_error(msg, status=400, detail=None):
    data = {"ok": False, "error": msg}
    if detail:
        data["detail"] = detail
    return JsonResponse(data, status=status)


def _normalize_phone(phone):
    if not phone:
        return ""
    p = str(phone).strip()
    if p.startswith("+"):
        p = p[1:]
    return re.sub(r"\D", "", p)


def _default_counter():
    return Counter.objects.filter(is_active=True).order_by("code").first()


def _issue_token_for_today(*, counter, name, phone, address):
    if not counter:
        raise ValueError("No active counter")

    service_date = timezone.localdate()
    prefix_len = len(TOKEN_PREFIX)

    for _ in range(5):
        try:
            with transaction.atomic():
                qs = Token.objects.select_for_update().filter(service_date=service_date)

                last_seq = qs.aggregate(m=Max("sequence"))["m"] or 0
                last_num = (
                    qs.filter(number__startswith=TOKEN_PREFIX)
                    .annotate(num=Cast(Substr("number", prefix_len + 1), IntegerField()))
                    .aggregate(m=Max("num"))["m"]
                ) or 0

                next_seq = max(last_seq, last_num) + 1
                number = f"{TOKEN_PREFIX}{next_seq:0{TOKEN_PAD}d}"

                return Token.objects.create(
                    counter=counter,
                    service_date=service_date,
                    sequence=next_seq,
                    number=number,
                    status=STATUS_ACTIVE,
                    name=name,          # ✅ FIXED
                    phone=phone,        # ✅ FIXED
                    address=address,    # ✅ FIXED
                )
        except IntegrityError:
            continue

    raise IntegrityError("Token collision")


# ------------------------
# Public pages
# ------------------------

@require_GET
def public_clinic_page(request, slug):
    return render(request, "public/clinic.html", {"clinic_slug": slug})


@require_GET
def public_clinic_snapshot(request, slug):
    service_date = timezone.localdate()

    last_used = (
        Token.objects.filter(service_date=service_date, status=STATUS_USED)
        .order_by("-used_at", "-id")
        .first()
    )

    waiting = Token.objects.filter(
        service_date=service_date,
        status=STATUS_ACTIVE
    ).count()

    return JsonResponse({
        "ok": True,
        "now_serving": last_used.number if last_used else None,
        "people_waiting": waiting,
        "estimated_wait_min": waiting * 5,
    })


@csrf_exempt
@require_POST
def public_reserve_token(request, slug):
    try:
        payload = json.loads(request.body.decode())
    except Exception:
        return _json_error("Invalid JSON")

    name = payload.get("name", "").strip()
    phone = _normalize_phone(payload.get("phone") or payload.get("mobile"))
    address = payload.get("address", "").strip()

    if not name:
        return _json_error("Name is required")

    if not phone or not PHONE_RE.match(phone):
        return _json_error("Invalid mobile number")

    counter = _default_counter()
    if not counter:
        return _json_error(
            "System not configured",
            status=500,
            detail="No active counters in database"
        )

    try:
        token = _issue_token_for_today(
            counter=counter,
            name=name,
            phone=phone,
            address=address
        )
    except Exception as e:
        return _json_error("Internal error", status=500, detail=str(e))

    return JsonResponse({
        "ok": True,
        "token_id": token.id,
        "token_number": token.number,
        "track_url": f"/public/token/{token.id}/",
    })


@require_GET
def public_token_page(request, token_id):
    token = get_object_or_404(Token, id=token_id)
    return render(request, "public/token.html", {"token": token})
