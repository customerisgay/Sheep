"""
broadcaster.py — Ultra High-Speed Power Broadcaster Engine
===========================================================
Blazing fast multi-worker asynchronous broadcast system designed for
10k-100k+ Telegram members with real-time rate limiting, FloodWait shield,
and live ETA speedometer.
"""
import asyncio
import time
import logging
from typing import Optional, List, Dict
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden, BadRequest, TelegramError
from database import get_db

logger = logging.getLogger("BROADCASTER")

class BroadcasterState:
    def __init__(self):
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self.cancel_requested = False
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self.total: int = 0
        self.sent: int = 0
        self.blocked: int = 0
        self.failed: int = 0
        self.speed: float = 0.0
        self.eta: float = 0.0
        self.progress: float = 0.0
        self.status: str = "idle"
        self.last_error: str = ""
        self.message_preview: str = ""
        self.history: List[Dict] = []

    def to_dict(self) -> dict:
        elapsed = (time.time() - self.start_time) if self.is_running else (self.end_time - self.start_time if self.start_time else 0)
        elapsed = max(0.0, elapsed)
        
        # Calculate real-time speed
        speed = round(self.sent / elapsed, 1) if (elapsed > 0 and self.sent > 0) else 0.0
        remaining = max(0, self.total - (self.sent + self.blocked + self.failed))
        eta = round(remaining / speed, 1) if (speed > 0 and remaining > 0) else 0.0
        pct = round(((self.sent + self.blocked + self.failed) / self.total * 100), 1) if self.total > 0 else 0.0

        return {
            "is_running": self.is_running,
            "status": self.status,
            "total": self.total,
            "sent": self.sent,
            "blocked": self.blocked,
            "failed": self.failed,
            "remaining": remaining,
            "progress_percent": pct,
            "speed_per_sec": speed,
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": eta,
            "last_error": self.last_error,
            "message_preview": self.message_preview[:150] if self.message_preview else "",
            "history": self.history[-10:]
        }

state = BroadcasterState()

def get_broadcast_status() -> dict:
    d = state.to_dict()
    if not state.is_running and d["total"] <= 1:
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            count = cursor.fetchone()[0]
            conn.close()
            if count > 1:
                d["total"] = count
            else:
                try:
                    from members_data import TOTAL_MEMBERS_COUNT
                    d["total"] = TOTAL_MEMBERS_COUNT
                except Exception:
                    pass
        except Exception:
            pass
    return d

def cancel_current_broadcast() -> dict:
    if not state.is_running:
        return {"ok": False, "message": "No broadcast is currently running."}
    state.cancel_requested = True
    state.status = "cancelling"
    return {"ok": True, "message": "Broadcast cancellation requested. Stopping workers..."}

async def _send_single_message(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    photo_url: Optional[str] = None,
    pin: bool = False,
    semaphore: Optional[asyncio.Semaphore] = None
):
    if state.cancel_requested:
        return

    async with (semaphore if semaphore else asyncio.Semaphore(1)):
        for attempt in range(3):
            if state.cancel_requested:
                return
            try:
                msg = None
                if photo_url:
                    msg = await bot.send_photo(
                        chat_id=user_id,
                        photo=photo_url,
                        caption=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
                else:
                    msg = await bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                        disable_web_page_preview=False
                    )

                if pin and msg:
                    try:
                        await bot.pin_chat_message(chat_id=user_id, message_id=msg.message_id, disable_notification=True)
                    except Exception:
                        pass

                state.sent += 1
                return

            except RetryAfter as e:
                # FloodWait: sleep only for this individual worker
                sleep_time = min(e.retry_after + 0.5, 30.0)
                await asyncio.sleep(sleep_time)
                continue

            except Forbidden:
                # User blocked bot or account deactivated
                state.blocked += 1
                return

            except BadRequest as e:
                err_str = str(e).lower()
                if "chat not found" in err_str or "user is deactivated" in err_str or "blocked" in err_str:
                    state.blocked += 1
                else:
                    state.failed += 1
                return

            except TelegramError:
                state.failed += 1
                return

            except Exception:
                state.failed += 1
                return

async def _run_broadcast_pipeline(
    bot: Bot,
    user_ids: List[int],
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    photo_url: Optional[str] = None,
    pin: bool = False,
    concurrency: int = 50,
    status_chat_id: Optional[int] = None,
    status_message_id: Optional[int] = None
):
    state.is_running = True
    state.cancel_requested = False
    state.start_time = time.time()
    state.end_time = 0.0
    state.total = len(user_ids)
    state.sent = 0
    state.blocked = 0
    state.failed = 0
    state.status = "running"
    state.message_preview = text

    semaphore = asyncio.Semaphore(concurrency)
    logger.info(f"🚀 Turbo Broadcast starting to {state.total} users with {concurrency} workers...")

    # Background task for live status updates to Telegram admin if applicable
    async def status_updater():
        while state.is_running:
            await asyncio.sleep(2.0)
            if not state.is_running:
                break
            if status_chat_id and status_message_id:
                try:
                    d = state.to_dict()
                    bars = int(d["progress_percent"] / 10)
                    prog_bar = "█" * bars + "░" * (10 - bars)
                    status_text = (
                        f"📢 <b>⚡ Power Broadcaster Live Engine</b>\n"
                        f"{'━' * 32}\n"
                        f"📊 <b>Progress:</b> [{prog_bar}] <b>{d['progress_percent']}%</b>\n\n"
                        f"✅ <b>Sent:</b> <code>{d['sent']:,}</code> / <code>{d['total']:,}</code>\n"
                        f"🚫 <b>Blocked:</b> <code>{d['blocked']:,}</code>\n"
                        f"⚠️ <b>Failed:</b> <code>{d['failed']:,}</code>\n\n"
                        f"🚀 <b>Speed:</b> <code>{d['speed_per_sec']:,} msg/sec</code>\n"
                        f"⏳ <b>ETA:</b> <code>{d['eta_seconds']}s</code> • <b>Elapsed:</b> <code>{d['elapsed_seconds']}s</code>\n"
                        f"{'━' * 32}\n"
                        f"<i>Broadcasting with {concurrency} async workers...</i>"
                    )
                    await bot.edit_message_text(
                        chat_id=status_chat_id,
                        message_id=status_message_id,
                        text=status_text,
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

    updater_task = asyncio.create_task(status_updater())

    try:
        tasks = [
            _send_single_message(
                bot=bot,
                user_id=uid,
                text=text,
                reply_markup=reply_markup,
                photo_url=photo_url,
                pin=pin,
                semaphore=semaphore
            )
            for uid in user_ids
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as err:
        state.last_error = str(err)
        state.status = "error"
        logger.error(f"Broadcast error: {err}")
    finally:
        state.is_running = False
        state.end_time = time.time()
        updater_task.cancel()

        if state.cancel_requested:
            state.status = "cancelled"
        elif state.status != "error":
            state.status = "completed"

        elapsed = round(max(0.1, state.end_time - state.start_time), 1)
        speed = round(state.sent / elapsed, 1)

        # Log history
        state.history.append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": state.total,
            "sent": state.sent,
            "blocked": state.blocked,
            "failed": state.failed,
            "elapsed_seconds": elapsed,
            "speed": speed,
            "status": state.status
        })

        logger.info(f"✅ Broadcast finished! Sent: {state.sent}/{state.total} in {elapsed}s (~{speed} msg/sec)")

        # Final Telegram update
        if status_chat_id and status_message_id:
            try:
                final_text = (
                    f"🎉 <b>⚡ Power Broadcast Finished!</b>\n"
                    f"{'━' * 32}\n"
                    f"✅ <b>Successfully Sent:</b> <code>{state.sent:,}</code>\n"
                    f"🚫 <b>Blocked/Dead:</b> <code>{state.blocked:,}</code>\n"
                    f"⚠️ <b>Failed:</b> <code>{state.failed:,}</code>\n"
                    f"👥 <b>Total Target:</b> <code>{state.total:,}</code>\n\n"
                    f"⚡ <b>Avg Speed:</b> <code>{speed:,} msg/sec</code>\n"
                    f"⏱️ <b>Time Taken:</b> <code>{elapsed} seconds</code>\n"
                    f"{'━' * 32}"
                )
                await bot.edit_message_text(
                    chat_id=status_chat_id,
                    message_id=status_message_id,
                    text=final_text,
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

def start_broadcast_job(
    bot: Bot,
    text: str,
    photo_url: Optional[str] = None,
    button_text: Optional[str] = None,
    button_url: Optional[str] = None,
    pin: bool = False,
    concurrency: int = 50,
    status_chat_id: Optional[int] = None,
    status_message_id: Optional[int] = None
) -> dict:
    """Start high-speed background broadcast task."""
    if state.is_running:
        return {"ok": False, "message": "Another broadcast is already in progress. Please wait or cancel it."}

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = [r["user_id"] for r in cursor.fetchall()]
    conn.close()

    if len(users) < 8000:
        try:
            from members_data import ALL_MEMBER_IDS
            users = list(dict.fromkeys(users + ALL_MEMBER_IDS))
            logger.info(f"Targeting combined dataset of {len(users):,} users for broadcast!")
        except Exception as e:
            logger.warning(f"Could not load members_data: {e}")

    if not users:
        return {"ok": False, "message": "No registered users found in database to broadcast to."}

    reply_markup = None
    if button_text and button_url:
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(text=button_text, url=button_url)]])

    state.task = asyncio.create_task(
        _run_broadcast_pipeline(
            bot=bot,
            user_ids=users,
            text=text,
            reply_markup=reply_markup,
            photo_url=photo_url,
            pin=pin,
            concurrency=concurrency,
            status_chat_id=status_chat_id,
            status_message_id=status_message_id
        )
    )

    return {
        "ok": True,
        "message": f"🚀 Turbo Broadcast started for {len(users):,} users with {concurrency} async workers!",
        "total": len(users)
    }
