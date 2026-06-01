"""
BookShook Bot — A premium Telegram bot for book discovery, PDF search, and reading lists.
Features: Stripe card payments, dynamic UPI fallback, admin dashboard, anti-abuse download limits.
"""

import logging
import time
import json
import aiohttp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError

from config import (
    TELEGRAM_BOT_TOKEN, GOOGLE_API_KEY, GOOGLE_CSE_ID,
    ADMIN_USER_ID, BOT_MODE, WEBHOOK_URL, PORT,
    PDF_SEARCH_COOLDOWN_SECONDS, PAYMENTS_ENABLED,
    SUBSCRIPTION_AMOUNT_PAISE, FREE_TRIAL_DAYS,
)
from database import (
    init_db, get_genres, genre_exists,
    get_books_by_genre, get_book_by_id, get_book_count,
    search_by_author, search_by_keyword, random_books,
    is_premium, add_premium_user, remove_premium_user,
    get_premium_info, list_premium_users, log_search, log_event,
    upsert_user, is_banned,
    add_to_wishlist, remove_from_wishlist, get_wishlist, is_in_wishlist,
    get_user_subscription, search_all_genres, record_pdf_download, get_pdf_download_count,
    get_payment_count,
)
from admin import (
    admin_dashboard, handle_admin_callback, handle_broadcast,
    is_admin, cmd_ban, cmd_unban, cmd_addbook, cmd_removebook, cmd_revenue,
)

logger = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# Global bot application reference for webhook handlers
bot_app = None


# ── Rate Limiting ─────────────────────────────────────────────────────────────
_last_pdf_search: dict[str, float] = {}

def _check_rate_limit(user_id: str) -> int | None:
    now = time.time()
    last = _last_pdf_search.get(user_id, 0)
    elapsed = now - last
    if elapsed < PDF_SEARCH_COOLDOWN_SECONDS:
        return int(PDF_SEARCH_COOLDOWN_SECONDS - elapsed)
    _last_pdf_search[user_id] = now
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _search_pdf(book_name: str) -> list[str]:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CSE_ID,
                      "q": f"{book_name} filetype:pdf", "num": 5}
            async with session.get("https://www.googleapis.com/customsearch/v1", params=params) as res:
                if res.status != 200:
                    logger.error("Google API error: %d", res.status)
                    return []
                data = await res.json()
                return [item["link"] for item in data.get("items", [])
                        if item.get("link") and "pdf" in item["link"].lower()]
    except Exception as e:
        logger.error("PDF search error: %s", e)
        return []


def _book_buttons(books: list[dict], max_btns: int = 8) -> list[list[InlineKeyboardButton]]:
    """Build book action buttons using DB IDs."""
    btns = []
    for b in books[:max_btns]:
        label = f"📖 {b['title'][:35]}" + ("..." if len(b['title']) > 35 else "")
        btns.append([InlineKeyboardButton(label, callback_data=f"book:{b['id']}")])
    return btns


def _pagination_buttons(current_page: int, total_pages: int, prefix: str) -> list[InlineKeyboardButton]:
    """Build ◀️ 1/5 ▶️ pagination row."""
    btns = []
    if current_page > 0:
        btns.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}:{current_page - 1}"))
    btns.append(InlineKeyboardButton(f"📄 {current_page + 1}/{total_pages}", callback_data="noop"))
    if current_page < total_pages - 1:
        btns.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}:{current_page + 1}"))
    return btns


async def _track_user(update: Update):
    """Track user on every interaction."""
    user = update.effective_user
    if user:
        upsert_user(str(user.id), user.username, user.first_name, user.last_name)


def _get_user_pricing(language_code: str) -> dict:
    """
    Get user pricing details based on their Telegram language code.
    Returns a dict with currency symbols, formatted strings, and local/UPI amounts.
    """
    lang = (language_code or "en").lower()
    
    # Typical Indian language codes
    indian_langs = {"hi", "bn", "te", "mr", "ta", "ur", "gu", "kn", "ml", "pa", "as", "or", "ne", "en-in"}
    # UK/European language codes
    european_langs = {"en-gb", "en-uk", "es", "fr", "de", "it", "pt", "nl", "pl", "sv", "da", "fi", "no", "hu", "cs", "sk", "ro", "bg", "el", "et", "lv", "lt", "hr", "sl", "uk", "ru"}
    
    if any(lang.startswith(x) for x in indian_langs) or lang == "hi":
        return {
            "currency_symbol": "₹",
            "currency_code": "INR",
            "promo_price_str": "₹99",
            "renewal_price_str": "₹149",
            "promo_price_val": 99.0,
            "renewal_price_val": 149.0,
            "upi_promo_inr": 99.00,
            "upi_renewal_inr": 149.00
        }
    elif any(lang.startswith(x) for x in european_langs) or lang in {"es", "fr", "de", "it", "pt", "nl", "uk"}:
        return {
            "currency_symbol": "£",
            "currency_code": "GBP",
            "promo_price_str": "£4.99",
            "renewal_price_str": "£9.99",
            "promo_price_val": 4.99,
            "renewal_price_val": 9.99,
            "upi_promo_inr": 529.00,
            "upi_renewal_inr": 1049.00
        }
    else:  # Default / USA
        return {
            "currency_symbol": "$",
            "currency_code": "USD",
            "promo_price_str": "$4.99",
            "renewal_price_str": "$9.99",
            "promo_price_val": 4.99,
            "renewal_price_val": 9.99,
            "upi_promo_inr": 419.00,
            "upi_renewal_inr": 839.00
        }


def _check_download_limit(user_id: str) -> tuple[bool, str | None]:
    """
    Check if the user has reached their download limits.
    Returns (allowed, error_message).
    """
    info = get_premium_info(user_id)
    if not info:
        return False, "🔒 You do not have an active premium subscription."
        
    method = info.get("method", "manual")
    created_at = info.get("created_at")
    
    if method == "trial":
        # 7-day free trial has a limit of 7 downloads
        downloads = get_pdf_download_count(user_id, created_at)
        if downloads >= 7:
            return False, (
                "⚠️ <b>Trial Download Limit Reached</b>\n\n"
                "You have reached the limit of 7 PDF downloads allowed during your 7-day free trial.\n\n"
                "To continue downloading unlimited books, please purchase a premium subscription! 💳"
            )
    elif method in ("subscription", "manual"):
        # We check how many successful payments they have.
        # If they have 1 payment, they are on their 1st month (promo price) -> limit is 12 downloads.
        # If they have 0 payments, they might be manually added by admin, which is unlimited.
        # If they have > 1 payments, they are on subsequent months -> unlimited.
        payments = get_payment_count(user_id)
        if payments == 1:
            downloads = get_pdf_download_count(user_id, created_at)
            if downloads >= 12:
                return False, (
                    "⚠️ <b>Promo Subscription Download Limit Reached</b>\n\n"
                    "You have reached the limit of 12 PDF downloads for your 1st month promo subscription.\n\n"
                    "This limit helps protect the service from abuse. Unlimited downloads will automatically unlock starting next month with your standard renewal! 📚"
                )
                
    return True, None


# ── Commands ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        genres = get_genres()
        if not genres:
            await update.message.reply_text("⚠️ Database empty. Run: <code>python seed_data.py</code>", parse_mode="HTML")
            return

        total_books = get_book_count()
        keyboard = [[InlineKeyboardButton(f"📚 {g}", callback_data=f"genre:{g}")] for g in genres]

        # Add premium CTA if not premium
        user_id = str(update.effective_user.id)
        if not is_premium(user_id):
            keyboard.append([InlineKeyboardButton("⭐ Try Premium FREE for 7 days!", callback_data="trial")])

        keyboard.append([InlineKeyboardButton("📋 My Wishlist", callback_data="wishlist:0")])

        await update.message.reply_text(
            f"✨ <b>Welcome to Book Shook!</b>\n\n"
            f"📚 Explore <b>{total_books:,}</b> books across <b>{len(genres)}</b> genres.\n"
            f"Search by author, keyword, or discover random gems!\n\n"
            f"🆘 /help for all commands",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        log_event("user_start", user_id)
    except Exception as e:
        logger.error("Error in /start: %s", e)
        await update.message.reply_text("⚠️ Something went wrong. Try again.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    total_books = get_book_count()
    genres_count = len(get_genres())
    
    user = update.effective_user
    lang = user.language_code if user else "en"
    pricing = _get_user_pricing(lang)

    help_msg = (
        "🆘 <b>Book Shook Commands</b>\n\n"
        "📖 /start — Browse genres & discover books\n"
        "❓ /help — Show this help\n"
        "⭐ /premium — Check premium status\n"
        "🔍 /getpdf <code>&lt;book&gt;</code> — Find book PDF (premium)\n"
        "📋 /wishlist — Your saved books\n"
    )
    if PAYMENTS_ENABLED:
        help_msg += f"💳 /subscribe — Get premium ({pricing['promo_price_str']} 1st month)\n"
        help_msg += "❌ /cancel — Cancel subscription\n"
    help_msg += (
        f"\n📊 <b>{total_books:,}</b> books • <b>{genres_count}</b> genres\n\n"
        "<b>Premium Benefits:</b>\n"
        "🔍 Unlimited PDF searches (anti-abuse limits apply for trial/promo)\n"
        "📋 Personal reading wishlists\n"
        "⭐ Priority support\n"
    )
    await update.message.reply_text(help_msg, parse_mode="HTML")


async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    user_id = str(update.effective_user.id)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    user = update.effective_user
    lang = user.language_code if user else "en"
    pricing = _get_user_pricing(lang)

    try:
        info = get_premium_info(user_id)
        if info and is_premium(user_id):
            method_text = {"subscription": "🔄 Auto-renewing", "manual": "👑 Admin granted",
                           "trial": "🎁 Free trial"}.get(info.get("method", ""), "📝")
            text = (
                f"✅ <b>You are Premium!</b>\n\n"
                f"📅 Expires: <b>{info['expiry'][:10]}</b>\n"
                f"📝 Type: {method_text}\n"
            )
            keyboard = [[InlineKeyboardButton("📋 My Wishlist", callback_data="wishlist:0")]]
        elif info:
            text = "⏰ <b>Your premium has expired.</b>\nRenew to keep your benefits!"
            keyboard = []
            if PAYMENTS_ENABLED:
                keyboard.append([InlineKeyboardButton(f"🔄 Renew — {pricing['promo_price_str']}", callback_data="subscribe")])
        else:
            text = (
                f"🔒 <b>You're not premium yet.</b>\n\n"
                f"<b>Premium Benefits:</b>\n"
                f"🔍 Unlimited PDF searches (anti-abuse limits apply for trial/promo)\n"
                f"📋 Personal wishlists\n"
                f"⭐ Priority support\n\n"
                f"💰 Promo Month: <b>{pricing['promo_price_str']}</b> (renews at {pricing['renewal_price_str']}/mo)"
            )
            keyboard = []
            if PAYMENTS_ENABLED:
                keyboard.append([InlineKeyboardButton(f"💳 Subscribe — {pricing['promo_price_str']}", callback_data="subscribe")])
            keyboard.append([InlineKeyboardButton(f"🎁 Free Trial ({FREE_TRIAL_DAYS} days)", callback_data="trial")])

        await update.message.reply_text(text, parse_mode="HTML",
                                         reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
    except Exception as e:
        logger.error("Error in /premium: %s", e)
        await update.message.reply_text("⚠️ Could not check status.")


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await _send_subscription_flow(update.effective_user.id, update.message, context)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    user_id = str(update.effective_user.id)
    
    if not is_premium(user_id):
        await update.message.reply_text("⚠️ You do not have an active premium subscription.")
        return

    info = get_premium_info(user_id)
    if info and info.get("method") == "trial":
        await update.message.reply_text("🎁 You are on a free trial. It will automatically expire and you will not be charged.")
        return

    sub = get_user_subscription(user_id)
    if sub and sub.get("razorpay_subscription_id") and sub.get("razorpay_subscription_id") != "stripe_auto":
        try:
            from payments import cancel_subscription
            success = cancel_subscription(sub["razorpay_subscription_id"])
            if success:
                await update.message.reply_text(
                    "✅ <b>Subscription cancelled.</b>\n\n"
                    "Your premium access remains active until the current period expires.",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text("⚠️ Could not cancel legacy subscription. Contact admin.")
        except Exception as e:
            logger.error("Cancel error: %s", e)
            await update.message.reply_text("⚠️ Error cancelling subscription.")
    else:
        await update.message.reply_text(
            "ℹ️ <b>BookShook Premium Access</b>\n\n"
            "Your premium access was purchased as a one-time payment (or UPI transfer).\n"
            "It will automatically expire at the end of the 30-day period. There are no recurring charges! 💳",
            parse_mode="HTML"
        )


async def wishlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    user_id = str(update.effective_user.id)
    books = get_wishlist(user_id)

    if not books:
        await update.message.reply_text(
            "📋 <b>Your Wishlist</b>\n\nEmpty! Browse books and tap ➕ to save them.",
            parse_mode="HTML",
        )
        return

    text = f"📋 <b>Your Wishlist ({len(books)} books)</b>\n\n"
    for b in books[:15]:
        text += f"• {b['title']} — <i>{b['author']}</i>\n"

    keyboard = _book_buttons(books[:8])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="start_back")])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def getpdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    user_id = str(update.effective_user.id)

    if not is_premium(user_id):
        keyboard = []
        if PAYMENTS_ENABLED:
            keyboard.append([InlineKeyboardButton("💳 Get Premium", callback_data="subscribe")])
        keyboard.append([InlineKeyboardButton("🎁 Free Trial", callback_data="trial")])
        await update.message.reply_text(
            "🔒 <b>PDF search is a premium feature.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        )
        return

    # Check anti-abuse download limits
    allowed, err_msg = _check_download_limit(user_id)
    if not allowed:
        await update.message.reply_text(err_msg, parse_mode="HTML")
        return

    if not context.args:
        await update.message.reply_text("Usage: /getpdf <code>&lt;book name&gt;</code>", parse_mode="HTML")
        return

    wait = _check_rate_limit(user_id)
    if wait is not None:
        await update.message.reply_text(f"⏳ Please wait {wait}s before searching again.")
        return

    book_name = " ".join(context.args)
    log_search(user_id, book_name, search_type="pdf")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    msg = await update.message.reply_text("🔍 Searching for PDF...")

    links = await _search_pdf(book_name)
    try:
        if links:
            # Find book ID from title if exists in DB to log accurately
            from database import get_db
            book_id = 0
            try:
                with get_db() as conn:
                    row = conn.execute("SELECT id FROM books WHERE LOWER(title) = ? LIMIT 1", (book_name.lower(),)).fetchone()
                    if row:
                        book_id = row["id"]
            except Exception:
                pass
            
            record_pdf_download(user_id, book_id)
            
            link_text = "\n".join([f'  {i+1}. <a href="{l}">Link {i+1}</a>' for i, l in enumerate(links[:3])])
            await msg.edit_text(
                f'📖 <b>{book_name}</b>\n\n📎 <b>PDF Links:</b>\n{link_text}',
                parse_mode="HTML", disable_web_page_preview=True,
            )
        else:
            await msg.edit_text(f"❌ No PDF found for <b>{book_name}</b>.", parse_mode="HTML")
    except TelegramError as e:
        logger.error("Edit message error: %s", e)


# ── Admin Commands ────────────────────────────────────────────────────────────

async def add_premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("🚫 Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addpremium <code>&lt;user_id&gt;</code> [days]", parse_mode="HTML")
        return
    target = context.args[0]
    days = int(context.args[1]) if len(context.args) >= 2 else 30
    add_premium_user(target, days=days, added_by=str(ADMIN_USER_ID), method="manual")
    await update.message.reply_text(f"✅ Premium granted to <code>{target}</code> for {days} days.", parse_mode="HTML")


async def remove_premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("🚫 Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removepremium <code>&lt;user_id&gt;</code>", parse_mode="HTML")
        return
    remove_premium_user(context.args[0])
    await update.message.reply_text(f"✅ Premium revoked for <code>{context.args[0]}</code>.", parse_mode="HTML")


async def list_premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("🚫 Admin only.")
        return
    users = list_premium_users()
    if not users:
        await update.message.reply_text("No premium users.")
        return
    lines = ["👑 <b>Premium Users:</b>\n"]
    for u in users[:50]:
        active = "✅" if is_premium(u["user_id"]) else "❌"
        lines.append(f"{active} <code>{u['user_id']}</code> — expires {u['expiry'][:10]}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── Callback Handlers ────────────────────────────────────────────────────────

async def handle_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _track_user(update)

    try:
        genre = query.data.split(":", 1)[1]
        if not genre_exists(genre):
            await query.edit_message_text("⚠️ Genre not found. /start again.")
            return

        context.user_data["genre"] = genre
        book_count = get_book_count(genre)
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

        buttons = [
            [InlineKeyboardButton("🔍 Search by Author", callback_data="search:author")],
            [InlineKeyboardButton("🎲 Random 5 Books", callback_data="search:random")],
            [InlineKeyboardButton("🔑 Search by Keyword", callback_data="search:keyword")],
            [InlineKeyboardButton("📋 Browse All", callback_data="page:0")],
            [InlineKeyboardButton("⬅️ Back to Genres", callback_data="start_back")],
        ]
        await query.edit_message_text(
            f"✨ <b>{genre}</b> — {book_count} books\n\nHow would you like to search?",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as e:
        logger.error("Genre handler error: %s", e)


async def handle_start_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    genres = get_genres()
    total = get_book_count()
    keyboard = [[InlineKeyboardButton(f"📚 {g}", callback_data=f"genre:{g}")] for g in genres]
    user_id = str(query.from_user.id)
    if not is_premium(user_id):
        keyboard.append([InlineKeyboardButton("⭐ Try Premium FREE!", callback_data="trial")])
    keyboard.append([InlineKeyboardButton("📋 My Wishlist", callback_data="wishlist:0")])

    await query.edit_message_text(
        f"✨ <b>Book Shook</b> — {total:,} books • {len(genres)} genres\n\nChoose a genre:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _track_user(update)

    search_type = query.data.split(":", 1)[1]
    genre = context.user_data.get("genre")
    if not genre:
        await query.edit_message_text("⚠️ No genre selected. /start again.")
        return

    context.user_data["search_type"] = search_type
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    if search_type == "random":
        books = random_books(genre, 5)
        if not books:
            await query.edit_message_text(f"⚠️ No books in {genre}.")
            return
        text = f"🎲 <b>Random {genre} Picks:</b>\n\n"
        text += "\n".join([f"• {b['title']} — <i>{b['author']}</i>" for b in books])
        keyboard = _book_buttons(books)
        keyboard.append([InlineKeyboardButton("🔄 More Random", callback_data="search:random")])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"genre:{genre}")])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        log_search(str(query.from_user.id), "random", genre, "random")
        return

    prompt = "👤 Type an <b>author name</b>:" if search_type == "author" else "🔑 Type a <b>keyword</b>:"
    await query.edit_message_text(prompt, parse_mode="HTML")
    context.user_data["awaiting_input"] = True


async def handle_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle paginated book browsing."""
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":", 1)[1])
    genre = context.user_data.get("genre")
    if not genre:
        await query.edit_message_text("⚠️ /start again.")
        return

    per_page = 8
    total = get_book_count(genre)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))

    books = get_books_by_genre(genre, limit=per_page, offset=page * per_page)
    text = f"📋 <b>{genre}</b> — Page {page + 1}/{total_pages}\n\n"
    text += "\n".join([f"• {b['title']} — <i>{b['author']}</i>" for b in books])

    keyboard = _book_buttons(books)
    keyboard.append(_pagination_buttons(page, total_pages, "page"))
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"genre:{genre}")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show book detail with action buttons."""
    query = update.callback_query
    await query.answer()

    try:
        book_id = int(query.data.split(":", 1)[1])
        book = get_book_by_id(book_id)
        if not book:
            await query.edit_message_text("⚠️ Book not found.")
            return

        user_id = str(query.from_user.id)
        in_wl = is_in_wishlist(user_id, book_id)
        is_prem = is_premium(user_id)

        text = (
            f"📖 <b>{book['title']}</b>\n"
            f"✍️ <i>{book['author']}</i>\n\n"
        )
        if in_wl:
            text += "📋 In your wishlist ✅\n"

        keyboard = []
        if is_prem:
            keyboard.append([InlineKeyboardButton("🔍 Find PDF", callback_data=f"pdf:{book_id}")])
        else:
            keyboard.append([InlineKeyboardButton("🔒 PDF (Premium)", callback_data="subscribe")])

        if in_wl:
            keyboard.append([InlineKeyboardButton("➖ Remove from Wishlist", callback_data=f"wlrm:{book_id}")])
        else:
            keyboard.append([InlineKeyboardButton("➕ Add to Wishlist", callback_data=f"wladd:{book_id}")])

        genre = context.user_data.get("genre")
        if genre:
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"genre:{genre}")])
        else:
            keyboard.append([InlineKeyboardButton("⬅️ Genres", callback_data="start_back")])

        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error("Book detail error: %s", e)


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)

    if not is_premium(user_id):
        await query.answer("🔒 Premium feature!", show_alert=True)
        return

    await query.answer()
    book_id = int(query.data.split(":", 1)[1])
    book = get_book_by_id(book_id)
    if not book:
        await query.edit_message_text("⚠️ Book not found.")
        return

    # Check anti-abuse download limits
    allowed, err_msg = _check_download_limit(user_id)
    if not allowed:
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data=f"book:{book_id}")]]
        keyboard.insert(0, [InlineKeyboardButton("💳 Get Premium", callback_data="subscribe")])
        await query.edit_message_text(err_msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    wait = _check_rate_limit(user_id)
    if wait:
        await query.answer(f"⏳ Wait {wait}s", show_alert=True)
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await query.edit_message_text(f"🔍 Searching PDF for <b>{book['title']}</b>...", parse_mode="HTML")

    links = await _search_pdf(f"{book['title']} {book['author']}")
    log_search(user_id, book["title"], search_type="pdf_button")

    if links:
        record_pdf_download(user_id, book_id)
        link_text = "\n".join([f'  {i+1}. <a href="{l}">Link {i+1}</a>' for i, l in enumerate(links[:3])])
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data=f"book:{book_id}")]]
        await query.edit_message_text(
            f"📖 <b>{book['title']}</b>\n✍️ <i>{book['author']}</i>\n\n📎 <b>PDF Links:</b>\n{link_text}",
            parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data=f"book:{book_id}")]]
        await query.edit_message_text(
            f"❌ No PDF found for <b>{book['title']}</b>.", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def handle_wishlist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    book_id = int(query.data.split(":", 1)[1])
    user_id = str(query.from_user.id)
    add_to_wishlist(user_id, book_id)
    await query.answer("✅ Added to wishlist!")
    # Refresh book detail view
    query.data = f"book:{book_id}"
    await handle_book(update, context)


async def handle_wishlist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    book_id = int(query.data.split(":", 1)[1])
    user_id = str(query.from_user.id)
    remove_from_wishlist(user_id, book_id)
    await query.answer("✅ Removed from wishlist!")
    query.data = f"book:{book_id}"
    await handle_book(update, context)


async def handle_wishlist_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    page = int(query.data.split(":", 1)[1])
    books = get_wishlist(user_id)

    if not books:
        await query.edit_message_text(
            "📋 <b>Your Wishlist</b>\n\nEmpty! Browse and tap ➕ to save books.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Genres", callback_data="start_back")]]),
        )
        return

    per_page = 8
    total_pages = max(1, (len(books) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    page_books = books[page * per_page:(page + 1) * per_page]

    text = f"📋 <b>Your Wishlist ({len(books)} books)</b>\n\n"
    text += "\n".join([f"• {b['title']} — <i>{b['author']}</i>" for b in page_books])

    keyboard = _book_buttons(page_books)
    if total_pages > 1:
        keyboard.append(_pagination_buttons(page, total_pages, "wishlist"))
    keyboard.append([InlineKeyboardButton("⬅️ Genres", callback_data="start_back")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


# ── Payment Callbacks ────────────────────────────────────────────────────────

async def handle_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle subscribe button click — create Razorpay or UPI subscription."""
    query = update.callback_query
    await query.answer()
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await _send_subscription_flow(query.from_user.id, update, context)


async def handle_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free trial button."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if is_premium(user_id):
        await query.answer("✅ You're already premium!", show_alert=True)
        return

    # Check if user already used a trial
    info = get_premium_info(user_id)
    if info and info.get("method") == "trial":
        await query.answer("⚠️ You've already used your free trial.", show_alert=True)
        return

    add_premium_user(user_id, days=FREE_TRIAL_DAYS, added_by="system", method="trial")
    log_event("trial_activated", user_id)

    await query.edit_message_text(
        f"🎉 <b>Free Trial Activated!</b>\n\n"
        f"You have <b>{FREE_TRIAL_DAYS} days</b> of premium access.\n\n"
        f"✅ Unlimited PDF searches\n"
        f"✅ Personal wishlists\n\n"
        f"Enjoy reading! 📚",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 Browse Books", callback_data="start_back")],
        ]),
    )


async def handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle no-op callbacks (page indicators etc)."""
    await update.callback_query.answer()


# ── Photo and Text Input Handlers ─────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    user_id = str(update.effective_user.id)
    if is_banned(user_id):
        return

    if not context.user_data.get("awaiting_payment_screenshot"):
        await update.message.reply_text("💡 Use /start to browse, or /help for commands.")
        return

    amount_inr = context.user_data.get("payment_amount_inr", 99.0)
    photo_file = update.message.photo[-1].file_id
    caption = (
        f"💰 <b>New Payment Verification Request (Screenshot)</b>\n\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Username: @{update.effective_user.username or 'None'}\n"
        f"Name: {update.effective_user.full_name}\n"
        f"Expected Amount: ₹{amount_inr:.0f}"
    )
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"adm:pay_appr:{user_id}:{int(amount_inr)}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"adm:pay_rej:{user_id}")
        ]
    ]
    
    await context.bot.send_photo(
        chat_id=ADMIN_USER_ID,
        photo=photo_file,
        caption=caption,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    await update.message.reply_text(
        "✅ <b>Verification Request Sent!</b>\n\n"
        "Thank you! The admin is verifying your transaction. You will be notified automatically as soon as it is approved! 📚",
        parse_mode="HTML"
    )
    context.user_data["awaiting_payment_screenshot"] = False


async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    user_id = str(update.effective_user.id)

    if is_banned(user_id):
        return

    # Admin broadcast
    if context.user_data.get("awaiting_broadcast") and is_admin(update.effective_user.id):
        await handle_broadcast(update, context)
        return

    # Payment UTR input fallback
    if context.user_data.get("awaiting_payment_screenshot"):
        user_input = update.message.text.strip()
        if not user_input:
            await update.message.reply_text("⚠️ Please enter the 12-digit UPI UTR/Ref number or send a screenshot image.")
            return
            
        amount_inr = context.user_data.get("payment_amount_inr", 99.0)
        caption = (
            f"💰 <b>New Payment Verification Request (UTR Number)</b>\n\n"
            f"UTR/Ref: <code>{user_input}</code>\n"
            f"User ID: <code>{user_id}</code>\n"
            f"Username: @{update.effective_user.username or 'None'}\n"
            f"Name: {update.effective_user.full_name}\n"
            f"Expected Amount: ₹{amount_inr:.0f}"
        )
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"adm:pay_appr:{user_id}:{int(amount_inr)}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm:pay_rej:{user_id}")
            ]
        ]
        
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=caption,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        await update.message.reply_text(
            "✅ <b>Verification Request Sent!</b>\n\n"
            "Thank you! The admin is verifying your transaction. You will be notified automatically as soon as it is approved! 📚",
            parse_mode="HTML"
        )
        context.user_data["awaiting_payment_screenshot"] = False
        return

    # Search input
    if not context.user_data.get("awaiting_input"):
        await update.message.reply_text("💡 Use /start to browse, or /help for commands.")
        return

    user_input = update.message.text.strip()
    if not user_input:
        await update.message.reply_text("⚠️ Please enter a search term.")
        return

    genre = context.user_data.get("genre")
    search_type = context.user_data.get("search_type", "keyword")

    if not genre or not genre_exists(genre):
        await update.message.reply_text("⚠️ No genre selected. /start again.")
        context.user_data["awaiting_input"] = False
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        results = search_by_author(genre, user_input) if search_type == "author" else search_by_keyword(genre, user_input)
        log_search(user_id, user_input, genre, search_type)

        if not results:
            alt = "keyword" if search_type == "author" else "author"
            await update.message.reply_text(
                f"❌ No matches for '<b>{user_input}</b>' in {genre}.\n💡 Try {alt} search or /start.",
                parse_mode="HTML",
            )
            context.user_data["awaiting_input"] = False
            return

        display = results[:10]
        text = (
            f"🔍 <b>'{user_input}' in {genre}</b> ({len(results)} found)\n\n"
            + "\n".join([f"• {b['title']} — <i>{b['author']}</i>" for b in display])
        )
        keyboard = _book_buttons(display)
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"genre:{genre}")])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error("Search error: %s", e)
        await update.message.reply_text("⚠️ Search failed. Try again.")

    context.user_data["awaiting_input"] = False


# ── Error Handler ─────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)
    if update and hasattr(update, "effective_message") and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Try again.")
        except Exception:
            pass


# ── Subscription Helper ──────────────────────────────────────────────────────

async def _send_subscription_flow(user_id: int, update_source, context: ContextTypes.DEFAULT_TYPE):
    """Sends the subscription flow, trying Razorpay first and falling back to UPI."""
    user = None
    if hasattr(update_source, "from_user") and update_source.from_user:
        user = update_source.from_user
    elif hasattr(update_source, "effective_user") and update_source.effective_user:
        user = update_source.effective_user
    elif hasattr(update_source, "callback_query") and update_source.callback_query and update_source.callback_query.from_user:
        user = update_source.callback_query.from_user

    lang_code = user.language_code if user else "en"
    pricing = _get_user_pricing(lang_code)

    # Try Stripe Checkout first if keys are configured
    if PAYMENTS_ENABLED:
        try:
            from payments import create_checkout_session
            amount_cents = int(pricing["promo_price_val"] * 100)
            currency = pricing["currency_code"]
            amount_inr_cents = int(pricing["upi_promo_inr"] * 100)
            session = create_checkout_session(str(user_id), amount_cents, currency, amount_inr_cents=amount_inr_cents)
            
            msg_text = (
                f"💳 <b>BookShook Premium — {pricing['promo_price_str']} (1st Month)</b>\n\n"
                f"🔥 Special Promo: {pricing['promo_price_str']} for the first month (limited to 12 downloads).\n"
                f"🔄 Standard Renewal: {pricing['renewal_price_str']}/month from the 2nd month (unlimited downloads).\n\n"
                f"👉 Tap the button below to pay securely via Credit/Debit Card, Apple Pay, or Google Pay:"
            )
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Pay securely via Card", url=session["url"])],
            ])
            
            if hasattr(update_source, "callback_query") and update_source.callback_query:
                await update_source.callback_query.edit_message_text(msg_text, parse_mode="HTML", reply_markup=reply_markup)
            else:
                await update_source.reply_text(msg_text, parse_mode="HTML", reply_markup=reply_markup)
            return
        except Exception as e:
            logger.error("Stripe checkout creation failed, falling back to UPI: %s", e)

    # Fallback to UPI manual verification flow
    from config import UPI_ID
    import urllib.parse
    
    amount_inr = pricing["upi_promo_inr"]
    upi_link = f"upi://pay?pa={UPI_ID}&pn={urllib.parse.quote('BookShook Premium')}&am={amount_inr:.2f}&cu=INR&tn={urllib.parse.quote(f'BookShook Promo Sub - {user_id}')}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={urllib.parse.quote(upi_link)}"
    
    context.user_data["awaiting_payment_screenshot"] = True
    context.user_data["payment_amount_inr"] = amount_inr
    
    caption_text = (
        f"💳 <b>BookShook Premium Subscription</b>\n\n"
        f"🔥 <b>Special Promo (1st Month):</b> {pricing['promo_price_str']} (₹{amount_inr:.0f} equivalent)\n"
        f"🔄 <b>Next Months (Renewal):</b> {pricing['renewal_price_str']}/month (₹{pricing['upi_renewal_inr']:.0f} equivalent)\n\n"
        f"🔒 <b>Anti-Abuse Download Policy:</b>\n"
        f"• <b>1st Month (Promo):</b> Up to <b>12 PDF downloads</b>.\n"
        f"• <b>2nd Month+ (Standard):</b> <b>Unlimited PDF downloads</b>.\n\n"
        f"👉 <b>UPI Direct Transfer:</b>\n"
        f"Please pay <b>₹{amount_inr:.0f}</b> directly to the admin's UPI ID:\n"
        f"👉 <code>{UPI_ID}</code>\n\n"
        f"📱 <b>On Mobile?</b> Tap the button below to pay directly using GPay, PhonePe, Paytm, or BHIM.\n\n"
        f"🖥️ <b>On Desktop?</b> Scan the QR code image using your UPI app.\n\n"
        f"📸 <b>Important</b>: After making the payment, **send the screenshot of the payment receipt** (or type the 12-digit UPI UTR/Reference number) here in this chat to activate your premium access."
    )
    
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Pay via UPI App", url=upi_link)],
        [InlineKeyboardButton("⬅️ Back", callback_data="start_back")]
    ])
    
    if hasattr(update_source, "callback_query") and update_source.callback_query:
        query = update_source.callback_query
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_photo(
            chat_id=int(user_id),
            photo=qr_url,
            caption=caption_text,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    else:
        await update_source.reply_photo(
            photo=qr_url,
            caption=caption_text,
            parse_mode="HTML",
            reply_markup=reply_markup
        )


# ── Webhook Server ────────────────────────────────────────────────────────────

async def _dodo_webhook_handler(request):
    """aiohttp handler for Dodo Payments webhooks."""
    try:
        raw_body = await request.text()
        
        # Extract headers for webhook signature validation
        webhook_id = request.headers.get("webhook-id", "")
        signature = request.headers.get("webhook-signature", "")
        webhook_timestamp = request.headers.get("webhook-timestamp", "")

        from payments import verify_webhook_signature, handle_webhook_event
        if not verify_webhook_signature(raw_body, signature, webhook_id, webhook_timestamp):
            logger.warning("Invalid Dodo Payments webhook signature")
            return web.json_response({"status": "invalid_signature"}, status=400)

        event_data = json.loads(raw_body)
        result = await handle_webhook_event(event_data)
        logger.info("Dodo Payments webhook processed: %s", result)
        
        # If premium was successfully granted, notify the user via Telegram
        action = result.get("action", "")
        if action.startswith("premium_granted_to_"):
            user_id = action.replace("premium_granted_to_", "")
            if bot_app:
                try:
                    await bot_app.bot.send_message(
                        chat_id=int(user_id),
                        text="🎉 <b>Checkout Successful!</b>\n\nYour payment was processed. 👑 <b>BookShook Premium</b> has been activated for 30 days! Enjoy browsing and downloading books! 📚",
                        parse_mode="HTML"
                    )
                except Exception as notify_err:
                    logger.error("Failed to notify user %s of Dodo payment: %s", user_id, notify_err)
                    
        return web.json_response({"status": "ok", **result})
    except Exception as e:
        logger.error("Dodo Payments webhook handler error: %s", e)
        return web.json_response({"status": "error"}, status=500)


async def _health_handler(request):
    """Health check endpoint for UptimeRobot."""
    return web.json_response({
        "status": "ok",
        "bot": "BookShook",
        "books": get_book_count(),
        "genres": len(get_genres()),
    })


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    init_db()
    add_premium_user(str(ADMIN_USER_ID), days=36500, added_by="system", method="manual")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("getpdf", getpdf))
    app.add_handler(CommandHandler("wishlist", wishlist_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # Admin commands
    app.add_handler(CommandHandler("admin", admin_dashboard))
    app.add_handler(CommandHandler("addpremium", add_premium_cmd))
    app.add_handler(CommandHandler("removepremium", remove_premium_cmd))
    app.add_handler(CommandHandler("listpremium", list_premium_cmd))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("addbook", cmd_addbook))
    app.add_handler(CommandHandler("removebook", cmd_removebook))
    app.add_handler(CommandHandler("revenue", cmd_revenue))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(handle_noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(handle_start_back, pattern=r"^start_back$"))
    app.add_handler(CallbackQueryHandler(handle_trial, pattern=r"^trial$"))
    app.add_handler(CallbackQueryHandler(handle_subscribe, pattern=r"^subscribe$"))
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(handle_genre, pattern=r"^genre:"))
    app.add_handler(CallbackQueryHandler(handle_search, pattern=r"^search:"))
    app.add_handler(CallbackQueryHandler(handle_page, pattern=r"^page:"))
    app.add_handler(CallbackQueryHandler(handle_book, pattern=r"^book:"))
    app.add_handler(CallbackQueryHandler(handle_pdf, pattern=r"^pdf:"))
    app.add_handler(CallbackQueryHandler(handle_wishlist_add, pattern=r"^wladd:"))
    app.add_handler(CallbackQueryHandler(handle_wishlist_remove, pattern=r"^wlrm:"))
    app.add_handler(CallbackQueryHandler(handle_wishlist_view, pattern=r"^wishlist:"))

    # Media and Text handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))

    # Error handler
    app.add_error_handler(error_handler)

    logger.info("Starting BookShook in %s mode...", BOT_MODE)

    if BOT_MODE == "webhook" and WEBHOOK_URL:
        # Set up the custom aiohttp application to host both custom web routes and Telegram webhook
        web_app = web.Application()
        
        # Feed telegram webhook updates into the PTB application
        async def telegram_webhook_handler(request):
            token = request.match_info.get("token")
            if token != TELEGRAM_BOT_TOKEN:
                return web.Response(status=403)
            try:
                data = await request.json()
                update = Update.de_json(data, app.bot)
                await app.process_update(update)
                return web.Response(status=200)
            except Exception as e:
                logger.error("Error processing telegram update: %s", e)
                return web.Response(status=500)

        # Add the routing table
        web_app.router.add_post(f"/webhook/{{token}}", telegram_webhook_handler)
        web_app.router.add_post("/dodo/webhook", _dodo_webhook_handler)
        web_app.router.add_get("/ping", _health_handler)

        # Lifespan handlers for initializing/starting/stopping the PTB application
        async def on_startup(webapp):
            global bot_app
            bot_app = app
            await app.initialize()
            await app.start()
            webhook_url = f"{WEBHOOK_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
            logger.info("Setting Telegram Webhook to: %s", webhook_url)
            await app.bot.set_webhook(
                url=webhook_url,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False
            )

        async def on_cleanup(webapp):
            await app.stop()
            await app.shutdown()

        web_app.on_startup.append(on_startup)
        web_app.on_cleanup.append(on_cleanup)

        logger.info("Starting Custom Aiohttp server on port %s...", PORT)
        web.run_app(web_app, host="0.0.0.0", port=PORT)
    else:
        global bot_app
        bot_app = app
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()