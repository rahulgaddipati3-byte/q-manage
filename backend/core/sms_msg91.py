import os
import requests


class MSG91Error(Exception):
    pass


def normalize_phone(phone: str) -> str:
    p = (phone or "").strip().replace(" ", "")
    if p.startswith("+"):
        p = p[1:]
    # If 10 digits, assume India and prefix 91
    if len(p) == 10 and p.isdigit():
        p = "91" + p
    return p


def send_sms_msg91(phone: str, message: str) -> str:
    """
    MSG91 Flow API (recommended for India/DLT).
    You must create a FLOW in MSG91 and set MSG91_FLOW_ID.
    In that flow, create one variable to accept message text (var1).
    """
    authkey = os.environ.get("MSG91_AUTH_KEY")
    flow_id = os.environ.get("MSG91_FLOW_ID")  # FLOW ID in MSG91
    sender = os.environ.get("MSG91_SENDER_ID", "QMNAGE")

    if not authkey:
        raise MSG91Error("MSG91_AUTH_KEY missing")
    if not flow_id:
        raise MSG91Error("MSG91_FLOW_ID missing")

    phone = normalize_phone(phone)

    url = "https://api.msg91.com/api/v5/flow/"
    headers = {"authkey": authkey, "Content-Type": "application/json"}

    payload = {
        "flow_id": flow_id,
        "sender": sender,
        "mobiles": phone,
        "var1": message,  # configure var1 in MSG91 flow
    }

    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if r.status_code != 200:
        raise MSG91Error(f"MSG91 HTTP {r.status_code}: {r.text[:300]}")
    return r.text
