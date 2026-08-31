from __future__ import annotations

import html
import json
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiogram import Bot
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import Order, Product, SessionLocal, active_products, mark_order_paid, update_order_status
from app.notifications import notify_order_paid
from app.payments.cryptopay import verify_webhook as verify_crypto_webhook
from app.payments.rollypay import verify_webhook as verify_rolly_webhook

settings = get_settings()
logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _same_amount(received: object, expected: Decimal | None) -> bool:
    try:
        return expected is not None and Decimal(str(received)) == expected
    except (InvalidOperation, ValueError):
        return False


def _shell(content: str, title: str = "LIMUZINOV SHOP") -> str:
    return f"""<!doctype html><html lang="ru" data-theme="dark"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#08090d"><title>{html.escape(title)}</title><style>
:root{{color-scheme:dark;--bg:#08090d;--card:rgba(255,255,255,.065);--text:#f7f7fb;--muted:#aaaab7;--line:rgba(255,255,255,.11);--hot:#ff4d00;--gold:#ffbd2e;--glow:rgba(255,77,0,.2)}}
[data-theme=light]{{color-scheme:light;--bg:#f4f3ef;--card:rgba(255,255,255,.72);--text:#15151a;--muted:#62626e;--line:rgba(15,15,20,.1);--hot:#e84400;--gold:#ff9e00;--glow:rgba(232,68,0,.16)}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;min-height:100vh;background:radial-gradient(circle at 12% 8%,var(--glow),transparent 28rem),radial-gradient(circle at 88% 35%,rgba(255,189,46,.1),transparent 26rem),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;transition:.25s}}
body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.22;background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);background-size:54px 54px;mask-image:linear-gradient(to bottom,black,transparent 70%)}}
.wrap{{width:min(1120px,calc(100% - 32px));margin:auto;position:relative}}header{{display:flex;align-items:center;justify-content:space-between;padding:24px 0}}.logo{{font-weight:950;letter-spacing:-.055em;font-size:clamp(20px,3vw,28px)}}.logo span{{color:var(--hot)}}
.theme{{display:flex;gap:4px;padding:4px;border:1px solid var(--line);background:var(--card);backdrop-filter:blur(18px);border-radius:99px}}.theme button{{border:0;background:transparent;color:var(--muted);border-radius:99px;padding:8px 10px;cursor:pointer}}.theme button:hover,.theme button.on{{background:var(--text);color:var(--bg)}}
main{{padding:9vh 0 80px}}.badge{{display:inline-flex;gap:8px;align-items:center;border:1px solid var(--line);background:var(--card);padding:8px 12px;border-radius:99px;color:var(--muted);font-size:13px}}.dot{{width:7px;height:7px;border-radius:50%;background:#32d583;box-shadow:0 0 16px #32d583}}
h1{{font-size:clamp(48px,9vw,104px);line-height:.91;letter-spacing:-.07em;margin:28px 0 26px;max-width:940px}}.grad{{background:linear-gradient(115deg,var(--hot),var(--gold));background-clip:text;color:transparent}}.lead{{max-width:650px;color:var(--muted);font-size:clamp(17px,2.2vw,22px);line-height:1.55}}
.hero-layout{{display:grid;grid-template-columns:1.05fr .95fr;gap:42px;align-items:center}}.hero-art{{position:relative}}.hero-art:before{{content:"";position:absolute;inset:12%;background:radial-gradient(circle,rgba(255,33,144,.34),transparent 66%);filter:blur(28px)}}.hero-art img{{position:relative;width:100%;height:auto;display:block;filter:drop-shadow(0 24px 70px rgba(255,25,140,.22));animation:float 5s ease-in-out infinite}}@keyframes float{{50%{{transform:translateY(-12px) rotate(-.5deg)}}}}
.actions{{display:flex;gap:12px;flex-wrap:wrap;margin:34px 0 72px}}.btn{{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;font-weight:800;padding:15px 21px;border-radius:15px;border:1px solid var(--line);color:var(--text);background:var(--card);transition:.2s}}.btn:hover{{transform:translateY(-3px)}}.primary{{border:0;background:linear-gradient(115deg,var(--hot),var(--gold));color:#fff;box-shadow:0 16px 50px var(--glow)}}
.section-title{{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:20px}}h2{{font-size:clamp(28px,5vw,46px);letter-spacing:-.045em;margin:0}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.card{{padding:26px;border:1px solid var(--line);border-radius:24px;background:var(--card);backdrop-filter:blur(22px);transition:.25s}}.card:hover{{transform:translateY(-6px);border-color:var(--hot)}}.emoji{{font-size:38px}}.card h3{{font-size:22px;margin:24px 0 8px}}.card p{{color:var(--muted);line-height:1.55;min-height:48px}}.price{{display:flex;align-items:center;justify-content:space-between;margin-top:24px;font-weight:900;font-size:22px}}.price small{{font-weight:700;color:var(--muted);font-size:11px}}
footer{{border-top:1px solid var(--line);padding:24px 0 36px;color:var(--muted);font-size:13px}}.result{{max-width:650px;margin:12vh auto;padding:38px;text-align:center;border:1px solid var(--line);border-radius:28px;background:var(--card);backdrop-filter:blur(24px)}}.result h1{{font-size:clamp(34px,7vw,58px);line-height:1}}.result p{{color:var(--muted);line-height:1.6}}
@media(max-width:760px){{.hero-layout,.grid{{grid-template-columns:1fr}}main{{padding-top:5vh}}.hero-art{{order:-1}}.card p{{min-height:0}}}}@media(prefers-reduced-motion:reduce){{*{{transition:none!important;animation:none!important}}}}
</style></head><body><div class="wrap">{content}</div><script>
const r=document.documentElement,b=[...document.querySelectorAll('[data-theme-set]')];function a(v){{const t=v==='system'?(matchMedia('(prefers-color-scheme:light)').matches?'light':'dark'):v;r.dataset.theme=t;b.forEach(x=>x.classList.toggle('on',x.dataset.themeSet===v))}}const s=localStorage.getItem('limuzinov-theme')||'system';a(s);b.forEach(x=>x.onclick=()=>{{localStorage.setItem('limuzinov-theme',x.dataset.themeSet);a(x.dataset.themeSet)}})
</script></body></html>"""


def _price(product: Product) -> str:
    values = []
    if product.price_rub:
        values.append(f"{product.price_rub} ₽")
    if product.price_stars:
        values.append(f"{product.price_stars} ⭐")
    return " / ".join(values) or "по запросу"


def _storefront(username: str, products: list[Product]) -> str:
    url = f"https://t.me/{html.escape(username, quote=True)}"
    cards = "".join(
        f'<article class="card"><div class="emoji">{html.escape(p.emoji)}</div><h3>{html.escape(p.title)}</h3><p>{html.escape(p.description)}</p><div class="price"><span>{_price(p)}</span><small>В БОТЕ →</small></div></article>'
        for p in products
    )
    return _shell(f"""<header><div class="logo">LIMUZINOV<span>.</span>SHOP</div><div class="theme"><button data-theme-set="light" title="Светлая">☀</button><button data-theme-set="dark" title="Тёмная">☾</button><button data-theme-set="system" title="Системная">◐</button></div></header><main><div class="hero-layout"><div><div class="badge"><span class="dot"></span> Магазин открыт 24/7</div><h1>Не просто вещи.<br><span class="grad">Это твой вайб.</span></h1><p class="lead">Фирменный мерч и персональная музыка. Выбрал в Telegram, оплатил удобным способом — готово.</p><div class="actions"><a class="btn primary" href="{url}">Открыть магазин в Telegram&nbsp; ↗</a><a class="btn" href="#catalog">Смотреть каталог</a></div></div><div class="hero-art"><img src="/static/brand/hero.png" alt="Логотип LIMUZINOV SHOP" width="1536" height="1024"></div></div><section id="catalog"><div class="section-title"><h2>Витрина</h2><span class="badge">₽ · ⭐ · ₿</span></div><div class="grid">{cards}</div></section></main><footer>© LIMUZINOV SHOP · Оплата и статус заказа доступны в Telegram</footer>""")


def _result(ok: bool, username: str) -> str:
    title = "Оплата отправлена" if ok else "Оплата не завершена"
    text = "Бот подтвердит платёж автоматически." if ok else "Вернитесь в бот и попробуйте оплатить ещё раз."
    return _shell(f'<main class="result"><div class="emoji">{"✨" if ok else "↩️"}</div><h1>{title}</h1><p>{text}</p><div class="actions" style="justify-content:center;margin:28px 0 0"><a class="btn primary" href="https://t.me/{html.escape(username, quote=True)}">Вернуться в бот</a></div></main>', title)


def create_web_app(bot: Bot, bot_username: str) -> FastAPI:
    app = FastAPI(title="LIMUZINOV SHOP", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def secure(request: Request, call_next):
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        async with SessionLocal() as session:
            products = await active_products(session)
        return _storefront(bot_username, products)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "OK"

    @app.get("/api/catalog", response_class=JSONResponse)
    async def catalog() -> list[dict[str, object]]:
        async with SessionLocal() as session:
            products = await active_products(session)
        return [{"key": p.key, "title": p.title, "description": p.description, "price_rub": p.price_rub, "price_stars": p.price_stars, "emoji": p.emoji} for p in products]

    @app.get("/payment/success", response_class=HTMLResponse)
    async def success() -> str:
        return _result(True, bot_username)

    @app.get("/payment/fail", response_class=HTMLResponse)
    async def fail() -> str:
        return _result(False, bot_username)

    @app.post("/rollypay/callback")
    @app.post("/webhooks/rollypay")
    async def rollypay_callback(request: Request, x_signature: str | None = Header(None), x_timestamp: str | None = Header(None)) -> dict[str, bool]:
        raw = await request.body()
        if not verify_rolly_webhook(raw, x_timestamp, x_signature):
            raise HTTPException(403, "Invalid signature")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "Invalid JSON") from exc
        order_id, payment_id, status = str(event.get("order_id", "")), str(event.get("payment_id", "")), str(event.get("status", ""))
        async with SessionLocal() as session:
            order = await session.get(Order, order_id)
            matches = order and order.payment_method == "rollypay" and order.provider_payment_id == payment_id and str(event.get("currency", event.get("payment_currency", ""))).upper() == "RUB" and _same_amount(event.get("amount"), order.amount_rub)
            if not matches:
                logger.warning("Rejected mismatched RollyPay callback for %s", order_id)
                raise HTTPException(409, "Payment does not match order")
            if status == "paid":
                order, changed = await mark_order_paid(session, order_id, payment_method="rollypay", provider_payment_id=payment_id)
                if order and changed:
                    await notify_order_paid(bot, order)
            elif status in {"processing", "canceled", "expired", "refunded", "chargeback"}:
                await update_order_status(session, order_id, status)
        return {"ok": True}

    @app.post("/webhooks/cryptopay")
    @app.post("/webhooks/cryptopay/{secret}")
    async def cryptopay_callback(request: Request, secret: str | None = None) -> dict[str, bool]:
        if secret is not None and (not settings.cryptopay_webhook_secret or secret != settings.cryptopay_webhook_secret):
            raise HTTPException(404, "Not found")
        raw = await request.body()
        if not verify_crypto_webhook(raw, request.headers.get("crypto-pay-api-signature")):
            raise HTTPException(403, "Invalid signature")
        try:
            update = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "Invalid JSON") from exc
        if update.get("update_type") != "invoice_paid":
            return {"ok": True}
        invoice = update.get("payload") or {}
        order_id, invoice_id = str(invoice.get("payload", "")), str(invoice.get("invoice_id", ""))
        async with SessionLocal() as session:
            order = await session.get(Order, order_id)
            matches = order and order.payment_method == "cryptopay" and order.provider_payment_id == invoice_id and invoice.get("status") == "paid" and str(invoice.get("fiat", "")).upper() == "RUB" and _same_amount(invoice.get("amount"), order.amount_rub)
            if not matches:
                logger.warning("Rejected mismatched Crypto Pay callback for %s", order_id)
                raise HTTPException(409, "Invoice does not match order")
            order, changed = await mark_order_paid(session, order_id, payment_method="cryptopay", provider_payment_id=invoice_id)
            if order and changed:
                await notify_order_paid(bot, order)
        return {"ok": True}

    return app
