# BookShook — Premium Telegram Book Discovery & PDF Bot

BookShook is a professional, high-performance Telegram bot that provides users with a premium book discovery experience, Open Library book descriptions, wishlist management, and fast PDF search. It features an automated recurring monthly subscription model powered by Razorpay, a comprehensive admin panel for dashboard analytics, and is optimized for 24/7 hosting on Render's free tier.

---

## 🚀 Key Features & Advantages

### 1. Automated Razorpay Subscriptions (₹149/month)
* **Seamless Payment Flow**: Generating secure one-click checkout links for card payments and UPI auto-debit mandates directly inside Telegram.
* **Auto-Provisioning**: Razorpay webhooks authenticate payments, automatically granting 30 days of premium access on successful checkout and extending access on monthly renewals.
* **Subscription Management**: Users can monitor their subscription and check details via `/premium`, or cancel anytime using `/cancel`.

### 2. Conversational UX & Responsive Controls
* **Dynamic Typing Indicators**: Simulates human responsiveness by displaying a *"typing..."* status before each response.
* **Inline Keyboards & Navigation**: Zero command-typing required for browsing. Users can navigate genres, view details, search, and manage lists entirely via responsive buttons.
* **Paginated Book Lists**: Displays books using cleanly formatted lists with `◀️ Page X/Y ▶️` buttons, avoiding chat window spam.
* **Personal Wishlist System**: Tap `➕ Wishlist` to save books for later reading, accessible instantly via `/wishlist` or menu buttons.

### 3. Comprehensive Admin Control Panel (`/admin`)
* **Real-time Analytics**: Displays total users, active premium members, active subscriptions, total books/genres, and 24h search volume.
* **Financial Auditing**: Monitors MRR (Monthly Recurring Revenue), 1-day/7-day/30-day earnings, failed payments, and visual progress bars towards the ₹50K and ₹1L/month revenue targets.
* **Broadcast Engine**: Administrators can broadcast messages to all active users with a real-time progress counter showing success/failure rates.
* **Database & User Mgmt**: Commands for manual database management (`/addbook`, `/removebook`), granting/revoking premium status (`/addpremium`, `/removepremium`), and user moderation (`/ban`, `/unban`).

### 4. Advanced Production Architecture
* **Modular Codebase**: Split cleanly into independent modules:
  * `BookShook.py` — Bot interface, update routing, and webhook handling.
  * `database.py` — SQLite database abstraction with WAL mode concurrency and indexing.
  * `payments.py` — Razorpay API integration and webhook event processor.
  * `admin.py` — Admin dashboard and management menus.
  * `config.py` — Environment variables validator.
* **Dual Bot Modes**: Runs in `polling` mode for local testing or `webhook` mode for high-throughput production deployment.
* **Resource Optimization**: Built-in 30-second rate-limiting for PDF searches prevents API abuse and controls search quota usage.

---

## 🛠️ Tech Stack & Dependencies

* **Core Runtime**: Python 3.12+ (tested up to Python 3.14 on Render)
* **Bot Framework**: `python-telegram-bot[webhooks]` v21.6 (built on `httpx` & `tornado` web framework)
* **Web Server**: `aiohttp` (high-performance async web framework for handling incoming payment/bot webhook requests)
* **Database**: SQLite3 (optimized with Indexing and Write-Ahead Logging `WAL` mode)
* **Payment Processor**: `razorpay` SDK v1.4.2

---

## ⚙️ Local Setup Guide

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/suryanshsugara/Telegram-Bots.git
   cd "Telegram-Bots/Book Shook"
   ```

2. **Configure Environment Variables**:
   Create a `.env` file in the root directory:
   ```ini
   TELEGRAM_BOT_TOKEN=your_bot_token
   GOOGLE_API_KEY=your_google_custom_search_api_key
   GOOGLE_CSE_ID=your_custom_search_engine_id
   ADMIN_USER_ID=your_telegram_numeric_id
   
   # Razorpay Configuration
   RAZORPAY_KEY_ID=your_razorpay_live_or_test_key_id
   RAZORPAY_KEY_SECRET=your_razorpay_secret
   RAZORPAY_WEBHOOK_SECRET=your_configured_webhook_secret
   SUBSCRIPTION_AMOUNT_PAISE=14900
   
   # Modes: "polling" for local, "webhook" for Render
   BOT_MODE=polling
   ```

3. **Install Requirements & Seed Database**:
   ```bash
   pip install -r requirements.txt
   python seed_data.py
   ```
   *The database seeder will create `books.db` and insert 755 high-rated titles across 12 genres.*

4. **Run the Bot Locally**:
   ```bash
   python BookShook.py
   ```

---

## 🌐 Production Deployment (Render)

1. **Push Changes**:
   Ensure all changes are pushed to your GitHub repository on the target branch (e.g. `bookshook`).

2. **Run One-Click Deploy Script**:
   ```bash
   python deploy_render.py
   ```
   *This script uses the Render REST API to build your service, register the Telegram Webhook, inject environment variables, and trigger the deployment.*

3. **Link Razorpay Webhooks**:
   Log in to your [Razorpay Dashboard](https://dashboard.razorpay.com) -> **Settings** -> **Webhooks**:
   * **URL**: `https://<your-render-app-subdomain>.onrender.com/razorpay/webhook`
   * **Secret**: The value of `RAZORPAY_WEBHOOK_SECRET` in your `.env`.
   * **Active Events**: `subscription.authenticated`, `subscription.activated`, `subscription.charged`, `subscription.cancelled`, `payment.captured`, `payment.failed`.

---

## 📖 Command Reference

### User Commands
* `/start` — Initializes onboarding, loads genre menu, and triggers premium trial CTA.
* `/help` — Displays commands, instructions, and subscriber benefits list.
* `/premium` — Check subscription status, renew/subscribe, and view trial status.
* `/subscribe` — Generates a direct payment link to subscribe for ₹149/month.
* `/cancel` — Cancels auto-renewal of active monthly subscription.
* `/wishlist` — Shows your saved reading list with one-click book removals.
* `/getpdf <book>` — Perform PDF search (unlimited for premium users).

### Admin Commands
* `/admin` — Opens the interactive inline-keyboard admin dashboard.
* `/revenue` — Displays quick revenue stats (MRR, 30-day earnings).
* `/addbook Title | Author | Genre` — Add a new book to the database.
* `/removebook <book_id>` — Delete a book from the library.
* `/addpremium <user_id> [days]` — Manually grant premium access to a user.
* `/removepremium <user_id>` — Manually revoke premium access.
* `/ban <user_id>` — Block a user from using the bot.
* `/unban <user_id>` — Unblock a banned user.
