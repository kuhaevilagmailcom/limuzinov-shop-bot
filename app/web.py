from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation

from aiogram import Bot
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.db import Order, SessionLocal, mark_order_paid, update_order_status
from app.notifications import notify_order_paid
from app.payments.rollypay import verify_webhook as verify_rolly_webhook


logger = logging.getLogger(__name__)


def _same_amount(received: object, expected: Decimal | None) -> bool:
    try:
        return expected is not None and Decimal(str(received)) == expected
    except (InvalidOperation, ValueError):
        return False


def create_web_app(bot: Bot) -> FastAPI:
    """Technical HTTP service only: health check and signed RollyPay callbacks."""
    app = FastAPI(title="LIMYZINOV SHOP API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def secure(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        return response

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "OK"

    @app.post("/rollypay/callback")
    @app.post("/webhooks/rollypay")
    async def rollypay_callback(
        request: Request,
        x_signature: str | None = Header(None),
        x_timestamp: str | None = Header(None),
    ) -> dict[str, bool]:
        raw = await request.body()
        if not verify_rolly_webhook(raw, x_timestamp, x_signature):
            raise HTTPException(403, "Invalid signature")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "Invalid JSON") from exc

        order_id = str(event.get("order_id", ""))
        payment_id = str(event.get("payment_id", ""))
        status = str(event.get("status", ""))
        async with SessionLocal() as session:
            order = await session.get(Order, order_id)
            matches = (
                order
                and order.payment_method == "rollypay"
                and order.provider_payment_id == payment_id
                and str(event.get("currency", event.get("payment_currency", ""))).upper() == "RUB"
                and _same_amount(event.get("amount"), order.amount_rub)
            )
            if not matches:
                logger.warning("Rejected mismatched RollyPay callback for %s", order_id)
                raise HTTPException(409, "Payment does not match order")
            if status == "paid":
                order, changed = await mark_order_paid(
                    session,
                    order_id,
                    payment_method="rollypay",
                    provider_payment_id=payment_id,
                )
                if order and changed:
                    await notify_order_paid(bot, order)
            elif status in {"processing", "canceled", "expired", "refunded", "chargeback"}:
                await update_order_status(session, order_id, status)
        return {"ok": True}

    return app
