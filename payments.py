from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import httpx


class RollyPayError(RuntimeError):
    pass


async def _request(api_base: str, api_key: str, method: str, path: str, **kwargs) -> dict:
    headers = dict(kwargs.pop("headers", {}))
    headers.update({"X-API-Key": api_key, "X-Nonce": str(uuid4())})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=4)) as client:
            response = await client.request(method, f"{api_base.rstrip('/')}{path}", headers=headers, **kwargs)
    except httpx.RequestError as exc:
        raise RollyPayError("RollyPay недоступен") from exc
    if response.is_error:
        raise RollyPayError(f"RollyPay HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RollyPayError("RollyPay вернул некорректный JSON") from exc
    if not isinstance(data, dict):
        raise RollyPayError("RollyPay вернул некорректный ответ")
    return data


async def create_payment(
    *, api_base: str, api_key: str, terminal_id: str, order_id: str,
    amount: Decimal, description: str, customer_id: str, metadata: dict,
    test: bool, success_url: str = "", fail_url: str = "",
) -> dict:
    payload: dict[str, object] = {
        "amount": f"{amount:.2f}",
        "payment_currency": "RUB",
        "order_id": order_id,
        "description": description[:255],
        "customer_id": customer_id,
        "metadata": metadata,
        "test": test,
    }
    if terminal_id:
        payload["terminal_id"] = terminal_id
    if success_url:
        payload["success_redirect_url"] = success_url
    if fail_url:
        payload["fail_redirect_url"] = fail_url
    data = await _request(
        api_base, api_key, "POST", "/api/v1/payments",
        json=payload, headers={"Content-Type": "application/json"},
    )
    payment_id, pay_url = data.get("payment_id"), data.get("pay_url")
    parsed = urlsplit(str(pay_url or ""))
    if not isinstance(payment_id, str) or parsed.scheme != "https" or not parsed.hostname:
        raise RollyPayError("RollyPay не вернул безопасную ссылку оплаты")
    return data


async def get_payment(*, api_base: str, api_key: str, payment_id: str) -> dict:
    if not payment_id or len(payment_id) > 200:
        raise RollyPayError("Некорректный ID платежа")
    return await _request(
        api_base, api_key, "GET", f"/api/v1/payments/{quote(payment_id, safe='')}"
    )


async def get_balance(*, api_base: str, api_key: str, terminal_id: str = "") -> dict:
    query = f"?{urlencode({'terminal_id': terminal_id})}" if terminal_id else ""
    return await _request(api_base, api_key, "GET", f"/api/v1/balance{query}")


async def get_rate(*, api_base: str, api_key: str, terminal_id: str = "") -> dict:
    query = f"?{urlencode({'terminal_id': terminal_id})}" if terminal_id else ""
    return await _request(api_base, api_key, "GET", f"/api/v1/rate{query}")


async def list_payments(
    *, api_base: str, api_key: str, terminal_id: str = "", status: str = ""
) -> dict:
    params = {}
    if terminal_id:
        params["terminal_id"] = terminal_id
    if status:
        params["status"] = status
    query = f"?{urlencode(params)}" if params else ""
    return await _request(api_base, api_key, "GET", f"/api/v1/payments{query}")
