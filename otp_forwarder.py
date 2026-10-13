"""
============================================================
  otp_forwarder.py — Automated Telegram OTP & Login Watcher
  Listens for incoming OTP / login messages from Telegram
  and delivers them to the buyer with custom animated emojis.
============================================================
"""

import asyncio
import logging
import os
import re
import sqlite3
import time

# ── Python 3.14 fix: pyrogram's sync.py calls get_event_loop() at import time ─
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from config import BOT_TOKEN, ADMIN_IDS
from emojis import e

logging.basicConfig(
    format="%(asctime)s - [OTP_FORWARDER] %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("OTP_FORWARDER")

DB_PATH = os.path.join(os.path.dirname(__file__), "tgapi_store.db")

# Telegram Official Service Senders
TELEGRAM_SERVICE_SENDERS = {
    "Telegram",
    "42777",
    "+42777",
    "777000",
}
TELEGRAM_SERVICE_IDS = {777000, 42777}

# Active Pyrogram clients — keyed by phone number
active_clients: dict[str, Client] = {}

# Event deduplication cache: (phone, text_prefix) -> timestamp
_processed_events: dict[tuple[str, str], float] = {}


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def extract_otp(text: str) -> str | None:
    """Extract 4-6 digit numeric OTP code from Telegram login messages."""
    if not text:
        return None
    patterns = [
        r"Login code[:\s]+(\d{4,6})",
        r"(\d{4,6})\s+is your login code",
        r"Your code[:\s]+(\d{4,6})",
        r"code[:\s]+(\d{4,6})",
        r"verification code[:\s]+(\d{4,6})",
        r"Do not give this code[^\d]*(\d{5,6})",
        r"^(\d{5,6})\b",
        r"\b(\d{5,6})\b",
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def is_login_notification(text: str) -> bool:
    """Check if the service message indicates a new device/location login."""
    if not text:
        return False
    t = text.lower()
    keywords = [
        "new login",
        "logged in",
        "login notification",
        "detected a login",
        "signed in from",
        "authorized from",
        "new device",
        "device:",
        "location:",
    ]
    return any(kw in t for kw in keywords)


async def send_telegram_msg(chat_id: int, text: str, reply_markup: dict | None = None):
    """Send message to buyer via Telegram Bot API."""
    import httpx
    bot_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(bot_api_url, json=payload, timeout=15.0)
            if resp.status_code == 200:
                logger.info(f"✅ Notification sent to {chat_id}")
            else:
                logger.warning(f"⚠️ Failed to send notification: {resp.text}")
        except Exception as err:
            logger.error(f"❌ Telegram send error: {err}")


def get_age_label(age_months: int) -> str:
    if age_months == 0:
        return "Fresh (0-1 Month)"
    elif age_months <= 3:
        return f"{age_months} Months (Aged)"
    elif age_months <= 6:
        return "6+ Months (Aged)"
    else:
        return "1+ Year (Aged)"


async def handle_otp_or_login(client: Client, phone: str, target_chat: int, account_data: dict, text: str) -> bool:
    """Unified deduplicated processing for incoming service messages (push & poll)."""
    if not text:
        return False

    now = time.time()
    event_key = (phone, text[:50].strip())
    if event_key in _processed_events and (now - _processed_events[event_key]) < 8.0:
        return False
    _processed_events[event_key] = now

    # ── 1. CHECK IF LOGIN NOTIFICATION ───────────────
    if is_login_notification(text):
        logger.info(f"🎉 New device login detected for {phone}!")
        try:
            me = await client.get_me()
            tg_id = me.id
            tg_username = f"@{me.username}" if me.username else "<i>Not Set</i>"
        except Exception:
            tg_id = "N/A"
            tg_username = "<i>Not Set</i>"

        country = account_data.get("country", "India")
        flag = account_data.get("flag_emoji", "🇮🇳")
        age_months = account_data.get("age_months", 0)
        age_label = get_age_label(age_months)

        # Extract Device Name and Location from Telegram notification
        device_match = re.search(r"Device:\s*([^\n\r]+)", text, re.IGNORECASE)
        device_name = device_match.group(1).strip() if device_match else "New Device"

        loc_match = re.search(r"Location:\s*([^\n\r]+)", text, re.IGNORECASE)
        location_info = f"{e('map')} <b>Location:</b> <code>{loc_match.group(1).strip()}</code>\n" if loc_match else ""

        congrat_msg = (
            f"{e('gift')} <b>Congratulations & Thank You for Your Purchase!</b>\n\n"
            f"{e('check')} <b>Your Telegram account has been successfully logged in!</b>\n\n"
            f"{e('text')} <b>Account Overview:</b>\n"
            f"{e('android')} <b>Phone Number:</b> <code>{phone}</code>\n"
            f"{e('pc')} <b>Logged-in Device:</b> <code>{device_name}</code>\n"
            f"{location_info}"
            f"{e('mani')} <b>Username:</b> {tg_username}\n"
            f"{e('idcard')} <b>Account ID:</b> <code>{tg_id}</code>\n"
            f"{e('hourglass')} <b>Account Age:</b> {age_label}\n"
            f"{e('globe')} <b>Country:</b> {flag} {country}\n\n"
            f"{e('shield')} <i>Your session is now active and secure on your device. Thank you for shopping with us!</i>"
        )
        await send_telegram_msg(target_chat, congrat_msg)

        # Mark as logged in & stop session monitoring
        conn = get_db()
        conn.execute("UPDATE tg_accounts SET is_logged_in = 1, is_monitoring = 0 WHERE phone = ?", (phone,))
        conn.commit()
        conn.close()

        await stop_session(phone)
        return True

    # ── 2. CHECK IF OTP CODE ─────────────────────────
    otp = extract_otp(text)
    if otp:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT otp_count, two_fa_password, awaiting_2nd_otp, last_otp FROM tg_accounts WHERE phone = ?", (phone,))
        row = cursor.fetchone()
        current_count = row["otp_count"] if row and row["otp_count"] is not None else 0
        awaiting_2nd = row["awaiting_2nd_otp"] if row and "awaiting_2nd_otp" in row.keys() else 0
        last_otp = row["last_otp"] if row and "last_otp" in row.keys() else ""
        two_fa = ((row["two_fa_password"] if row and row["two_fa_password"] else "") or account_data.get("two_fa_password", "")) or ""

        # If 1st OTP was already delivered, but user hasn't clicked 'Request Another OTP', and it's the duplicate code
        if current_count == 1 and not awaiting_2nd and (otp == last_otp):
            logger.info(f"⏳ Ignoring duplicate 1st OTP for {phone} (user hasn't tapped Request Next OTP)")
            conn.close()
            return False

        if current_count >= 2:
            logger.info(f"🚫 OTP limit reached (2) for {phone}")
            conn.close()
            return False

        new_count = current_count + 1
        conn.execute("UPDATE tg_accounts SET otp_count = ?, last_otp = ?, awaiting_2nd_otp = 0 WHERE phone = ?", (new_count, otp, phone))
        conn.commit()
        conn.close()

        logger.info(f"🔑 OTP #{new_count} detected for {phone}: {otp}")
        two_fa_line = f"{e('lock')} <b>2FA Password:</b> <code>{two_fa}</code>\n" if two_fa else ""

        if new_count == 1:
            # 1st OTP: Provide button for requesting another OTP
            otp_msg = (
                f"{e('lock')} <b>Login Code:</b> <code>{otp}</code>\n"
                f"{two_fa_line}"
                f"{e('android')} <b>Phone:</b> <code>{phone}</code>\n\n"
                f"{e('warning')} <b>Notice:</b> <i>If your code expired or failed, you can request another OTP using the button below. (Limit: 1 more OTP)</i>"
            )
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "🔄 Request Another OTP", "callback_data": f"req_next_otp_{phone}"}
                ]]
            }
            await send_telegram_msg(target_chat, otp_msg, reply_markup=reply_markup)

        elif new_count == 2:
            # 2nd OTP: Final code, no more buttons
            otp_msg = (
                f"{e('lock')} <b>Login Code:</b> <code>{otp}</code>\n"
                f"{two_fa_line}"
                f"{e('android')} <b>Phone:</b> <code>{phone}</code>\n\n"
                f"{e('warning')} <b>Notice:</b> <i>This is your FINAL (2nd) OTP code. Maximum OTP limit (2 attempts) has been reached. We cannot provide any further OTP codes.</i>"
            )
            await send_telegram_msg(target_chat, otp_msg)

        return True

    return False


def build_otp_handler(phone: str, otp_forward_to: int, account_data: dict):
    """Build message handler bound to a specific phone, buyer, and account metadata."""

    async def on_message(client: Client, message: Message):
        chat_id = getattr(message.chat, "id", None)
        sender = message.from_user
        sender_id    = getattr(sender, "id", 0) or 0
        sender_phone = getattr(sender, "phone_number", "") or ""
        sender_name  = getattr(sender, "first_name", "") or ""
        text = message.text or message.caption or ""

        # Check if from official Telegram service
        is_service = (
            chat_id in TELEGRAM_SERVICE_IDS
            or sender_id in TELEGRAM_SERVICE_IDS
            or str(chat_id) in TELEGRAM_SERVICE_SENDERS
            or sender_phone in TELEGRAM_SERVICE_SENDERS
            or sender_name in TELEGRAM_SERVICE_SENDERS
            or "login code" in text.lower()
            or "your code" in text.lower()
            or is_login_notification(text)
        )

        if not is_service:
            return

        logger.info(f"📩 Service message received for {phone}: {text[:80]}...")

        # Ensure we have valid buyer chat_id
        target_chat = otp_forward_to
        if not target_chat:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT otp_forward_to, sold_to FROM tg_accounts WHERE phone = ?", (phone,))
            r = c.fetchone()
            conn.close()
            if r:
                target_chat = r["otp_forward_to"] or r["sold_to"]

        if not target_chat:
            logger.warning(f"⚠️ No buyer chat_id found to forward OTP for {phone}")
            return

        await handle_otp_or_login(client, phone, target_chat, account_data, text)

    return on_message


async def poll_otp_service(client: Client, phone: str, account_data: dict):
    """Fail-safe polling on 777000 history every 2 seconds."""
    last_seen_msg_id = 0
    try:
        async for m in client.get_chat_history(777000, limit=1):
            last_seen_msg_id = m.id
    except Exception:
        pass

    while phone in active_clients:
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT is_monitoring, is_logged_in, otp_forward_to, sold_to FROM tg_accounts WHERE phone = ?", (phone,))
            acc_row = cursor.fetchone()
            conn.close()

            if not acc_row or acc_row["is_logged_in"] or not acc_row["is_monitoring"]:
                break

            target_chat = acc_row["otp_forward_to"] or acc_row["sold_to"]
            if not target_chat:
                await asyncio.sleep(2)
                continue

            new_messages = []
            async for msg in client.get_chat_history(777000, limit=5):
                if msg.id > last_seen_msg_id:
                    new_messages.append(msg)

            for msg in reversed(new_messages):
                last_seen_msg_id = max(last_seen_msg_id, msg.id)
                text = msg.text or msg.caption or ""
                if not text:
                    continue

                await handle_otp_or_login(client, phone, target_chat, account_data, text)

        except Exception as err:
            logger.debug(f"poll_otp_service check error for {phone}: {err}")

        await asyncio.sleep(2)


async def start_session(account: sqlite3.Row) -> bool:
    """Connect a Pyrogram client for one account and attach OTP & Login watcher."""
    phone = account["phone"]
    if phone in active_clients:
        return True  # already running

    logger.info(f"🔌 Connecting session for {phone}...")
    try:
        client = Client(
            name=f"tgaccount_{phone.replace('+', '')}",
            api_id=account["api_id"],
            api_hash=account["api_hash"],
            session_string=account["session_string"],
            in_memory=True,
            no_updates=False,
        )

        acc_dict = {
            "country": account["country"],
            "flag_emoji": account["flag_emoji"],
            "age_months": account["age_months"],
            "two_fa_password": account["two_fa_password"] if ("two_fa_password" in account.keys() and account["two_fa_password"]) else "",
        }
        otp_forward_to = account["otp_forward_to"]

        # Attach the handler with incoming filter
        client.add_handler(
            MessageHandler(
                build_otp_handler(phone, otp_forward_to, acc_dict),
                filters.incoming
            )
        )

        await client.start()
        active_clients[phone] = client

        # Pre-cache dialogs to prevent Pyrogram lookup error
        try:
            async for _ in client.get_dialogs(limit=10):
                pass
        except Exception:
            pass

        # Mark as monitoring in DB
        conn = get_db()
        conn.execute("UPDATE tg_accounts SET is_monitoring = 1 WHERE phone = ?", (phone,))
        conn.commit()
        conn.close()

        # Start background polling task as fail-safe
        asyncio.create_task(poll_otp_service(client, phone, acc_dict))

        me = await client.get_me()
        logger.info(f"✅ Session active for {phone} ({me.first_name}), monitoring OTP & Login...")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to start session for {phone}: {e}")
        return False


async def stop_session(phone: str):
    """Disconnect a Pyrogram client."""
    if phone in active_clients:
        try:
            await active_clients[phone].stop()
        except Exception:
            pass
        del active_clients[phone]
        logger.info(f"🔌 Session stopped for {phone}")


async def sync_sessions():
    """
    Every 15 seconds:
    - Load all SOLD accounts from DB that are NOT yet monitoring and NOT yet finished
    - Start their sessions
    """
    while True:
        try:
            conn = get_db()
            cursor = conn.cursor()

            # Accounts sold, need monitoring, and not yet confirmed logged in
            cursor.execute("""
                SELECT * FROM tg_accounts
                WHERE is_sold = 1 AND otp_forward_to IS NOT NULL AND is_monitoring = 0 AND is_logged_in = 0
            """)
            new_accounts = cursor.fetchall()
            conn.close()

            for account in new_accounts:
                await start_session(account)

        except Exception as e:
            logger.error(f"sync_sessions error: {e}")

        await asyncio.sleep(15)


async def load_all_existing():
    """On startup, immediately load all already-sold active sessions."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM tg_accounts
        WHERE is_sold = 1 AND otp_forward_to IS NOT NULL AND is_logged_in = 0
    """)
    sold_accounts = cursor.fetchall()
    conn.close()

    logger.info(f"Loading {len(sold_accounts)} existing sold account session(s)...")
    for acc in sold_accounts:
        await start_session(acc)
