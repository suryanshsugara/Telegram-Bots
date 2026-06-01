# BookShook Bot — Admin Commands and Capabilities

This document provides a guide for using the admin panel, commands, and options available for managing the BookShook Telegram bot. These capabilities are only accessible to the account configured under `ADMIN_USER_ID`.

---

## 1. Interactive Admin Dashboard (`/admin`)

Type `/admin` in the Telegram bot chat to open the interactive panel. This dashboard displays live database stats and provides quick inline keyboard navigation:

### Live Statistics Shown
* **Total Users**: The total number of users who have registered or interacted with the bot.
* **Active Premium**: The number of users currently holding active premium privileges.
* **Active Subscriptions**: The number of users with an active automated subscription via Dodo Payments.
* **Books in DB**: Total count of unique book entries currently stored in the database.
* **Genres**: Count of unique book genres.
* **Searches (24h)**: Count of search queries performed by users in the last 24 hours.
* **Revenue Breakdown**:
  * **Today**: Total revenue received today and the number of payments.
  * **7 Days**: Accumulated revenue over the last week.
  * **30 Days**: Accumulated revenue over the last 30 days.
  * **All-Time**: Total revenue collected since launch.
  * **Failed (30d)**: Number of failed subscription transactions in the last 30 days.

### Dashboard Quick Menus
* **👥 Users Menu**: Access premium user listings and check status statistics.
* **💰 Revenue Menu**: View detailed revenue tracking, Monthly Recurring Revenue (MRR) projection, and progress metrics toward target revenue (e.g. ₹50k or ₹1L per month targets).
* **📚 Books Menu**: View database statistics by genre with custom ASCII distribution charts.
* **⭐ Premium Menu**: Displays a lists of premium users, their registration method (subscription `🔄`, manual `👑`, trial `🎁`), and expiry dates.
* **📢 Broadcast Menu**: Allows sending a message to **every user** in the database.
* **🔄 Refresh**: Re-query the database to update the shown statistics.

---

## 2. User & Subscription Commands

Admin commands can be run directly from the chat box:

* **Grant Premium Access**
  ```text
  /addpremium <user_id> [days]
  ```
  Grants premium privileges to the specified user ID. If `[days]` is not provided, it defaults to `30` days.
  * *Example:* `/addpremium 123456789 90`

* **Revoke Premium Access**
  ```text
  /removepremium <user_id>
  ```
  Immediately revokes premium status for the specified user ID.
  * *Example:* `/removepremium 123456789`

* **List Premium Users**
  ```text
  /listpremium
  ```
  Lists the first 50 active premium users along with their expiry dates.

* **Ban a User**
  ```text
  /ban <user_id>
  ```
  Bans the user from interacting with the bot.
  * *Example:* `/ban 123456789`

* **Unban a User**
  ```text
  /unban <user_id>
  ```
  Restores access for a previously banned user ID.
  * *Example:* `/unban 123456789`

---

## 3. Book Database Management

Manage the bot's catalog directly from Telegram:

* **Add a Book**
  ```text
  /addbook Title | Author | Genre
  ```
  Registers a new book in the database. Use vertical pipes (`|`) to separate the title, author, and genre.
  * *Example:* `/addbook Atomic Habits | James Clear | Self-help`

* **Remove a Book**
  ```text
  /removebook <book_id>
  ```
  Permanently deletes a book from the catalog by its numeric database ID.
  * *Example:* `/removebook 42`

---

## 4. Financial & Analytics Commands

* **Quick Revenue Summary**
  ```text
  /revenue
  ```
  Outputs a quick text summary of revenue analytics (MRR, 30d, all-time, etc.).

---

## 5. Manual Payment Verification (UPI QR Fallback)

When a user submits a manual payment verification screenshot/receipt or types their UTR reference number, the bot automatically forwards the receipt to the admin's chat.

The message will include two inline action buttons:
1. **✅ Approve (`pay_appr:<user_id>:<amount_inr>`)**:
   * Creates a payment transaction record in the database using the converted INR amount.
   * Grants the user 30 days of Premium access.
   * Automatically sends a notification message to the user letting them know their transaction is verified and premium has been enabled.
2. **❌ Reject (`pay_rej:<user_id>`)**:
   * Rejects the verification attempt.
   * Automatically sends a notification to the user letting them know verification failed and asking them to contact support.
