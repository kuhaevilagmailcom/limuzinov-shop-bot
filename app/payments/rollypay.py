from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from uuid import uuid4

import httpx

from app.config import get_settings


class RollyPayError(RuntimeError):
    pass


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
        return str(data.get("detail") or data.get("message") or data.get("error") or "request failed")[:300]
    except ValueError:
        return "request failed"


async def create_payment(order_id: str, amount: Decimal, description: str, user_id: int) -> dict:
    settings = get_settings()
    if not settings.rollypay_api_key:
        raise RollyPayError("ROLLYPAY_API_KEY is not configured")

    payload = {
        "amount": f"{amount:.2f}",
        "payment_currency": "RUB",
        "order_id": order_id,
        "description": description[:255],
        "customer_id": str(user_id),
        "metadata": {"telegram_user_id": str(user_id)},
        "test": settings.rollypay_test_mode,
    }
    if settings.rollypay_terminal_id:
        payload["terminal_id"] = settings.rollypay_terminal_id

    headers = {
        "X-API-Key": settings.rollypay_api_key,
        "X-Nonce": str(uuid4()),
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{settings.rollypay_api_base.rstrip('/')}/api/v1/payments",
            json=payload,
            headers=headers,
        )
    if response.is_error:
        raise RollyPayError(f"RollyPay returned HTTP {response.status_code}: {_error_message(response)}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RollyPayError("RollyPay returned invalid JSON") from exc
    if not result.get("payment_id") or not result.get("pay_url"):
        raise RollyPayError("RollyPay response has no payment link")
    return result


async def get_payment(payment_id: str) -> dict:
    settings = get_settings()
    headers = {
        "X-API-Key": settings.rollypay_api_key,
        "X-Nonce": str(uuid4()),
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{settings.rollypay_api_base.rstrip('/')}/api/v1/payments/{payment_id}",
            headers=headers,
        )
    if response.is_error:
        raise RollyPayError(f"RollyPay returned HTTP {response.status_code}: {_error_message(response)}")
    try:
        return response.json()
    except ValueError as exc:
        raise RollyPayError("RollyPay returned invalid JSON") from exc


def verify_webhook(raw_body: bytes, timestamp: str | None, signature: str | None, max_age_seconds: int = 300) -> bool:
    settings = get_settings()
    if not timestamp or not signature or not settings.rollypay_signing_secret:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > max_age_seconds:
        return False

    signed = timestamp.encode() + b"." + raw_body
    expected = hmac.new(
        settings.rollypay_signing_secret.encode(),
        signed,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
