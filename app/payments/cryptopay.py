from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal

import httpx

from app.config import get_settings


class CryptoPayError(RuntimeError):
    pass


def _response_error(response: httpx.Response) -> str:
    try:
        data = response.json()
        return str(data.get("error") or "request failed")[:300]
    except ValueError:
        return "request failed"


async def create_invoice(order_id: str, amount_rub: Decimal, description: str) -> dict:
    settings = get_settings()
    if not settings.cryptopay_token:
        raise CryptoPayError("CRYPTOPAY_TOKEN is not configured")

    payload = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": f"{amount_rub:.2f}",
        "accepted_assets": "USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC",
        "description": description[:1024],
        "payload": order_id,
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": 1800,
    }
    headers = {"Crypto-Pay-API-Token": settings.cryptopay_token}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{settings.cryptopay_api_base.rstrip('/')}/api/createInvoice",
            json=payload,
            headers=headers,
        )
    if response.is_error:
        raise CryptoPayError(f"Crypto Pay returned HTTP {response.status_code}: {_response_error(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CryptoPayError("Crypto Pay returned invalid JSON") from exc
    if not data.get("ok"):
        raise CryptoPayError(str(data.get("error", "Unknown Crypto Pay error")))
    return data["result"]


def verify_webhook(raw_body: bytes, signature: str | None) -> bool:
    settings = get_settings()
    if not signature or not settings.cryptopay_token:
        return False
    secret = hashlib.sha256(settings.cryptopay_token.encode()).digest()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def get_invoice(invoice_id: str) -> dict | None:
    settings = get_settings()
    headers = {"Crypto-Pay-API-Token": settings.cryptopay_token}
    params = {"invoice_ids": invoice_id, "count": 1}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{settings.cryptopay_api_base.rstrip('/')}/api/getInvoices",
            params=params,
            headers=headers,
        )
    if response.is_error:
        raise CryptoPayError(f"Crypto Pay returned HTTP {response.status_code}: {_response_error(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CryptoPayError("Crypto Pay returned invalid JSON") from exc
    if not data.get("ok"):
        raise CryptoPayError(str(data.get("error", "Unknown Crypto Pay error")))
    invoices = data.get("result", {}).get("items") if isinstance(data.get("result"), dict) else data.get("result")
    if not invoices:
        return None
    return invoices[0]
