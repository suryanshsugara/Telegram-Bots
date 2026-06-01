"""
BookShook Bot — Edge Cases and Integration Tests
Validates:
1. Dynamic regional pricing mapping for different language codes.
2. Anti-abuse download restrictions for Trial, Promo, Standard, and Admin users.
3. Dodo Payments webhook payload handling and DB writes.
4. Webhook signature validation.
"""

import sys
import os
import unittest
from datetime import datetime, timedelta
import json
import hmac
import hashlib

# Add project path to python import path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import DODO_WEBHOOK_SECRET
from database import (
    init_db, get_db, is_premium, add_premium_user, remove_premium_user,
    get_premium_info, record_payment, get_payment_count, record_pdf_download,
    get_pdf_download_count, get_user_subscription
)
from BookShook import _get_user_pricing, _check_download_limit
from payments import verify_webhook_signature, handle_webhook_event


class TestBookShookEdgeCases(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        # Clean up database tables related to tests
        with get_db() as conn:
            conn.execute("DELETE FROM premium_users")
            conn.execute("DELETE FROM payments")
            conn.execute("DELETE FROM bot_stats WHERE event_type = 'pdf_download'")
            conn.execute("DELETE FROM subscriptions")

    # ── 1. PRICING EDGE CASES ──────────────────────────────────────────────────
    
    def test_pricing_indian_locale(self):
        """Should return INR formatting and pricing for Indian language codes."""
        pricing_hi = _get_user_pricing("hi")
        pricing_bn = _get_user_pricing("bn")
        pricing_en_in = _get_user_pricing("en-in")
        
        self.assertEqual(pricing_hi["currency_code"], "INR")
        self.assertEqual(pricing_bn["currency_code"], "INR")
        self.assertEqual(pricing_en_in["currency_code"], "INR")
        self.assertEqual(pricing_hi["promo_price_str"], "₹99")
        self.assertEqual(pricing_hi["renewal_price_str"], "₹149")

    def test_pricing_european_locale(self):
        """Should return GBP formatting and pricing for European/UK language codes."""
        pricing_gb = _get_user_pricing("en-gb")
        pricing_es = _get_user_pricing("es")
        
        self.assertEqual(pricing_gb["currency_code"], "GBP")
        self.assertEqual(pricing_es["currency_code"], "GBP")
        self.assertEqual(pricing_gb["promo_price_str"], "£4.99")
        self.assertEqual(pricing_gb["renewal_price_str"], "£9.99")
        self.assertEqual(pricing_gb["upi_promo_inr"], 529.00)

    def test_pricing_default_locale(self):
        """Should fallback to USD defaults (USA) for None, en, ja, or other unrecognized codes."""
        pricing_none = _get_user_pricing(None)
        pricing_ja = _get_user_pricing("ja")
        
        self.assertEqual(pricing_none["currency_code"], "USD")
        self.assertEqual(pricing_ja["currency_code"], "USD")
        self.assertEqual(pricing_none["promo_price_str"], "$4.99")
        self.assertEqual(pricing_none["renewal_price_str"], "$9.99")
        self.assertEqual(pricing_none["upi_promo_inr"], 419.00)

    # ── 2. ANTI-ABUSE DOWNLOAD LIMIT EDGE CASES ──────────────────────────────
    
    def test_download_limit_non_premium(self):
        """Should block downloads if user does not have premium."""
        allowed, msg = _check_download_limit("test_user_non_premium")
        self.assertFalse(allowed)
        self.assertIn("do not have an active premium", msg)

    def test_download_limit_trial_user(self):
        """Should allow <= 7 downloads for Trial users, block > 7."""
        user_id = "test_user_trial"
        add_premium_user(user_id, days=7, added_by="system", method="trial")
        info = get_premium_info(user_id)
        
        # Mock 6 downloads -> allowed (requesting the 7th book is allowed)
        for i in range(6):
            record_pdf_download(user_id, book_id=i+1)
        allowed, msg = _check_download_limit(user_id)
        self.assertTrue(allowed)
        
        # Make the 7th download (so total in DB is 7)
        record_pdf_download(user_id, book_id=7)
        
        # Check limit again (requesting the 8th book should be blocked)
        allowed, msg = _check_download_limit(user_id)
        self.assertFalse(allowed)
        self.assertIn("Trial Download Limit Reached", msg)

    def test_download_limit_promo_user(self):
        """Should allow <= 12 downloads for 1st Month Promo users (1 payment), block > 12."""
        user_id = "test_user_promo"
        add_premium_user(user_id, days=30, added_by="dodo", method="subscription")
        record_payment(user_id, "pay_1", "dodo_auto", 9900, "captured", "card")
        info = get_premium_info(user_id)
        
        # Mock 11 downloads -> allowed (requesting the 12th book is allowed)
        for i in range(11):
            record_pdf_download(user_id, book_id=i+1)
        allowed, msg = _check_download_limit(user_id)
        self.assertTrue(allowed)
        
        # Make the 12th download (so total in DB is 12)
        record_pdf_download(user_id, book_id=12)
        
        # Check limit again (requesting the 13th book should be blocked)
        allowed, msg = _check_download_limit(user_id)
        self.assertFalse(allowed)
        self.assertIn("Promo Subscription Download Limit Reached", msg)

    def test_download_limit_standard_user(self):
        """Should allow unlimited downloads for subsequent months (payments > 1)."""
        user_id = "test_user_standard"
        add_premium_user(user_id, days=30, added_by="dodo", method="subscription")
        # 2 successful payments
        record_payment(user_id, "pay_1", "dodo_auto", 9900, "captured", "card")
        record_payment(user_id, "pay_2", "dodo_auto", 14900, "captured", "card")
        
        # Mock 15 downloads -> still allowed
        for i in range(15):
            record_pdf_download(user_id, book_id=i+1)
        allowed, msg = _check_download_limit(user_id)
        self.assertTrue(allowed)
        self.assertIsNone(msg)

    def test_download_limit_admin_user(self):
        """Should allow unlimited downloads for admin-granted premium (0 payments, manual method)."""
        user_id = "test_user_admin_grant"
        add_premium_user(user_id, days=30, added_by="admin", method="manual")
        
        # Mock 20 downloads -> still allowed
        for i in range(20):
            record_pdf_download(user_id, book_id=i+1)
        allowed, msg = _check_download_limit(user_id)
        self.assertTrue(allowed)
        self.assertIsNone(msg)

    # ── 3. WEBHOOK & PAYMENTS EDGE CASES ──────────────────────────────────────
    
    def test_webhook_payment_succeeded(self):
        """Should grant premium and record converted INR equivalent payment when webhook is received."""
        user_id = "test_user_webhook"
        event_data = {
            "type": "payment.succeeded",
            "data": {
                "transaction_id": "dodo_tx_999",
                "id": "dodo_pay_999",
                "metadata": {
                    "user_id": user_id,
                    "amount_cents": "499",
                    "currency": "USD",
                    "amount_inr_cents": "41900"  # Converted INR equivalent
                }
            }
        }
        
        # Process mock webhook event
        import asyncio
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(handle_webhook_event(event_data))
        
        self.assertEqual(result["action"], f"premium_granted_to_{user_id}")
        self.assertTrue(is_premium(user_id))
        
        # Verify stored amount is INR paise (41900 paise = 419 INR)
        payments = get_payment_count(user_id)
        self.assertEqual(payments, 1)
        
        with get_db() as conn:
            row = conn.execute("SELECT amount, currency FROM payments WHERE user_id = ?", (user_id,)).fetchone()
            self.assertEqual(row["amount"], 41900)

    def test_webhook_unhandled_event(self):
        """Should gracefully log and report unhandled events without upgrading users."""
        event_data = {
            "type": "payment.failed",
            "data": {
                "transaction_id": "failed_tx",
                "metadata": {"user_id": "test_failed_user"}
            }
        }
        
        import asyncio
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(handle_webhook_event(event_data))
        
        self.assertEqual(result["action"], "unhandled")
        self.assertFalse(is_premium("test_failed_user"))

    # ── 4. WEBHOOK SIGNATURE VERIFICATION EDGE CASES ─────────────────────────
    
    def test_verify_signature_missing_secret(self):
        """Should pass if DODO_WEBHOOK_SECRET is not configured (dev fallback)."""
        # Save real secret
        global DODO_WEBHOOK_SECRET
        orig_secret = DODO_WEBHOOK_SECRET
        
        try:
            # Force secret to empty
            import payments
            payments.DODO_WEBHOOK_SECRET = ""
            
            # Should return True
            self.assertTrue(verify_webhook_signature("body", "sig", "id", "ts"))
        finally:
            payments.DODO_WEBHOOK_SECRET = orig_secret

    def test_verify_signature_valid(self):
        """Should return True for a valid SHA256 HMAC signature."""
        secret = "test_webhook_key_123"
        webhook_id = "evt_001"
        timestamp = "1717200000"
        payload = '{"type":"payment.succeeded"}'
        
        # Expected signature message: webhook-id.webhook-timestamp.raw_payload
        message = f"{webhook_id}.{timestamp}.{payload}"
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        # Mock global secret
        import payments
        orig_secret = payments.DODO_WEBHOOK_SECRET
        payments.DODO_WEBHOOK_SECRET = secret
        
        try:
            # Verify with correct signature
            self.assertTrue(verify_webhook_signature(payload, expected_sig, webhook_id, timestamp))
            
            # Verify with incorrect signature -> should fail
            self.assertFalse(verify_webhook_signature(payload, "invalid_signature", webhook_id, timestamp))
        finally:
            payments.DODO_WEBHOOK_SECRET = orig_secret


if __name__ == "__main__":
    unittest.main()
