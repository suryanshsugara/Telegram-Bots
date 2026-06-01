"""
BookShook Bot — Database Layer
SQLite-backed storage for books, genres, premium users, payments, wishlists, and analytics.
"""

import sqlite3
import random
import json
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager

from config import DATABASE_PATH

logger = logging.getLogger(__name__)


# ── Connection Management ─────────────────────────────────────────────────────

@contextmanager
def get_db():
    """Context manager for database connections with WAL mode for concurrency."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                description TEXT,
                rating REAL,
                UNIQUE(title, author)
            );

            CREATE TABLE IF NOT EXISTS genres (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS book_genres (
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
                PRIMARY KEY (book_id, genre_id)
            );

            CREATE TABLE IF NOT EXISTS premium_users (
                user_id TEXT PRIMARY KEY,
                expiry TEXT NOT NULL,
                added_by TEXT,
                method TEXT DEFAULT 'manual',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                query TEXT NOT NULL,
                genre TEXT,
                search_type TEXT,
                timestamp TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                razorpay_payment_id TEXT UNIQUE,
                razorpay_subscription_id TEXT,
                amount INTEGER NOT NULL,
                currency TEXT DEFAULT 'INR',
                status TEXT NOT NULL,
                method TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL UNIQUE,
                razorpay_subscription_id TEXT UNIQUE,
                razorpay_plan_id TEXT,
                status TEXT NOT NULL,
                short_url TEXT,
                current_start TEXT,
                current_end TEXT,
                total_count INTEGER,
                paid_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS wishlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                book_id INTEGER NOT NULL REFERENCES books(id),
                added_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, book_id)
            );

            CREATE TABLE IF NOT EXISTS bot_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                user_id TEXT,
                data TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_banned INTEGER DEFAULT 0,
                first_seen TEXT DEFAULT (datetime('now')),
                last_seen TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_book_genres_genre ON book_genres(genre_id);
            CREATE INDEX IF NOT EXISTS idx_book_genres_book ON book_genres(book_id);
            CREATE INDEX IF NOT EXISTS idx_user_searches_user ON user_searches(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);
            CREATE INDEX IF NOT EXISTS idx_wishlists_user ON wishlists(user_id);
            CREATE INDEX IF NOT EXISTS idx_bot_stats_type ON bot_stats(event_type);
            CREATE INDEX IF NOT EXISTS idx_bot_stats_date ON bot_stats(created_at);
        """)
    logger.info("Database initialized at %s", DATABASE_PATH)


# ── User Tracking ─────────────────────────────────────────────────────────────

def upsert_user(user_id: str, username: str = None, first_name: str = None, last_name: str = None):
    """Track user on every interaction."""
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name),
                    last_name = COALESCE(excluded.last_name, users.last_name),
                    last_seen = datetime('now')
            """, (str(user_id), username, first_name, last_name))
    except Exception as e:
        logger.warning("Failed to upsert user: %s", e)


def is_banned(user_id: str) -> bool:
    """Check if a user is banned."""
    with get_db() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (str(user_id),)).fetchone()
    return bool(row and row["is_banned"])


def ban_user(user_id: str):
    """Ban a user."""
    with get_db() as conn:
        conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (str(user_id),))


def unban_user(user_id: str):
    """Unban a user."""
    with get_db() as conn:
        conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (str(user_id),))


def get_all_user_ids() -> list[str]:
    """Get all user IDs for broadcasting."""
    with get_db() as conn:
        rows = conn.execute("SELECT user_id FROM users WHERE is_banned = 0").fetchall()
    return [row["user_id"] for row in rows]


def get_total_user_count() -> int:
    """Total registered users."""
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    return row[0] if row else 0


# ── Genre Operations ──────────────────────────────────────────────────────────

def get_genres() -> list[str]:
    """Return all genre names sorted alphabetically."""
    with get_db() as conn:
        rows = conn.execute("SELECT name FROM genres ORDER BY name").fetchall()
    return [row["name"] for row in rows]


def genre_exists(genre_name: str) -> bool:
    """Check if a genre exists."""
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM genres WHERE name = ?", (genre_name,)).fetchone()
    return row is not None


# ── Book Operations ───────────────────────────────────────────────────────────

def get_books_by_genre(genre: str, limit: int | None = None, offset: int = 0) -> list[dict]:
    """Get books in a genre with pagination support."""
    query = """
        SELECT b.id, b.title, b.author
        FROM books b
        JOIN book_genres bg ON b.id = bg.book_id
        JOIN genres g ON bg.genre_id = g.id
        WHERE g.name = ?
        ORDER BY b.title
    """
    params: list = [genre]
    if limit:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_book_by_id(book_id: int) -> dict | None:
    """Get a single book by its ID."""
    with get_db() as conn:
        row = conn.execute("SELECT id, title, author FROM books WHERE id = ?", (book_id,)).fetchone()
    return dict(row) if row else None


def search_by_author(genre: str, author_name: str) -> list[dict]:
    """Search books in a genre by author name."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT b.id, b.title, b.author
            FROM books b
            JOIN book_genres bg ON b.id = bg.book_id
            JOIN genres g ON bg.genre_id = g.id
            WHERE g.name = ? AND LOWER(b.author) LIKE ?
            ORDER BY b.title
        """, (genre, f"%{author_name.lower()}%")).fetchall()
    return [dict(row) for row in rows]


def search_by_keyword(genre: str, keyword: str) -> list[dict]:
    """Search books in a genre by keyword in title or author."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT b.id, b.title, b.author
            FROM books b
            JOIN book_genres bg ON b.id = bg.book_id
            JOIN genres g ON bg.genre_id = g.id
            WHERE g.name = ? AND (LOWER(b.title) LIKE ? OR LOWER(b.author) LIKE ?)
            ORDER BY b.title
        """, (genre, f"%{keyword.lower()}%", f"%{keyword.lower()}%")).fetchall()
    return [dict(row) for row in rows]


def search_all_genres(keyword: str) -> list[dict]:
    """Search all books across all genres."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT b.id, b.title, b.author
            FROM books b
            WHERE LOWER(b.title) LIKE ? OR LOWER(b.author) LIKE ?
            ORDER BY b.title LIMIT 20
        """, (f"%{keyword.lower()}%", f"%{keyword.lower()}%")).fetchall()
    return [dict(row) for row in rows]


def random_books(genre: str, count: int = 5) -> list[dict]:
    """Get random books from a genre."""
    all_books = get_books_by_genre(genre)
    if not all_books:
        return []
    return random.sample(all_books, min(len(all_books), count))


def get_book_count(genre: str | None = None) -> int:
    """Get total book count, optionally filtered by genre."""
    with get_db() as conn:
        if genre:
            row = conn.execute("""
                SELECT COUNT(DISTINCT b.id)
                FROM books b
                JOIN book_genres bg ON b.id = bg.book_id
                JOIN genres g ON bg.genre_id = g.id
                WHERE g.name = ?
            """, (genre,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM books").fetchone()
    return row[0] if row else 0


def add_book_to_db(title: str, author: str, genre_name: str) -> int:
    """Add a book and link to genre. Returns book ID."""
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (genre_name,))
        conn.execute("INSERT OR IGNORE INTO books (title, author) VALUES (?, ?)", (title.strip(), author.strip()))
        book_row = conn.execute("SELECT id FROM books WHERE title = ? AND author = ?", (title.strip(), author.strip())).fetchone()
        genre_row = conn.execute("SELECT id FROM genres WHERE name = ?", (genre_name,)).fetchone()
        if book_row and genre_row:
            conn.execute("INSERT OR IGNORE INTO book_genres (book_id, genre_id) VALUES (?, ?)",
                         (book_row["id"], genre_row["id"]))
        return book_row["id"] if book_row else 0


def remove_book_from_db(book_id: int) -> bool:
    """Remove a book by ID."""
    with get_db() as conn:
        conn.execute("DELETE FROM book_genres WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM wishlists WHERE book_id = ?", (book_id,))
        result = conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        return result.rowcount > 0


# ── Premium User Operations ──────────────────────────────────────────────────

def is_premium(user_id: str) -> bool:
    """Check if a user has an active premium subscription."""
    with get_db() as conn:
        row = conn.execute("SELECT expiry FROM premium_users WHERE user_id = ?", (str(user_id),)).fetchone()
    if not row:
        return False
    try:
        expiry = datetime.fromisoformat(row["expiry"])
        return expiry > datetime.now()
    except (ValueError, TypeError):
        return False


def add_premium_user(user_id: str, days: int = 30, added_by: str = "admin", method: str = "manual"):
    """Grant premium access for N days."""
    expiry = (datetime.now() + timedelta(days=days)).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO premium_users (user_id, expiry, added_by, method)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                expiry = excluded.expiry,
                added_by = excluded.added_by,
                method = excluded.method
        """, (str(user_id), expiry, str(added_by), method))
    logger.info("Premium granted to user %s for %d days via %s", user_id, days, method)


def remove_premium_user(user_id: str):
    """Revoke premium access."""
    with get_db() as conn:
        conn.execute("DELETE FROM premium_users WHERE user_id = ?", (str(user_id),))
    logger.info("Premium revoked for user %s", user_id)


def get_premium_info(user_id: str) -> dict | None:
    """Get premium user details."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id, expiry, added_by, method, created_at FROM premium_users WHERE user_id = ?",
            (str(user_id),)
        ).fetchone()
    return dict(row) if row else None


def list_premium_users() -> list[dict]:
    """List all premium users."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, expiry, added_by, method, created_at FROM premium_users ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_active_premium_count() -> int:
    """Count currently active premium users."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) FROM premium_users WHERE expiry > ?", (now,)).fetchone()
    return row[0] if row else 0


# ── Payment Operations ────────────────────────────────────────────────────────

def record_payment(user_id: str, razorpay_payment_id: str, razorpay_subscription_id: str,
                   amount: int, status: str, method: str = None):
    """Record a payment transaction."""
    with get_db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO payments
            (user_id, razorpay_payment_id, razorpay_subscription_id, amount, status, method)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(user_id), razorpay_payment_id, razorpay_subscription_id, amount, status, method))
    logger.info("Payment recorded: user=%s, amount=%d, status=%s", user_id, amount, status)


def get_user_payments(user_id: str) -> list[dict]:
    """Get payment history for a user."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE user_id = ? ORDER BY created_at DESC", (str(user_id),)
        ).fetchall()
    return [dict(row) for row in rows]


def get_payment_count(user_id: str) -> int:
    """Get the count of successful payments for a user."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM payments WHERE user_id = ? AND status = 'captured'",
                (str(user_id),)
            ).fetchone()
        return row[0] if row else 0
    except Exception as e:
        logger.warning("Failed to get payment count: %s", e)
        return 0



def get_revenue_stats(days: int = 30) -> dict:
    """Get revenue statistics."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        # Total revenue in period
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM payments WHERE status = 'captured' AND created_at >= ?",
            (cutoff,)
        ).fetchone()
        total_revenue = row["total"] if row else 0

        # Payment count
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM payments WHERE status = 'captured' AND created_at >= ?",
            (cutoff,)
        ).fetchone()
        payment_count = row["cnt"] if row else 0

        # Failed payments
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM payments WHERE status = 'failed' AND created_at >= ?",
            (cutoff,)
        ).fetchone()
        failed_count = row["cnt"] if row else 0

        # All-time revenue
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM payments WHERE status = 'captured'"
        ).fetchone()
        all_time_revenue = row["total"] if row else 0

    return {
        "period_days": days,
        "total_revenue_paise": total_revenue,
        "total_revenue_inr": total_revenue / 100,
        "payment_count": payment_count,
        "failed_count": failed_count,
        "all_time_revenue_paise": all_time_revenue,
        "all_time_revenue_inr": all_time_revenue / 100,
        "avg_per_payment": (total_revenue / payment_count / 100) if payment_count else 0,
    }


# ── Subscription Operations ──────────────────────────────────────────────────

def upsert_subscription(user_id: str, razorpay_subscription_id: str, plan_id: str,
                        status: str, short_url: str = None):
    """Create or update a subscription record."""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO subscriptions (user_id, razorpay_subscription_id, razorpay_plan_id, status, short_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                razorpay_subscription_id = excluded.razorpay_subscription_id,
                razorpay_plan_id = excluded.razorpay_plan_id,
                status = excluded.status,
                short_url = COALESCE(excluded.short_url, subscriptions.short_url)
        """, (str(user_id), razorpay_subscription_id, plan_id, status, short_url))


def update_subscription_status(razorpay_subscription_id: str, status: str, paid_count: int = None):
    """Update subscription status from webhook."""
    with get_db() as conn:
        if paid_count is not None:
            conn.execute("""
                UPDATE subscriptions SET status = ?, paid_count = ? WHERE razorpay_subscription_id = ?
            """, (status, paid_count, razorpay_subscription_id))
        else:
            conn.execute("""
                UPDATE subscriptions SET status = ? WHERE razorpay_subscription_id = ?
            """, (status, razorpay_subscription_id))


def get_subscription_by_razorpay_id(razorpay_sub_id: str) -> dict | None:
    """Get subscription by Razorpay subscription ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE razorpay_subscription_id = ?", (razorpay_sub_id,)
        ).fetchone()
    return dict(row) if row else None


def get_user_subscription(user_id: str) -> dict | None:
    """Get a user's current subscription."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (str(user_id),)
        ).fetchone()
    return dict(row) if row else None


def get_active_subscription_count() -> int:
    """Count active subscriptions."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status IN ('authenticated', 'active')"
        ).fetchone()
    return row[0] if row else 0


# ── Wishlist Operations ───────────────────────────────────────────────────────

def add_to_wishlist(user_id: str, book_id: int) -> bool:
    """Add a book to user's wishlist. Returns True if added, False if already exists."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO wishlists (user_id, book_id) VALUES (?, ?)",
                (str(user_id), book_id)
            )
            return True
    except Exception:
        return False


def remove_from_wishlist(user_id: str, book_id: int):
    """Remove a book from wishlist."""
    with get_db() as conn:
        conn.execute("DELETE FROM wishlists WHERE user_id = ? AND book_id = ?", (str(user_id), book_id))


def get_wishlist(user_id: str) -> list[dict]:
    """Get a user's wishlist."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT b.id, b.title, b.author, w.added_at
            FROM wishlists w
            JOIN books b ON w.book_id = b.id
            WHERE w.user_id = ?
            ORDER BY w.added_at DESC
        """, (str(user_id),)).fetchall()
    return [dict(row) for row in rows]


def is_in_wishlist(user_id: str, book_id: int) -> bool:
    """Check if a book is in user's wishlist."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM wishlists WHERE user_id = ? AND book_id = ?", (str(user_id), book_id)
        ).fetchone()
    return row is not None


# ── Analytics & Stats ─────────────────────────────────────────────────────────

def log_event(event_type: str, user_id: str = None, data: dict = None):
    """Log a bot event for analytics."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO bot_stats (event_type, user_id, data) VALUES (?, ?, ?)",
                (event_type, str(user_id) if user_id else None, json.dumps(data) if data else None)
            )
    except Exception as e:
        logger.warning("Failed to log event: %s", e)


def log_search(user_id: str, query: str, genre: str = None, search_type: str = None):
    """Log a user search."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO user_searches (user_id, query, genre, search_type) VALUES (?, ?, ?, ?)",
                (str(user_id), query, genre, search_type)
            )
    except Exception as e:
        logger.warning("Failed to log search: %s", e)


def get_searches_today() -> int:
    """Count searches in the last 24 hours."""
    cutoff = (datetime.now() - timedelta(days=1)).isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) FROM user_searches WHERE timestamp >= ?", (cutoff,)).fetchone()
    return row[0] if row else 0


def get_events_count(event_type: str, days: int = 1) -> int:
    """Count events of a type in the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM bot_stats WHERE event_type = ? AND created_at >= ?",
            (event_type, cutoff)
        ).fetchone()
    return row[0] if row else 0


def get_dashboard_stats() -> dict:
    """Get comprehensive stats for admin dashboard."""
    return {
        "total_users": get_total_user_count(),
        "active_premium": get_active_premium_count(),
        "active_subscriptions": get_active_subscription_count(),
        "total_books": get_book_count(),
        "total_genres": len(get_genres()),
        "searches_today": get_searches_today(),
        "revenue_30d": get_revenue_stats(30),
        "revenue_7d": get_revenue_stats(7),
        "revenue_1d": get_revenue_stats(1),
    }


# ── Seed Helpers ──────────────────────────────────────────────────────────────

def insert_genre(name: str) -> int:
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (name,))
        row = conn.execute("SELECT id FROM genres WHERE name = ?", (name,)).fetchone()
    return row["id"]


def insert_book(title: str, author: str) -> int:
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO books (title, author) VALUES (?, ?)", (title.strip(), author.strip()))
        row = conn.execute("SELECT id FROM books WHERE title = ? AND author = ?", (title.strip(), author.strip())).fetchone()
    return row["id"]


def link_book_genre(book_id: int, genre_id: int):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO book_genres (book_id, genre_id) VALUES (?, ?)", (book_id, genre_id))


def record_pdf_download(user_id: str, book_id: int):
    """Record a successful PDF download by a user."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO bot_stats (event_type, user_id, data) VALUES ('pdf_download', ?, ?)",
                (str(user_id), json.dumps({"book_id": book_id}))
            )
    except Exception as e:
        logger.warning("Failed to record PDF download: %s", e)


def get_pdf_download_count(user_id: str, since_date_iso: str) -> int:
    """Get the number of PDF downloads by a user since a specific ISO date."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM bot_stats WHERE event_type = 'pdf_download' AND user_id = ? AND created_at >= ?",
                (str(user_id), since_date_iso)
            ).fetchone()
        return row[0] if row else 0
    except Exception as e:
        logger.warning("Failed to get PDF download count: %s", e)
        return 0
