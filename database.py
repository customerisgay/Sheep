import os
import logging
import sqlite3
from typing import List, Dict, Optional

logger = logging.getLogger("DATABASE")

DB_PATH = os.path.join(os.path.dirname(__file__), "tgapi_store.db")
STORAGE_DIR = os.path.join(os.path.dirname(__file__), "session_storage")
os.makedirs(STORAGE_DIR, exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 20000;")
    conn.execute("PRAGMA cache_size = -64000;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # Users Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        balance REAL DEFAULT 0.0,
        api_key TEXT UNIQUE,
        referred_by INTEGER DEFAULT NULL,
        total_spent REAL DEFAULT 0.0,
        total_orders INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Categories Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        emoji TEXT DEFAULT '📦',
        description TEXT DEFAULT '',
        display_order INTEGER DEFAULT 0
    )
    """)

    # Products Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        description TEXT DEFAULT '',
        item_type TEXT DEFAULT 'text', -- 'text', 'file', 'session', 'tdata'
        FOREIGN KEY (category_id) REFERENCES categories (id)
    )
    """)

    # Stock Inventory (Available items to sell)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        data_text TEXT DEFAULT '', -- Text data, key, token or login info
        file_path TEXT DEFAULT '', -- Path to uploaded .session or .tdata zip
        is_sold INTEGER DEFAULT 0,
        sold_to INTEGER DEFAULT NULL,
        sold_at TIMESTAMP DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (product_id) REFERENCES products (id)
    )
    """)

    # Orders Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        delivered_data TEXT DEFAULT '',
        delivered_file TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id),
        FOREIGN KEY (product_id) REFERENCES products (id)
    )
    """)

    # Deposits / Top-ups Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS deposits (
        tx_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        gateway TEXT DEFAULT 'CryptoBot', -- 'CryptoBot', 'UPI', 'Manual'
        status TEXT DEFAULT 'pending', -- 'pending', 'completed', 'rejected'
        utr TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Incoming Bank Transactions Table (Auto Payment Gateway Feed)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS incoming_bank_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        utr TEXT UNIQUE NOT NULL,
        amount REAL NOT NULL,
        sender_name TEXT DEFAULT '',
        source_app TEXT DEFAULT 'UPI',
        is_redeemed INTEGER DEFAULT 0,
        redeemed_by INTEGER DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Seller Payouts Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS seller_payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_user_id INTEGER NOT NULL,
        seller_username TEXT DEFAULT '',
        phone TEXT NOT NULL,
        country TEXT DEFAULT 'India',
        age_months INTEGER DEFAULT 0,
        payout_amount REAL NOT NULL,
        upi_id TEXT NOT NULL,
        status TEXT DEFAULT 'pending', -- 'pending', 'paid', 'rejected'
        account_id INTEGER DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # NetBanking / Bank Sessions Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bank_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank_name TEXT NOT NULL,
        account_number TEXT DEFAULT '',
        customer_id TEXT DEFAULT '',
        mobile_number TEXT DEFAULT '',
        session_token TEXT DEFAULT '',
        cookies_json TEXT DEFAULT '',
        balance REAL DEFAULT 0.0,
        is_active INTEGER DEFAULT 1,
        last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # FamPay / FamApp Cloud Gateway Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fampay_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL UNIQUE,
        upi_id TEXT DEFAULT '',
        auth_token TEXT NOT NULL,
        device_id TEXT DEFAULT '',
        account_name TEXT DEFAULT 'FamPay User',
        balance REAL DEFAULT 0.0,
        is_active INTEGER DEFAULT 1,
        last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # ──────────────────────────────────────────────────────────────
    # Telegram Accounts — OTP Auto-Forwarder Store
    # ──────────────────────────────────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tg_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL UNIQUE,          -- e.g. +919876543210
        session_string TEXT NOT NULL,        -- Pyrogram StringSession (base64)
        api_id INTEGER DEFAULT 2040,
        api_hash TEXT DEFAULT 'b18441a1ff607e10a989891a5462e627',
        price REAL DEFAULT 99.0,
        usd_price REAL DEFAULT 1.0,
        country TEXT DEFAULT 'India',
        flag_emoji TEXT DEFAULT '🇮🇳',
        account_type TEXT DEFAULT 'fresh',   -- 'fresh' | 'aged'
        age_months INTEGER DEFAULT 0,        -- how old the account is
        is_sold INTEGER DEFAULT 0,
        sold_to INTEGER DEFAULT NULL,        -- buyer's Telegram user_id
        sold_at TIMESTAMP DEFAULT NULL,
        otp_forward_to INTEGER DEFAULT NULL, -- chat_id where OTP goes (same as sold_to)
        otp_count INTEGER DEFAULT 0,         -- how many OTPs forwarded
        is_logged_in INTEGER DEFAULT 0,      -- 1 = new device login confirmed
        two_fa_password TEXT DEFAULT '',     -- 2FA password if account has two-step verification
        has_2fa INTEGER DEFAULT 0,
        server_tier TEXT DEFAULT 'main',
        quality_tag TEXT DEFAULT 'Fresh',
        is_monitoring INTEGER DEFAULT 0,     -- 1 = otp_forwarder has loaded this session
        awaiting_2nd_otp INTEGER DEFAULT 0,
        last_otp TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Bot Settings Table (Dynamic channel links, force sub, toggles)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT ''
    )
    """)

    # Channel Join Requests Table (Tracks users who clicked Join Link / Requested to Join)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS channel_join_requests (
        user_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, chat_id)
    )
    """)

    # Run auto migrations on existing table
    cursor.execute("PRAGMA table_info(deposits)")
    dep_cols = [r["name"] for r in cursor.fetchall()]
    if "utr" not in dep_cols:
        cursor.execute("ALTER TABLE deposits ADD COLUMN utr TEXT DEFAULT ''")

    cursor.execute("PRAGMA table_info(tg_accounts)")
    existing_cols = [r["name"] for r in cursor.fetchall()]
    if "server_tier" not in existing_cols:
        cursor.execute("ALTER TABLE tg_accounts ADD COLUMN server_tier TEXT DEFAULT 'main'")
    if "usd_price" not in existing_cols:
        cursor.execute("ALTER TABLE tg_accounts ADD COLUMN usd_price REAL DEFAULT 1.0")
    if "has_2fa" not in existing_cols:
        cursor.execute("ALTER TABLE tg_accounts ADD COLUMN has_2fa INTEGER DEFAULT 0")
    if "quality_tag" not in existing_cols:
        cursor.execute("ALTER TABLE tg_accounts ADD COLUMN quality_tag TEXT DEFAULT 'Fresh'")
    if "otp_count" not in existing_cols:
        cursor.execute("ALTER TABLE tg_accounts ADD COLUMN otp_count INTEGER DEFAULT 0")
    if "is_logged_in" not in existing_cols:
        cursor.execute("ALTER TABLE tg_accounts ADD COLUMN is_logged_in INTEGER DEFAULT 0")
    if "two_fa_password" not in existing_cols:
        cursor.execute("ALTER TABLE tg_accounts ADD COLUMN two_fa_password TEXT DEFAULT ''")
    if "awaiting_2nd_otp" not in existing_cols:
        cursor.execute("ALTER TABLE tg_accounts ADD COLUMN awaiting_2nd_otp INTEGER DEFAULT 0")
    if "last_otp" not in existing_cols:
        cursor.execute("ALTER TABLE tg_accounts ADD COLUMN last_otp TEXT DEFAULT ''")

    conn.commit()

    # Auto-seed existing 9,286 members from members_data or JSON/TXT file if database count < 8000
    try:
        cursor.execute("SELECT COUNT(*) FROM users")
        current_u_count = cursor.fetchone()[0]
        if current_u_count < 8000:
            imported = False
            try:
                from members_data import ALL_MEMBERS_LIST
                cursor.executemany("""
                    INSERT OR IGNORE INTO users (user_id, username, first_name, balance, total_spent, total_orders, is_banned, joined_at)
                    VALUES (:user_id, :username, :first_name, :balance, :total_spent, :total_orders, :is_banned, :joined_at)
                """, ALL_MEMBERS_LIST)
                conn.commit()
                logger.info(f"✅ Auto-imported {len(ALL_MEMBERS_LIST)} members from members_data.py!")
                imported = True
            except Exception as e_mod:
                logger.debug(f"members_data module import skipped: {e_mod}")

            if not imported:
                import os, json
                base_dir = os.path.dirname(os.path.abspath(__file__))
                json_file = os.path.join(base_dir, "members_9286.json")
                txt_file = os.path.join(base_dir, "members_user_ids.txt")

                if os.path.exists(json_file):
                    with open(json_file, "r", encoding="utf-8") as jf:
                        m_list = json.load(jf)
                    cursor.executemany("""
                        INSERT OR IGNORE INTO users (user_id, username, first_name, balance, total_spent, total_orders, is_banned, joined_at)
                        VALUES (:user_id, :username, :first_name, :balance, :total_spent, :total_orders, :is_banned, :joined_at)
                    """, m_list)
                    conn.commit()
                    logger.info(f"✅ Auto-imported {len(m_list)} members into users table from members_9286.json!")
                elif os.path.exists(txt_file):
                    with open(txt_file, "r", encoding="utf-8") as tf:
                        uids = [int(line.strip()) for line in tf if line.strip().isdigit()]
                    cursor.executemany("""
                        INSERT OR IGNORE INTO users (user_id, first_name)
                        VALUES (?, 'TG User')
                    """, [(uid,) for uid in uids])
                    conn.commit()
                    logger.info(f"✅ Auto-imported {len(uids)} members into users table from members_user_ids.txt!")
    except Exception as imp_err:
        logger.error(f"Error during auto-import of members: {imp_err}")

    # Pre-seed default categories and demo products if empty
    cursor.execute("SELECT COUNT(*) FROM categories")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO categories (name, emoji, description) VALUES ('Telegram Tdata Accounts', '📁', 'Fresh and Aged Tdata sessions')")
        cursor.execute("INSERT INTO categories (name, emoji, description) VALUES ('Pyrogram / Telethon Sessions', '⚡', '.session files for bot & script development')")
        cursor.execute("INSERT INTO categories (name, emoji, description) VALUES ('TG API & Cloud Tokens', '🔑', 'High-speed automated Telegram API access')")
        cursor.execute("INSERT INTO categories (name, emoji, description) VALUES ('Virtual OTP Services', '📱', 'One-time SMS/OTP numbers for verification')")

        # Products
        cursor.execute("INSERT INTO products (category_id, name, price, description, item_type) VALUES (1, '🇮🇳 India (+91) Tdata Fresh', 45.0, 'Fresh Indian number Telegram Tdata, Clean & Spam-free.', 'text')")
        cursor.execute("INSERT INTO products (category_id, name, price, description, item_type) VALUES (1, '🇺🇸 USA (+1) Tdata Aged (6+ Months)', 85.0, 'High quality aged USA Tdata account.', 'text')")
        cursor.execute("INSERT INTO products (category_id, name, price, description, item_type) VALUES (2, '⚡ Pyrogram .session (+91 Clean)', 50.0, 'Directly loadable in Python Pyrogram / Telethon scripts.', 'text')")
        cursor.execute("INSERT INTO products (category_id, name, price, description, item_type) VALUES (3, '🔑 TGAPI 1000 Requests Key', 150.0, 'API access key with 1,000 automated API credits.', 'text')")

        # Demo Stock
        cursor.execute("INSERT INTO stock (product_id, data_text) VALUES (1, 'Phone: +919876500001 | 2FA: Pass@1234 | Tdata_Key: TD_IN_88239128')")
        cursor.execute("INSERT INTO stock (product_id, data_text) VALUES (1, 'Phone: +919876500002 | 2FA: Pass@5678 | Tdata_Key: TD_IN_99124812')")
        cursor.execute("INSERT INTO stock (product_id, data_text) VALUES (2, 'Phone: +919123400010 | Session_String: 1BJWap1wBu8v92... | API_ID: 2040 | API_HASH: b8281')")
        cursor.execute("INSERT INTO stock (product_id, data_text) VALUES (3, 'API_KEY: tgapi_live_9921894819284 | Balance: 1000 Credits | Endpoint: https://api.tgstore.com')")

        conn.commit()

    # High-Performance Indexes for 100k+ Concurrency
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tg_acc_search ON tg_accounts (is_sold, server_tier, country, age_months);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tg_acc_phone ON tg_accounts (phone);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_id_banned ON users (user_id, is_banned);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_uid ON orders (user_id);")
    conn.commit()

    conn.close()


def record_incoming_bank_tx(utr: str, amount: float, sender_name: str = "", source_app: str = "UPI", redeemed_by: int = None) -> dict:
    """Store an incoming bank transaction in database."""
    utr = utr.strip().replace(" ", "").upper()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO incoming_bank_transactions (utr, amount, sender_name, source_app, is_redeemed, redeemed_by)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (utr, amount, sender_name, source_app, 1 if redeemed_by else 0, redeemed_by))
        conn.commit()
        return {"ok": True, "is_new": True, "utr": utr, "amount": amount, "message": f"Recorded bank TX {utr} for ₹{amount}"}
    except sqlite3.IntegrityError:
        return {"ok": True, "is_new": False, "utr": utr, "amount": amount, "message": f"Bank TX {utr} already recorded"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        conn.close()


def verify_and_redeem_utr(user_id: int, utr: str, expected_amount: float = None) -> dict:
    """
    Check if UTR is in incoming_bank_transactions, not already redeemed, and credit user balance.
    Returns: { "status": "approved" | "pending" | "already_redeemed" | "amount_mismatch", "amount": float }
    """
    utr = utr.strip().replace(" ", "").upper()
    conn = get_db()
    cursor = conn.cursor()

    # 1. Check if already redeemed in incoming_bank_transactions
    cursor.execute("SELECT * FROM incoming_bank_transactions WHERE utr = ?", (utr,))
    tx = cursor.fetchone()

    if tx and tx["is_redeemed"]:
        conn.close()
        return {
            "status": "already_redeemed",
            "message": "❌ This UTR / Transaction ID has already been redeemed!"
        }

    # 2. Check if this UTR exists in deposits table as completed
    cursor.execute("SELECT * FROM deposits WHERE utr = ? AND status = 'completed'", (utr,))
    if cursor.fetchone():
        conn.close()
        return {
            "status": "already_redeemed",
            "message": "❌ This UTR has already been credited to a wallet."
        }

    # 3. If incoming bank transaction exists and is not redeemed
    if tx:
        tx_amount = float(tx["amount"])
        if expected_amount and abs(tx_amount - expected_amount) > 1.0:
            conn.close()
            return {
                "status": "amount_mismatch",
                "actual_amount": tx_amount,
                "message": f"⚠️ Amount mismatch: Bank received ₹{tx_amount}, but request was for ₹{expected_amount}."
            }

        # Credit user balance
        cursor.execute("UPDATE incoming_bank_transactions SET is_redeemed = 1, redeemed_by = ? WHERE utr = ?", (user_id, utr))
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (tx_amount, user_id))
        cursor.execute("SELECT balance, referred_by FROM users WHERE user_id = ?", (user_id,))
        u_row = cursor.fetchone()
        new_bal = u_row["balance"] if u_row else 0.0

        # Credit referral commission if user was referred
        try:
            from config import REFERRAL_PERCENT
            if u_row and u_row["referred_by"]:
                ref_uid = u_row["referred_by"]
                comm = round(tx_amount * (REFERRAL_PERCENT / 100.0), 2)
                if comm > 0:
                    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (comm, ref_uid))
        except Exception:
            pass

        # Record completed deposit
        tx_id = f"UPI_{utr}_{user_id}"
        cursor.execute("""
            INSERT OR REPLACE INTO deposits (tx_id, user_id, amount, gateway, status, utr)
            VALUES (?, ?, ?, 'UPI_Auto', 'completed', ?)
        """, (tx_id, user_id, tx_amount, utr))
        conn.commit()
        conn.close()

        return {
            "status": "approved",
            "amount": tx_amount,
            "new_balance": new_bal,
            "sender": tx["sender_name"],
            "message": f"✅ Payment of ₹{tx_amount} verified and credited successfully!"
        }

    # 4. Bank transaction not received yet -> Record as pending deposit
    tx_id = f"PEND_{utr}_{user_id}"
    amt = expected_amount or 0.0
    cursor.execute("""
        INSERT OR REPLACE INTO deposits (tx_id, user_id, amount, gateway, status, utr)
        VALUES (?, ?, ?, 'UPI_Auto', 'pending', ?)
    """, (tx_id, user_id, amt, utr))
    conn.commit()
    conn.close()

    return {
        "status": "pending",
        "amount": amt,
        "message": "⏳ Payment record submitted. Waiting for live bank notification..."
    }


def get_all_bank_transactions() -> list[dict]:
    """Fetch all incoming bank transactions with redemption status for Admin panel."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ibt.id, ibt.utr, ibt.amount, ibt.sender_name, ibt.source_app,
               ibt.is_redeemed, ibt.redeemed_by, ibt.created_at,
               u.username, u.first_name
        FROM incoming_bank_transactions ibt
        LEFT JOIN users u ON ibt.redeemed_by = u.user_id
        ORDER BY ibt.created_at DESC
        LIMIT 200
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_gateway_stats() -> dict:
    """Statistics for automated payment gateway."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM incoming_bank_transactions")
    r_total = cursor.fetchone()
    total_tx, total_amt = r_total[0], r_total[1]

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM incoming_bank_transactions WHERE is_redeemed = 1")
    r_red = cursor.fetchone()
    redeemed_tx, redeemed_amt = r_red[0], r_red[1]

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM deposits WHERE status = 'pending' AND gateway = 'UPI_Auto'")
    r_pend = cursor.fetchone()
    pending_deposits_cnt, pending_deposits_amt = r_pend[0], r_pend[1]

    conn.close()
    return {
        "total_tx": total_tx,
        "total_amt": total_amt,
        "redeemed_tx": redeemed_tx,
        "redeemed_amt": redeemed_amt,
        "unclaimed_tx": total_tx - redeemed_tx,
        "unclaimed_amt": total_amt - redeemed_amt,
        "pending_deposits_cnt": pending_deposits_cnt,
        "pending_deposits_amt": pending_deposits_amt
    }


def save_bank_session(bank_name: str, account_number: str, customer_id: str,
                      mobile_number: str, session_token: str = "", balance: float = 0.0) -> dict:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO bank_sessions (bank_name, account_number, customer_id, mobile_number, session_token, balance, is_active, last_sync)
        VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
    """, (bank_name, account_number, customer_id, mobile_number, session_token, balance))
    sess_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"id": sess_id, "bank_name": bank_name, "account_number": account_number}


def get_active_bank_sessions() -> list[dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bank_sessions WHERE is_active = 1 ORDER BY last_sync DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_bank_session_sync(session_id: int, balance: float = None):
    conn = get_db()
    cursor = conn.cursor()
    if balance is not None:
        cursor.execute("UPDATE bank_sessions SET balance = ?, last_sync = CURRENT_TIMESTAMP WHERE id = ?", (balance, session_id))
    else:
        cursor.execute("UPDATE bank_sessions SET last_sync = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


def delete_bank_session(session_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM bank_sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "message": f"Bank session #{session_id} removed."}


# ─────────────────────────────────────────────────────────────────────────────
# FAMPAY / FAMAPP DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def save_fampay_session(phone: str, auth_token: str, upi_id: str = "",
                        device_id: str = "", account_name: str = "FamPay User", balance: float = 0.0) -> dict:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO fampay_sessions (phone, upi_id, auth_token, device_id, account_name, balance, is_active, last_sync)
        VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(phone) DO UPDATE SET
            auth_token = excluded.auth_token,
            upi_id = CASE WHEN excluded.upi_id != '' THEN excluded.upi_id ELSE fampay_sessions.upi_id END,
            device_id = CASE WHEN excluded.device_id != '' THEN excluded.device_id ELSE fampay_sessions.device_id END,
            account_name = CASE WHEN excluded.account_name != 'FamPay User' THEN excluded.account_name ELSE fampay_sessions.account_name END,
            is_active = 1,
            last_sync = CURRENT_TIMESTAMP
    """, (phone, upi_id, auth_token, device_id, account_name, balance))
    sess_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "phone": phone, "upi_id": upi_id, "account_name": account_name}


def get_active_fampay_session() -> dict | None:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM fampay_sessions WHERE is_active = 1 ORDER BY last_sync DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def update_fampay_sync(phone: str, balance: float = None):
    conn = get_db()
    cursor = conn.cursor()
    if balance is not None:
        cursor.execute("UPDATE fampay_sessions SET balance = ?, last_sync = CURRENT_TIMESTAMP WHERE phone = ?", (balance, phone))
    else:
        cursor.execute("UPDATE fampay_sessions SET last_sync = CURRENT_TIMESTAMP WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()


def delete_fampay_session(phone: str = ""):
    conn = get_db()
    cursor = conn.cursor()
    if phone:
        cursor.execute("DELETE FROM fampay_sessions WHERE phone = ?", (phone,))
    else:
        cursor.execute("DELETE FROM fampay_sessions")
    conn.commit()
    conn.close()
    return {"ok": True, "message": "FamPay session removed."}


# ──────────────────────────────────────────────────────────────
# DYNAMIC BOT SETTINGS & JOIN REQUESTS HELPERS
# ──────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    """Fetch setting value from bot_settings table."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if row and row["value"] is not None:
        return row["value"]
    return default


def set_setting(key: str, value: str):
    """Store or update setting value in bot_settings table."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def record_join_request(user_id: int, chat_id: str, username: str = "", first_name: str = ""):
    """Record a Telegram channel join request."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO channel_join_requests (user_id, chat_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (user_id, str(chat_id), username or "", first_name or ""))
    conn.commit()
    conn.close()


def has_sent_join_request(user_id: int) -> bool:
    """Check if user has submitted a join request to the channel."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM channel_join_requests WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row)


def get_join_requests_count() -> int:
    """Get total count of recorded join requests."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM channel_join_requests")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def remove_join_request(user_id: int):
    """Remove user from join requests when they leave the channel."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM channel_join_requests WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
