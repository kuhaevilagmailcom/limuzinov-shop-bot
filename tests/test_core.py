import hashlib
import hmac
import json
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

from app.payments.cryptopay import verify_webhook as verify_crypto
from app.payments.rollypay import verify_webhook as verify_rolly
from app.web import _same_amount, _storefront


class CoreTests(unittest.TestCase):
    def test_storefront_escapes_bot_username(self):
        page = _storefront('safe" onclick="alert(1)')
        self.assertNotIn('onclick="alert(1)', page)
        self.assertIn("LIMUZINOV", page)

    def test_amount_match_is_exact(self):
        self.assertTrue(_same_amount("1000.00", Decimal("1000")))
        self.assertFalse(_same_amount("999.99", Decimal("1000")))
        self.assertFalse(_same_amount("not-a-number", Decimal("1000")))

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
