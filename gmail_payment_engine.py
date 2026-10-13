"""
gmail_payment_engine.py — Gmail IMAP Auto-Verification Engine for FamPay & Bank Alerts
======================================================================================
Connects 24/7 via IMAP SSL to Gmail (e.g. chinupatraq@gmail.com).
Scans incoming payment alerts from FamApp / FamPay (@famapp.in / @fampay.in / Banks).
Extracts:
  - 💵 Amount (₹ / Rs. / INR)
  - 🆔 12-Digit UTR / Transaction Reference Number
  - 👤 Sender Name / Counterparty
  - 🏷️ Purpose / Top-up Code

Instant Action:
  - Records into incoming_bank_transactions table
  - Auto-approves matching pending deposit in Telegram Bot
  - Credits user wallet balance in 1 second and sends Telegram notification!
"""

import asyncio
import email
from email.header import decode_header
import imaplib
import logging
import re
from datetime import datetime
import httpx

from config import (
    BOT_TOKEN,
    CURRENCY_SYMBOL,
    GMAIL_EMAIL,
    GMAIL_PASSWORD,
    STORE_NAME
)
from emojis import e, _E
from database import (
    get_db,
    record_incoming_bank_tx,
    verify_and_redeem_utr
)

logger = logging.getLogger("GMAIL_IMAP")
logger.setLevel(logging.INFO)

# In-memory cache of already processed email message IDs to prevent redundant parsing
_processed_email_ids: set[str] = set()
_last_gmail_sync_time: datetime | None = None
_gmail_connection_status: dict = {"ok": False, "message": "Not connected yet"}


def get_gmail_status() -> dict:
    """Returns current Gmail IMAP connection health and last sync timestamp."""
    return {
        "ok": _gmail_connection_status.get("ok", False),
        "email": GMAIL_EMAIL,
        "message": _gmail_connection_status.get("message", "Idle"),
        "last_sync": _last_gmail_sync_time.strftime("%Y-%m-%d %H:%M:%S") if _last_gmail_sync_time else "Never",
        "processed_count": len(_processed_email_ids)
    }


def parse_email_body(msg) -> str:
    """Extract plain text or html content from email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get('Content-Disposition') or '')
            if ctype in ("text/plain", "text/html") and "attachment" not in cdispo:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += " " + payload.decode(errors="ignore")
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(errors="ignore")
        except Exception:
            pass
    return body


def extract_payment_details_from_text(subject: str, body: str, from_addr: str) -> dict | None:
    """
    Parses amount, UTR, sender, and purpose from FamPay/Bank email text.
    """
    full_text = f"{subject} {body}"
    clean_text = re.sub(r'<[^>]+>', ' ', full_text) # strip HTML tags
    clean_text = re.sub(r'\s+', ' ', clean_text)

    # 1. Amount Extraction (e.g. ₹100, Rs. 100, INR 100.00, credited by Rs 100, Received ₹100)
    amount = 0.0
    amt_patterns = [
        r'(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)',
        r'credited\s+(?:by|with|for)?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)',
        r'received\s+(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)',
        r'paid\s+you\s+(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)',
        r'added\s+(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)',
    ]
    for p in amt_patterns:
        m = re.search(p, clean_text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    amount = val
                    break
            except Exception:
                pass

    if amount <= 0:
        return None

    # 2. UTR / Ref No Extraction (10-16 digits strictly required)
    utr = ""
    utr_patterns = [
        r'(?:UTR|Ref(?:\s*No)?|RRN|Txn(?:\s*ID)?|UPI\s*Ref(?:\s*No)?)\s*[:#-]?\s*([0-9]{10,16})',
        r'UPI\s*/\s*([0-9]{10,16})',
        r'\b([0-9]{12})\b', # Standard 12-digit UPI UTR
        r'Transaction\s*ID\s*[:#-]?\s*([0-9]{10,18})'
    ]
    for p in utr_patterns:
        m = re.search(p, clean_text, re.IGNORECASE)
        if m:
            utr = m.group(1).strip()
            break

    # If NO valid UTR found, this is a normal email (not a genuine bank payment alert)
    if not utr:
        return None

    # 3. Sender Name
    sender = "FamPay / UPI Payer"
    sender_patterns = [
        r'from\s+([A-Za-z0-9\s]{3,30})\b',
        r'by\s+UPI\s+user\s+([A-Za-z0-9\s]{3,30})\b',
        r'received\s+from\s+([A-Za-z0-9\s]{3,30})\b',
        r'paid\s+by\s+([A-Za-z0-9\s]{3,30})\b'
    ]
    for p in sender_patterns:
        m = re.search(p, clean_text, re.IGNORECASE)
        if m:
            s_name = m.group(1).strip()
            if not s_name.lower().startswith("famapp") and not s_name.lower().startswith("google"):
                sender = s_name
                break

    # 4. Purpose / Custom Code (e.g. FAMBOTBY... or TOPUP_<uid>_<rand>)
    purpose = ""
    purp_m = re.search(r'(FAMBOTBY[A-Za-z0-9_]{4,20}|TOPUP_[0-9]+(?:_[0-9A-Za-z]+)?)', clean_text, re.IGNORECASE)
    if purp_m:
        purpose = purp_m.group(1).upper()

    return {
        "amount": amount,
        "utr": utr,
        "sender": sender,
        "purpose": purpose,
        "from": from_addr,
        "subject": subject
    }


def scan_gmail_inbox_sync() -> list[dict]:
    """
    Synchronous IMAP scan of Gmail inbox for new FamPay & Bank payment alert emails.
    """
    global _last_gmail_sync_time, _gmail_connection_status

    if not GMAIL_EMAIL or not GMAIL_PASSWORD:
        _gmail_connection_status = {"ok": False, "message": "Gmail credentials not configured."}
        return []

    clean_pass = GMAIL_PASSWORD.replace(" ", "")
    found_txs = []

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_EMAIL, clean_pass)
        status, _ = mail.select("inbox")
        if status != "OK":
            mail.logout()
            _gmail_connection_status = {"ok": False, "message": "Could not select Inbox folder."}
            return []

        _gmail_connection_status = {"ok": True, "message": "Connected & Active"}
        _last_gmail_sync_time = datetime.now()

        # Search recent 25 emails in inbox
        status, messages = mail.search(None, "ALL")
        if status == "OK" and messages[0]:
            msg_ids = messages[0].split()[-25:]

            for e_id in reversed(msg_ids):
                e_id_str = e_id.decode() if isinstance(e_id, bytes) else str(e_id)
                if e_id_str in _processed_email_ids:
                    continue

                status, data = mail.fetch(e_id, "(RFC822)")
                if status != "OK" or not data or not data[0]:
                    continue

                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Decode Subject
                raw_subj = msg.get("Subject", "")
                subject = ""
                try:
                    dec_list = decode_header(raw_subj)
                    for item, enc in dec_list:
                        if isinstance(item, bytes):
                            subject += item.decode(enc or "utf-8", errors="ignore")
                        else:
                            subject += str(item)
                except Exception:
                    subject = str(raw_subj)

                from_addr = str(msg.get("From", ""))
                body = parse_email_body(msg)

                # Filter genuine payment-related emails only
                f_low = from_addr.lower()
                s_low = subject.lower()
                is_payment_alert = (
                    "famapp" in f_low or "fampay" in f_low or "famapp" in s_low or "fampay" in s_low or
                    "alerts@" in f_low or "notify@" in f_low or "bank" in f_low or "upi" in f_low or
                    "credited" in s_low or "payment received" in s_low or "money received" in s_low
                )

                if is_payment_alert:
                    parsed = extract_payment_details_from_text(subject, body, from_addr)
                    if parsed and parsed.get("utr"):
                        parsed["email_id"] = e_id_str
                        found_txs.append(parsed)

                _processed_email_ids.add(e_id_str)

        mail.close()
        mail.logout()
        return found_txs

    except Exception as e:
        logger.error(f"Gmail IMAP scanning error: {e}")
        _gmail_connection_status = {"ok": False, "message": f"Error: {e}"}
        return []


async def scan_and_auto_credit_gmail():
    """
    Scans Gmail asynchronously in thread pool, records transactions, and auto-credits users.
    """
    new_txs = await asyncio.to_thread(scan_gmail_inbox_sync)
    new_credits = 0

    for tx in new_txs:
        utr = tx["utr"]
        amt = tx["amount"]
        sender = tx["sender"]
        purpose = tx.get("purpose", "")

        # Record incoming bank/fam transaction
        rec = record_incoming_bank_tx(
            utr=utr,
            amount=amt,
            sender_name=sender,
            source_app="FamPay Gmail IMAP Alert"
        )

        if rec.get("is_new"):
            new_credits += 1
            logger.info(f"⚡ [GMAIL IMAP] New Payment Detected: ₹{amt:.2f} | UTR: {utr} | Sender: {sender} | Purpose: {purpose}")
            await check_and_auto_credit_pending_deposit(utr=utr, amount=amt, purpose=purpose)

    return new_credits


async def check_and_auto_credit_pending_deposit(utr: str, amount: float, purpose: str = ""):
    """
    Checks if a Telegram user is waiting for this payment and credits their wallet instantly.
    """
    conn = get_db()
    cursor = conn.cursor()

    target_dep = None
    if purpose:
        cursor.execute("SELECT * FROM deposits WHERE tx_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1", (purpose,))
        target_dep = cursor.fetchone()

        if not target_dep and purpose.startswith("TOPUP_"):
            try:
                parts = purpose.split("_")
                if len(parts) >= 2 and parts[1].isdigit():
                    t_uid = int(parts[1])
                    cursor.execute("SELECT * FROM deposits WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1", (t_uid,))
                    target_dep = cursor.fetchone()
            except Exception:
                pass

    if not target_dep:
        cursor.execute("""
            SELECT * FROM deposits 
            WHERE (utr = ? OR (utr = '' AND amount = ?)) AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
        """, (utr, amount))
        target_dep = cursor.fetchone()

    pending_dep = target_dep
    conn.close()

    if pending_dep:
        uid = pending_dep["user_id"]
        dep_tx_id = pending_dep["tx_id"]
        res = verify_and_redeem_utr(user_id=uid, utr=utr, expected_amount=amount)
        if res.get("status") == "approved":
            try:
                conn_u = get_db()
                cur_u = conn_u.cursor()
                cur_u.execute("UPDATE deposits SET status = 'completed', utr = ? WHERE tx_id = ?", (utr, dep_tx_id))
                conn_u.commit()
                conn_u.close()
            except Exception:
                pass
            try:
                new_bal = res.get("new_balance", 0.0)
                msg = (
                    f"{e('check')} <b>Payment Received — Thank You!</b>\n"
                    f"{'━' * 28}\n\n"
                    f"{e('coin')} <b>Amount Credited:</b> {CURRENCY_SYMBOL}{amount:.2f}\n"
                    f"{e('wallet')} <b>New Wallet Balance:</b> <b>{CURRENCY_SYMBOL}{new_bal:.2f}</b>\n"
                    f"{e('card')} <b>UTR:</b> <code>{utr}</code>\n"
                    f"{e('clock')} <b>Status:</b> <b>Completed</b> {e('check')}\n\n"
                    f"✨ <i>Thank you for choosing {STORE_NAME}! Your balance is ready to use.</i>"
                )
                kb = {
                    "inline_keyboard": [
                        [{"text": "Browse Products", "callback_data": "products", "icon_custom_emoji_id": _E["cart"]}],
                        [{"text": "My Account", "callback_data": "user_account", "icon_custom_emoji_id": _E["mani"]}],
                        [{"text": "Home", "callback_data": "main_menu", "icon_custom_emoji_id": _E["globe"]}]
                    ]
                }
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                async with httpx.AsyncClient() as client:
                    await client.post(url, json={"chat_id": uid, "text": msg, "parse_mode": "HTML", "reply_markup": kb})
                logger.info(f"✅ User {uid} credited with ₹{amount:.2f} for UTR {utr} via Gmail engine")
            except Exception as e:
                logger.error(f"Failed sending Telegram approval notification: {e}")


async def gmail_auto_syncer_loop():
    """
    24/7 Background Continuous Polling Loop for Gmail FamPay & Bank Alerts (every 6 seconds).
    """
    logger.info("🚀 Gmail IMAP Auto-Verification background syncer started!")
    while True:
        try:
            await scan_and_auto_credit_gmail()
        except Exception as e:
            logger.error(f"Error in gmail_auto_syncer_loop: {e}")
        await asyncio.sleep(6)
