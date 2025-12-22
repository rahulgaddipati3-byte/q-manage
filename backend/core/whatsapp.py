import os
import requests

def _normalize_phone(phone: str) -> str:
    p = (phone or "").strip().replace(" ", "")
    if p.startswith("+"):
        p = p[1:]
    # If 10-digit Indian number, add 91
    if len(p) == 10 and p.isdigit():
        p = "91" + p
    return p

def send_whatsapp_text(to_phone: str, message: str):
    """
    Returns: (ok: bool, info: str)
    Requires Render env vars:
      WHATSAPP_ACCESS_TOKEN
      WHATSAPP_PHONE_NUMBER_ID
    """
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()

    if not access_token or not phone_number_id:
        return False, "Missing WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID"

    to_phone = _normalize_phone(to_phone)

    url = f"https://graph.facebook.com/v22.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message},
    }

    r = requests.post(url, json=payload, headers=headers, timeout=20)
    if 200 <= r.status_code < 300:
        return True, "sent"
    return False, f"{r.status_code}: {r.text[:300]}"
