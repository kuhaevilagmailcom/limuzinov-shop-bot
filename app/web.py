from __future__ import annotations

import json

from aiogram import Bot
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.config import get_settings
from app.db import OrderStatus, SessionLocal, mark_order_paid, update_order_status
from app.payments.cryptopay import verify_webhook as verify_crypto_webhook
from app.payments.rollypay import verify_webhook as verify_rolly_webhook

settings = get_settings()


def create_web_app(bot: Bot) -> FastAPI:
    app = FastAPI(title="Limuzinov Shop Payments")

    async def notify_paid(user_id: int, order_id: str, title: str) -> None:
        try:
            await bot.send_message(
                user_id,
                "✅ <b>Платёж подтверждён</b>\n\n"
                f"{title}\n"
                f"Заказ: <code>{order_id}</code>\n\n"
                "Спасибо за покупку!"
            )
        except Exception:
            pass
        for admin_id in settings.admins:
            try:
                await bot.send_message(
                    admin_id,
                    "💸 <b>Оплачен новый заказ</b>\n\n"
                    f"Товар: {title}\n"
                    f"Пользователь: <code>{user_id}</code>\n"
                    f"Заказ: <code>{order_id}</code>"
                )
            except Exception:
                pass

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "OK"

    @app.get("/payment/success", response_class=HTMLResponse)
    async def payment_success() -> str:
        return "<h2>Оплата отправлена</h2><p>Вернитесь в Telegram — бот подтвердит платеж автоматически.</p>"

    @app.get("/payment/fail", response_class=HTMLResponse)
    async def payment_fail() -> str:
        return "<h2>Оплата не завершена</h2><p>Вернитесь в Telegram и попробуйте снова.</p>"

    @app.post("/webhooks/rollypay")
    async def rollypay_webhook(
        request: Request,
        x_signature: str | None = Header(default=None),
        x_timestamp: str | None = Header(default=None),
    ):
        raw = await request.body()
        if not verify_rolly_webhook(raw, x_timestamp, x_signature):
            raise HTTPException(status_code=403, detail="Invalid signature")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        order_id = str(event.get("order_id", ""))
        status = str(event.get("status", ""))
        provider_payment_id = str(event.get("payment_id", ""))
        if not order_id:
            raise HTTPException(status_code=400, detail="Missing order_id")

        async with SessionLocal() as session:
            if status == "paid":
                order, changed = await mark_order_paid(
                    session,
                    order_id,
                    payment_method="rollypay",
                    provider_payment_id=provider_payment_id,
                )
                if order and changed:
                    await notify_paid(order.user_id, order.id, order.title)
            elif status in {"canceled", "expired", "refunded"}:
                await update_order_status(session, order_id, status)
        return {"ok": True}

    @app.post("/webhooks/cryptopay/{secret}")
    async def cryptopay_webhook(request: Request, secret: str):
        if not settings.cryptopay_webhook_secret or secret != settings.cryptopay_webhook_secret:
            raise HTTPException(status_code=404, detail="Not found")
        raw = await request.body()
        signature = request.headers.get("crypto-pay-api-signature")
        if not verify_crypto_webhook(raw, signature):
            raise HTTPException(status_code=403, detail="Invalid signature")
        try:
            update = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        if update.get("update_type") != "invoice_paid":
            return {"ok": True}
        invoice = update.get("payload") or {}
        order_id = str(invoice.get("payload", ""))
        invoice_id = str(invoice.get("invoice_id", ""))
        if not order_id:
            raise HTTPException(status_code=400, detail="Missing payload/order_id")

        async with SessionLocal() as session:
            order, changed = await mark_order_paid(
                session,
                order_id,
                payment_method="cryptopay",
                provider_payment_id=invoice_id,
            )
            if order and changed:
                await notify_paid(order.user_id, order.id, order.title)
        return {"ok": True}

    return app
