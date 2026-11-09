"""Grade a Binance API key's permissions S to F. Uses Binance's read-only apiRestrictions endpoint.

The secret is used only to sign this one request. It is never stored, printed or logged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://api.binance.com/sapi/v1/account/apiRestrictions"


def grade_restrictions(r: dict) -> dict:
    """Pure grading of an apiRestrictions response."""
    withdrawals = bool(r.get("enableWithdrawals"))
    transfer = bool(r.get("enableInternalTransfer") or r.get("permitsUniversalTransfer"))
    ip = bool(r.get("ipRestrict"))
    trading = bool(r.get("enableSpotAndMarginTrading") or r.get("enableFutures") or r.get("enableMargin")
                   or r.get("enablePortfolioMarginTrading") or r.get("enableVanillaOptions"))
    checks = [
        {"id": "withdrawals", "label": "Withdrawals are disabled", "ok": not withdrawals, "severity": "critical"},
        {"id": "transfer", "label": "Internal and universal transfers are disabled", "ok": not transfer, "severity": "high"},
        {"id": "ip", "label": "Restricted to trusted IP addresses", "ok": ip, "severity": "high"},
        {"id": "trading", "label": "Read-only (no trading permission)", "ok": not trading, "severity": "info"},
    ]
    fixes = []
    if withdrawals:
        grade, head = "F", "Anyone with this key can withdraw your funds"
        fixes.append("Turn off 'Enable Withdrawals' on this key now, or delete the key.")
    elif transfer and not ip:
        grade, head = "D", "Funds can be moved and the key works from any IP"
    elif not ip and trading:
        grade, head = "C", "A leaked key can trade your account from anywhere"
    elif transfer:
        grade, head = "B", "IP-locked, but funds can be moved between accounts"
    elif not ip:
        grade, head = "B", "Read-only, but usable from any IP"
    elif trading:
        grade, head = "A", "IP-locked trading key with no withdrawals or transfers"
    else:
        grade, head = "S", "Read-only and IP-locked"
    if transfer:
        fixes.append("Turn off 'Enable Internal Transfer' and 'Permits Universal Transfer' unless a tool you trust needs them.")
    if not ip:
        fixes.append("Choose 'Restrict access to trusted IPs only' and add only the IPs that use this key.")
    if trading and grade in ("S", "A"):
        pass
    return {"grade": grade, "headline": head, "checks": checks, "fixes": fixes}


def check_binance_key(api_key: str, api_secret: str, timeout: float = 10.0, opener=None) -> dict:
    api_key, api_secret = (api_key or "").strip(), (api_secret or "").strip()
    if not api_key or not api_secret:
        return {"ok": False, "error": "Both the API key and the secret are needed to ask Binance about the key."}
    if len(api_key) > 128 or len(api_secret) > 128:
        return {"ok": False, "error": "That does not look like a Binance API key."}
    q = urllib.parse.urlencode({"timestamp": int(time.time() * 1000), "recvWindow": 10000})
    sig = hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{ENDPOINT}?{q}&signature={sig}", headers={"X-MBX-APIKEY": api_key})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}
        code = body.get("code")
        if code in (-2015, -2014, -1022):
            return {"ok": False, "code": code, "error": (
                "Binance refused the request. Either the key or secret is wrong, or the key is restricted to other "
                "IP addresses (which is good). Run `dmoera-grade key` from a machine on the key's IP list to check it fully.")}
        return {"ok": False, "code": code, "error": f"Binance returned HTTP {e.code}."}
    except Exception:
        return {"ok": False, "error": "Could not reach Binance."}
    out = grade_restrictions(data)
    out["ok"] = True
    return out
