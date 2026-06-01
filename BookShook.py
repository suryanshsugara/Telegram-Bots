"""
BookShook Bot — A premium Telegram bot for book discovery, PDF search, and reading lists.
Features: Razorpay subscriptions, admin panel, wishlists, typing indicators, pagination.
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
    get_user_subscription, search_all_genres,
)
from admin import (
    admin_dashboard, handle_admin_callback, handle_broadcast,
    is_admin, cmd_ban, cmd_unban, cmd_addbook, cmd_removebook, cmd_revenue,
)

logger = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

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
    price = SUBSCRIPTION_AMOUNT_PAISE / 100

    help_msg = (
        "🆘 <b>Book Shook Commands</b>\n\n"
        "📖 /start — Browse genres & discover books\n"
        "❓ /help — Show this help\n"
        "⭐ /premium — Check premium status\n"
        "🔍 /getpdf <code>&lt;book&gt;</code> — Find book PDF (premium)\n"
        "📋 /wishlist — Your saved books\n"
    )
    if PAYMENTS_ENABLED:
        help_msg += f"💳 /subscribe — Get premium (₹{price:.0f}/month)\n"
        help_msg += "❌ /cancel — Cancel subscription\n"
    help_msg += (
        f"\n📊 <b>{total_books:,}</b> books • <b>{genres_count}</b> genres\n\n"
        "<b>Premium Benefits:</b>\n"
        "🔍 Unlimited PDF searches\n"
        "📋 Personal reading wishlists\n"
        "⭐ Priority support\n"
    )
    await update.message.reply_text(help_msg, parse_mode="HTML")


async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    user_id = str(update.effective_user.id)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

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
                keyboard.append([InlineKeyboardButton("🔄 Renew — ₹99/month", callback_data="subscribe")])
        else:
            price = SUBSCRIPTION_AMOUNT_PAISE / 100
            text = (
                f"🔒 <b>You're not premium yet.</b>\n\n"
                f"<b>Premium Benefits:</b>\n"
                f"🔍 Unlimited PDF searches\n"
                f"📋 Personal wishlists\n"
                f"⭐ Priority support\n\n"
                f"💰 Only <b>₹{price:.0f}/month</b> — auto-renews via UPI/Card"
            )
            keyboard = []
            if PAYMENTS_ENABLED:
                keyboard.append([InlineKeyboardButton(f"💳 Subscribe — ₹{price:.0f}/month", callback_data="subscribe")])
            keyboard.append([InlineKeyboardButton(f"🎁 Free Trial ({FREE_TRIAL_DAYS} days)", callback_data="trial")])

        await update.message.reply_text(text, parse_mode="HTML",
                                         reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
    except Exception as e:
        logger.error("Error in /premium: %s", e)
        await update.message.reply_text("⚠️ Could not check status.")


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    if not PAYMENTS_ENABLED:
        await update.message.reply_text("⚠️ Payments are not configured yet.")
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await _create_subscription(update.effective_user.id, update.message)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    user_id = str(update.effective_user.id)
    sub = get_user_subscription(user_id)

    if not sub or sub.get("status") not in ("authenticated", "active"):
        await update.message.reply_text("⚠️ You don't have an active subscription.")
        return

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
            await update.message.reply_text("⚠️ Could not cancel. Contact admin.")
    except Exception as e:
        logger.error("Cancel error: %s", e)
        await update.message.reply_text("⚠️ Error cancelling subscription.")


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

    wait = _check_rate_limit(user_id)
    if wait:
        await query.answer(f"⏳ Wait {wait}s", show_alert=True)
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await query.edit_message_text(f"🔍 Searching PDF for <b>{book['title']}</b>...", parse_mode="HTML")

    links = await _search_pdf(f"{book['title']} {book['author']}")
    log_search(user_id, book["title"], search_type="pdf_button")

    if links:
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
    """Handle subscribe button click — create Razorpay subscription."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if not PAYMENTS_ENABLED:
        await query.edit_message_text("⚠️ Payments coming soon! Contact admin for premium.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        from payments import create_subscription
        sub = create_subscription(user_id)
        price = SUBSCRIPTION_AMOUNT_PAISE / 100

        await query.edit_message_text(
            f"💳 <b>Subscribe to BookShook Premium</b>\n\n"
            f"💰 ₹{price:.0f}/month — auto-renews via UPI/Card\n"
            f"✅ Cancel anytime with /cancel\n\n"
            f"👉 <b>Tap below to pay:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Pay ₹{price:.0f}/month", url=sub["short_url"])],
                [InlineKeyboardButton("⬅️ Back", callback_data="start_back")],
            ]),
        )
    except Exception as e:
        logger.error("Subscribe error: %s", e)
        await query.edit_message_text("⚠️ Could not create subscription. Contact admin.")


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


# ── Text Input Handler ────────────────────────────────────────────────────────

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_user(update)
    user_id = str(update.effective_user.id)

    if is_banned(user_id):
        return

    # Admin broadcast
    if context.user_data.get("awaiting_broadcast") and is_admin(update.effective_user.id):
        await handle_broadcast(update, context)
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

async def _create_subscription(user_id: int, message):
    """Create subscription and send payment link."""
    if not PAYMENTS_ENABLED:
        await message.reply_text("⚠️ Payments not configured yet.")
        return

    try:
        from payments import create_subscription
        sub = create_subscription(str(user_id))
        price = SUBSCRIPTION_AMOUNT_PAISE / 100
        await message.reply_text(
            f"💳 <b>BookShook Premium — ₹{price:.0f}/month</b>\n\n"
            f"Auto-renews monthly via UPI/Card.\n"
            f"Cancel anytime with /cancel\n\n"
            f"👉 Tap below to pay:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Pay ₹{price:.0f}/month", url=sub["short_url"])],
            ]),
        )
    except Exception as e:
        logger.error("Create subscription error: %s", e)
        await message.reply_text("⚠️ Payment setup failed. Contact admin.")


# ── Webhook Server ────────────────────────────────────────────────────────────

async def _razorpay_webhook_handler(request):
    """aiohttp handler for Razorpay webhooks."""
    try:
        raw_body = await request.text()
        signature = request.headers.get("X-Razorpay-Signature", "")

        from payments import verify_webhook_signature, handle_webhook_event
        if not verify_webhook_signature(raw_body, signature):
            logger.warning("Invalid Razorpay webhook signature")
            return web.json_response({"status": "invalid_signature"}, status=400)

        event_data = json.loads(raw_body)
        result = await handle_webhook_event(event_data)
        logger.info("Webhook processed: %s", result)
        return web.json_response({"status": "ok", **result})
    except Exception as e:
        logger.error("Webhook handler error: %s", e)
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

    # Text handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))

    # Error handler
    app.add_error_handler(error_handler)

    logger.info("Starting BookShook in %s mode...", BOT_MODE)

    if BOT_MODE == "webhook" and WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
        logger.info("Webhook: %s", WEBHOOK_URL)

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=f"/webhook/{TELEGRAM_BOT_TOKEN}",
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()