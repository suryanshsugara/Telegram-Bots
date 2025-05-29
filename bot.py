import logging
import random
import aiohttp
import os
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# Load sensitive data from environment variables
TOKEN = os.getenv('BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
CSE_ID = os.getenv('CSE_ID')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '0'))

premium_users = {str(ADMIN_USER_ID): 9999999999}

# Example book lists (expand as needed)
GENRE_BOOKS = {
    "Fantasy": [
        "The Lord of the Rings - J.R.R. Tolkien",
        "A Game of Thrones - George R.R. Martin",
        "Harry Potter and the Sorcerer's Stone - J.K. Rowling",
        # ... more books ...
    ],
    "Sci-Fi": [
        "Dune - Frank Herbert",
        "Ender's Game - Orson Scott Card",
        "Foundation - Isaac Asimov",
        # ... more books ...
    ],
    # ... other genres ...
}

GENRES = list(GENRE_BOOKS.keys())

COMMAND_ALIASES = {
    "/st": "/start", "/sta": "/start", "/star": "/start",
    "/get": "/getpdf", "/getp": "/getpdf", "/getpdf": "/getpdf",
    "/prem": "/premium", "/premium": "/premium",
    "/hel": "/help", "/help": "/help",
    "/addprem": "/addpremium", "/addpremium": "/addpremium",
}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def safe_sample(books, n):
    return random.sample(books, min(len(books), n)) if books else []

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_msg = (
        "🆘 <b>Book Shook Commands:</b>\n\n"
        "/start - Start and pick a genre\n"
        "/help - Show this help message\n"
        "/premium - Check your premium status\n"
        "/addpremium &lt;user_id&gt; - [Admin] Grant premium access\n"
        "/getpdf &lt;book name&gt; - Get a book's PDF link (premium only)\n\n"
        "Pick a genre, then search by author, keyword, or get random suggestions!"
    )
    await update.message.reply_text(help_msg, parse_mode="HTML")

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in premium_users:
        await update.message.reply_text("✅ <b>You are a Premium user.</b>", parse_mode="HTML")
    else:
        await update.message.reply_text("🔒 <b>You are not a premium user.</b>", parse_mode="HTML")

async def add_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Only the admin can add premium users.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addpremium <user_id>")
        return
    user_id = context.args[0]
    premium_users[user_id] = 9999999999
    await update.message.reply_text(f"User {user_id} added to premium list.")

async def getpdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in premium_users:
        await update.message.reply_text("🔒 <b>This feature is for premium users only.</b>", parse_mode="HTML")
        return
    if not context.args:
        await update.message.reply_text("Usage: /getpdf <book name>")
        return
    book_name = " ".join(context.args)
    await update.message.reply_text("🔍 Searching for PDF...")
    async with aiohttp.ClientSession() as session:
        params = {
            "key": GOOGLE_API_KEY,
            "cx": CSE_ID,
            "q": f"{book_name} filetype:pdf",
            "num": 3
        }
        async with session.get("https://www.googleapis.com/customsearch/v1", params=params) as res:
            data = await res.json()
            links = [item["link"] for item in data.get("items", []) if "pdf" in item["link"]]
    if links:
        await update.message.reply_text(
            f'📖 <b>{book_name}</b>\n<a href="{links[0]}">Download PDF</a>',
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ PDF not found.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"📚 {g}", callback_data=f"genre:{g}")] for g in GENRES]
    await update.message.reply_text(
        "✨ <b>Welcome to Book Shook!</b>\n\nChoose a genre to get started:\n\n"
        "🆘 Use /help to see all commands.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    genre = query.data.split(":", 1)[1]
    context.user_data["genre"] = genre
    buttons = [
        [InlineKeyboardButton("🔍 Search by Author", callback_data="search:author")],
        [InlineKeyboardButton("🎲 Random 5 Books", callback_data="search:random")],
        [InlineKeyboardButton("🔑 Search by Keyword", callback_data="search:keyword")]
    ]
    await query.edit_message_text(
        f"✨ <b>{genre}</b> selected!\nHow would you like to search?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    search_type = query.data.split(":", 1)[1]
    context.user_data["search_type"] = search_type
    genre = context.user_data["genre"]
    if search_type == "random":
        books = safe_sample(GENRE_BOOKS[genre], 5)
        await query.edit_message_text(
            "🎲 <b>Random Picks:</b>\n" + "\n".join([f"• {b}" for b in books]),
            parse_mode="HTML"
        )
        return
    prompt = "👤 Enter author name:" if search_type == "author" else "🔑 Enter keyword:"
    await query.edit_message_text(prompt)
    context.user_data["awaiting_input"] = True

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_input"):
        return
    user_input = update.message.text.lower()
    genre = context.user_data["genre"]
    books = GENRE_BOOKS[genre]
    if context.user_data["search_type"] == "author":
        results = [b for b in books if user_input in b.lower()]
    else:
        results = [b for b in books if user_input in b.lower()]
    if not results:
        await update.message.reply_text("❌ No matches found. Try another search or /start.")
        return
    selected = safe_sample(results, 5)
    keyboard = [[InlineKeyboardButton(b, callback_data=f"pdf:{b}")] for b in selected]
    await update.message.reply_text(
        "🔍 <b>Search results:</b>\nTap a book to get its PDF (premium only):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["awaiting_input"] = False

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    book_title = query.data.split(":", 1)[1].split(" - ")[0]
    if str(query.from_user.id) not in premium_users:
        await query.answer("🔒 Premium feature!", show_alert=True)
        return
    async with aiohttp.ClientSession() as session:
        params = {
            "key": GOOGLE_API_KEY, 
            "cx": CSE_ID,
            "q": f"{book_title} filetype:pdf",
            "num": 3
        }
        async with session.get("https://www.googleapis.com/customsearch/v1", params=params) as res:
            data = await res.json()
            links = [item["link"] for item in data.get("items", []) if "pdf" in item["link"]]
    if links:
        await query.edit_message_text(
            f'📖 <b>{book_title}</b>\n<a href="{links[0]}">Download PDF</a>',
            parse_mode="HTML"
        )
    else:
        await query.edit_message_text("❌ PDF not found.")

async def dynamic_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip().split()[0].lower()
    args = update.message.text.strip().split()[1:]
    context.args = args
    if text in COMMAND_ALIASES:
        full_command = COMMAND_ALIASES[text]
        if full_command == "/start":
            await start(update, context)
        elif full_command == "/help":
            await help_command(update, context)
        elif full_command == "/premium":
            await premium(update, context)
        elif full_command == "/addpremium":
            await add_premium(update, context)
        elif full_command == "/getpdf":
            await getpdf(update, context)
        else:
            await update.message.reply_text("Unknown command.")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("addpremium", add_premium))
    app.add_handler(CommandHandler("getpdf", getpdf))
    app.add_handler(CallbackQueryHandler(handle_genre, pattern=r"^genre:"))
    app.add_handler(CallbackQueryHandler(handle_search, pattern=r"^search:"))
    app.add_handler(CallbackQueryHandler(handle_pdf, pattern=r"^pdf:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))
    partial_command_regex = r"^/(st|sta|star|get|getp|getpdf|prem|premium|hel|help|addprem|addpremium)\b"
    pattern = re.compile(partial_command_regex, re.IGNORECASE)
    app.add_handler(MessageHandler(filters.Regex(pattern), dynamic_command_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
  
