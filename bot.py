import os
import re
import uuid
import random
import string
import secrets
import logging
import asyncio
import math

import io
import qrcode
from PIL import Image

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ChatJoinRequest, ChatMemberUpdated
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ChatJoinRequestHandler, ChatMemberHandler, ContextTypes, filters,
)

from urllib.parse import quote

from config import (
    BOT_TOKEN, ADMIN_IDS, CURRENCY_SYMBOL, STORE_NAME,
    SUPPORT_USERNAME, CHANNEL_USERNAME, UPI_ID, UPI_NAME, REFERRAL_PERCENT, USDT_RATE,
    PANEL_BASE_URL, ADMIN_AUTH_SECRET, ADMIN_PANEL_PASSWORD
)
from broadcaster import start_broadcast_job, cancel_current_broadcast, get_broadcast_status
from database import (
    get_db, init_db, record_incoming_bank_tx, verify_and_redeem_utr,
    get_setting, set_setting, record_join_request, has_sent_join_request, get_join_requests_count, remove_join_request
)
from emojis import e, _E, num_to_emoji
from countries import WORLD_COUNTRIES

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# SERVER TIERS (matching reference bot)
# ─────────────────────────────────────────────────────────────────────────────
SERVER_INFO = {
    "main": {"emoji": "🟢", "label": "Main Server", "desc": "our premium stock",  "quality": "Reliable | Premium | Good Quality"},
    "s1":   {"emoji": "🟣", "label": "Server 1",    "desc": "best accounts",       "quality": "Best Quality | High Success Rate"},
    "s2":   {"emoji": "🔴", "label": "Server 2",    "desc": "cheap accounts",      "quality": "Affordable | Good Quality"},
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_user(user_id, username="", first_name="", ref_by=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        api_key = f"tgapi_{secrets.token_hex(16)}"
        cursor.execute(
            "INSERT INTO users (user_id, username, first_name, balance, api_key, referred_by) VALUES (?, ?, ?, 0.0, ?, ?)",
            (user_id, username or "", first_name or "User", api_key, ref_by)
        )
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
    conn.close()
    return user


def inr_to_usd(inr: float) -> float:
    return round(inr / USDT_RATE, 2)


def usd_to_inr(usd: float) -> float:
    return round(usd * USDT_RATE, 2)


def dual_price(inr: float) -> str:
    """Format: $0.22 • ₹22"""
    usd = inr_to_usd(inr)
    return f"${usd:.2f} • {CURRENCY_SYMBOL}{inr:.0f}"


def mask_phone(phone: str) -> str:
    """+919876543210  →  +91 98●●●●●210"""
    p = phone.replace("+", "").replace(" ", "")
    if len(p) >= 7:
        return f"+{p[:2]} {p[2:4]}{'●' * (len(p)-7)}{p[-3:]}"
    return phone


def generate_qr_bytes(upi_string: str) -> io.BytesIO:
    """Generate lightweight, ultra-fast PNG QR code in-memory (600 bytes, <20ms)."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(upi_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG", optimize=True)
    bio.seek(0)
    return bio


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fired automatically by Telegram whenever a user clicks a 'Request to Join' link!"""
    req = update.chat_join_request
    if not req:
        return
    u = req.from_user
    c = req.chat
    record_join_request(u.id, str(c.id), u.username or "", u.first_name or "")
    logger.info(f"📩 [AUTO JOIN REQUEST] User {u.id} ({u.first_name} / @{u.username}) sent join request to channel {c.id}")

    # Instantly unlock and send Main Menu in PM without needing to click verify!
    try:
        db_user = get_or_create_user(u.id, u.username, u.first_name)
        bal_inr = db_user["balance"]
        welcome_msg = (
            f"{e('check')} <b>Join Request Verified!</b>\n"
            f"{'━' * 28}\n\n"
            f"🎉 Thank you, <b>{u.first_name}</b>! Your join request has been verified.\n"
            f"Your access to <b>{STORE_NAME}</b> is now unlocked!\n\n"
            f"<blockquote>• Buy Telegram Accounts {e('cart')}\n"
            f"• Instant and automatic\n"
            f"• Sell Accounts {e('money')}</blockquote>\n"
            f"{'─' * 28}\n\n"
            f"<blockquote>- <b>Your Id:</b> <code>{u.id}</code>\n"
            f"- <b>Your Balance:</b> {CURRENCY_SYMBOL}{bal_inr:.2f}\n"
            f"- <b>Support:</b> @{SUPPORT_USERNAME.lstrip('@')}</blockquote>"
        )
        await context.bot.send_message(
            chat_id=u.id,
            text=welcome_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(u.id)
        )
    except Exception as err:
        logger.warning(f"Could not auto-send unlocked menu to user {u.id}: {err}")


async def chat_member_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track when an active member leaves a public channel in real time."""
    cm = update.chat_member
    if not cm:
        return
    u = cm.new_chat_member.user
    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status
    # Only remove if they were an actual joined member and explicitly left
    if old_status in ("member", "restricted", "administrator") and new_status in ("left", "kicked"):
        remove_join_request(u.id)
        logger.info(f"🚪 [CHANNEL LEFT] User {u.id} ({u.first_name} / @{u.username}) left channel {cm.chat.id}")


async def check_user_joined_channel(bot, user_id: int) -> bool:
    """Check if user is currently an active member or has submitted a join request."""
    if user_id in ADMIN_IDS:
        return True

    enabled = get_setting("force_sub_enabled", "true").lower()
    if enabled in ("false", "0", "no", "off"):
        return True

    # 1. If user previously submitted a Join Request (for request-to-join links), they have permanent access!
    if has_sent_join_request(user_id):
        return True

    ch = get_setting("force_channel_id", CHANNEL_USERNAME).strip()
    if not ch:
        return True

    if not ch.startswith("@") and not ch.startswith("-100"):
        ch = f"@{ch}"

    # 2. Check live membership status from Telegram API (for public channels)
    try:
        member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
        if member.status in ("creator", "administrator", "member", "restricted"):
            return True
        elif member.status in ("left", "kicked"):
            return False
    except Exception as e:
        logger.warning(f"Live member check error for user {user_id} on {ch}: {e}")
        return True

    return False


def build_force_join_text(first_name: str) -> str:
    """Build premium Force Join prompt with custom luxury emojis."""
    return (
        f"{e('header')} <b>Join Request Required!</b>\n"
        f"{'━' * 28}\n\n"
        f"Hey <b>{first_name}</b>, to access <b>{STORE_NAME}</b> and buy/sell accounts, please send a <b>Join Request</b> to our official channel:\n\n"
        f"{num_to_emoji(1)} Click <b>Request to Join Channel</b> below\n"
        f"{num_to_emoji(2)} Tap <b>Join Channel / Request</b>\n\n"
        f"{e('lightni')} <i>Access will be granted automatically as soon as your request is detected!</i>"
    )


def force_join_kb(user_id: int) -> InlineKeyboardMarkup:
    """Build premium Force Channel Join Inline Keyboard with single primary blue button."""
    link = get_setting("force_channel_link", "").strip()
    if not link:
        ch = get_setting("force_channel_id", CHANNEL_USERNAME).strip()
        if ch.startswith("http://") or ch.startswith("https://"):
            link = ch
        elif ch.startswith("@"):
            link = f"https://t.me/{ch.lstrip('@')}"
        elif ch.startswith("-100"):
            link = f"https://t.me/c/{ch.replace('-100', '')}"
        else:
            link = f"https://t.me/{ch}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Request to Join Channel", url=link, icon_custom_emoji_id=_E["link"], style="primary")]
    ])


async def send_deposit_qr_prompt(message, context, uid: int, amount: float):
    """Generates dynamic UPI QR Code with clean luxury UI and 1-tap instant Verify button."""
    context.user_data["awaiting_deposit_utr"] = False
    context.user_data["deposit_expected_amount"] = amount

    rand_alpha = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    purpose = f"FAMBOTBY{rand_alpha}"
    upi_uri = f"upi://pay?pa={UPI_ID}&pn={quote(UPI_NAME)}&am={amount:.2f}&cu=INR&tn={purpose}"

    # Insert pending deposit tracking in database
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO deposits (tx_id, user_id, amount, gateway, status, utr)
        VALUES (?, ?, ?, 'UPI_Auto', 'pending', '')
    """, (purpose, uid, amount))
    conn.commit()
    conn.close()

    caption = (
        f"{e('money')} <b>Auto UPI Payment Gateway</b>\n"
        f"{'━' * 28}\n\n"
        f"{e('coin')} <b>Amount:</b> <b>{CURRENCY_SYMBOL}{amount:.0f}</b>\n"
        f"{e('point')} <b>UPI ID:</b> <code>{UPI_ID}</code>\n"
        f"{e('shield')} <b>Code:</b> <code>{purpose}</code>\n\n"
        f"{num_to_emoji(1)} Scan QR code or pay to UPI ID\n"
        f"{num_to_emoji(2)} Put <code>{purpose}</code> in Purpose/Note field\n"
        f"{num_to_emoji(3)} Tap <b>Verify Payment</b> button below after paying!\n\n"
        f"{e('lightni')} <i>Instant auto-verification active!</i>"
    )

    kb = [
        [InlineKeyboardButton("Verify Payment", callback_data=f"verify_pay_{uid}_{int(amount)}_{purpose}", icon_custom_emoji_id=_E["check"], style="success")],
        [
            InlineKeyboardButton("Change Amount", callback_data="wallet_deposit", icon_custom_emoji_id=_E["relok"], style="primary"),
            InlineKeyboardButton("Cancel", callback_data="main_menu", icon_custom_emoji_id=_E["cross"], style="danger"),
        ]
    ]

    try:
        qr_bio = await asyncio.to_thread(generate_qr_bytes, upi_uri)
        await message.reply_photo(
            photo=qr_bio,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb)
        )
    except Exception as err:
        logger.error(f"Error sending QR photo: {err}")
        await message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb)
        )


async def poll_bank_verification(application, chat_id: int, uid: int, utr: str, expected_amount: float, status_msg_id: int):
    """Polls database for up to 35 seconds to see if bank webhook arrived and redeemed the UTR."""
    for attempt in range(7):
        await asyncio.sleep(5)
        res = verify_and_redeem_utr(user_id=uid, utr=utr, expected_amount=expected_amount)
        if res.get("status") == "approved":
            try:
                success_msg = (
                    f"{e('check')} <b>PAYMENT VERIFIED & CREDITED!</b>\n"
                    f"{'━' * 28}\n\n"
                    f"{e('money')} <b>Amount Credited:</b> {CURRENCY_SYMBOL}{res.get('amount', expected_amount):.2f}\n"
                    f"{e('wallet')} <b>New Balance:</b> {CURRENCY_SYMBOL}{res.get('new_balance', 0):.2f}\n"
                    f"🆔 <b>UTR:</b> <code>{utr}</code>\n\n"
                    f"🎉 <i>Auto UPI reconciliation completed successfully! Happy shopping with {STORE_NAME}.</i>"
                )
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=success_msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]])
                )
            except Exception:
                pass
            return


# ─────────────────────────────────────────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────

def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("Buy Account",  callback_data="products",     icon_custom_emoji_id=_E["cart"], style="success"),
            InlineKeyboardButton("Sell Account", callback_data="sell_account", icon_custom_emoji_id=_E["money"], style="primary"),
        ],
        [
            InlineKeyboardButton("Profile",   callback_data="user_profile",   icon_custom_emoji_id=_E["mani"], style="primary"),
            InlineKeyboardButton("Deposit",   callback_data="wallet_deposit", icon_custom_emoji_id=_E["wallet"], style="success"),
        ],
        [
            InlineKeyboardButton("Refer & Earn", callback_data="user_referral",icon_custom_emoji_id=_E["gift"], style="primary"),
            InlineKeyboardButton("Support",   url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}", icon_custom_emoji_id=_E["support"], style="primary"),
        ],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("Admin Panel", callback_data="admin_panel", icon_custom_emoji_id=_E["crown"], style="danger")])
    return InlineKeyboardMarkup(keyboard)


def server_select_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Main Server — our premium stock", callback_data="srv_main", icon_custom_emoji_id=_E["fire"], style="success")],
        [InlineKeyboardButton("Server 1 — best accounts",        callback_data="srv_s1",   icon_custom_emoji_id=_E["shield"], style="primary")],
        [InlineKeyboardButton("Server 2 — cheap accounts",       callback_data="srv_s2",   icon_custom_emoji_id=_E["coin"], style="primary")],
        [InlineKeyboardButton("Back to Home",                    callback_data="main_menu",icon_custom_emoji_id=_E["arrow"], style="danger")],
    ])


def country_grid_kb(server: str, countries: list) -> InlineKeyboardMarkup:
    """Build 2-column country grid."""
    rows = []
    for i in range(0, len(countries), 2):
        row = []
        for j in range(2):
            if i + j < len(countries):
                c = countries[i + j]
                row.append(InlineKeyboardButton(
                    f"{c['flag']} {c['country']} ({c['usd_price']:.2f}$)",
                    callback_data=f"ctry_{server}_{c['country'][:12]}",
                    style="primary"
                ))
        rows.append(row)
    rows += [
        [InlineKeyboardButton("Back to Servers", callback_data="products",  icon_custom_emoji_id=_E["arrow"], style="danger")],
        [InlineKeyboardButton("Back to Home",    callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
    ]
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────────
# DB QUERIES FOR ACCOUNTS
# ─────────────────────────────────────────────────────────────────────────────

def get_countries_for_server(server: str) -> list:
    """Return list of distinct countries with cheapest USD price and stock count."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT country, flag_emoji, MIN(usd_price) as usd_price, COUNT(*) as stock
        FROM tg_accounts
        WHERE server_tier = ? AND is_sold = 0
        GROUP BY country, flag_emoji
        ORDER BY usd_price ASC
    """, (server,))
    rows = cursor.fetchall()
    conn.close()
    return [{"country": r["country"], "flag": r["flag_emoji"],
             "usd_price": r["usd_price"], "stock": r["stock"]} for r in rows]


PAGE_SIZE = 14


def get_all_countries() -> list:
    """Return only in-stock countries with active accounts in database (stock > 0)."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT country, flag_emoji,
               MIN(usd_price) as usd_price,
               COUNT(*) as stock,
               server_tier
        FROM tg_accounts
        WHERE is_sold = 0
        GROUP BY country
        HAVING COUNT(*) > 0
        ORDER BY stock DESC, country ASC
    """)
    rows = cursor.fetchall()
    conn.close()

    result = []
    for r in rows:
        cname = r["country"].strip()
        flag = r["flag_emoji"] or "🌍"
        if flag == "🌍":
            for def_c in WORLD_COUNTRIES:
                if def_c["country"].lower() == cname.lower():
                    flag = def_c["flag"]
                    break
        result.append({
            "country": cname,
            "flag": flag,
            "usd_price": r["usd_price"] if (r["usd_price"] and r["usd_price"] > 0) else 0.25,
            "stock": r["stock"],
            "server": r["server_tier"] or "main",
        })
    return result


def build_products_page_kb(countries: list, page: int = 1, page_size: int = PAGE_SIZE) -> tuple[InlineKeyboardMarkup, int, int]:
    """Build paginated 2-column country list with Search and Next/Prev buttons."""
    total_countries = len(countries)
    total_pages = max(1, math.ceil(total_countries / page_size))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * page_size
    end_idx   = start_idx + page_size
    page_items = countries[start_idx:end_idx]

    rows = []
    # 🔍 Search button at top (Green with premium search icon)
    rows.append([InlineKeyboardButton("Search Country", callback_data="search_country", icon_custom_emoji_id=_E["mani"], style="success")])

    # 2-column country buttons (with premium android icon)
    for i in range(0, len(page_items), 2):
        row = []
        for j in range(2):
            if i + j < len(page_items):
                c = page_items[i + j]
                label = f"{c['flag']} {c['country']} [{c['stock']}]"
                row.append(InlineKeyboardButton(
                    label,
                    callback_data=f"ctry_{c['server']}_{c['country'][:12]}",
                    style="primary"
                ))
        rows.append(row)

    # ◀️ Previous / Page Indicator / Next ▶️
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("Prev", callback_data=f"prodpage_{page-1}", icon_custom_emoji_id=_E["arrow"], style="primary"))
    nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop", icon_custom_emoji_id=_E["text"], style="primary"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next", callback_data=f"prodpage_{page+1}", icon_custom_emoji_id=_E["arrow"], style="primary"))
    rows.append(nav_row)

    # Home button
    rows.append([InlineKeyboardButton("Back to Home", callback_data="main_menu",
                                      icon_custom_emoji_id=_E["globe"], style="danger")])
    return InlineKeyboardMarkup(rows), page, total_pages


def build_sell_countries_page_kb(countries: list, page: int = 1, page_size: int = PAGE_SIZE) -> tuple[InlineKeyboardMarkup, int, int]:
    """Build paginated 2-column country list for selling."""
    total_countries = len(countries)
    total_pages = max(1, math.ceil(total_countries / page_size))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * page_size
    end_idx   = start_idx + page_size
    page_items = countries[start_idx:end_idx]

    rows = []
    # 🔍 Search button at top
    rows.append([InlineKeyboardButton("Search Country to Sell", callback_data="sell_search_country", icon_custom_emoji_id=_E["mani"], style="success")])

    # 2-column country buttons
    for i in range(0, len(page_items), 2):
        row = []
        for j in range(2):
            if i + j < len(page_items):
                c = page_items[i + j]
                label = f"{c['flag']} {c['country']}"
                row.append(InlineKeyboardButton(
                    label,
                    callback_data=f"sellctry_{c['country']}",
                    style="primary"
                ))
        rows.append(row)

    # ◀️ Previous / Page Indicator / Next ▶️
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("Prev", callback_data=f"sellpage_{page-1}", icon_custom_emoji_id=_E["arrow"], style="primary"))
    nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop", icon_custom_emoji_id=_E["text"], style="primary"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next", callback_data=f"sellpage_{page+1}", icon_custom_emoji_id=_E["arrow"], style="primary"))
    rows.append(nav_row)

    # Home button
    rows.append([InlineKeyboardButton("Back to Home", callback_data="main_menu",
                                      icon_custom_emoji_id=_E["globe"], style="danger")])
    return InlineKeyboardMarkup(rows), page, total_pages


def get_sell_payout_price(country: str, age_months: int) -> float:
    """Calculate seller payout in INR based on country and age."""
    base_inr = 45.0
    for wc in WORLD_COUNTRIES:
        if wc["country"].lower() == country.lower():
            base_inr = round(wc.get("usd_price", 0.45) * 100)
            break

    if age_months == 0:
        return max(35.0, round(base_inr * 0.75))
    elif age_months <= 6:
        return max(55.0, round(base_inr * 1.3))
    else:
        return max(80.0, round(base_inr * 1.8))


DEFAULT_AGE_TIERS = [
    {"age_months": 0,  "label": "🌱 Fresh (0-1 Month)", "price_mult": 1.0},
    {"age_months": 6,  "label": "🏆 6+ Months (Aged)",   "price_mult": 1.8},
    {"age_months": 12, "label": "👑 1+ Year (Aged)",    "price_mult": 2.5},
]


def get_country_age_tiers(country: str, server: str = "main") -> list:
    """Fetch only in-stock age categories for this country from database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT age_months, account_type,
               MIN(price) as inr_price,
               MIN(usd_price) as usd_price,
               COUNT(*) as stock,
               flag_emoji
        FROM tg_accounts
        WHERE server_tier = ? AND country LIKE ? AND is_sold = 0
        GROUP BY age_months
        HAVING COUNT(*) > 0
        ORDER BY age_months ASC
    """, (server, f"{country}%"))
    rows = cursor.fetchall()
    conn.close()

    # Find base country price & flag from WORLD_COUNTRIES
    base_inr = 25.0
    flag = "🌍"
    for wc in WORLD_COUNTRIES:
        if wc["country"].lower() == country.lower():
            base_inr = round(wc.get("usd_price", 0.25) * 100)
            flag = wc["flag"]
            break

    tiers = []
    for r in rows:
        am = r["age_months"]
        calc_inr = r["inr_price"] if (r["inr_price"] and r["inr_price"] > 0) else (
            round(r["usd_price"] * 100) if (r["usd_price"] and r["usd_price"] > 0) else round(base_inr * 1.5)
        )
        if am == 0:
            lbl = "Fresh (0-1 Month)"
        elif am <= 3:
            lbl = f"{am} Months (Aged)"
        elif am <= 6:
            lbl = "6+ Months (Aged)"
        else:
            lbl = "1+ Year (Aged)"

        tiers.append({
            "age_months": am,
            "label": lbl,
            "inr_price": calc_inr,
            "stock": r["stock"],
            "flag": r["flag_emoji"] or flag
        })

    return tiers


def get_account_info(server: str, country: str, age_months: int = None) -> dict | None:
    """Get cheapest available account for server+country (+age)."""
    conn = get_db()
    cursor = conn.cursor()
    if age_months is not None:
        cursor.execute("""
            SELECT *, MIN(usd_price) as min_usd,
                   COUNT(*) as stock_count
            FROM tg_accounts
            WHERE server_tier = ? AND country LIKE ? AND age_months = ? AND is_sold = 0
            LIMIT 1
        """, (server, f"{country}%", age_months))
    else:
        cursor.execute("""
            SELECT *, MIN(usd_price) as min_usd,
                   COUNT(*) as stock_count
            FROM tg_accounts
            WHERE server_tier = ? AND country LIKE ? AND is_sold = 0
            LIMIT 1
        """, (server, f"{country}%"))
    row = cursor.fetchone()
    conn.close()
    if not row or not row["id"]:
        return None
    return dict(row)


def get_account_stock_count(server: str, country: str, age_months: int = None) -> int:
    conn = get_db()
    cursor = conn.cursor()
    if age_months is not None:
        cursor.execute(
            "SELECT COUNT(*) FROM tg_accounts WHERE server_tier=? AND country LIKE ? AND age_months=? AND is_sold=0",
            (server, f"{country}%", age_months)
        )
    else:
        cursor.execute(
            "SELECT COUNT(*) FROM tg_accounts WHERE server_tier=? AND country LIKE ? AND is_sold=0",
            (server, f"{country}%")
        )
    cnt = cursor.fetchone()[0]
    conn.close()
    return cnt


def pick_one_account(server: str, country: str, age_months: int = None):
    """Pick one unsold account for purchase."""
    conn = get_db()
    cursor = conn.cursor()
    if age_months is not None:
        cursor.execute("""
            SELECT * FROM tg_accounts
            WHERE server_tier = ? AND country LIKE ? AND age_months = ? AND is_sold = 0
            ORDER BY id ASC LIMIT 1
        """, (server, f"{country}%", age_months))
    else:
        cursor.execute("""
            SELECT * FROM tg_accounts
            WHERE server_tier = ? AND country LIKE ? AND is_sold = 0
            ORDER BY id ASC LIMIT 1
        """, (server, f"{country}%"))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# START
# ─────────────────────────────────────────────────────────────────────────────
# MAIN MENU — colored inline keyboard (replaces plain bottom reply keyboard)
# ─────────────────────────────────────────────────────────────────────────────


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref_by = None
    if context.args:
        try:
            arg = context.args[0]
            if arg.startswith("ref_"):
                ref_by = int(arg.replace("ref_", ""))
                if ref_by == user.id:
                    ref_by = None
        except Exception:
            pass

    db_user = get_or_create_user(user.id, user.username, user.first_name, ref_by)
    bal_inr = db_user["balance"]

    # Force Channel Join Verification
    is_joined = await check_user_joined_channel(context.bot, user.id)
    if not is_joined:
        force_msg = build_force_join_text(user.first_name)
        await update.message.reply_text(
            force_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=force_join_kb(user.id)
        )
        return

    msg = (
        f"Hey, <b>{user.first_name}</b>!\n\n"
        f"• <b>{STORE_NAME} - Buy & Sell TG Accounts</b>\n\n"
        f"<blockquote>• Buy Telegram Accounts {e('cart')}\n"
        f"• Instant and automatic\n"
        f"• Sell Accounts {e('money')}</blockquote>\n"
        f"{'─' * 28}\n\n"
        f"<blockquote>- <b>Your Id:</b> <code>{user.id}</code>\n"
        f"- <b>Your Balance:</b> {CURRENCY_SYMBOL}{bal_inr:.2f}\n"
        f"- <b>Support:</b> @{SUPPORT_USERNAME.lstrip('@')}</blockquote>"
    )
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(user.id)
    )


async def monitor_seller_logout(app, chat_id: int, phone: str, session_string: str, payout_inr: float, country: str, age_months: int):
    """Background task that polls authorizations every 5s to verify seller has logged out."""
    import pyrogram_manager
    max_checks = 36  # up to 3 minutes
    for _ in range(max_checks):
        await asyncio.sleep(5)
        try:
            count = await pyrogram_manager.count_other_sessions(session_string)
            if count <= 1:
                break
        except Exception as e:
            logger.warning(f"Error checking logout for {phone}: {e}")

    try:
        msg = (
            f"{e('gift')} <b>Thank you! Logout Verified Successfully!</b>\n"
            f"{'━' * 28}\n\n"
            f"{e('check')} <i>Your device session has been terminated and the account is securely verified in our system.</i>\n\n"
            f"💳 <b>Step 3 of 3: Enter Payment Details</b>\n"
            f"Please send your <b>UPI ID</b> to receive your payment of <b>{CURRENCY_SYMBOL}{payout_inr:.2f}</b>:\n"
            f"<i>(Example: <code>name@okhdfcbank</code> or <code>9876543210@paytm</code>)</i>"
        )

        if chat_id in app.user_data:
            app.user_data[chat_id]["awaiting_sell_upi"]     = True
            app.user_data[chat_id]["sell_payout"]          = payout_inr
            app.user_data[chat_id]["sell_phone"]           = phone
            app.user_data[chat_id]["sell_country"]         = country
            app.user_data[chat_id]["sell_age"]             = age_months

        await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to send logout verification message: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# USER TEXT ROUTER
# ─────────────────────────────────────────────────────────────────────────────

async def user_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search input or payment input."""
    user = update.effective_user
    if not await check_user_joined_channel(context.bot, user.id):
        force_msg = build_force_join_text(user.first_name)
        await update.message.reply_text(
            force_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=force_join_kb(user.id)
        )
        return

    text = update.message.text.strip()

    # ── AWAITING COUNTRY SEARCH (BUYING) ─────────────────────────────────────
    if context.user_data.get("awaiting_country_search"):
        context.user_data["awaiting_country_search"] = False
        query_str = text.lower()
        all_countries = get_all_countries()
        matches = [c for c in all_countries if query_str in c["country"].lower()]

        if not matches:
            # Check if this country exists in WORLD_COUNTRIES but currently out of stock
            world_match = [c for c in WORLD_COUNTRIES if query_str in c["country"].lower()]
            if world_match:
                cname = world_match[0]["country"]
                flag = world_match[0]["flag"]
                await update.message.reply_text(
                    f"📦 <b>{flag} {cname} is currently Out of Stock!</b>\n\n"
                    f"There are currently no {cname} accounts available in stock. Please check back later or browse other available countries.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔍 Search Another Country", callback_data="search_country", style="success")],
                        [InlineKeyboardButton("« View In-Stock Countries", callback_data="products", icon_custom_emoji_id=_E["arrow"], style="primary")],
                        [InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
                    ])
                )
                return

            await update.message.reply_text(
                f"{e('cross','❌')} <b>No country found matching '<code>{text}</code>'</b>\n\n"
                f"Please check the spelling and try again.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Search Again", callback_data="search_country", style="success")],
                    [InlineKeyboardButton("« View In-Stock Countries", callback_data="products", icon_custom_emoji_id=_E["arrow"], style="primary")],
                    [InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
                ])
            )
            return

        rows = []
        rows.append([InlineKeyboardButton("🔍 Search Again", callback_data="search_country", icon_custom_emoji_id=_E["mani"], style="success")])
        for i in range(0, len(matches), 2):
            row = []
            for j in range(2):
                if i + j < len(matches):
                    c = matches[i + j]
                    label = f"{c['flag']} {c['country']} [{c['stock']}]"
                    row.append(InlineKeyboardButton(
                        label,
                        callback_data=f"ctry_{c['server']}_{c['country'][:12]}",
                        style="primary"
                    ))
            rows.append(row)
        rows.append([InlineKeyboardButton("« View In-Stock Countries", callback_data="products", icon_custom_emoji_id=_E["arrow"], style="primary")])
        rows.append([InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")])

        await update.message.reply_text(
            f"{e('check','✅')} <b>Found {len(matches)} matching countries for '<code>{text}</code>':</b>\n\n"
            f"Tap a country to view & buy:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    # ── AWAITING SELL COUNTRY SEARCH ─────────────────────────────────────────
    if context.user_data.get("awaiting_sell_search"):
        context.user_data["awaiting_sell_search"] = False
        query_str = text.lower()
        matches = [c for c in WORLD_COUNTRIES if query_str in c["country"].lower()]

        if not matches:
            await update.message.reply_text(
                f"{e('cross','❌')} <b>No country found matching '<code>{text}</code>'</b>\n\n"
                f"Please check the spelling and try again.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Search Again", callback_data="sell_search_country", style="success")],
                    [InlineKeyboardButton("« View All Countries", callback_data="sell_account", icon_custom_emoji_id=_E["arrow"], style="primary")],
                    [InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
                ])
            )
            return

        rows = []
        rows.append([InlineKeyboardButton("🔍 Search Again", callback_data="sell_search_country", icon_custom_emoji_id=_E["mani"], style="success")])
        for i in range(0, len(matches), 2):
            row = []
            for j in range(2):
                if i + j < len(matches):
                    c = matches[i + j]
                    label = f"{c['flag']} {c['country']}"
                    row.append(InlineKeyboardButton(
                        label,
                        callback_data=f"sellctry_{c['country']}",
                        style="primary"
                    ))
            rows.append(row)
        rows.append([InlineKeyboardButton("« View All Countries", callback_data="sell_account", icon_custom_emoji_id=_E["arrow"], style="primary")])
        rows.append([InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")])

        await update.message.reply_text(
            f"{e('check','✅')} <b>Found {len(matches)} matching countries:</b>\n\n"
            f"Tap a country you want to sell:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    # ── AWAITING SELL PHONE NUMBER ───────────────────────────────────────────
    if context.user_data.get("awaiting_sell_phone"):
        phone = text.strip()
        if not phone.startswith("+"):
            phone = "+" + phone

        clean_digits = re.sub(r"[^\d]", "", phone)
        if len(clean_digits) < 8 or len(clean_digits) > 15:
            await update.message.reply_text(
                f"{e('cross','❌')} <b>Invalid Phone Number!</b>\n\n"
                f"Please enter a valid phone number with country code (e.g. <code>+919876543210</code>):",
                parse_mode=ParseMode.HTML
            )
            return

        import pyrogram_manager
        load_msg = await update.message.reply_text(
            f"{e('clock')} <b>Sending login OTP via Telegram to <code>{phone}</code>...</b>",
            parse_mode=ParseMode.HTML
        )

        res = await pyrogram_manager.start_login(phone)
        if res.get("ok"):
            context.user_data["awaiting_sell_phone"] = False
            context.user_data["awaiting_sell_otp"]   = True
            context.user_data["sell_phone"]          = phone

            otp_prompt = (
                f"{e('lock')} <b>Step 2 of 3: Enter Telegram Login Code</b>\n"
                f"{'━' * 28}\n\n"
                f"An official login OTP has been sent by Telegram to <code>{phone}</code>.\n\n"
                f"👉 <i>Open your Telegram app on this number, check the code from Telegram (777000), and enter it below:</i>\n\n"
                f"<i>(Example: <code>1 2 3 4 5</code>)</i>"
            )
            await load_msg.edit_text(otp_prompt, parse_mode=ParseMode.HTML)
        else:
            await load_msg.edit_text(
                f"{e('cross','❌')} <b>Failed to send OTP:</b>\n"
                f"<code>{res.get('message', 'Unknown error')}</code>\n\n"
                f"Please verify your number and try again:",
                parse_mode=ParseMode.HTML
            )
        return

    # ── AWAITING SELL OTP CODE ───────────────────────────────────────────────
    if context.user_data.get("awaiting_sell_otp"):
        otp_code = text.replace(" ", "").strip()
        phone = context.user_data.get("sell_phone", "")
        country = context.user_data.get("sell_country", "India")
        flag = context.user_data.get("sell_flag", "🇮🇳")
        age = context.user_data.get("sell_age", 0)
        payout_inr = context.user_data.get("sell_payout", 45.0)

        import pyrogram_manager
        load_msg = await update.message.reply_text(
            f"{e('clock')} <b>Verifying login code...</b>",
            parse_mode=ParseMode.HTML
        )

        retail_price = round(payout_inr * 1.35)
        res = await pyrogram_manager.complete_login(
            phone=phone,
            otp=otp_code,
            country=country,
            flag_emoji=flag,
            account_type="fresh" if age == 0 else "aged",
            age_months=age,
            price=retail_price
        )

        if res.get("needs_2fa"):
            context.user_data["awaiting_sell_otp"] = False
            context.user_data["awaiting_sell_2fa"] = True
            context.user_data["sell_last_otp"]    = otp_code

            two_fa_prompt = (
                f"{e('lock')} <b>Two-Step Verification (2FA) Required</b>\n"
                f"{'━' * 28}\n\n"
                f"This Telegram account has a 2FA Cloud Password enabled.\n\n"
                f"Please enter your 2FA password to complete account connection:"
            )
            await load_msg.edit_text(two_fa_prompt, parse_mode=ParseMode.HTML)
            return

        if res.get("ok"):
            context.user_data["awaiting_sell_otp"] = False

            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT session_string FROM tg_accounts WHERE phone = ?", (phone,))
            row = c.fetchone()
            conn.close()
            session_str = row["session_string"] if row else ""

            logout_instr = (
                f"{e('check')} <b>Account Verified & Connected Successfully!</b>\n\n"
                f"⚠️ <b>ACTION REQUIRED: PLEASE LOG OUT FROM YOUR DEVICE</b>\n"
                f"{'━' * 28}\n"
                f"To complete the sale and receive your payment, please log out of <code>{phone}</code> from your phone/PC:\n\n"
                f"👉 <b>How to log out:</b>\n"
                f"1. Open Telegram on your device\n"
                f"2. Go to <b>Settings > Devices</b>\n"
                f"3. Tap <b>'Terminate all other sessions'</b> (or Log Out)\n\n"
                f"⏳ <i>We are automatically monitoring your session. Once you log out, we will instantly ask for your UPI ID to transfer your {CURRENCY_SYMBOL}{payout_inr:.2f}...</i>"
            )
            await load_msg.edit_text(logout_instr, parse_mode=ParseMode.HTML)

            asyncio.create_task(monitor_seller_logout(context.application, update.effective_chat.id, phone, session_str, payout_inr, country, age))
        else:
            await load_msg.edit_text(
                f"{e('cross','❌')} <b>Login Failed:</b>\n"
                f"<code>{res.get('message', 'Invalid OTP')}</code>\n\n"
                f"Please re-enter the correct login OTP code:",
                parse_mode=ParseMode.HTML
            )
        return

    # ── AWAITING SELL 2FA PASSWORD ────────────────────────────────────────────
    if context.user_data.get("awaiting_sell_2fa"):
        two_fa_pass = text.strip()
        phone = context.user_data.get("sell_phone", "")
        otp_code = context.user_data.get("sell_last_otp", "")
        country = context.user_data.get("sell_country", "India")
        flag = context.user_data.get("sell_flag", "🇮🇳")
        age = context.user_data.get("sell_age", 0)
        payout_inr = context.user_data.get("sell_payout", 45.0)

        import pyrogram_manager
        load_msg = await update.message.reply_text(
            f"{e('clock')} <b>Verifying 2FA password...</b>",
            parse_mode=ParseMode.HTML
        )

        retail_price = round(payout_inr * 1.35)
        res = await pyrogram_manager.complete_login(
            phone=phone,
            otp=otp_code,
            two_fa_password=two_fa_pass,
            country=country,
            flag_emoji=flag,
            account_type="fresh" if age == 0 else "aged",
            age_months=age,
            price=retail_price
        )

        if res.get("ok"):
            context.user_data["awaiting_sell_2fa"] = False

            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT session_string FROM tg_accounts WHERE phone = ?", (phone,))
            row = c.fetchone()
            conn.close()
            session_str = row["session_string"] if row else ""

            logout_instr = (
                f"{e('check')} <b>Account Verified & Connected Successfully!</b>\n\n"
                f"⚠️ <b>ACTION REQUIRED: PLEASE LOG OUT FROM YOUR DEVICE</b>\n"
                f"{'━' * 28}\n"
                f"To complete the sale and receive your payment, please log out of <code>{phone}</code> from your phone/PC:\n\n"
                f"👉 <b>How to log out:</b>\n"
                f"1. Open Telegram on your device\n"
                f"2. Go to <b>Settings > Devices</b>\n"
                f"3. Tap <b>'Terminate all other sessions'</b> (or Log Out)\n\n"
                f"⏳ <i>We are automatically monitoring your session. Once you log out, we will instantly ask for your UPI ID to transfer your {CURRENCY_SYMBOL}{payout_inr:.2f}...</i>"
            )
            await load_msg.edit_text(logout_instr, parse_mode=ParseMode.HTML)

            asyncio.create_task(monitor_seller_logout(context.application, update.effective_chat.id, phone, session_str, payout_inr, country, age))
        else:
            await load_msg.edit_text(
                f"{e('cross','❌')} <b>2FA Verification Failed:</b>\n"
                f"<code>{res.get('message', 'Invalid password')}</code>\n\n"
                f"Please enter the correct 2FA password:",
                parse_mode=ParseMode.HTML
            )
        return

    # ── AWAITING SELL UPI ID ──────────────────────────────────────────────────
    if context.user_data.get("awaiting_sell_upi"):
        upi_id = text.strip()
        phone = context.user_data.get("sell_phone", "")
        payout_inr = context.user_data.get("sell_payout", 45.0)
        country = context.user_data.get("sell_country", "India")
        age = context.user_data.get("sell_age", 0)

        context.user_data["awaiting_sell_upi"] = False

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO seller_payouts (seller_user_id, seller_username, phone, country, age_months, payout_amount, upi_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (update.effective_user.id, update.effective_user.username or "", phone, country, age, payout_inr, upi_id))
        payout_id = cursor.lastrowid
        conn.commit()
        conn.close()

        seller_confirm = (
            f"{e('check')} <b>Payout Request Submitted Successfully!</b>\n"
            f"{'━' * 28}\n\n"
            f"{e('money')} <b>Payout Amount:</b> {CURRENCY_SYMBOL}{payout_inr:.2f}\n"
            f"{e('wallet')} <b>Payment UPI ID:</b> <code>{upi_id}</code>\n"
            f"{e('android')} <b>Account Phone:</b> <code>{phone}</code>\n"
            f"{e('clock')} <b>Status:</b> <i>Pending Admin Verification & Transfer</i>\n\n"
            f"🛡️ <i>Your payment will be sent to your UPI ID shortly. Thank you for selling with us!</i>"
        )
        await update.message.reply_text(
            seller_confirm,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]])
        )

        admin_notice = (
            f"{e('bell')} <b>NEW ACCOUNT SOLD BY USER!</b>\n"
            f"{'━' * 28}\n\n"
            f"{e('mani')} <b>Seller:</b> {update.effective_user.first_name} (@{update.effective_user.username or 'NoUser'} | ID: <code>{update.effective_user.id}</code>)\n"
            f"{e('android')} <b>Phone:</b> <code>{phone}</code>\n"
            f"{e('globe')} <b>Country:</b> {country}\n"
            f"{e('hourglass')} <b>Account Age:</b> {get_age_label(age)}\n"
            f"{e('money')} <b>Payout Due:</b> {CURRENCY_SYMBOL}{payout_inr:.2f}\n"
            f"{e('wallet')} <b>UPI ID:</b> <code>{upi_id}</code>\n"
            f"🆔 <b>Payout ID:</b> #{payout_id}\n\n"
            f"⚡ <i>Account session is secured, logged out from user device, and active in stock!</i>"
        )
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Get Login OTP / 2FA", callback_data=f"adm_otp_{phone}", icon_custom_emoji_id=_E["lock"], style="primary")],
            [InlineKeyboardButton(f"✅ Mark Payout #{payout_id} Paid", callback_data=f"pay_seller_{payout_id}", icon_custom_emoji_id=_E["check"], style="success")]
        ])
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=admin_notice, parse_mode="HTML", reply_markup=admin_kb)
            except Exception as err:
                logger.warning(f"Could not notify admin {admin_id}: {err}")
        return

    # ── AWAITING CUSTOM DEPOSIT AMOUNT ────────────────────────────────────────
    if context.user_data.get("awaiting_deposit_amount"):
        context.user_data["awaiting_deposit_amount"] = False
        try:
            amt = float(text.replace("₹", "").replace(",", "").strip())
            if amt < 1:
                await update.message.reply_text(
                    f"{e('cross')} Minimum deposit amount is ₹1. Please enter a valid amount:",
                    parse_mode=ParseMode.HTML
                )
                context.user_data["awaiting_deposit_amount"] = True
                return
        except ValueError:
            await update.message.reply_text(
                f"{e('cross')} Invalid amount. Please enter numbers only (e.g. <code>250</code>):",
                parse_mode=ParseMode.HTML
            )
            context.user_data["awaiting_deposit_amount"] = True
            return

        await send_deposit_qr_prompt(update.message, context, update.effective_user.id, amt)
        return

    # ── AWAITING 12-DIGIT UTR / TRANSACTION ID ────────────────────────────────
    is_utr_pattern = bool(re.search(r'\b[0-9]{10,14}\b', text) or re.search(r'(?:utr|ref|rrn)\s*[:#-]?\s*([0-9]{10,14})', text, re.I))
    if context.user_data.get("awaiting_deposit_utr") or (is_utr_pattern and not context.user_data.get("awaiting_sell_phone")):
        utr_m = re.search(r'\b([0-9]{10,14})\b', text)
        utr = utr_m.group(1) if utr_m else text.strip().replace(" ", "").upper()
        exp_amt = context.user_data.get("deposit_expected_amount", None)
        uid = update.effective_user.id

        # Verify UTR in Database
        res = verify_and_redeem_utr(user_id=uid, utr=utr, expected_amount=exp_amt)
        st = res.get("status")

        if st == "approved":
            context.user_data["awaiting_deposit_utr"] = False
            success_msg = (
                f"{e('check')} <b>PAYMENT VERIFIED & CREDITED!</b>\n"
                f"{'━' * 28}\n\n"
                f"{e('money')} <b>Amount Credited:</b> {CURRENCY_SYMBOL}{res.get('amount', exp_amt or 0):.2f}\n"
                f"{e('wallet')} <b>New Balance:</b> {CURRENCY_SYMBOL}{res.get('new_balance', 0):.2f}\n"
                f"🆔 <b>UTR:</b> <code>{utr}</code>\n\n"
                f"🎉 <i>Auto UPI reconciliation completed successfully! Happy shopping with {STORE_NAME}.</i>"
            )
            await update.message.reply_text(
                success_msg,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]])
            )
            return

        elif st == "already_redeemed":
            await update.message.reply_text(
                f"{e('cross')} <b>Payment Already Redeemed!</b>\n\n"
                f"<code>{res.get('message')}</code>\n\n"
                f"Please check your UTR number or contact support if you need assistance.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Contact Support", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}", icon_custom_emoji_id=_E["support"], style="primary")]])
            )
            return

        elif st == "amount_mismatch":
            await update.message.reply_text(
                f"{e('warning')} <b>Amount Mismatch Detected!</b>\n\n"
                f"{res.get('message')}\n\n"
                f"Please contact support to manually adjust your balance.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Contact Support", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}", icon_custom_emoji_id=_E["support"], style="primary")]])
            )
            return

        else:
            # Pending live bank verification
            status_msg = await update.message.reply_text(
                f"{e('clock')} <b>Payment Recorded & Verifying...</b>\n"
                f"{'━' * 28}\n\n"
                f"🆔 <b>UTR:</b> <code>{utr}</code>\n"
                f"💵 <b>Expected Amount:</b> {CURRENCY_SYMBOL}{exp_amt:.2f if exp_amt else 0.0}\n\n"
                f"📡 <i>Scanning real-time bank transaction feeds...</i>\n"
                f"<i>As soon as the bank confirmation arrives (within 5-30 seconds), your balance will be credited automatically!</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]])
            )
            # Background polling watcher for 35 seconds
            asyncio.create_task(poll_bank_verification(context.application, update.effective_chat.id, uid, utr, exp_amt, status_msg.message_id))
            return

    await payment_amount_handler(update, context)




# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# MAIN CALLBACK ROUTER
async def safe_edit_or_reply(query, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """Safely edit message or delete photo and send a clean new text message without reply errors."""
    chat_id = query.message.chat_id if (query and query.message) else (query.from_user.id if query else None)
    bot = None
    if query:
        try:
            bot = query.get_bot()
        except Exception:
            pass
    if not bot and query and query.message:
        try:
            bot = query.message.get_bot()
        except Exception:
            pass

    msg = query.message if query else None
    if msg and getattr(msg, "photo", None):
        try:
            await msg.delete()
        except Exception:
            pass
        if bot and chat_id:
            try:
                return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            except Exception as err:
                logger.error(f"safe_edit_or_reply photo delete send_message error: {err}")
                return None
        elif msg:
            try:
                return await msg.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            except Exception:
                pass

    try:
        return await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as err:
        err_str = str(err)
        if "Message is not modified" in err_str:
            return None
        logger.warning(f"edit_message_text failed ({err}), falling back to send_message")
        if bot and chat_id:
            try:
                return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            except Exception as e2:
                logger.error(f"Fallback send_message failed: {e2}")
                return None
        elif msg:
            try:
                return await msg.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            except Exception:
                pass
        return None


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    user  = update.effective_user
    uid   = user.id

    # ── FORCE JOIN VERIFICATION ───────────────────────────────────────────────
    if data == "check_joined":
        try:
            await query.answer()
        except Exception:
            pass
        is_joined = await check_user_joined_channel(context.bot, uid)
        if is_joined:
            db_user = get_or_create_user(uid)
            bal_inr = db_user["balance"]
            msg = (
                f"🎉 <b>Thank You! Access Granted</b> {e('check')}\n"
                f"{'━' * 28}\n\n"
                f"Hey, <b>{user.first_name}</b>! Welcome to <b>{STORE_NAME}</b>.\n\n"
                f"<blockquote>• Buy Telegram Accounts {e('cart')}\n"
                f"• Instant and automatic\n"
                f"• Sell Accounts {e('money')}</blockquote>\n"
                f"{'─' * 28}\n\n"
                f"<blockquote>- <b>Your Id:</b> <code>{uid}</code>\n"
                f"- <b>Your Balance:</b> {CURRENCY_SYMBOL}{bal_inr:.2f}\n"
                f"- <b>Support:</b> @{SUPPORT_USERNAME.lstrip('@')}</blockquote>"
            )
            await safe_edit_or_reply(query, msg, reply_markup=main_menu_kb(uid))
        else:
            await query.answer("❌ Join request not detected yet! Please click the link above, tap 'Request to Join', and try again.", show_alert=True)
        return

    # Check if user is member of channel before allowing any other action
    if not await check_user_joined_channel(context.bot, uid):
        try:
            await query.answer()
        except Exception:
            pass
        force_msg = build_force_join_text(user.first_name)
        await safe_edit_or_reply(query, force_msg, reply_markup=force_join_kb(uid))
        return

    try:
        await query.answer()
    except Exception:
        pass

    # ── MAIN MENU ────────────────────────────────────────────────────────────
    if data == "main_menu":
        context.user_data["awaiting_country_search"] = False
        context.user_data["awaiting_sell_search"] = False
        context.user_data["awaiting_sell_phone"] = False
        context.user_data["awaiting_sell_otp"] = False
        context.user_data["awaiting_sell_2fa"] = False
        context.user_data["awaiting_sell_upi"] = False
        db_user = get_or_create_user(uid)
        bal_inr = db_user["balance"]
        msg = (
            f"Hey, <b>{update.effective_user.first_name}</b>!\n\n"
            f"• <b>{STORE_NAME} - Buy & Sell TG Accounts</b>\n\n"
            f"<blockquote>• Buy Telegram Accounts {e('cart')}\n"
            f"• Instant and automatic\n"
            f"• Sell Accounts {e('money')}</blockquote>\n"
            f"{'─' * 28}\n\n"
            f"<blockquote>- <b>Your Id:</b> <code>{uid}</code>\n"
            f"- <b>Your Balance:</b> {CURRENCY_SYMBOL}{bal_inr:.2f}\n"
            f"- <b>Support:</b> @{SUPPORT_USERNAME.lstrip('@')}</blockquote>"
        )
        await safe_edit_or_reply(query, msg, reply_markup=main_menu_kb(uid))

    # ── SELL ACCOUNT → PAGINATED COUNTRY PICKER ───────────────────────────────
    elif data == "sell_account" or data.startswith("sellpage_"):
        context.user_data["awaiting_sell_search"] = False
        context.user_data["awaiting_country_search"] = False
        page = 1
        if data.startswith("sellpage_"):
            try:
                page = int(data.split("_")[1])
            except (IndexError, ValueError):
                page = 1

        countries = WORLD_COUNTRIES
        kb, current_page, total_pages = build_sell_countries_page_kb(countries, page=page)

        msg = (
            f"{e('money')} <b>Sell Telegram Accounts</b> (Page {current_page}/{total_pages})\n\n"
            f"Please choose the <b>Country</b> of the account you want to sell:"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)

    # ── SELL COUNTRY SEARCH PROMPT ────────────────────────────────────────────
    elif data == "sell_search_country":
        context.user_data["awaiting_sell_search"] = True
        msg = (
            f"{e('mani','🔍')} <b>Search Country to Sell</b>\n\n"
            f"Please type the name of the country you want to sell:\n"
            f"<i>(e.g. <code>India</code>, <code>USA</code>, <code>Russia</code>, <code>Brazil</code>, <code>Bangladesh</code>)</i>"
        )
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back to Countries", callback_data="sell_account", icon_custom_emoji_id=_E["arrow"], style="primary")],
                [InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]
            ])
        )

    # ── SELL COUNTRY SELECTED → AGE TIERS WITH PAYOUT PRICE ───────────────────
    elif data.startswith("sellctry_"):
        country = data.split("_", 1)[1]
        context.user_data["sell_country"] = country

        flag = "🌍"
        for wc in WORLD_COUNTRIES:
            if wc["country"].lower().startswith(country.lower()):
                country = wc["country"]
                flag = wc["flag"]
                break

        context.user_data["sell_country"] = country
        context.user_data["sell_flag"] = flag

        msg = (
            f"{e('money')} <b>{flag} {country} — Select Account Age to Sell:</b>\n"
            f"{'─' * 28}\n"
            f"Choose the age of your Telegram account:\n"
            f"<i>(Payout will be sent instantly to your UPI)</i>"
        )
        rows = []
        for t in DEFAULT_AGE_TIERS:
            am = t["age_months"]
            payout = get_sell_payout_price(country, am)
            if am == 0:
                icon_id = _E["android"]
                clean_name = "Fresh (0-1 Month)"
            elif am <= 6:
                icon_id = _E["fire"]
                clean_name = "6+ Months (Aged)"
            else:
                icon_id = _E["crown"]
                clean_name = "1+ Year (Aged)"

            label = f"{clean_name} • Payout: {CURRENCY_SYMBOL}{payout:.0f}"
            rows.append([InlineKeyboardButton(
                label,
                callback_data=f"sellage_{country}_{am}",
                icon_custom_emoji_id=icon_id,
                style="primary"
            )])

        rows.append([InlineKeyboardButton("« Back to Countries", callback_data="sell_account", icon_custom_emoji_id=_E["arrow"], style="primary")])
        rows.append([InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")])
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

    # ── SELL AGE SELECTED → CONFIRMATION & PHONE PROMPT ───────────────────────
    elif data.startswith("sellage_"):
        parts = data.split("_")
        country = parts[1]
        age_months = int(parts[2])
        flag = context.user_data.get("sell_flag", "🌍")
        payout_inr = get_sell_payout_price(country, age_months)

        context.user_data["sell_country"] = country
        context.user_data["sell_age"] = age_months
        context.user_data["sell_payout"] = payout_inr
        context.user_data["awaiting_sell_phone"] = True

        age_labels = {
            0: "🌱 Fresh (0-1 Month)",
            6: "🏆 6+ Months (Aged)",
            12: "👑 1+ Year (Aged)",
        }
        age_label = age_labels.get(age_months, f"⏳ {age_months} Months (Aged)")

        msg = (
            f"{e('money')} <b>Sell Telegram Account</b>\n"
            f"{'━' * 28}\n\n"
            f"🌍 <b>Country:</b> {flag} {country}\n"
            f"⏳ <b>Account Age:</b> {age_label}\n"
            f"💵 <b>Payout You Will Receive:</b> <b>{CURRENCY_SYMBOL}{payout_inr:.2f}</b> (via UPI)\n\n"
            f"📱 <b>Step 1 of 3: Enter Phone Number</b>\n"
            f"Please type and send the Telegram phone number of this account with country code:\n"
            f"<i>(Example: <code>+919876543210</code>)</i>"
        )
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Cancel & Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["cross"], style="danger")]
            ])
        )

    # ── SEARCH COUNTRY PROMPT ────────────────────────────────────────────────
    elif data == "search_country":
        context.user_data["awaiting_country_search"] = True
        msg = (
            f"{e('mani','🔍')} <b>Search Country</b>\n\n"
            f"Please type the name of the country you want to search:\n"
            f"<i>(e.g. <code>India</code>, <code>USA</code>, <code>Russia</code>, <code>Brazil</code>, <code>Bangladesh</code>)</i>"
        )
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back to Countries", callback_data="products", icon_custom_emoji_id=_E["arrow"], style="primary")],
                [InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]
            ])
        )

    # ── PRODUCTS → PAGINATED DIRECT LISTING (196+ COUNTRIES) ─────────────────
    elif data == "products" or data.startswith("prodpage_"):
        context.user_data["awaiting_country_search"] = False
        page = 1
        if data.startswith("prodpage_"):
            try:
                page = int(data.split("_")[1])
            except (IndexError, ValueError):
                page = 1

        countries = get_all_countries()
        if not countries:
            msg = (
                f"{e('cross','❌')} <b>No accounts currently in stock!</b>\n\n"
                f"New accounts are added frequently through our admin panel.\n"
                f"Please check back shortly or join our channel for updates."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Refresh Stock", callback_data="products", icon_custom_emoji_id=_E["relok"], style="primary")],
                [InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]
            ])
            await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        kb, current_page, total_pages = build_products_page_kb(countries, page=page)

        msg = (
            f"{e('cart','🛒')} <b>Available Accounts</b> (Page {current_page}/{total_pages})\n"
            f"{'─' * 28}\n"
            f"Tap any country to view & buy:"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb)


    # ── SERVER SELECTED → COUNTRY GRID ───────────────────────────────────────
    elif data.startswith("srv_"):
        server = data[4:]  # main / s1 / s2
        srv = SERVER_INFO.get(server, SERVER_INFO["main"])
        countries = get_countries_for_server(server)

        if not countries:
            await query.edit_message_text(
                f"{e('cross','❌')} <b>No accounts available right now.</b>\n"
                f"Please check back later!",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back", callback_data="products", icon_custom_emoji_id=_E["arrow"], style="danger")],
                ])
            )
            return

        context.user_data["selected_server"] = server

        msg = (
            f"{e('globe','🌐')} <b>Choose the country you want to buy:</b>\n\n"
            f"Tap a country for details & purchase:"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML,
                                       reply_markup=country_grid_kb(server, countries))

    # ── COUNTRY SELECTED → AGE TIERS SELECTION ───────────────────────────────
    elif data.startswith("ctry_"):
        parts = data.split("_", 2)   # ctry_{server}_{country}
        server  = parts[1]
        country = parts[2]
        context.user_data["selected_server"]  = server
        context.user_data["selected_country"] = country

        tiers = get_country_age_tiers(country, server)
        if not tiers:
            await query.edit_message_text(
                f"{e('cross','❌')} <b>No accounts currently in stock for {country}!</b>\n\n"
                f"Please select another country or check back soon.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Back to Countries", callback_data="products", icon_custom_emoji_id=_E["arrow"], style="primary")],
                    [InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]
                ])
            )
            return

        flag = "🌍"
        for wc in WORLD_COUNTRIES:
            if wc["country"].lower() == country.lower():
                flag = wc["flag"]
                break

        msg = (
            f"{e('worldd','🌍')} <b>{flag} {country} — Select Account Age:</b>\n"
            f"{'─' * 28}\n"
            f"Choose the account age / quality tier below:"
        )
        rows = []
        for t in tiers:
            am = t["age_months"]
            if am == 0:
                icon_id = _E["android"]
                clean_name = "Fresh (0-1 Month)"
            elif am <= 3:
                icon_id = _E["hourglass"]
                clean_name = f"{am} Months (Aged)"
            elif am <= 6:
                icon_id = _E["fire"]
                clean_name = "6+ Months (Aged)"
            else:
                icon_id = _E["crown"]
                clean_name = "1+ Year (Aged)"

            label = f"{clean_name} • {CURRENCY_SYMBOL}{t['inr_price']:.0f} [{t['stock']}]"
            rows.append([InlineKeyboardButton(
                label,
                callback_data=f"buyage_{server}_{country}_{t['age_months']}",
                icon_custom_emoji_id=icon_id,
                style="primary"
            )])
        rows.append([InlineKeyboardButton("« Back to Countries", callback_data="products", icon_custom_emoji_id=_E["arrow"], style="primary")])
        rows.append([InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")])
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

    # ── AGE SELECTED → ACCOUNT INFO & PURCHASE OPTIONS ───────────────────────
    elif data.startswith("buyage_"):
        parts = data.split("_")   # buyage_{server}_{country}_{age_months}
        server     = parts[1]
        country    = parts[2]
        age_months = int(parts[3])
        srv        = SERVER_INFO.get(server, SERVER_INFO["main"])
        context.user_data["selected_server"]  = server
        context.user_data["selected_country"] = country
        context.user_data["selected_age"]     = age_months

        acc = get_account_info(server, country, age_months)
        stock = get_account_stock_count(server, country, age_months)

        age_labels = {
            0: "🌱 Fresh (0-1 Month)",
            6: "🏆 6+ Months (Aged)",
            12: "👑 1+ Year (Aged)",
        }
        age_label = age_labels.get(age_months, f"⏳ {age_months} Months (Aged)")

        flag = "🌍"
        for wc in WORLD_COUNTRIES:
            if wc["country"].lower() == country.lower():
                flag = wc["flag"]
                break

        if not acc or stock == 0:
            base_inr = 25.0
            for wc in WORLD_COUNTRIES:
                if wc["country"].lower() == country.lower():
                    base_inr = round(wc.get("usd_price", 0.25) * 100)
                    break
            price_inr = round(base_inr * (1.8 if age_months >= 6 else (1.4 if age_months >= 1 else 1.0)))
            await query.edit_message_text(
                f"{e('cross','❌')} <b>Out of Stock!</b>\n\n"
                f"Currently no accounts available for <b>{flag} {country}</b> ({age_label}).\n"
                f"{e('money','💵')} Price: {CURRENCY_SYMBOL}{price_inr:.0f}\n\n"
                f"{e('warning','⚠️')} <i>Stock abhi available nahi hai. Naya stock add hote hi update ho jayega!</i>\n"
                f"Kripya doosri age ya country choose karein:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Back to Ages", callback_data=f"ctry_{server}_{country}", icon_custom_emoji_id=_E["arrow"], style="primary")],
                    [InlineKeyboardButton("« View All Countries", callback_data="products", icon_custom_emoji_id=_E["arrow"], style="primary")],
                    [InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
                ])
            )
            return

        inr = acc["price"] if (acc["price"] and acc["price"] > 0) else round((acc.get("usd_price") or 0.25) * 100)
        quality = acc.get("quality_tag") or "New Registration"
        quality_line = srv["quality"]

        msg = (
            f"{e('lightni','⚡')} <b>Telegram Account Info</b>\n\n"
            f"{e('globe','🌍')} <b>Country:</b> {acc['flag_emoji'] or flag} {country}\n"
            f"⏳ <b>Age / Tier:</b> {age_label}\n"
            f"{e('money','💵')} <b>Price:</b> {CURRENCY_SYMBOL}{inr:.0f}\n"
            f"{e('text','📦')} <b>Available Stock:</b> {stock}\n"
            f"{e('shield','🛡️')} <b>Quality:</b> {quality_line}\n\n"
            f"{e('warning','⚠️')} <b>Note:</b> Please use Graph Manager / Plus Messenger.\n"
            f"{e('shield','🛡️')} OTP will be auto-delivered to your chat!"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Buy Account", callback_data=f"acbuy_{server}_{country}_{age_months}", icon_custom_emoji_id=_E["android"], style="success"),
                InlineKeyboardButton("Buy Session", callback_data=f"acbuy_{server}_{country}_{age_months}", icon_custom_emoji_id=_E["lock"], style="success"),
            ],
            [
                InlineKeyboardButton("« Back to Ages", callback_data=f"ctry_{server}_{country}", icon_custom_emoji_id=_E["arrow"], style="danger"),
                InlineKeyboardButton("Back to Home",   callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger"),
            ],
        ])
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    # ── BUY → PURCHASE CONFIRMATION ──────────────────────────────────────────
    elif data.startswith("acbuy_"):
        parts   = data.split("_")
        server  = parts[1]
        country = parts[2]
        age_months = int(parts[3]) if len(parts) > 3 else None

        acc = pick_one_account(server, country, age_months)
        if not acc:
            await query.edit_message_text(
                f"{e('cross','❌')} <b>Out of stock! Please try again later.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"ctry_{server}_{country}", icon_custom_emoji_id=_E["arrow"], style="danger")]])
            )
            return

        db_user = get_or_create_user(uid)
        bal_inr = db_user["balance"]

        inr = acc["price"] if (acc["price"] and acc["price"] > 0) else round((acc.get("usd_price") or 0.25) * 100)
        after_inr = bal_inr - inr
        has_2fa   = bool(acc.get("has_2fa", 0))
        quality   = acc.get("quality_tag") or "New Registration"

        # Mask the phone
        masked = mask_phone(acc["phone"])

        context.user_data["pending_acc_id"] = acc["id"]
        context.user_data["pending_server"]  = server
        context.user_data["pending_country"] = country

        if after_inr < 0:
            needed_inr = abs(after_inr)
            await query.edit_message_text(
                f"{e('cross','❌')} <b>Insufficient Balance!</b>\n\n"
                f"{e('money','💵')} Price: {CURRENCY_SYMBOL}{inr:.0f}\n"
                f"{e('wallet','💰')} Your Balance: {CURRENCY_SYMBOL}{bal_inr:.0f}\n"
                f"{e('cash','📉')} Short by: {CURRENCY_SYMBOL}{needed_inr:.0f}\n\n"
                f"Please deposit funds via UPI to continue.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Deposit via UPI", callback_data="wallet_deposit", icon_custom_emoji_id=_E["wallet"], style="success")],
                    [InlineKeyboardButton("Back", callback_data=f"ctry_{server}_{country}", icon_custom_emoji_id=_E["arrow"], style="danger")],
                ])
            )
            return

        msg = (
            f"{e('cart','🛒')} <b>PURCHASE CONFIRMATION</b>\n"
            f"{'━' * 28}\n\n"
            f"{e('card','🏷️')} <b>{acc['flag_emoji']} {country} ({acc['age_months']}M - {quality})</b>\n\n"
            f"<b>Account Details:</b>\n"
            f"{e('android','📱')} Phone: <code>{masked}</code>\n"
            f"{e('lock','🔐')} 2FA: {'Yes' if has_2fa else 'No'}\n\n"
            f"<b>Payment:</b>\n"
            f"{e('money','💵')} Price: {CURRENCY_SYMBOL}{inr:.0f}\n"
            f"{e('wallet','💰')} Balance: {CURRENCY_SYMBOL}{bal_inr:.0f}\n"
            f"{e('cash','💸')} After: {CURRENCY_SYMBOL}{after_inr:.0f}\n\n"
            f"{e('shield','🛡️')} Please use Graph / Plus Messenger."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Confirm & Buy", callback_data=f"acconfirm_{acc['id']}", icon_custom_emoji_id=_E["check"], style="success")],
            [
                InlineKeyboardButton("« Back",  callback_data=f"ctry_{server}_{country}", icon_custom_emoji_id=_E["arrow"], style="danger"),
                InlineKeyboardButton("Cancel",  callback_data="main_menu", icon_custom_emoji_id=_E["cross"], style="danger"),
            ],
        ])
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    # ── CONFIRM PURCHASE ──────────────────────────────────────────────────────
    elif data.startswith("acconfirm_"):
        acc_id = int(data.split("_")[1])

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tg_accounts WHERE id = ? AND is_sold = 0", (acc_id,))
        acc = cursor.fetchone()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (uid,))
        db_user = cursor.fetchone()

        if not acc:
            conn.close()
            await query.edit_message_text(
                f"{e('cross','❌')} <b>Account just went out of stock!</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]])
            )
            return

        usd = acc["usd_price"] or inr_to_usd(acc["price"])
        inr = acc["price"] or usd_to_inr(usd)
        after_inr = db_user["balance"] - inr

        if after_inr < 0:
            conn.close()
            await query.edit_message_text(
                f"{e('cross','❌')} <b>Insufficient balance!</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Add Balance", callback_data="wallet_deposit", icon_custom_emoji_id=_E["wallet"], style="success")]
                ])
            )
            return

        order_id = f"TGACC_{uuid.uuid4().hex[:8].upper()}"
        after_usd = inr_to_usd(after_inr)
        new_spent  = db_user["total_spent"] + inr
        new_orders = db_user["total_orders"] + 1

        cursor.execute("UPDATE users SET balance=?, total_spent=?, total_orders=? WHERE user_id=?",
                       (after_inr, new_spent, new_orders, uid))
        cursor.execute("""UPDATE tg_accounts
                          SET is_sold=1, sold_to=?, sold_at=CURRENT_TIMESTAMP,
                              otp_forward_to=?, is_monitoring=0
                          WHERE id=?""", (uid, uid, acc_id))
        cursor.execute("""INSERT INTO orders (order_id, user_id, product_id, amount, delivered_data)
                          VALUES (?, ?, 0, ?, ?)""",
                       (order_id, uid, inr, f"TG_ACC | {acc['phone']} | {acc['country']}"))
        conn.commit()

        # Fetch fresh account row with updated otp_forward_to
        cursor.execute("SELECT * FROM tg_accounts WHERE id = ?", (acc_id,))
        fresh_acc = cursor.fetchone()
        conn.close()

        # Start instant OTP listener for this account
        try:
            from otp_forwarder import start_session
            asyncio.create_task(start_session(fresh_acc))
        except Exception as err:
            logger.warning(f"Could not launch instant session: {err}")

        two_fa_str = f"{e('lock','🔐')} <b>2FA Password:</b> <code>{fresh_acc['two_fa_password']}</code>\n" if (fresh_acc['two_fa_password'] and len(fresh_acc['two_fa_password']) > 0) else ""

        success = (
            f"{e('check','✅')} <b>Account Purchased!</b>\n\n"
            f"{e('android','📱')} <b>Phone:</b> <code>{fresh_acc['phone']}</code>\n"
            f"{two_fa_str}"
            f"{e('money','💵')} <b>Paid:</b> {CURRENCY_SYMBOL}{inr:.0f}\n"
            f"{e('wallet','💰')} <b>Balance:</b> {CURRENCY_SYMBOL}{after_inr:.0f}\n\n"
            f"{e('bell','🔔')} <b>OTP Auto-Forward is ACTIVE!</b>\n"
            f"Login on Telegram now — your OTP code will be sent here automatically {e('lightni','⚡')}"
        )
        await query.edit_message_text(success, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Buy Another", callback_data="products", icon_custom_emoji_id=_E["cart"], style="primary")],
                [InlineKeyboardButton("My Orders",   callback_data="user_orders", icon_custom_emoji_id=_E["text"], style="primary")],
                [InlineKeyboardButton("Home",        callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
            ]))

    # ── REQUEST NEXT OTP (2ND ATTEMPT) ─────────────────────────────────────────
    elif data.startswith("req_next_otp_"):
        phone = data.replace("req_next_otp_", "")
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tg_accounts WHERE phone = ?", (phone,))
        acc = cursor.fetchone()

        if not acc:
            conn.close()
            await query.answer("❌ Account not found!", show_alert=True)
            return

        if acc["otp_count"] >= 2:
            conn.close()
            await query.answer("❌ Maximum OTP limit reached! Only 2 OTP codes are allowed per purchase.", show_alert=True)
            return

        if acc["is_logged_in"]:
            conn.close()
            await query.answer("✅ Account has already been logged in successfully!", show_alert=True)
            return

        cursor.execute("UPDATE tg_accounts SET awaiting_2nd_otp = 1 WHERE phone = ?", (phone,))
        conn.commit()
        conn.close()

        scan_msg = (
            f"{e('clock')} <b>Scanning for New OTP Code...</b>\n\n"
            f"{e('android')} <b>Phone:</b> <code>{phone}</code>\n\n"
            f"{e('lightni')} <i>Please request a new OTP code on your Telegram app now.\n"
            f"We are actively scanning and waiting to receive your code...</i>\n\n"
            f"{e('warning')} <b>Notice:</b> <i>This will be your final (2nd) OTP code.</i>"
        )
        await query.edit_message_text(scan_msg, parse_mode=ParseMode.HTML)
        await query.answer("🔄 Ready! Request a new OTP in Telegram app now.", show_alert=False)
        return

    # ── PROFILE ───────────────────────────────────────────────────────────────
    elif data == "user_profile":
        db_user = get_or_create_user(uid)
        bal_inr = db_user["balance"]
        msg = (
            f"{e('mani','👤')} <b>My Profile</b>\n\n"
            f"{e('idcard','🆔')} <b>User ID:</b> <code>{uid}</code>\n"
            f"{e('mani','👤')} <b>Name:</b> {update.effective_user.first_name}\n"
            f"{e('wallet','💰')} <b>Balance:</b> {CURRENCY_SYMBOL}{bal_inr:.0f}\n"
            f"{e('text','📦')} <b>Total Orders:</b> {db_user['total_orders']}\n"
            f"{e('coin','💸')} <b>Total Spent:</b> {CURRENCY_SYMBOL}{db_user['total_spent']:.0f}\n"
            f"{e('calendar','📅')} <b>Joined:</b> {db_user['joined_at']}"
        )
        await safe_edit_or_reply(query, msg,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Add Balance", callback_data="wallet_deposit", icon_custom_emoji_id=_E["wallet"], style="success")],
                [InlineKeyboardButton("My Orders",   callback_data="user_orders", icon_custom_emoji_id=_E["text"], style="primary")],
                [InlineKeyboardButton("Home",        callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
            ]))

    # ── ORDERS ────────────────────────────────────────────────────────────────
    elif data == "user_orders":
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT o.*, COALESCE(p.name, 'TG Account') as product_name
            FROM orders o LEFT JOIN products p ON o.product_id = p.id
            WHERE o.user_id = ? ORDER BY o.created_at DESC LIMIT 5
        """, (uid,))
        orders = cursor.fetchall()
        conn.close()
        if not orders:
            await safe_edit_or_reply(
                query,
                f"{e('text','📜')} <b>No orders yet.</b>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Browse Products", callback_data="products", icon_custom_emoji_id=_E["cart"], style="primary")]])
            )
            return
        msg = f"{e('text','📜')} <b>Recent Orders:</b>\n\n"
        for o in orders:
            msg += (
                f"{e('card','🧾')} <code>{o['order_id']}</code>\n"
                f"{e('android','📱')} {o['product_name']}\n"
                f"{e('money','💵')} {CURRENCY_SYMBOL}{o['amount']:.0f}\n"
                f"{e('lock','🔑')} <code>{o['delivered_data']}</code>\n"
                f"{'─' * 21}\n"
            )
        await safe_edit_or_reply(query, msg,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]]))

    # ── DEPOSIT (AUTO UPI GATEWAY) ────────────────────────────────────────────
    elif data in ("wallet_deposit", "dep_upi"):
        db_user = get_or_create_user(uid)
        bal_inr = db_user["balance"]
        msg = (
            f"{e('money')} <b>Auto UPI Payment Gateway</b>\n"
            f"{'━' * 28}\n\n"
            f"{e('wallet')} <b>Current Balance:</b> {CURRENCY_SYMBOL}{bal_inr:.2f}\n"
            f"{e('point')} <b>UPI ID:</b> <code>{UPI_ID}</code>\n\n"
            f"{e('lightni')} <i>Select a deposit amount or enter a custom amount:</i>"
        )
        kb = [
            [
                InlineKeyboardButton("₹1", callback_data="depamt_1", icon_custom_emoji_id=_E["cash"], style="primary"),
                InlineKeyboardButton("₹10", callback_data="depamt_10", icon_custom_emoji_id=_E["cash"], style="primary"),
                InlineKeyboardButton("₹50", callback_data="depamt_50", icon_custom_emoji_id=_E["cash"], style="primary"),
                InlineKeyboardButton("₹100", callback_data="depamt_100", icon_custom_emoji_id=_E["cash"], style="primary"),
            ],
            [
                InlineKeyboardButton("₹250", callback_data="depamt_250", icon_custom_emoji_id=_E["cash"], style="primary"),
                InlineKeyboardButton("₹500", callback_data="depamt_500", icon_custom_emoji_id=_E["cash"], style="primary"),
                InlineKeyboardButton("₹1,000", callback_data="depamt_1000", icon_custom_emoji_id=_E["cash"], style="primary"),
                InlineKeyboardButton("Custom", callback_data="depamt_custom", icon_custom_emoji_id=_E["text"], style="primary"),
            ],
            [
                InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")
            ]
        ]
        await safe_edit_or_reply(
            query,
            msg,
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("depamt_"):
        amt_str = data.split("_")[1]
        if amt_str == "custom":
            context.user_data["awaiting_deposit_amount"] = True
            await safe_edit_or_reply(
                query,
                f"{e('text')} <b>Enter Custom Deposit Amount:</b>\n\n"
                f"Please enter the amount in ₹ (e.g. <code>350</code>):",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Cancel", callback_data="wallet_deposit", icon_custom_emoji_id=_E["cross"], style="danger")]
                ])
            )
            return

        amount = float(amt_str)
        await send_deposit_qr_prompt(query.message, context, uid, amount)

    elif data.startswith("verify_pay_"):
        parts = data.split("_")
        pay_uid = int(parts[2])
        pay_amt = float(parts[3])
        dep_code = parts[4] if len(parts) > 4 else None

        try:
            await query.answer()
        except Exception:
            pass

        # Send instant checking progress message with premium circle loading emoji
        status_msg = None
        try:
            status_msg = await query.message.reply_text(
                f"{e('relok')} Checking payment {CURRENCY_SYMBOL}{int(pay_amt)}...",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        conn = get_db()
        cursor = conn.cursor()

        # 1. Check if THIS specific deposit request is already completed
        comp_dep = None
        if dep_code:
            cursor.execute("SELECT * FROM deposits WHERE tx_id = ? AND status = 'completed'", (dep_code,))
            comp_dep = cursor.fetchone()

        # 2. If not completed yet, trigger real-time Gmail scan
        if not comp_dep:
            from gmail_payment_engine import scan_and_auto_credit_gmail
            try:
                await scan_and_auto_credit_gmail()
            except Exception as err:
                logger.warning(f"Error during on-demand verify scan: {err}")

            if dep_code:
                cursor.execute("SELECT * FROM deposits WHERE tx_id = ? AND status = 'completed'", (dep_code,))
                comp_dep = cursor.fetchone()

        # 3. If still not completed, check for unredeemed bank transaction matching this specific code
        if not comp_dep and dep_code:
            cursor.execute("""
                SELECT * FROM incoming_bank_transactions
                WHERE is_redeemed = 0 AND (sender_name LIKE ? OR sender_name LIKE ?)
                ORDER BY created_at DESC LIMIT 1
            """, (f"%{dep_code}%", f"%{pay_uid}%"))
            bank_tx = cursor.fetchone()
            if bank_tx:
                utr = bank_tx["utr"]
                from database import verify_and_redeem_utr
                res = verify_and_redeem_utr(pay_uid, utr, expected_amount=pay_amt)
                if res.get("status") == "approved":
                    cursor.execute("UPDATE deposits SET status = 'completed', utr = ? WHERE tx_id = ?", (utr, dep_code))
                    conn.commit()
                    cursor.execute("SELECT * FROM deposits WHERE tx_id = ? AND status = 'completed'", (dep_code,))
                    comp_dep = cursor.fetchone()

        # Get current user balance
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (pay_uid,))
        user_row = cursor.fetchone()
        new_bal = user_row["balance"] if user_row else 0.0
        conn.close()

        if comp_dep:
            # Delete temporary checking message
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

            utr_disp = comp_dep["utr"] or "FAM_AUTO"
            credited_amt = comp_dep["amount"]
            success_text = (
                f"{e('check')} <b>Payment Received — Thank You!</b>\n"
                f"{'━' * 28}\n\n"
                f"{e('coin')} <b>Amount Credited:</b> {CURRENCY_SYMBOL}{credited_amt:.2f}\n"
                f"{e('wallet')} <b>New Wallet Balance:</b> <b>{CURRENCY_SYMBOL}{new_bal:.2f}</b>\n"
                f"{e('card')} <b>UTR:</b> <code>{utr_disp}</code>\n"
                f"{e('clock')} <b>Status:</b> <b>Completed</b> {e('check')}\n\n"
                f"✨ <i>Thank you for choosing {STORE_NAME}! Your balance is ready to use.</i>"
            )
            success_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Browse Products", callback_data="products", icon_custom_emoji_id=_E["cart"], style="success")],
                [InlineKeyboardButton("My Profile", callback_data="user_profile", icon_custom_emoji_id=_E["mani"], style="primary")],
                [InlineKeyboardButton("Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
            ])
            # Delete the QR code photo message so QR is removed after payment
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=success_text,
                parse_mode=ParseMode.HTML,
                reply_markup=success_kb
            )
        else:
            logger.info(f"⏳ [VERIFY] User {pay_uid} tapped verify for ₹{pay_amt} — No payment found in bank/Gmail records yet.")
            not_found_text = (
                f"{e('cross')} <b>Payment not detected</b>\n\n"
                f"Please make sure you paid or wait for few minutes before trying again."
            )
            if status_msg:
                try:
                    await status_msg.edit_text(not_found_text, parse_mode=ParseMode.HTML)
                except Exception:
                    await query.message.reply_text(not_found_text, parse_mode=ParseMode.HTML)
            else:
                await query.message.reply_text(not_found_text, parse_mode=ParseMode.HTML)

    # ── REFERRAL ──────────────────────────────────────────────────────────────
    elif data == "user_referral":
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{uid}"
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (uid,))
        ref_count = cursor.fetchone()[0]
        conn.close()
        msg = (
            f"{e('gift','🎁')} <b>Refer & Earn {REFERRAL_PERCENT:.2f}%</b>\n\n"
            f"Earn <b>{REFERRAL_PERCENT:.2f}% instant commission</b> every time your friend deposits!\n\n"
            f"{e('link','🔗')} <b>Your Link:</b>\n<code>{ref_link}</code>\n\n"
            f"{e('manemo','👥')} <b>Friends Invited:</b> {ref_count}"
        )
        await safe_edit_or_reply(query, msg,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]]))

    # ── ADMIN PANEL ───────────────────────────────────────────────────────────
    elif data == "admin_panel" and uid in ADMIN_IDS:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_u = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM orders")
        r = cursor.fetchone(); total_o, total_rev = r[0], r[1]
        cursor.execute("SELECT COUNT(*) FROM tg_accounts WHERE is_sold=0")
        avail = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tg_accounts")
        total_acc = cursor.fetchone()[0]
        conn.close()

        panel_base = get_setting("panel_url", PANEL_BASE_URL).rstrip("/")
        direct_chrome_link = f"{panel_base}/?token={ADMIN_AUTH_SECRET}"

        msg = (
            f"{e('crown','👑')} <b>Admin Control Panel</b>\n"
            f"{'━' * 28}\n\n"
            f"{e('manemo','👥')} Users: <code>{total_u}</code>\n"
            f"{e('text','📦')} Available Accounts: <code>{avail}</code>\n"
            f"{e('android','📱')} Total Accounts: <code>{total_acc}</code>\n"
            f"{e('card','🧾')} Total Sales: <code>{total_o}</code>\n"
            f"{e('money','💰')} Revenue: <code>{CURRENCY_SYMBOL}{total_rev:.0f}</code>\n\n"
            f"🌐 <b>Direct Chrome Link:</b>\n"
            f"👉 <a href=\"{direct_chrome_link}\">{direct_chrome_link}</a>\n\n"
            f"🔐 <b>Password:</b> <code>{ADMIN_PANEL_PASSWORD}</code>\n\n"
            f"<b>💰 Balance Management:</b>\n"
            f"• <code>/addbalance &lt;uid&gt; &lt;amount&gt;</code> — Balance badhayein (+)\n"
            f"• <code>/cutbalance &lt;uid&gt; &lt;amount&gt;</code> — Balance kam karein (-)\n"
            f"• <code>/setbalance &lt;uid&gt; &lt;amount&gt;</code> — Set exact balance\n"
            f"• <code>/checkuser &lt;uid&gt;</code> — User details & balance\n\n"
            f"<b>⚙️ Other Commands:</b>\n"
            f"• <code>/addaccount &lt;phone&gt; &lt;price&gt; &lt;country&gt; &lt;flag&gt; &lt;type&gt; &lt;age&gt;</code>\n"
            f"• <code>/broadcast &lt;msg&gt;</code>\n"
            f"• <code>/panel</code> — Open Chrome Web Dashboard"
        )
        rows = []
        if "localhost" not in panel_base and "127.0.0.1" not in panel_base:
            rows.append([InlineKeyboardButton("🌐 Open Admin Panel in Chrome", url=direct_chrome_link, icon_custom_emoji_id=_E["link"])])
        rows.append([InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")])

        await safe_edit_or_reply(
            query,
            msg,
            reply_markup=InlineKeyboardMarkup(rows)
        )

    # ── ADMIN: APPROVE PAYMENT ────────────────────────────────────────────────
    elif data.startswith("payapprove_"):
        if uid not in ADMIN_IDS:
            await query.answer("Admin only!", show_alert=True)
            return
        parts      = data.split("_")
        pay_id     = "_".join(parts[1:4])
        target_uid = int(parts[4])
        amount_inr = float(parts[5])
        await handle_pay_approve(query, context, pay_id, target_uid, amount_inr)

    # ── ADMIN: DECLINE PAYMENT ────────────────────────────────────────────────
    elif data.startswith("paydecline_"):
        if uid not in ADMIN_IDS:
            await query.answer("Admin only!", show_alert=True)
            return
        parts      = data.split("_")
        pay_id     = "_".join(parts[1:4])
        target_uid = int(parts[4])
        await handle_pay_decline(query, context, pay_id, target_uid)

    # ── ADMIN: MARK SELLER PAYOUT PAID ─────────────────────────────────────────
    elif data.startswith("pay_seller_"):
        if uid not in ADMIN_IDS:
            await query.answer("Admin only!", show_alert=True)
            return
        payout_id = int(data.split("_")[2])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM seller_payouts WHERE id = ?", (payout_id,))
        p = cursor.fetchone()

        if not p:
            conn.close()
            await query.answer("❌ Payout record not found!", show_alert=True)
            return

        if p["status"] == "paid":
            conn.close()
            await query.answer("✅ This payout is already marked as PAID!", show_alert=True)
            return

        cursor.execute("UPDATE seller_payouts SET status = 'paid' WHERE id = ?", (payout_id,))
        conn.commit()
        conn.close()

        await query.edit_message_text(
            f"{query.message.text_html}\n\n"
            f"{e('check')} <b>STATUS: PAID by Admin ({update.effective_user.first_name})</b>",
            parse_mode=ParseMode.HTML
        )
        await query.answer("✅ Payout marked as PAID!", show_alert=True)

        # Send notification to the seller
        try:
            seller_msg = (
                f"{e('gift')} <b>Payout Transferred Successfully!</b>\n"
                f"{'━' * 28}\n\n"
                f"Your payout of <b>{CURRENCY_SYMBOL}{p['payout_amount']:.2f}</b> for account <code>{p['phone']}</code> has been sent to your UPI ID <code>{p['upi_id']}</code>.\n\n"
                f"🎉 <i>Thank you for selling with {STORE_NAME}!</i>"
            )
            await context.bot.send_message(chat_id=p["seller_user_id"], text=seller_msg, parse_mode="HTML")
        except Exception as err:
            logger.warning(f"Could not notify seller {p['seller_user_id']}: {err}")

    # ── ADMIN: FETCH LIVE OTP FOR SELLER/STORE ACCOUNT ─────────────────────────
    elif data.startswith("adm_otp_"):
        if uid not in ADMIN_IDS:
            await query.answer("Admin only!", show_alert=True)
            return
        phone = data.replace("adm_otp_", "")
        await query.answer("🔄 Fetching live OTP from Telegram...", show_alert=False)

        import pyrogram_manager
        res = await pyrogram_manager.fetch_account_login_otp(phone)
        if res.get("ok"):
            msg = (
                f"{e('lock')} <b>Admin Device Login Info:</b>\n"
                f"{'━' * 28}\n\n"
                f"{e('android')} <b>Phone:</b> <code>{phone}</code>\n"
                f"{e('mani')} <b>Latest Login OTP:</b> <code>{res.get('otp')}</code>\n"
                f"{e('crown')} <b>2FA Password:</b> <code>{res.get('two_fa', 'None')}</code>\n\n"
                f"📄 <b>Telegram Message:</b>\n"
                f"<blockquote>{res.get('full_text', '')}</blockquote>"
            )
        else:
            msg = (
                f"{e('cross')} <b>Failed to get OTP for <code>{phone}</code>:</b>\n"
                f"<code>{res.get('message')}</code>\n\n"
                f"💡 <i>Make sure you initiated login on your Telegram app first!</i>"
            )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh OTP Code", callback_data=f"adm_otp_{phone}", icon_custom_emoji_id=_E["clock"], style="primary")]
        ])
        await context.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML", reply_markup=kb)

    elif data == "noop":
        pass


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def admin_add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add balance to user wallet: /addbalance <user_id> <amount_inr>"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            f"<b>Usage:</b> <code>/addbalance &lt;user_id&gt; &lt;amount_inr&gt;</code>\n"
            f"<i>Example: /addbalance 123456789 100</i>",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        target = int(context.args[0])
        amount = float(context.args[1])
        if amount <= 0:
            await update.message.reply_text("Amount must be greater than 0.")
            return
    except ValueError:
        await update.message.reply_text("Invalid user ID or amount.")
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (target,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        await update.message.reply_text(f"{e('cross','❌')} User <code>{target}</code> not found in database.", parse_mode=ParseMode.HTML)
        return

    conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, target))
    conn.commit()
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (target,))
    new_bal = cursor.fetchone()[0]
    conn.close()

    await update.message.reply_text(
        f"{e('check','✅')} <b>Balance Added!</b>\n\n"
        f"{e('mani','👤')} User: <code>{target}</code>\n"
        f"{e('money','💵')} Added: <code>+{CURRENCY_SYMBOL}{amount:.0f}</code>\n"
        f"{e('wallet','💰')} New Balance: {CURRENCY_SYMBOL}{new_bal:.0f}",
        parse_mode=ParseMode.HTML
    )
    try:
        await context.bot.send_message(
            target,
            f"{e('crown','🎉')} <b>Balance Credited!</b>\n\n"
            f"<code>+{CURRENCY_SYMBOL}{amount:.0f}</code> credited by Admin.\n"
            f"{e('wallet','💰')} <b>New Balance:</b> {CURRENCY_SYMBOL}{new_bal:.0f}",
            parse_mode=ParseMode.HTML
        )
    except Exception as err:
        logger.warning(f"Could not notify user {target}: {err}")


async def admin_cut_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deduct balance from user wallet: /cutbalance <user_id> <amount_inr>"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            f"<b>Usage:</b> <code>/cutbalance &lt;user_id&gt; &lt;amount_inr&gt;</code>\n"
            f"<i>Example: /cutbalance 123456789 50</i>",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        target = int(context.args[0])
        amount = float(context.args[1])
        if amount <= 0:
            await update.message.reply_text("Amount must be greater than 0.")
            return
    except ValueError:
        await update.message.reply_text("Invalid user ID or amount.")
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (target,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        await update.message.reply_text(f"{e('cross','❌')} User <code>{target}</code> not found in database.", parse_mode=ParseMode.HTML)
        return

    current_bal = user["balance"]
    new_bal = max(0.0, current_bal - amount)
    conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, target))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"{e('check','✅')} <b>Balance Deducted!</b>\n\n"
        f"{e('mani','👤')} User: <code>{target}</code>\n"
        f"{e('money','💵')} Deducted: <code>-{CURRENCY_SYMBOL}{amount:.0f}</code>\n"
        f"{e('wallet','💰')} New Balance: {CURRENCY_SYMBOL}{new_bal:.0f}",
        parse_mode=ParseMode.HTML
    )
    try:
        await context.bot.send_message(
            target,
            f"{e('warning','⚠️')} <b>Balance Updated!</b>\n\n"
            f"<code>-{CURRENCY_SYMBOL}{amount:.0f}</code> deducted by Admin.\n"
            f"{e('wallet','💰')} <b>New Balance:</b> {CURRENCY_SYMBOL}{new_bal:.0f}",
            parse_mode=ParseMode.HTML
        )
    except Exception as err:
        logger.warning(f"Could not notify user {target}: {err}")


async def admin_set_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Directly set exact balance for user: /setbalance <user_id> <amount_inr>"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            f"<b>Usage:</b> <code>/setbalance &lt;user_id&gt; &lt;amount_inr&gt;</code>\n"
            f"<i>Example: /setbalance 123456789 250</i>",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        target = int(context.args[0])
        amount = max(0.0, float(context.args[1]))
    except ValueError:
        await update.message.reply_text("Invalid user ID or amount.")
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (target,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        await update.message.reply_text(f"{e('cross','❌')} User <code>{target}</code> not found in database.", parse_mode=ParseMode.HTML)
        return

    conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (amount, target))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"{e('check','✅')} <b>Balance Set!</b>\n\n"
        f"{e('mani','👤')} User: <code>{target}</code>\n"
        f"{e('wallet','💰')} Set Balance: {CURRENCY_SYMBOL}{amount:.0f}",
        parse_mode=ParseMode.HTML
    )
    try:
        await context.bot.send_message(
            target,
            f"{e('crown','🎉')} <b>Balance Set by Admin!</b>\n\n"
            f"{e('wallet','💰')} <b>New Balance:</b> {CURRENCY_SYMBOL}{amount:.0f}",
            parse_mode=ParseMode.HTML
        )
    except Exception as err:
        logger.warning(f"Could not notify user {target}: {err}")


async def admin_check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check user details & balance: /checkuser <user_id>"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("<b>Usage:</b> <code>/checkuser &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (target,))
    u = cursor.fetchone()
    conn.close()

    if not u:
        await update.message.reply_text(f"{e('cross','❌')} User <code>{target}</code> not found.", parse_mode=ParseMode.HTML)
        return

    msg = (
        f"{e('mani','👤')} <b>User Info:</b>\n\n"
        f"{e('idcard','🆔')} <b>ID:</b> <code>{u['user_id']}</code>\n"
        f"👤 <b>Name:</b> {u['first_name']} (@{u['username'] or 'None'})\n"
        f"{e('wallet','💰')} <b>Balance:</b> {CURRENCY_SYMBOL}{u['balance']:.0f}\n"
        f"{e('text','📦')} <b>Orders:</b> {u['total_orders']}\n"
        f"{e('money','💸')} <b>Spent:</b> {CURRENCY_SYMBOL}{u['total_spent']:.0f}\n"
        f"{e('calendar','📅')} <b>Joined:</b> {u['joined_at']}\n\n"
        f"<b>Quick Actions:</b>\n"
        f"• <code>/addbalance {target} &lt;amount&gt;</code>\n"
        f"• <code>/cutbalance {target} &lt;amount&gt;</code>\n"
        f"• <code>/setbalance {target} &lt;amount&gt;</code>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def admin_add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 5:
        await update.message.reply_text(
            "<b>Usage:</b>\n<code>/addaccount +91XXXXXXX 22 India 🇮🇳 fresh 0 main 0.22</code>\n\n"
            "Args: phone price country flag type age_months server(main/s1/s2) usd_price\n"
            "Then send session string as next message.",
            parse_mode=ParseMode.HTML
        )
        return
    args = context.args
    phone      = args[0]
    price      = float(args[1])
    country    = args[2]
    flag       = args[3] if len(args) > 3 else "🌍"
    acc_type   = args[4] if len(args) > 4 else "fresh"
    age_months = int(args[5]) if len(args) > 5 else 0
    server     = args[6] if len(args) > 6 else "main"
    usd_price  = float(args[7]) if len(args) > 7 else inr_to_usd(price)

    context.user_data["pending_account"] = {
        "phone": phone, "price": price, "country": country,
        "flag": flag, "acc_type": acc_type, "age_months": age_months,
        "server": server, "usd_price": usd_price,
        "api_id": 2040, "api_hash": "b18441a1ff607e10a989891a5462e627"
    }
    await update.message.reply_text(
        f"{e('check','✅')} Details saved!\n{e('android','📱')} <code>{phone}</code> | {flag} {country} | {CURRENCY_SYMBOL}{price} / ${usd_price}\n\n"
        f"{e('lock','🔑')} Now send the <b>Pyrogram Session String</b> as your next message.",
        parse_mode=ParseMode.HTML
    )


async def admin_receive_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    pending = context.user_data.get("pending_account")
    if not pending:
        return
    session_string = update.message.text.strip()
    if len(session_string) < 100:
        await update.message.reply_text(f"{e('cross','❌')} Too short. Paste the full Pyrogram session string.", parse_mode=ParseMode.HTML)
        return
    conn = get_db()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO tg_accounts
            (phone, session_string, api_id, api_hash, price, usd_price, country,
             flag_emoji, account_type, age_months, server_tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pending["phone"], session_string, pending["api_id"], pending["api_hash"],
            pending["price"], pending["usd_price"], pending["country"], pending["flag"],
            pending["acc_type"], pending["age_months"], pending["server"]
        ))
        conn.commit()
        await update.message.reply_text(
            f"{e('check','✅')} <b>Account added!</b>\n{e('android','📱')} <code>{pending['phone']}</code>\n"
            f"{pending['flag']} {pending['country']}\n"
            f"{e('money','💰')} {CURRENCY_SYMBOL}{pending['price']} / ${pending['usd_price']}",
            parse_mode=ParseMode.HTML
        )
        context.user_data.pop("pending_account", None)
    except Exception as err:
        await update.message.reply_text(f"{e('cross','❌')} DB error: {err}", parse_mode=ParseMode.HTML)
    finally:
        conn.close()


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text(
            f"{e('header','📢')} <b>⚡ Turbo Power Broadcaster</b>\n\n"
            f"<b>Usage:</b>\n"
            f"• <code>/broadcast &lt;message&gt;</code> — Start high-speed broadcast\n"
            f"• <code>/cancelbroadcast</code> — Stop ongoing broadcast\n\n"
            f"<i>💡 You can also use the Web Admin Dashboard to send broadcasts with images & buttons!</i>",
            parse_mode=ParseMode.HTML
        )
        return

    msg = " ".join(context.args)
    status_msg = await update.message.reply_text(
        f"🚀 <b>Initializing Turbo Broadcaster Engine...</b>\n"
        f"<i>Connecting to worker pool...</i>",
        parse_mode=ParseMode.HTML
    )

    res = start_broadcast_job(
        bot=context.bot,
        text=msg,
        concurrency=60,
        status_chat_id=update.effective_chat.id,
        status_message_id=status_msg.message_id
    )

    if not res.get("ok"):
        await status_msg.edit_text(f"{e('cross','❌')} {res.get('message')}", parse_mode=ParseMode.HTML)


async def admin_cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    res = cancel_current_broadcast()
    await update.message.reply_text(f"{e('check','✅') if res.get('ok') else e('cross','❌')} {res.get('message')}", parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT SCREENSHOT FLOW
# ─────────────────────────────────────────────────────────────────────────────

async def payment_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if uid in ADMIN_IDS:
        return

    photo    = update.message.photo[-1]
    file_id  = photo.file_id
    caption  = update.message.caption or ""

    context.user_data["pay_photo_id"]  = file_id
    context.user_data["pay_caption"]   = caption
    context.user_data["awaiting_utr"]  = True

    await update.message.reply_text(
        f"{e('check','✅')} <b>Screenshot received!</b>\n\n"
        f"{e('text','📝')} Now send the <b>deposit amount</b> (in ₹) as a message.\n"
        f"<i>Example: 500</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Cancel", callback_data="main_menu", icon_custom_emoji_id=_E["cross"], style="danger")]
        ])
    )


async def payment_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if uid in ADMIN_IDS:
        return
    if not context.user_data.get("awaiting_utr"):
        return

    text = update.message.text.strip()
    try:
        amount_inr = float(text.replace("₹", "").replace(",", "").strip())
        if amount_inr <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"{e('cross','❌')} Invalid amount. Please send a number like <code>500</code>",
            parse_mode=ParseMode.HTML
        )
        return

    photo_id = context.user_data.get("pay_photo_id")
    if not photo_id:
        await update.message.reply_text(f"{e('cross','❌')} No screenshot found. Please send your payment screenshot first.", parse_mode=ParseMode.HTML)
        return

    context.user_data.pop("awaiting_utr",  None)
    context.user_data.pop("pay_photo_id",  None)
    context.user_data.pop("pay_caption",   None)

    amount_usd = inr_to_usd(amount_inr)
    user       = update.effective_user
    uname      = f"@{user.username}" if user.username else user.first_name

    db_user  = get_or_create_user(uid)
    cur_inr  = db_user["balance"]

    pay_id = f"PAY_{uid}_{uuid.uuid4().hex[:6].upper()}"
    context.user_data[f"payreq_{pay_id}_amount"] = amount_inr
    context.user_data[f"payreq_{pay_id}_uid"]    = uid

    admin_msg = (
        f"{e('money','💰')} <b>PAYMENT REQUEST</b>\n"
        f"{'━' * 28}\n\n"
        f"{e('mani','👤')} <b>User:</b> {uname} (<code>{uid}</code>)\n"
        f"{e('cash','💵')} <b>Amount:</b> {CURRENCY_SYMBOL}{amount_inr:.0f}\n"
        f"{e('wallet','💼')} <b>Current Balance:</b> {CURRENCY_SYMBOL}{cur_inr:.0f}\n"
        f"{e('idcard','🆔')} <b>Pay ID:</b> <code>{pay_id}</code>\n"
        f"{e('point','📌')} <b>UPI:</b> <code>{UPI_ID}</code>\n\n"
        f"▶️ Approve to credit {CURRENCY_SYMBOL}{amount_inr:.0f} to user's wallet."
    )
    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Approve +{CURRENCY_SYMBOL}{amount_inr:.0f}", callback_data=f"payapprove_{pay_id}_{uid}_{amount_inr:.0f}", style="success", icon_custom_emoji_id=_E["check"]),
        ],
        [
            InlineKeyboardButton("Decline", callback_data=f"paydecline_{pay_id}_{uid}", style="danger", icon_custom_emoji_id=_E["cross"]),
        ],
    ])

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id   = admin_id,
                photo     = photo_id,
                caption   = admin_msg,
                parse_mode= ParseMode.HTML,
                reply_markup = admin_kb
            )
        except Exception as err:
            logger.error(f"Could not send to admin {admin_id}: {err}")

    await update.message.reply_text(
        f"{e('check','📤')} <b>Payment request submitted!</b>\n\n"
        f"{e('money','💵')} Amount: {CURRENCY_SYMBOL}{amount_inr:.0f}\n"
        f"{e('idcard','🆔')} Pay ID: <code>{pay_id}</code>\n\n"
        f"{e('clock','⏳')} Admin will review and approve within <b>5 minutes</b>.\n"
        f"You'll get a notification once approved! {e('check','✅')}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")]
        ])
    )


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate secure 1-tap Chrome Admin Panel login link for bot administrators."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text(f"{e('cross','❌')} Access Denied! Only bot administrators can access the panel.")
        return

    panel_base = get_setting("panel_url", PANEL_BASE_URL).rstrip("/")
    direct_chrome_link = f"{panel_base}/?token={ADMIN_AUTH_SECRET}"

    msg = (
        f"{e('crown')} <b>Tg store Admin Panel Access</b>\n"
        f"{'━' * 28}\n\n"
        f"Hey <b>{user.first_name}</b>, your private admin dashboard is ready!\n\n"
        f"🌐 <b>Direct Chrome Link:</b>\n"
        f"👉 <a href=\"{direct_chrome_link}\">{direct_chrome_link}</a>\n\n"
        f"🔐 <b>Admin Password:</b> <code>{ADMIN_PANEL_PASSWORD}</code>\n"
        f"🛡️ <b>Status:</b> Encrypted & Protected\n\n"
        f"✨ <i>Tap the link above to open directly in Google Chrome / Browser!</i>"
    )
    rows = []
    if "localhost" not in panel_base and "127.0.0.1" not in panel_base:
        rows.append([InlineKeyboardButton("🌐 Open Admin Panel in Chrome", url=direct_chrome_link, icon_custom_emoji_id=_E["link"])])
    rows.append([InlineKeyboardButton("Back to Home", callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")])

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def handle_pay_approve(query, context, pay_id: str, target_uid: int, amount: float):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (target_uid,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n❌ User not found in DB!",
            parse_mode=ParseMode.HTML
        )
        return

    new_bal_inr = row[0] + amount_inr
    conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal_inr, target_uid))
    conn.commit()
    conn.close()

    await query.edit_message_caption(
        caption=(
            f"{e('check','✅')} <b>APPROVED</b> by admin\n\n"
            f"{e('money','💵')} Credited: {CURRENCY_SYMBOL}{amount_inr:.0f}\n"
            f"{e('mani','👤')} User: <code>{target_uid}</code>\n"
            f"{e('wallet','💰')} New Balance: {CURRENCY_SYMBOL}{new_bal_inr:.0f}\n"
            f"{e('idcard','🆔')} Pay ID: <code>{pay_id}</code>"
        ),
        parse_mode=ParseMode.HTML
    )

    try:
        await context.bot.send_message(
            chat_id=target_uid,
            text=(
                f"{e('crown','🎉')} <b>Payment Approved!</b>\n\n"
                f"{e('money','💵')} <b>Credited:</b> {CURRENCY_SYMBOL}{amount_inr:.0f}\n"
                f"{e('wallet','💰')} <b>New Balance:</b> {CURRENCY_SYMBOL}{new_bal_inr:.0f}\n\n"
                f"You can now buy Telegram accounts! {e('cart','🛒')}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Browse Products", callback_data="products", icon_custom_emoji_id=_E["cart"], style="primary")],
                [InlineKeyboardButton("Home",            callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
            ])
        )
    except Exception as err:
        logger.error(f"Could not notify user {target_uid}: {err}")


async def handle_pay_decline(query, context, pay_id: str, target_uid: int):
    await query.edit_message_caption(
        caption=(
            f"{e('cross','❌')} <b>DECLINED</b> by admin\n\n"
            f"{e('mani','👤')} User: <code>{target_uid}</code>\n"
            f"{e('idcard','🆔')} Pay ID: <code>{pay_id}</code>"
        ),
        parse_mode=ParseMode.HTML
    )
    try:
        await context.bot.send_message(
            chat_id=target_uid,
            text=(
                f"{e('cross','❌')} <b>Payment Declined</b>\n\n"
                f"Your payment request was declined by admin.\n"
                f"Please contact support if you believe this is an error."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Contact Support", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}", style="primary", icon_custom_emoji_id=_E["support"])],
                [InlineKeyboardButton("Home",            callback_data="main_menu", icon_custom_emoji_id=_E["globe"], style="danger")],
            ])
        )
    except Exception as err:
        logger.error(f"Could not notify user {target_uid}: {err}")



# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def post_init(application):
    try:
        from otp_forwarder import load_all_existing, sync_sessions
        from merchant_gateway import merchant_cloud_syncer_loop
        from gmail_payment_engine import gmail_auto_syncer_loop
        asyncio.create_task(load_all_existing())
        asyncio.create_task(sync_sessions())
        asyncio.create_task(merchant_cloud_syncer_loop())
        asyncio.create_task(gmail_auto_syncer_loop())
        logger.info("✅ OTP Forwarder, Merchant Cloud & Gmail FamPay Syncer background watchers started!")
    except Exception as err:
        logger.error(f"Error starting background services: {err}")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log uncaught errors and ensure hanging callback queries are cleanly answered."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.answer("⚠️ An error occurred. Please try again.", show_alert=True)
        except Exception:
            pass


def main():
    init_db()
    print("🤖 Starting Tg store Bot (100k+ Concurrency Turbo Engine)...")
    request = HTTPXRequest(
        connection_pool_size=1000,
        pool_timeout=15.0,
        connect_timeout=15.0,
        read_timeout=15.0,
        write_timeout=15.0
    )
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("start",            start_handler))
    app.add_handler(CommandHandler("panel",            admin_panel_command))
    app.add_handler(CommandHandler("addbalance",       admin_add_balance))
    app.add_handler(CommandHandler("cutbalance",       admin_cut_balance))
    app.add_handler(CommandHandler("subbalance",       admin_cut_balance))
    app.add_handler(CommandHandler("setbalance",       admin_set_balance))
    app.add_handler(CommandHandler("checkuser",        admin_check_user))
    app.add_handler(CommandHandler("addaccount",       admin_add_account))
    app.add_handler(CommandHandler("broadcast",        admin_broadcast))
    app.add_handler(CommandHandler("cancelbroadcast",  admin_cancel_broadcast))

    app.add_handler(MessageHandler(
        filters.PHOTO,
        payment_photo_handler
    ), group=0)

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        user_text_router
    ), group=0)

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(list(ADMIN_IDS)),
        admin_receive_session
    ), group=1)

    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(ChatMemberHandler(chat_member_update_handler, ChatMemberHandler.CHAT_MEMBER))

    print("✅ Bot is live and polling with instant response!")
    app.run_polling(poll_interval=0.0, timeout=10, drop_pending_updates=False,
                    allowed_updates=["message", "callback_query", "chat_join_request", "chat_member", "my_chat_member"])


if __name__ == "__main__":
    main()
