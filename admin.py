"""
BookShook Bot — Telegram Admin Panel
Rich inline-keyboard admin interface for managing users, books, revenue, and settings.
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from config import ADMIN_USER_ID, PAYMENTS_ENABLED, SUBSCRIPTION_AMOUNT_PAISE
from database import (
    get_dashboard_stats, list_premium_users, is_premium,
    add_premium_user, remove_premium_user,
    get_revenue_stats, get_all_user_ids, get_total_user_count,
    get_active_premium_count, get_book_count, get_genres,
    get_searches_today, ban_user, unban_user,
    add_book_to_db, remove_book_from_db, get_book_by_id,
    search_all_genres, get_user_payments, get_user_subscription,
    get_active_subscription_count,
)

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    """Check if user is the admin."""
    return user_id == ADMIN_USER_ID


def _format_inr(paise: int) -> str:
    """Format paise to INR string."""
    rupees = paise / 100
    if rupees >= 100000:
        return f"₹{rupees/100000:.1f}L"
    elif rupees >= 1000:
        return f"₹{rupees/1000:.1f}K"
    return f"₹{rupees:,.0f}"


# ── Main Dashboard ────────────────────────────────────────────────────────────

async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the main admin dashboard with stats and navigation."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admin access only.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    stats = get_dashboard_stats()
    rev_30d = stats["revenue_30d"]
    rev_7d = stats["revenue_7d"]
    rev_1d = stats["revenue_1d"]

    text = (
        "📊 <b>BookShook Admin Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Total Users: <b>{stats['total_users']:,}</b>\n"
        f"⭐ Active Premium: <b>{stats['active_premium']}</b>\n"
        f"🔄 Active Subscriptions: <b>{stats['active_subscriptions']}</b>\n"
        f"📚 Books in DB: <b>{stats['total_books']:,}</b>\n"
        f"📂 Genres: <b>{stats['total_genres']}</b>\n"
        f"🔍 Searches (24h): <b>{stats['searches_today']:,}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>Revenue</b>\n"
        f"  Today: <b>{_format_inr(rev_1d['total_revenue_paise'])}</b>"
        f" ({rev_1d['payment_count']} payments)\n"
        f"  7 Days: <b>{_format_inr(rev_7d['total_revenue_paise'])}</b>"
        f" ({rev_7d['payment_count']} payments)\n"
        f"  30 Days: <b>{_format_inr(rev_30d['total_revenue_paise'])}</b>"
        f" ({rev_30d['payment_count']} payments)\n"
        f"  All-Time: <b>{_format_inr(rev_30d['all_time_revenue_paise'])}</b>\n"
        f"  Failed (30d): <b>{rev_30d['failed_count']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = [
        [
            InlineKeyboardButton("👥 Users", callback_data="adm:users"),
            InlineKeyboardButton("💰 Revenue", callback_data="adm:revenue"),
        ],
        [
            InlineKeyboardButton("📚 Books", callback_data="adm:books"),
            InlineKeyboardButton("⭐ Premium", callback_data="adm:premium"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="adm:broadcast"),
            InlineKeyboardButton("🔄 Refresh", callback_data="adm:refresh"),
        ],
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await update.message.reply_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


# ── Admin Callback Router ────────────────────────────────────────────────────

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all admin panel callbacks."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("🚫 Admin only!", show_alert=True)
        return

    action = query.data.split(":", 1)[1] if ":" in query.data else ""

    try:
        if action == "refresh":
            await admin_dashboard(update, context)

        elif action == "users":
            await _show_user_panel(query)

        elif action == "revenue":
            await _show_revenue_panel(query)

        elif action == "books":
            await _show_books_panel(query)

        elif action == "premium":
            await _show_premium_panel(query)

        elif action == "broadcast":
            await _show_broadcast_prompt(query, context)

        elif action == "back":
            await admin_dashboard(update, context)

        else:
            await query.answer("Unknown action", show_alert=True)

    except Exception as e:
        logger.error("Admin callback error: %s", e)
        await query.answer(f"Error: {str(e)[:100]}", show_alert=True)


# ── User Management Panel ────────────────────────────────────────────────────

async def _show_user_panel(query):
    """Show user management panel."""
    total = get_total_user_count()
    premium = get_active_premium_count()
    subs = get_active_subscription_count()

    text = (
        "👥 <b>User Management</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Total Users: <b>{total:,}</b>\n"
        f"⭐ Active Premium: <b>{premium}</b>\n"
        f"🔄 Active Subs: <b>{subs}</b>\n"
        f"🆓 Free Users: <b>{total - premium}</b>\n\n"
        "💡 Use commands to manage:\n"
        "<code>/addpremium &lt;user_id&gt; [days]</code>\n"
        "<code>/removepremium &lt;user_id&gt;</code>\n"
        "<code>/ban &lt;user_id&gt;</code>\n"
        "<code>/unban &lt;user_id&gt;</code>\n"
    )

    keyboard = [
        [InlineKeyboardButton("⭐ List Premium Users", callback_data="adm:premium")],
        [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm:back")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


# ── Revenue Panel ────────────────────────────────────────────────────────────

async def _show_revenue_panel(query):
    """Show detailed revenue analytics."""
    rev_1d = get_revenue_stats(1)
    rev_7d = get_revenue_stats(7)
    rev_30d = get_revenue_stats(30)
    rev_90d = get_revenue_stats(90)

    price_inr = SUBSCRIPTION_AMOUNT_PAISE / 100
    active_subs = get_active_subscription_count()
    mrr = active_subs * price_inr

    text = (
        "💰 <b>Revenue Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📈 <b>Monthly Recurring Revenue (MRR)</b>\n"
        f"  Active Subs: <b>{active_subs}</b> × ₹{price_inr:.0f} = <b>₹{mrr:,.0f}/mo</b>\n\n"
        "📊 <b>Revenue Breakdown</b>\n"
        f"  Today:   <b>{_format_inr(rev_1d['total_revenue_paise'])}</b>"
        f" ({rev_1d['payment_count']} txns, {rev_1d['failed_count']} failed)\n"
        f"  7 Days:  <b>{_format_inr(rev_7d['total_revenue_paise'])}</b>"
        f" ({rev_7d['payment_count']} txns)\n"
        f"  30 Days: <b>{_format_inr(rev_30d['total_revenue_paise'])}</b>"
        f" ({rev_30d['payment_count']} txns)\n"
        f"  90 Days: <b>{_format_inr(rev_90d['total_revenue_paise'])}</b>"
        f" ({rev_90d['payment_count']} txns)\n\n"
        f"  All-Time: <b>{_format_inr(rev_30d['all_time_revenue_paise'])}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>Targets</b>\n"
        f"  ₹50K/mo needs: <b>{int(50000/price_inr)}</b> subscribers\n"
        f"  ₹1L/mo needs:  <b>{int(100000/price_inr)}</b> subscribers\n"
        f"  Progress: <b>{mrr/50000*100:.1f}%</b> of ₹50K target\n"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="adm:revenue")],
        [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm:back")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


# ── Premium Panel ────────────────────────────────────────────────────────────

async def _show_premium_panel(query):
    """Show premium users list."""
    users = list_premium_users()

    if not users:
        text = "⭐ <b>Premium Users</b>\n\nNo premium users found."
    else:
        text = f"⭐ <b>Premium Users ({len(users)})</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for u in users[:30]:
            active = "✅" if is_premium(u["user_id"]) else "❌"
            method_emoji = {"subscription": "🔄", "manual": "👑", "trial": "🎁"}.get(u.get("method", ""), "📝")
            text += (
                f"{active} <code>{u['user_id']}</code>\n"
                f"   {method_emoji} {u.get('method', 'manual')} • expires {u['expiry'][:10]}\n"
            )
        if len(users) > 30:
            text += f"\n... and {len(users) - 30} more"

    keyboard = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="adm:premium")],
        [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm:back")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


# ── Books Panel ──────────────────────────────────────────────────────────────

async def _show_books_panel(query):
    """Show book database statistics."""
    genres = get_genres()
    total = get_book_count()

    text = f"📚 <b>Book Database ({total:,} books)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for g in genres:
        count = get_book_count(g)
        bar = "█" * min(count // 10, 15) + "░" * max(0, 15 - count // 10)
        text += f"  {g}: <b>{count}</b> {bar}\n"

    text += (
        "\n💡 <b>Manage:</b>\n"
        "<code>/addbook Title | Author | Genre</code>\n"
        "<code>/removebook &lt;book_id&gt;</code>\n"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="adm:books")],
        [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm:back")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


# ── Broadcast ────────────────────────────────────────────────────────────────

async def _show_broadcast_prompt(query, context):
    """Prompt admin to type broadcast message."""
    context.user_data["awaiting_broadcast"] = True

    text = (
        "📢 <b>Broadcast Message</b>\n\n"
        "Type your message below. It will be sent to ALL users.\n\n"
        "⚠️ Type <code>/cancelbroadcast</code> to cancel.\n"
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm:back")]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send broadcast message to all users."""
    if not is_admin(update.effective_user.id):
        return

    message = update.message.text
    user_ids = get_all_user_ids()
    sent = 0
    failed = 0

    status_msg = await update.message.reply_text(
        f"📢 Broadcasting to {len(user_ids)} users..."
    )

    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"📢 <b>BookShook Announcement</b>\n\n{message}",
                parse_mode="HTML",
            )
            sent += 1
        except Exception:
            failed += 1

        # Update progress every 50 messages
        if (sent + failed) % 50 == 0:
            try:
                await status_msg.edit_text(
                    f"📢 Broadcasting... {sent + failed}/{len(user_ids)} "
                    f"(✅ {sent} / ❌ {failed})"
                )
            except Exception:
                pass

    await status_msg.edit_text(
        f"📢 <b>Broadcast Complete</b>\n\n"
        f"✅ Sent: {sent}\n❌ Failed: {failed}\n📊 Total: {len(user_ids)}",
        parse_mode="HTML",
    )
    context.user_data["awaiting_broadcast"] = False


# ── Admin Command Handlers ───────────────────────────────────────────────────

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban a user."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <code>&lt;user_id&gt;</code>", parse_mode="HTML")
        return
    ban_user(context.args[0])
    await update.message.reply_text(f"🚫 User <code>{context.args[0]}</code> banned.", parse_mode="HTML")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unban a user."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <code>&lt;user_id&gt;</code>", parse_mode="HTML")
        return
    unban_user(context.args[0])
    await update.message.reply_text(f"✅ User <code>{context.args[0]}</code> unbanned.", parse_mode="HTML")


async def cmd_addbook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a book: /addbook Title | Author | Genre"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return
    text = update.message.text.replace("/addbook", "").strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3:
        await update.message.reply_text(
            "Usage: /addbook <code>Title | Author | Genre</code>\n"
            "Example: /addbook Atomic Habits | James Clear | Self-help",
            parse_mode="HTML",
        )
        return
    title, author, genre = parts
    book_id = add_book_to_db(title, author, genre)
    await update.message.reply_text(
        f"✅ Added: <b>{title}</b> by {author} → {genre} (ID: {book_id})",
        parse_mode="HTML",
    )


async def cmd_removebook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a book: /removebook <book_id>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removebook <code>&lt;book_id&gt;</code>", parse_mode="HTML")
        return
    try:
        book_id = int(context.args[0])
        book = get_book_by_id(book_id)
        if not book:
            await update.message.reply_text("⚠️ Book not found.")
            return
        remove_book_from_db(book_id)
        await update.message.reply_text(
            f"✅ Removed: <b>{book['title']}</b> by {book['author']} (ID: {book_id})",
            parse_mode="HTML",
        )
    except ValueError:
        await update.message.reply_text("⚠️ Invalid book ID.")


async def cmd_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick revenue stats command."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return

    rev = get_revenue_stats(30)
    active_subs = get_active_subscription_count()
    price = SUBSCRIPTION_AMOUNT_PAISE / 100
    mrr = active_subs * price

    await update.message.reply_text(
        f"💰 <b>Quick Revenue</b>\n\n"
        f"MRR: <b>₹{mrr:,.0f}</b> ({active_subs} subs × ₹{price:.0f})\n"
        f"30d Revenue: <b>{_format_inr(rev['total_revenue_paise'])}</b>\n"
        f"All-Time: <b>{_format_inr(rev['all_time_revenue_paise'])}</b>\n\n"
        f"Use /admin for full dashboard.",
        parse_mode="HTML",
    )
