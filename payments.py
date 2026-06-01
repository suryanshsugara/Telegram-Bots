"""
BookShook Bot — Stripe Payment Integration
Handles subscription Checkout Sessions, webhook signature verification, and event processing.
"""

import logging
import json
import time
from datetime import datetime

from config import STRIPE_API_KEY, STRIPE_WEBHOOK_SECRET, PAYMENTS_ENABLED
from database import (
    record_payment, add_premium_user, log_event,
)

logger = logging.getLogger(__name__)

# Initialize Stripe (lazy)
_stripe_module = None

def _get_stripe():
    """Lazy import and setup stripe client."""
    global _stripe_module
    if _stripe_module is None:
        if not PAYMENTS_ENABLED:
            raise RuntimeError("Stripe is not configured. Set STRIPE_API_KEY.")
        try:
            import stripe
            stripe.api_key = STRIPE_API_KEY
            _stripe_module = stripe
            logger.info("Stripe client initialized")
        except ImportError:
            raise RuntimeError("stripe package not installed. Run: pip install stripe")
    return _stripe_module


def create_checkout_session(user_id: str, amount_cents: int, currency: str, amount_inr_cents: int = None, success_url: str = "https://t.me/BookShook_bot") -> dict:
    """
    Create a Stripe Checkout Session for premium payment.
    Returns session dict with 'url' and 'id'.
    """
    stripe = _get_stripe()
    
    metadata = {
        'user_id': str(user_id),
        'amount_cents': str(amount_cents),
        'currency': currency
    }
    if amount_inr_cents is not None:
        metadata['amount_inr_cents'] = str(amount_inr_cents)
        
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{
            'price_data': {
                'currency': currency.lower(),
                'product_data': {
                    'name': 'BookShook Premium Access',
                    'description': f'30 days of BookShook Premium',
                },
                'unit_amount': amount_cents,
            },
            'quantity': 1,
        }],
        mode='payment',
        success_url=success_url,
        cancel_url=success_url,
        metadata=metadata
    )
    
    log_event("stripe_session_created", user_id, {"session_id": session.id})
    logger.info("Stripe session created for user %s: %s", user_id, session.id)
    
    return {
        "id": session.id,
        "url": session.url
    }


def verify_webhook_signature(payload_body: str, signature: str) -> bool:
    """
    Verify Stripe webhook signature using webhook secret.
    """
    if not STRIPE_WEBHOOK_SECRET:
        logger.warning("STRIPE_WEBHOOK_SECRET not set — skipping verification")
        return True  # Allow in dev/fallback
        
    stripe = _get_stripe()
    try:
        stripe.Webhook.construct_event(
            payload_body, signature, STRIPE_WEBHOOK_SECRET
        )
        return True
    except Exception as e:
        logger.error("Stripe webhook verification failed: %s", e)
        return False


async def handle_webhook_event(event_data: dict) -> dict:
    """
    Process a verified Stripe webhook event.
    Returns a dict with the action taken.
    """
    event_type = event_data.get("type", "")
    data = event_data.get("data", {})
    obj = data.get("object", {})

    logger.info("Processing Stripe webhook event: %s", event_type)
    result = {"event": event_type, "action": "none"}

    try:
        if event_type == "checkout.session.completed":
            metadata = obj.get("metadata", {})
            user_id = metadata.get("user_id")
            amount_cents = int(metadata.get("amount_cents", "0"))
            amount_inr_cents = int(metadata.get("amount_inr_cents", str(amount_cents)))
            currency = metadata.get("currency", "USD")
            payment_id = obj.get("id")
            
            if user_id:
                # Record payment in DB
                record_payment(
                    user_id=user_id,
                    razorpay_payment_id=payment_id,
                    razorpay_subscription_id="stripe_auto",
                    amount=amount_inr_cents,
                    status="captured",
                    method="card"
                )
                
                # Grant premium for 30 days
                add_premium_user(user_id, days=30, added_by="stripe", method="subscription")
                log_event("stripe_payment_captured", user_id, {
                    "amount": amount_cents,
                    "currency": currency,
                    "payment_id": payment_id
                })
                result["action"] = f"premium_granted_to_{user_id}"
        else:
            logger.info("Unhandled Stripe webhook event: %s", event_type)
            result["action"] = "unhandled"
            
    except Exception as e:
        logger.error("Error processing Stripe webhook event %s: %s", event_type, e)
        result["action"] = f"error: {str(e)}"
        
    return result
