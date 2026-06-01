"""
BookShook Bot — Configuration
Loads all secrets and settings from environment variables.
"""

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _require_env(name: str) -> str:
    """Get a required environment variable or exit with a clear error."""
    value = os.environ.get(name)
    if not value:
        print(f"❌ ERROR: Required environment variable '{name}' is not set.")
        print(f"   Copy .env.example to .env and fill in your values.")
        sys.exit(1)
    return value


def _optional_env(name: str, default: str = "") -> str:
    """Get an optional environment variable with a default."""
    return os.environ.get(name, default)


# ── Required Secrets ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = _require_env("GOOGLE_API_KEY")
GOOGLE_CSE_ID = _require_env("GOOGLE_CSE_ID")
ADMIN_USER_ID = int(_require_env("ADMIN_USER_ID"))

# ── Dodo Payments (optional — payment features disabled if not set) ───────────
DODO_PAYMENTS_API_KEY = _optional_env("DODO_PAYMENTS_API_KEY")
DODO_WEBHOOK_SECRET = _optional_env("DODO_WEBHOOK_SECRET")
DODO_PRODUCT_ID = _optional_env("DODO_PRODUCT_ID")
PAYMENTS_ENABLED = bool(DODO_PAYMENTS_API_KEY and DODO_PRODUCT_ID)


# ── Bot Mode & Webhook ───────────────────────────────────────────────────────
BOT_MODE = _optional_env("BOT_MODE", "polling").lower()  # "polling" or "webhook"
WEBHOOK_URL = _optional_env("WEBHOOK_URL")
PORT = int(_optional_env("PORT", "10000"))

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, "books.db")

# ── Rate Limiting ─────────────────────────────────────────────────────────────
PDF_SEARCH_COOLDOWN_SECONDS = 30
GOOGLE_API_DAILY_LIMIT = 100

# ── Subscription Pricing ─────────────────────────────────────────────────────
SUBSCRIPTION_AMOUNT_PAISE = int(_optional_env("SUBSCRIPTION_AMOUNT_PAISE", "9900"))  # ₹99
SUBSCRIPTION_CURRENCY = "INR"
SUBSCRIPTION_NAME = "BookShook Premium"
SUBSCRIPTION_DESCRIPTION = "Monthly premium access — unlimited PDFs, wishlists, summaries & more"
FREE_TRIAL_DAYS = int(_optional_env("FREE_TRIAL_DAYS", "7"))
PREMIUM_DURATION_DAYS = 30
UPI_ID = _optional_env("UPI_ID", "suryanshsugara@okaxis")
