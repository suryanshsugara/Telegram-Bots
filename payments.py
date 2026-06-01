"""
BookShook Bot — Dodo Payments Integration
Handles subscription Checkout Sessions, webhook signature verification, and event processing.
"""

import logging
import json
import hmac
import hashlib

from config import DODO_PAYMENTS_API_KEY, DODO_WEBHOOK_SECRET, DODO_PRODUCT_ID, PAYMENTS_ENABLED
from database import (
    record_payment, add_premium_user, log_event,
)

logger = logging.getLogger(__name__)

# Initialize Dodo Payments client (lazy)
_dodo_client = None

def _get_client():
    """Lazy import and setup Dodo Payments client."""
    global _dodo_client
    if _dodo_client is None:
        if not PAYMENTS_ENABLED:
            raise RuntimeError("Dodo Payments is not configured. Set DODO_PAYMENTS_API_KEY and DODO_PRODUCT_ID.")
        try:
            from dodopayments import DodoPayments
            # Detect environment (test_mode or live_mode) from API key prefix
            env = "test_mode" if DODO_PAYMENTS_API_KEY.startswith("keys_test") else "live_mode"
            _dodo_client = DodoPayments(
                bearer_token=DODO_PAYMENTS_API_KEY,
                environment=env
            )
            logger.info("Dodo Payments client initialized in %s", env)
        except ImportError:
            raise RuntimeError("dodopayments package not installed. Run: pip install dodopayments")
    return _dodo_client


def create_checkout_session(user_id: str, amount_cents: int, currency: str, amount_inr_cents: int = None, success_url: str = "https://t.me/BookShook_bot") -> dict:
    """
    Create a Dodo Payments Checkout Session for premium payment.
    Returns session dict with 'url' and 'id'.
    """
    client = _get_client()
    
    # Dodo Payments requires a product_id and amount (in cents/paise)
    product_cart = [{
        "product_id": DODO_PRODUCT_ID,
        "quantity": 1,
        "amount": amount_cents
    }]
    
    # customer is required by Dodo Payments (email is required)
    customer = {
        "name": f"Telegram User {user_id}",
        "email": f"tg_{user_id}@bookshook.bot"
    }
    
    metadata = {
        "user_id": str(user_id),
        "amount_cents": str(amount_cents),
        "currency": currency
    }
    if amount_inr_cents is not None:
        metadata["amount_inr_cents"] = str(amount_inr_cents)
        
    session = client.checkout_sessions.create(
        product_cart=product_cart,
        customer=customer,
        metadata=metadata,
        return_url=success_url
    )
    
    log_event("dodo_session_created", user_id, {"session_id": session.checkout_session_id})
    logger.info("Dodo Payments session created for user %s: %s", user_id, session.checkout_session_id)
    
    return {
        "id": getattr(session, "checkout_session_id", None) or getattr(session, "id", "dodo_session"),
        "url": session.checkout_url
    }


def verify_webhook_signature(payload_body: str, signature: str, webhook_id: str, webhook_timestamp: str) -> bool:
    """
    Verify Dodo Payments webhook signature using SHA256 HMAC.
    """
    if not DODO_WEBHOOK_SECRET:
        logger.warning("DODO_WEBHOOK_SECRET not set — skipping verification")
        return True  # Allow in dev
        
    # Message: webhook-id.webhook-timestamp.raw_payload
    message = f"{webhook_id}.{webhook_timestamp}.{payload_body}"
    expected = hmac.new(
        DODO_WEBHOOK_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected, signature)


async def handle_webhook_event(event_data: dict) -> dict:
    """
    Process a verified Dodo Payments webhook event.
    Returns a dict with the action taken.
    """
    event_type = event_data.get("type", "")
    data = event_data.get("data", {})

    logger.info("Processing Dodo Payments webhook event: %s", event_type)
    result = {"event": event_type, "action": "none"}

    try:
        if event_type == "payment.succeeded":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            
            # Check multiple possible keys for amount
            amount_cents = int(metadata.get("amount_cents", "0"))
            amount_inr_cents = int(metadata.get("amount_inr_cents", str(amount_cents)))
            currency = metadata.get("currency", "USD")
            
            # payment ID from Dodo Payments data
            payment_id = data.get("transaction_id") or data.get("id") or "dodo_payment"
            
            if user_id:
                # Record payment in DB
                record_payment(
                    user_id=user_id,
                    razorpay_payment_id=payment_id,
                    razorpay_subscription_id="dodo_auto",
                    amount=amount_inr_cents,
                    status="captured",
                    method="card"
                )
                
                # Grant premium for 30 days
                add_premium_user(user_id, days=30, added_by="dodo", method="subscription")
                log_event("dodo_payment_captured", user_id, {
                    "amount": amount_cents,
                    "currency": currency,
                    "payment_id": payment_id
                })
                result["action"] = f"premium_granted_to_{user_id}"
        else:
            logger.info("Unhandled Dodo Payments webhook event: %s", event_type)
            result["action"] = "unhandled"
            
    except Exception as e:
        logger.error("Error processing Dodo Payments webhook event %s: %s", event_type, e)
        result["action"] = f"error: {str(e)}"
        
    return result
