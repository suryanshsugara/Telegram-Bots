"""
BookShook Bot — Razorpay Payment Integration
Handles subscription creation, webhook verification, and payment event processing.
"""

import hmac
import hashlib
import json
import logging
from datetime import datetime

from config import (
    RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET,
    RAZORPAY_PLAN_ID, PAYMENTS_ENABLED,
    SUBSCRIPTION_AMOUNT_PAISE, SUBSCRIPTION_NAME, SUBSCRIPTION_DESCRIPTION,
    PREMIUM_DURATION_DAYS,
)
from database import (
    record_payment, upsert_subscription, update_subscription_status,
    get_subscription_by_razorpay_id, add_premium_user, log_event,
)

logger = logging.getLogger(__name__)

# Initialize Razorpay client (lazy — only when payments are enabled)
_client = None


def _get_client():
    """Lazy-init the Razorpay client."""
    global _client
    if _client is None:
        if not PAYMENTS_ENABLED:
            raise RuntimeError("Payments are not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.")
        try:
            import razorpay
            _client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
            logger.info("Razorpay client initialized")
        except ImportError:
            raise RuntimeError("razorpay package not installed. Run: pip install razorpay")
    return _client


# ── Plan Management ───────────────────────────────────────────────────────────

def create_plan() -> dict:
    """
    Create a Razorpay subscription plan (run once during setup).
    Returns the plan object with plan['id'] you need to save as RAZORPAY_PLAN_ID.
    """
    client = _get_client()
    plan = client.plan.create({
        "period": "monthly",
        "interval": 1,
        "item": {
            "name": SUBSCRIPTION_NAME,
            "amount": SUBSCRIPTION_AMOUNT_PAISE,
            "currency": "INR",
            "description": SUBSCRIPTION_DESCRIPTION,
        }
    })
    logger.info("Created Razorpay plan: %s", plan["id"])
    return plan


def get_plan_details() -> dict | None:
    """Fetch current plan details from Razorpay."""
    if not RAZORPAY_PLAN_ID:
        return None
    try:
        client = _get_client()
        return client.plan.fetch(RAZORPAY_PLAN_ID)
    except Exception as e:
        logger.error("Failed to fetch plan: %s", e)
        return None


# ── Subscription Management ──────────────────────────────────────────────────

def create_subscription(user_id: str) -> dict:
    """
    Create a new subscription for a user.
    Returns dict with 'short_url' (payment link) and 'id' (subscription ID).
    """
    if not PAYMENTS_ENABLED:
        raise RuntimeError("Payments are not configured.")
    if not RAZORPAY_PLAN_ID:
        raise RuntimeError("RAZORPAY_PLAN_ID is not set. Create a plan first.")

    client = _get_client()
    subscription = client.subscription.create({
        "plan_id": RAZORPAY_PLAN_ID,
        "total_count": 12,  # Up to 12 monthly cycles (1 year)
        "customer_notify": 1,  # Razorpay sends payment reminders
        "notes": {
            "user_id": str(user_id),
            "bot": "BookShook",
        }
    })

    # Save subscription to database
    upsert_subscription(
        user_id=str(user_id),
        razorpay_subscription_id=subscription["id"],
        plan_id=RAZORPAY_PLAN_ID,
        status=subscription["status"],
        short_url=subscription.get("short_url", ""),
    )

    log_event("subscription_created", user_id, {"subscription_id": subscription["id"]})
    logger.info("Subscription created for user %s: %s", user_id, subscription["id"])

    return {
        "id": subscription["id"],
        "short_url": subscription.get("short_url", ""),
        "status": subscription["status"],
    }


def cancel_subscription(razorpay_subscription_id: str) -> bool:
    """Cancel an active subscription."""
    try:
        client = _get_client()
        client.subscription.cancel(razorpay_subscription_id)
        update_subscription_status(razorpay_subscription_id, "cancelled")
        logger.info("Subscription cancelled: %s", razorpay_subscription_id)
        return True
    except Exception as e:
        logger.error("Failed to cancel subscription %s: %s", razorpay_subscription_id, e)
        return False


def get_subscription_details(razorpay_subscription_id: str) -> dict | None:
    """Fetch subscription status from Razorpay."""
    try:
        client = _get_client()
        return client.subscription.fetch(razorpay_subscription_id)
    except Exception as e:
        logger.error("Failed to fetch subscription %s: %s", razorpay_subscription_id, e)
        return None


# ── Webhook Handling ──────────────────────────────────────────────────────────

def verify_webhook_signature(payload_body: str, signature: str) -> bool:
    """
    Verify Razorpay webhook signature using HMAC-SHA256.
    IMPORTANT: Use raw request body (string), not parsed JSON.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.warning("RAZORPAY_WEBHOOK_SECRET not set — skipping verification")
        return True  # Allow in dev mode

    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        payload_body.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


async def handle_webhook_event(event_data: dict) -> dict:
    """
    Process a Razorpay webhook event.
    Returns a dict with the action taken.

    Key events:
    - subscription.authenticated → mandate created, waiting for first charge
    - subscription.activated → first payment successful
    - subscription.charged → recurring payment successful
    - subscription.completed → all cycles done
    - subscription.cancelled → user or admin cancelled
    - payment.captured → payment was successful
    - payment.failed → payment failed
    """
    event = event_data.get("event", "")
    payload = event_data.get("payload", {})

    logger.info("Processing webhook event: %s", event)

    result = {"event": event, "action": "none"}

    try:
        if event in ("subscription.authenticated", "subscription.activated"):
            sub_data = payload.get("subscription", {}).get("entity", {})
            sub_id = sub_data.get("id")
            notes = sub_data.get("notes", {})
            user_id = notes.get("user_id")

            if sub_id:
                update_subscription_status(sub_id, "active")

            if user_id:
                add_premium_user(user_id, days=PREMIUM_DURATION_DAYS, added_by="razorpay", method="subscription")
                log_event("subscription_activated", user_id, {"subscription_id": sub_id})
                result["action"] = f"premium_granted_to_{user_id}"

        elif event == "subscription.charged":
            sub_data = payload.get("subscription", {}).get("entity", {})
            payment_data = payload.get("payment", {}).get("entity", {})
            sub_id = sub_data.get("id")
            notes = sub_data.get("notes", {})
            user_id = notes.get("user_id")
            paid_count = sub_data.get("paid_count", 0)

            if sub_id:
                update_subscription_status(sub_id, "active", paid_count=paid_count)

            if user_id and payment_data:
                record_payment(
                    user_id=user_id,
                    razorpay_payment_id=payment_data.get("id", ""),
                    razorpay_subscription_id=sub_id or "",
                    amount=payment_data.get("amount", 0),
                    status="captured",
                    method=payment_data.get("method", ""),
                )
                # Extend premium by another month
                add_premium_user(user_id, days=PREMIUM_DURATION_DAYS, added_by="razorpay", method="subscription")
                log_event("payment_captured", user_id, {
                    "amount": payment_data.get("amount", 0),
                    "method": payment_data.get("method", ""),
                })
                result["action"] = f"payment_captured_and_premium_extended_for_{user_id}"

        elif event == "subscription.cancelled":
            sub_data = payload.get("subscription", {}).get("entity", {})
            sub_id = sub_data.get("id")
            notes = sub_data.get("notes", {})
            user_id = notes.get("user_id")

            if sub_id:
                update_subscription_status(sub_id, "cancelled")

            if user_id:
                log_event("subscription_cancelled", user_id, {"subscription_id": sub_id})
                result["action"] = f"subscription_cancelled_for_{user_id}"
            # Note: we don't remove premium — it expires naturally

        elif event == "subscription.completed":
            sub_data = payload.get("subscription", {}).get("entity", {})
            sub_id = sub_data.get("id")
            if sub_id:
                update_subscription_status(sub_id, "completed")
            result["action"] = "subscription_completed"

        elif event == "payment.captured":
            payment_data = payload.get("payment", {}).get("entity", {})
            notes = payment_data.get("notes", {})
            user_id = notes.get("user_id")

            if user_id and payment_data:
                record_payment(
                    user_id=user_id,
                    razorpay_payment_id=payment_data.get("id", ""),
                    razorpay_subscription_id=payment_data.get("subscription_id", ""),
                    amount=payment_data.get("amount", 0),
                    status="captured",
                    method=payment_data.get("method", ""),
                )
                result["action"] = f"payment_recorded_for_{user_id}"

        elif event == "payment.failed":
            payment_data = payload.get("payment", {}).get("entity", {})
            notes = payment_data.get("notes", {})
            user_id = notes.get("user_id")

            if user_id:
                record_payment(
                    user_id=user_id,
                    razorpay_payment_id=payment_data.get("id", ""),
                    razorpay_subscription_id=payment_data.get("subscription_id", ""),
                    amount=payment_data.get("amount", 0),
                    status="failed",
                    method=payment_data.get("method", ""),
                )
                log_event("payment_failed", user_id, {"amount": payment_data.get("amount", 0)})
                result["action"] = f"payment_failed_for_{user_id}"

        else:
            logger.info("Unhandled webhook event: %s", event)
            result["action"] = "unhandled"

    except Exception as e:
        logger.error("Error processing webhook event %s: %s", event, e)
        result["action"] = f"error: {str(e)}"

    return result
