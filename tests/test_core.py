import hashlib
import hmac
import json
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.payments.cryptopay import verify_webhook as verify_crypto
from app.payments.rollypay import verify_webhook as verify_rolly
from app.db import Product
from app.keyboards import admin_product_keyboard, product_keyboard
from app.web import _same_amount, _storefront


class CoreTests(unittest.TestCase):
    def test_storefront_escapes_bot_username(self):
        product = SimpleNamespace(emoji="🎵", title="Создать песню", description="Описание", price_rub=None, price_stars=350)
        page = _storefront('safe" onclick="alert(1)', [product])
        self.assertNotIn('onclick="alert(1)', page)
        self.assertIn("LIMYZINOV", page)
        self.assertIn("350 ⭐", page)
        self.assertIn("/static/brand/hero.png", page)

    def test_amount_match_is_exact(self):
        self.assertTrue(_same_amount("1000.00", Decimal("1000")))
        self.assertFalse(_same_amount("999.99", Decimal("1000")))
        self.assertFalse(_same_amount("not-a-number", Decimal("1000")))

    def test_product_has_stars_and_admin_controls(self):
        product = Product(id=7, key="song", title="Песня", description="ТЗ", price_rub=990, price_stars=350, emoji="🎵", kind="digital", requires_brief=True)
        buy_data = [button.callback_data for row in product_keyboard(product).inline_keyboard for button in row if button.callback_data]
        admin_data = [button.callback_data for row in admin_product_keyboard(product).inline_keyboard for button in row if button.callback_data]
        self.assertIn("buy:stars:7", buy_data)
        self.assertIn("admin:edit:rub:7", admin_data)
        self.assertIn("admin:edit:stars:7", admin_data)

    @patch("app.payments.rollypay.get_settings")
    def test_rollypay_signature(self, mocked_settings):
        mocked_settings.return_value.rollypay_signing_secret = "secret"
        body = json.dumps({"status": "paid"}, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(b"secret", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_rolly(body, timestamp, signature))
        self.assertFalse(verify_rolly(body + b" ", timestamp, signature))

    @patch("app.payments.cryptopay.get_settings")
    def test_crypto_signature(self, mocked_settings):
        mocked_settings.return_value.cryptopay_token = "token"
        body = b'{"update_type":"invoice_paid"}'
        secret = hashlib.sha256(b"token").digest()
        signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_crypto(body, signature))
        self.assertFalse(verify_crypto(body + b" ", signature))


if __name__ == "__main__":
    unittest.main()
