"""
account_panel.py — Web Admin Dashboard with Direct NetBanking, Cloud Merchant & Auto UPI Gateway
=================================================================================================
Access at: http://localhost:8001
Features:
  - 📱 Store Stock Catalog: 196+ Countries live stock, add account via browser, login OTP & 2FA
  - 💸 User Sold Accounts Dashboard: View all accounts sold by users, device login OTP/2FA, UPI payouts
  - 🏦 Direct NetBanking & Bank Account Login: SBI, HDFC, ICICI, Axis, Kotak, PNB, Paytm Bank (User ID + Password + OTP)
  - 💳 Cloud Merchant Banking Login: Phone + OTP direct login for Paytm Business / BharatPe / Merchant
  - ⚡ 24/7 Cloud Background Transaction Polling & Instant Telegram Wallet Top-up
  - 👥 User Balance Manager: Credit/Deduct/Set balances with quick chips
"""

import asyncio
import logging
import re
import httpx
from typing import Optional, List, Dict
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from database import (
    init_db, get_db,
    record_incoming_bank_tx, verify_and_redeem_utr,
    get_all_bank_transactions, get_gateway_stats,
    get_active_bank_sessions, delete_bank_session,
    get_active_fampay_session, delete_fampay_session
)
from merchant_gateway import (
    get_active_merchant_session, start_merchant_login,
    verify_merchant_login, sync_merchant_transactions_now,
    delete_merchant_session, merchant_cloud_syncer_loop
)
from netbanking_engine import (
    initiate_bank_login, verify_bank_login_otp,
    poll_connected_bank_statements, netbanking_auto_syncer_loop
)
from fampay_engine import (
    start_fampay_login, verify_fampay_otp,
    connect_fampay_by_token, sync_fampay_transactions,
    fampay_auto_syncer_loop
)
from gmail_payment_engine import (
    get_gmail_status, scan_and_auto_credit_gmail,
    gmail_auto_syncer_loop
)
from config import BOT_TOKEN, CURRENCY_SYMBOL, STORE_NAME, UPI_ID, UPI_NAME, GMAIL_EMAIL
from pyrogram_manager import (
    start_login, complete_login, get_all_accounts,
    delete_account, update_account_price, update_category_price,
    edit_account_details,
    get_stats, get_all_users, update_user_balance,
    fetch_account_login_otp
)
from broadcaster import start_broadcast_job, cancel_current_broadcast, get_broadcast_status
from countries import WORLD_COUNTRIES

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [PANEL] %(levelname)s - %(message)s")
logger = logging.getLogger("PANEL")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.create_task(merchant_cloud_syncer_loop())
    asyncio.create_task(gmail_auto_syncer_loop())
    yield

app = FastAPI(title="TG Account Manager & Cloud Bank Gateway Panel", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS FOR SELLER PAYOUTS
# ─────────────────────────────────────────────────────────────────────────────

def get_seller_payouts() -> list[dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sp.id, sp.seller_user_id, sp.seller_username, sp.phone,
               sp.country, sp.age_months, sp.payout_amount, sp.upi_id,
               sp.status, sp.created_at,
               acc.flag_emoji, acc.two_fa_password, acc.is_sold, acc.is_monitoring
        FROM seller_payouts sp
        LEFT JOIN tg_accounts acc ON sp.phone = acc.phone
        ORDER BY sp.created_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_seller_stats() -> dict:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM seller_payouts")
    total_sold = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(payout_amount), 0) FROM seller_payouts WHERE status = 'pending'")
    r_pend = cursor.fetchone()
    pending_count, pending_amt = r_pend[0], r_pend[1]

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(payout_amount), 0) FROM seller_payouts WHERE status = 'paid'")
    r_paid = cursor.fetchone()
    paid_count, paid_amt = r_paid[0], r_paid[1]

    conn.close()
    return {
        "total_sold": total_sold,
        "pending_count": pending_count,
        "pending_amt": pending_amt,
        "paid_count": paid_count,
        "paid_amt": paid_amt
    }


async def update_seller_payout_status(payout_id: int, status: str) -> dict:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM seller_payouts WHERE id = ?", (payout_id,))
    p = cursor.fetchone()
    if not p:
        conn.close()
        return {"ok": False, "message": "Payout request not found"}

    cursor.execute("UPDATE seller_payouts SET status = ? WHERE id = ?", (status, payout_id))
    conn.commit()
    conn.close()

    if status == "paid":
        try:
            msg = (
                f"🎉 <b>Payout Transferred Successfully!</b>\n"
                f"{'━' * 28}\n\n"
                f"Your payout of <b>{CURRENCY_SYMBOL}{p['payout_amount']:.2f}</b> for account <code>{p['phone']}</code> has been transferred to your UPI ID <code>{p['upi_id']}</code>.\n\n"
                f"✨ <i>Thank you for selling with {STORE_NAME}!</i>"
            )
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={
                    "chat_id": p["seller_user_id"],
                    "text": msg,
                    "parse_mode": "HTML"
                })
        except Exception as e:
            logger.warning(f"Failed to notify seller on Telegram: {e}")

    return {"ok": True, "message": f"Payout #{payout_id} marked as {status.upper()}!"}


def delete_seller_payout(payout_id: int) -> dict:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM seller_payouts WHERE id = ?", (payout_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "message": f"Payout #{payout_id} deleted successfully."}


# ─────────────────────────────────────────────────────────────────────────────
# HTML DASHBOARD (Embedded)
# ─────────────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>TG Account Manager & Direct NetBanking Gateway — Admin Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet"/>
<style>
  :root {
    --bg: #0a0e1a;
    --surface: #111827;
    --surface2: #1a2235;
    --border: #1e2d47;
    --accent: #3b82f6;
    --accent2: #6366f1;
    --green: #10b981;
    --red: #ef4444;
    --yellow: #f59e0b;
    --text: #e2e8f0;
    --muted: #64748b;
    --radius: 14px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }

  /* NAVBAR */
  .navbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 32px; border-bottom: 1px solid var(--border);
    background: rgba(17,24,39,0.9); backdrop-filter: blur(12px);
    position: sticky; top: 0; z-index: 100;
  }
  .navbar-brand { display: flex; align-items: center; gap: 12px; }
  .navbar-brand .logo { font-size: 24px; }
  .navbar-brand h1 { font-size: 18px; font-weight: 700; background: linear-gradient(135deg,#3b82f6,#8b5cf6); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
  .navbar-brand p { font-size: 12px; color: var(--muted); }
  .btn-add { background: linear-gradient(135deg, #10b981, #059669); border: none; color: white; padding: 10px 20px; border-radius: 10px; font-size: 14px; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: all 0.2s; }
  .btn-add:hover { transform: translateY(-1px); box-shadow: 0 8px 24px rgba(16,185,129,0.4); }

  /* MAIN */
  .main { max-width: 1280px; margin: 0 auto; padding: 28px 24px; }

  /* NAV TABS */
  .nav-tabs { display: flex; gap: 10px; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 12px; flex-wrap: wrap; }
  .tab-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); padding: 10px 20px; border-radius: 10px; font-size: 14px; font-weight: 600; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; gap: 8px; }
  .tab-btn:hover { color: var(--text); border-color: var(--accent); }
  .tab-btn.active { background: linear-gradient(135deg, #3b82f6, #6366f1); color: white; border-color: transparent; box-shadow: 0 4px 16px rgba(99,102,241,0.3); }
  .tab-btn .badge-count { background: #ef4444; color: white; font-size: 11px; padding: 2px 7px; border-radius: 10px; font-weight: 700; }
  .tab-content { display: none; }
  .tab-content.active { display: block; animation: fadeIn 0.3s ease; }
  @keyframes fadeIn { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }

  /* STATS */
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap: 16px; margin-bottom: 28px; }
  .stat-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; position: relative; overflow: hidden;
  }
  .stat-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; }
  .stat-card.total::before { background: linear-gradient(90deg, #3b82f6, #6366f1); }
  .stat-card.available::before { background: linear-gradient(90deg, #10b981, #059669); }
  .stat-card.sold::before { background: linear-gradient(90deg, #ef4444, #dc2626); }
  .stat-card.monitoring::before { background: linear-gradient(90deg, #f59e0b, #d97706); }
  .stat-num { font-size: 32px; font-weight: 800; margin: 8px 0 4px; }
  .stat-label { font-size: 12px; color: var(--muted); font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }
  .stat-icon { font-size: 28px; position: absolute; right: 20px; top: 20px; opacity: 0.2; }

  /* COUNTRY STOCK RANKING WIDGET */
  .country-ranking-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 20px; margin-bottom: 32px;
  }
  .country-ranking-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .country-chips-wrap {
    display: flex; gap: 8px; flex-wrap: wrap; max-height: 120px; overflow-y: auto; padding-right: 4px;
  }
  .country-chip {
    background: var(--surface2); border: 1px solid var(--border); border-radius: 8px;
    padding: 6px 12px; font-size: 13px; cursor: pointer; display: flex; align-items: center; gap: 6px;
    transition: all 0.2s;
  }
  .country-chip:hover { border-color: var(--accent); background: rgba(59,130,246,0.1); }
  .country-chip.active { border-color: var(--green); background: rgba(16,185,129,0.15); color: #34d399; font-weight: 600; }
  .country-chip .stock-badge {
    background: rgba(16,185,129,0.2); color: #10b981; padding: 2px 6px; border-radius: 12px; font-size: 11px; font-weight: 700;
  }
  .country-chip .stock-badge.zero {
    background: rgba(100,116,139,0.2); color: var(--muted); font-weight: 500;
  }

  /* AGE CARDS GRID */
  .age-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 6px; }
  .age-card {
    background: #0f172a; border: 1.5px solid rgba(255,255,255,0.1); border-radius: 8px;
    padding: 10px 12px; cursor: pointer; display: flex; flex-direction: column; gap: 2px;
    transition: all 0.2s; user-select: none;
  }
  .age-card:hover { border-color: #6366f1; background: rgba(99,102,241,0.1); }
  .age-card.active { border-color: #10b981; background: rgba(16,185,129,0.15); box-shadow: 0 0 0 1px #10b981; }
  .age-card .age-title { font-size: 13px; font-weight: 700; color: #fff; display: flex; align-items: center; justify-content: space-between; }
  .age-card .age-sub { font-size: 11px; color: #94a3b8; }
  .age-card.active .age-title { color: #34d399; }

  /* SECTION */
  .section-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; flex-wrap: wrap; gap: 12px; }
  .section-title { font-size: 16px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
  .controls-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .refresh-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 8px 14px; border-radius: 8px; font-size: 13px; cursor: pointer; display: flex; align-items: center; gap: 6px; transition: all 0.2s; }
  .refresh-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* TABLE */
  .table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; text-align: left; font-size: 13.5px; }
  th { background: var(--surface2); color: var(--muted); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; padding: 14px 18px; border-bottom: 1px solid var(--border); }
  td { padding: 14px 18px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.015); }

  .badge { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .badge.available { background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }
  .badge.sold { background: rgba(239,68,68,0.15); color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
  .badge.monitoring { background: rgba(245,158,11,0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3); }
  .badge.pending { background: rgba(245,158,11,0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3); }
  .badge.paid { background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }
  .badge.rejected { background: rgba(239,68,68,0.15); color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
  .badge .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }

  .phone-cell { font-family: monospace; font-weight: 600; color: #93c5fd; font-size: 14px; }
  .price-cell { font-weight: 700; color: #10b981; font-size: 14px; }
  .del-btn { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.25); color: #f87171; padding: 6px 12px; border-radius: 8px; cursor: pointer; font-size: 12px; transition: all 0.2s; }
  .del-btn:hover { background: #ef4444; color: white; }

  /* ACTION BUTTONS */
  .btn-bal { padding: 6px 12px; border-radius: 8px; border: none; font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
  .btn-bal.add { background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }
  .btn-bal.add:hover { background: #10b981; color: white; }
  .btn-bal.cut { background: rgba(239,68,68,0.15); color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
  .btn-bal.cut:hover { background: #ef4444; color: white; }
  .btn-bal.set { background: rgba(59,130,246,0.15); color: #60a5fa; border: 1px solid rgba(59,130,246,0.3); }
  .btn-bal.set:hover { background: #3b82f6; color: white; }
  .bal-actions { display: flex; gap: 6px; }
  .search-box { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 8px 14px; color: var(--text); font-size: 13px; outline: none; width: 240px; }
  .search-box:focus { border-color: var(--accent); }
  .quick-chips { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
  .chip { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 4px 10px; font-size: 12px; cursor: pointer; color: var(--text); }
  .chip:hover { border-color: var(--accent); color: var(--accent); }

  /* CLOUD MERCHANT & NETBANKING BOXES */
  .grid-two-cols { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 20px; margin-bottom: 24px; }
  @media(max-width: 900px) { .grid-two-cols { grid-template-columns: 1fr; } }
  
  .merchant-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 24px; position: relative;
  }
  .connected-box {
    background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.3);
    border-radius: 12px; padding: 18px; margin-top: 14px;
  }

  /* MODAL */
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(6px); z-index: 200; display: none; align-items: center; justify-content: center; }
  .overlay.active { display: flex; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: 20px; padding: 32px; width: 100%; max-width: 500px; position: relative; animation: slideUp 0.3s ease; max-height: 90vh; overflow-y: auto; }
  @keyframes slideUp { from{transform:translateY(30px);opacity:0} to{transform:translateY(0);opacity:1} }
  .modal-title { font-size: 20px; font-weight: 700; margin-bottom: 6px; }
  .modal-sub { font-size: 13px; color: var(--muted); margin-bottom: 24px; }
  .modal-close { position: absolute; top: 16px; right: 16px; background: var(--surface2); border: none; color: var(--muted); width: 32px; height: 32px; border-radius: 8px; cursor: pointer; font-size: 16px; display: flex; align-items: center; justify-content: center; }
  .modal-close:hover { color: var(--text); }
  .step-indicators { display: flex; gap: 8px; margin-bottom: 24px; }
  .step-dot { flex: 1; height: 4px; border-radius: 2px; background: var(--border); transition: background 0.3s; }
  .step-dot.active { background: var(--accent); }
  .step-dot.done { background: var(--green); }

  /* FORM */
  .form-group { margin-bottom: 16px; }
  label { display: block; font-size: 13px; font-weight: 500; color: var(--muted); margin-bottom: 6px; }
  input, select { width: 100%; background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 11px 14px; border-radius: 10px; font-size: 14px; outline: none; transition: border 0.2s; font-family: inherit; }
  input:focus, select:focus { border-color: var(--accent); }
  .input-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .form-btn { width: 100%; background: linear-gradient(135deg,#3b82f6,#6366f1); border: none; color: white; padding: 13px; border-radius: 10px; font-size: 15px; font-weight: 600; cursor: pointer; transition: all 0.2s; margin-top: 4px; }
  .form-btn:hover { transform: translateY(-1px); box-shadow: 0 8px 24px rgba(99,102,241,0.35); }
  .form-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
  .otp-input { font-family: monospace; font-size: 22px; letter-spacing: 8px; text-align: center; font-weight: 700; }

  /* TOAST */
  .toast-wrap { position: fixed; bottom: 24px; right: 24px; z-index: 999; display: flex; flex-direction: column; gap: 10px; }
  .toast { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 18px; min-width: 280px; font-size: 14px; display: flex; align-items: center; gap: 10px; animation: toastIn 0.3s ease; box-shadow: 0 8px 30px rgba(0,0,0,0.4); }
  .toast.success { border-color: rgba(16,185,129,0.4); }
  .toast.error { border-color: rgba(239,68,68,0.4); }
  @keyframes toastIn { from{transform:translateX(100px);opacity:0} to{transform:translateX(0);opacity:1} }

  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid rgba(255,255,255,0.3); border-top-color: white; border-radius: 50%; animation: spin 0.7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .info-box { background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.2); border-radius: 10px; padding: 12px 14px; font-size: 13px; color: #93c5fd; margin-bottom: 16px; display: flex; gap: 10px; align-items: flex-start; }
</style>
</head>
<body>

<!-- LOGIN OVERLAY -->
<div id="login-screen" style="display:flex;position:fixed;top:0;left:0;right:0;bottom:0;background:#0a0e1a;z-index:99999;align-items:center;justify-content:center;padding:20px;">
  <div style="background:#111827;border:1px solid #1e2d47;border-radius:18px;padding:32px;max-width:420px;width:100%;box-shadow:0 20px 50px rgba(0,0,0,0.6);text-align:center;">
    <div style="font-size:42px;margin-bottom:12px;">🔐</div>
    <h2 style="font-size:20px;font-weight:700;margin-bottom:6px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Tg store Admin Login</h2>
    <p style="font-size:13px;color:#94a3b8;margin-bottom:22px;">Enter your admin password to unlock the dashboard</p>

    <div id="login-err" style="display:none;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);color:#f87171;padding:10px;border-radius:8px;font-size:13px;margin-bottom:14px;"></div>

    <div style="margin-bottom:16px;text-align:left;">
      <label style="font-size:12px;font-weight:600;color:#cbd5e1;margin-bottom:6px;display:block;">Admin Secret Password</label>
      <input type="password" id="login-pwd" placeholder="Enter admin password..." style="width:100%;padding:12px 14px;border-radius:10px;border:1px solid #1e2d47;background:#1a2235;color:#fff;font-size:14px;outline:none;" onkeydown="if(event.key==='Enter')doLogin()"/>
    </div>

    <button class="form-btn" style="background:linear-gradient(135deg,#3b82f6,#6366f1);width:100%;padding:13px;font-size:15px;font-weight:600;" onclick="doLogin()">🚀 Unlock Dashboard</button>

    <div style="margin-top:20px;padding:12px;background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.2);border-radius:10px;font-size:12px;color:#cbd5e1;text-align:left;line-height:1.5;">
      💡 <b>1-Tap Chrome Access:</b><br/>
      Type <code>/panel</code> in your Telegram Bot to get a direct 1-tap Chrome login link without typing password!
    </div>
  </div>
</div>

<div id="app-content" style="display:none;">

<!-- NAVBAR -->
<nav class="navbar">
  <div class="navbar-brand">
    <span class="logo">🤖</span>
    <div>
      <h1>TG Account Manager & Bank Gateway</h1>
      <p>Admin Panel — 196+ Countries, Seller Payouts & 24/7 NetBanking Statement Polling</p>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:10px;">
    <button class="btn-bal set" style="background:linear-gradient(135deg,#0284c7,#0369a1);color:#fff;font-size:13px;font-weight:700;padding:10px 16px;border-radius:10px;border:none;cursor:pointer;display:flex;align-items:center;gap:6px;box-shadow:0 4px 12px rgba(2,132,199,0.3);" onclick="openCategoryPriceModal()">
      🏷️ Category Price Manager
    </button>
    <button class="btn-add" onclick="openModal()">
      ➕ Add Telegram Account
    </button>
    <button class="btn-bal cut" style="font-size:13px;padding:9px 14px;border-radius:10px;" onclick="doLogout()">
      🚪 Logout
    </button>
  </div>
</nav>

<!-- MAIN CONTENT -->
<div class="main">

  <!-- TAB NAVIGATION -->
  <div class="nav-tabs">
    <button class="tab-btn active" id="tab-btn-stock" onclick="switchTab('stock')">
      📱 Store Stock Catalog
    </button>
    <button class="tab-btn" id="tab-btn-sellers" onclick="switchTab('sellers')">
      💸 User Sold Accounts & Payouts <span class="badge-count" id="pending-badge" style="display:none;">0</span>
    </button>
    <button class="tab-btn" id="tab-btn-gateway" onclick="switchTab('gateway')">
      🏦 NetBanking Login & Auto Gateway <span class="badge-count" id="gateway-unclaimed-badge" style="background:#10b981;display:none;">0</span>
    </button>
    <button class="tab-btn" id="tab-btn-users" onclick="switchTab('users')">
      👥 User Balance Manager
    </button>
    <button class="tab-btn" id="tab-btn-broadcast" onclick="switchTab('broadcast')">
      📢 Power Broadcaster
    </button>
  </div>

  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <!-- TAB 1: STORE STOCK CATALOG -->
  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <div class="tab-content active" id="tab-stock">
    <div class="stats-grid">
      <div class="stat-card total">
        <div class="stat-icon">📱</div>
        <div class="stat-label">Total Accounts</div>
        <div class="stat-num" id="stat-total">–</div>
      </div>
      <div class="stat-card available">
        <div class="stat-icon">🟢</div>
        <div class="stat-label">Available In Stock</div>
        <div class="stat-num" id="stat-available">–</div>
      </div>
      <div class="stat-card sold">
        <div class="stat-icon">💰</div>
        <div class="stat-label">Sold Orders</div>
        <div class="stat-num" id="stat-sold">–</div>
      </div>
      <div class="stat-card monitoring">
        <div class="stat-icon">🔔</div>
        <div class="stat-label">OTP Monitoring</div>
        <div class="stat-num" id="stat-monitoring">–</div>
      </div>
    </div>

    <!-- COUNTRY STOCK RANKING -->
    <div class="country-ranking-card">
      <div class="country-ranking-header">
        <div class="section-title">🌍 Country Stock Ranking (196+ Countries)</div>
        <span style="font-size:12px;color:var(--muted)">Sorted by highest stock • Click a country to filter</span>
      </div>
      <div class="country-chips-wrap" id="country-ranking-chips">
        <span style="color:var(--muted);font-size:13px;">Loading countries ranking...</span>
      </div>
    </div>

    <!-- ACCOUNTS TABLE -->
    <div class="section-header">
      <div class="section-title">📋 Stored Accounts Catalog</div>
      <div class="controls-row">
        <input type="text" id="acc-search" class="search-box" placeholder="🔍 Filter phone or country..." oninput="filterAccounts()"/>
        <select id="acc-status-filter" style="width:140px;padding:8px 12px;font-size:13px;" onchange="filterAccounts()">
          <option value="all">All Statuses</option>
          <option value="available">Available</option>
          <option value="sold">Sold</option>
          <option value="monitoring">Monitoring</option>
        </select>
        <button class="refresh-btn" onclick="loadAccounts()">🔄 Refresh</button>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Phone Number</th>
            <th>Country</th>
            <th>Account Age / Quality</th>
            <th>Sale Price (₹)</th>
            <th>Status</th>
            <th>Sold To</th>
            <th>Added At</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="accounts-tbody">
          <tr><td colspan="9" style="text-align:center;padding:40px;color:#64748b">Loading accounts...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <!-- TAB 2: USER SOLD ACCOUNTS & PAYOUTS DASHBOARD -->
  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <div class="tab-content" id="tab-sellers">
    <div class="stats-grid">
      <div class="stat-card total">
        <div class="stat-icon">📥</div>
        <div class="stat-label">Total Sold by Users</div>
        <div class="stat-num" id="stat-seller-total">0</div>
      </div>
      <div class="stat-card sold">
        <div class="stat-icon">⏳</div>
        <div class="stat-label">Pending Payouts</div>
        <div class="stat-num" id="stat-seller-pending" style="color:#f59e0b;">0</div>
      </div>
      <div class="stat-card monitoring">
        <div class="stat-icon">💸</div>
        <div class="stat-label">Pending Amount Due</div>
        <div class="stat-num" id="stat-seller-pending-amt" style="color:#ef4444;">₹0</div>
      </div>
      <div class="stat-card available">
        <div class="stat-icon">💵</div>
        <div class="stat-label">Total Paid Out</div>
        <div class="stat-num" id="stat-seller-paid-amt" style="color:#10b981;">₹0</div>
      </div>
    </div>

    <div class="section-header">
      <div class="section-title">💸 User Sold Accounts & Device Login Manager</div>
      <div class="controls-row">
        <input type="text" id="seller-search" class="search-box" placeholder="🔍 Search seller, phone, or UPI..." oninput="filterSellerPayouts()"/>
        <select id="seller-status-filter" style="width:150px;padding:8px 12px;font-size:13px;" onchange="filterSellerPayouts()">
          <option value="all">All Payouts</option>
          <option value="pending">⏳ Pending</option>
          <option value="paid">✅ Paid</option>
          <option value="rejected">❌ Rejected</option>
        </select>
        <button class="refresh-btn" onclick="loadSellerPayouts()">🔄 Refresh</button>
      </div>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#ID</th>
            <th>Seller User</th>
            <th>Phone Number</th>
            <th>Country & Age</th>
            <th>Payout (₹)</th>
            <th>Seller UPI ID</th>
            <th>Status</th>
            <th>Date</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="sellers-tbody">
          <tr><td colspan="9" style="text-align:center;padding:40px;color:#64748b">Loading user sold accounts...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <!-- TAB 3: NETBANKING LOGIN & AUTO UPI GATEWAY -->
  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <div class="tab-content" id="tab-gateway">
    <div class="stats-grid">
      <div class="stat-card available">
        <div class="stat-icon">🏦</div>
        <div class="stat-label">Total Bank Received</div>
        <div class="stat-num" id="stat-gw-total-amt" style="color:#10b981;">₹0</div>
      </div>
      <div class="stat-card total">
        <div class="stat-icon">⚡</div>
        <div class="stat-label">Auto-Redeemed / Credited</div>
        <div class="stat-num" id="stat-gw-redeemed-amt" style="color:#60a5fa;">₹0</div>
      </div>
      <div class="stat-card monitoring">
        <div class="stat-icon">📦</div>
        <div class="stat-label">Unclaimed Pool Balance</div>
        <div class="stat-num" id="stat-gw-unclaimed-amt" style="color:#f59e0b;">₹0</div>
      </div>
      <div class="stat-card sold">
        <div class="stat-icon">⏳</div>
        <div class="stat-label">Pending User UTRs</div>
        <div class="stat-num" id="stat-gw-pending-cnt" style="color:#ef4444;">0</div>
      </div>
    </div>

    <!-- GATEWAY CARDS GRID -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:20px;margin-bottom:28px;">
      
      <!-- GMAIL IMAP FAMPAY AUTO-SCANNER CARD -->
      <div class="merchant-card" style="border:1px solid rgba(239,68,68,0.35);background:linear-gradient(180deg,rgba(239,68,68,0.05),var(--surface));">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
          <h3 style="font-size:16px;font-weight:700;display:flex;align-items:center;gap:8px;color:#f87171;">
            📧 Gmail IMAP FamPay Auto-Scanner
          </h3>
          <span style="font-size:11px;background:#7f1d1d;padding:3px 8px;border-radius:6px;color:#fca5a5;font-weight:700;">24/7 Cloud Polling</span>
        </div>
        <p style="font-size:13px;color:var(--muted);margin-bottom:14px;">
          Connected to <b>chinupatraq@gmail.com</b>. Scans incoming <b>FamApp / FamPay payment alerts</b> via IMAP SSL and auto-credits wallets in 1 second!
        </p>

        <!-- Gmail Live Status Badge -->
        <div style="background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.3);border-radius:10px;padding:12px;margin-bottom:14px;">
          <div style="display:flex;align-items:center;justify-content:space-between;">
            <div>
              <div style="display:flex;align-items:center;gap:6px;">
                <span class="dot" style="background:#10b981;box-shadow:0 0 8px #10b981;"></span>
                <strong style="color:#34d399;font-size:14px;">Gmail IMAP Active</strong>
              </div>
              <div style="font-size:13px;margin-top:4px;">
                Email: <code style="color:#60a5fa;">chinupatraq@gmail.com</code> • Target UPI: <strong style="color:#fbbf24;">pradip960@fam</strong>
              </div>
              <div style="font-size:12px;color:var(--muted);margin-top:2px;">
                Status: <span id="gmail-status-text" style="color:#34d399;">Connected (scanning every 6s)</span> • Last Scan: <span id="gmail-last-sync" style="color:#cbd5e1;">Just now</span>
              </div>
            </div>
            <button class="btn-bal add" style="font-size:12px;" onclick="syncGmailNow()">🔄 Scan Inbox</button>
          </div>
        </div>
      </div>

      <!-- FORCE CHANNEL & JOIN REQUEST SETTINGS -->
      <div class="merchant-card" style="border-color: rgba(99,102,241,0.35);background:linear-gradient(180deg,rgba(30,27,75,0.4),rgba(15,23,42,0.6));">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
          <h3 style="font-size:16px;font-weight:700;color:#c7d2fe;">📢 Force Channel & Join Request</h3>
          <span id="force-sub-badge" class="badge" style="background:#10b981;color:#fff;">ACTIVE</span>
        </div>
        <p style="font-size:13px;color:var(--muted);margin-bottom:14px;">
          Set your private/public channel & custom <b>Request to Join Link</b>. When users click and submit a join request, the bot detects it and unlocks access automatically!
        </p>

        <div class="form-group">
          <label style="font-size:12px;font-weight:600;color:#e2e8f0;">Channel Username or Chat ID</label>
          <input type="text" id="force-channel-id" placeholder="@sii_3s or -100..." value="@sii_3s" />
        </div>

        <div class="form-group">
          <label style="font-size:12px;font-weight:600;color:#e2e8f0;">Custom Join / Request to Join Link</label>
          <input type="text" id="force-channel-link" placeholder="https://t.me/+AbCdEfGhIjKlMnOp or https://t.me/sii_3s" />
        </div>

        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;padding:10px 12px;background:rgba(15,23,42,0.5);border:1px solid rgba(255,255,255,0.06);border-radius:8px;">
          <div>
            <div style="font-size:13px;font-weight:600;color:#f8fafc;">Force Join Lock</div>
            <div style="font-size:11.5px;color:var(--muted);">Require users to send join request before using bot</div>
          </div>
          <input type="checkbox" id="force-sub-toggle" checked style="width:20px;height:20px;accent-color:#6366f1;cursor:pointer;">
        </div>

        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;padding:8px 12px;background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.2);border-radius:8px;">
          <span style="font-size:12px;color:#c7d2fe;">📩 Total Join Requests Detected:</span>
          <strong id="force-join-count" style="font-size:14px;color:#38bdf8;">0</strong>
        </div>

        <button class="form-btn" style="background:linear-gradient(135deg,#6366f1,#4f46e5);" onclick="saveChannelSettings()">💾 Save Channel Settings</button>
      </div>
      
      <!-- SIMULATOR & WEBHOOK BOX -->
      <div class="merchant-card">
        <h3 style="font-size:16px;font-weight:700;margin-bottom:6px;">🧪 Bank Payment Simulator</h3>
        <p style="font-size:13px;color:var(--muted);margin-bottom:14px;">Inject a test bank payment into the cloud engine to test instant Telegram wallet reconciliation:</p>
        
        <div class="form-group">
          <label>12-Digit UTR Number</label>
          <input type="text" id="sim-utr" placeholder="423891048291" />
        </div>
        <div class="form-group">
          <label>Amount (₹ INR)</label>
          <input type="number" id="sim-amt" value="100" />
        </div>
        <button class="form-btn" style="background:linear-gradient(135deg,#10b981,#059669);" onclick="simulateBankTx()">⚡ Inject Real-Time Bank Payment</button>

        <div style="margin-top:16px;padding:12px;background:rgba(59,130,246,0.06);border:1px solid rgba(59,130,246,0.2);border-radius:10px;">
          <div style="font-size:12px;font-weight:700;color:#93c5fd;margin-bottom:4px;">🔗 Bank Webhook Endpoint</div>
          <code style="font-size:11.5px;color:#cbd5e1;word-break:break-all;">http://localhost:8001/api/upi/notify</code>
        </div>
      </div>
    </div>

    <!-- LIVE TRANSACTIONS FEED -->
    <div class="section-header">
      <div class="section-title">⚡ Real-Time Bank Transactions Feed</div>
      <div class="controls-row">
        <input type="text" id="gw-search" class="search-box" placeholder="🔍 Search UTR, sender, or user ID..." oninput="filterGatewayFeed()"/>
        <select id="gw-status-filter" style="width:150px;padding:8px 12px;font-size:13px;" onchange="filterGatewayFeed()">
          <option value="all">All Transactions</option>
          <option value="redeemed">✅ Auto-Redeemed</option>
          <option value="unclaimed">📦 Unclaimed Pool</option>
        </select>
        <button class="refresh-btn" onclick="loadGatewayFeed()">🔄 Refresh Feed</button>
      </div>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#ID</th>
            <th>12-Digit UTR / Ref No</th>
            <th>Amount (₹)</th>
            <th>Sender Name</th>
            <th>Source Bank / Gateway</th>
            <th>Status</th>
            <th>Credited To User</th>
            <th>Received Time</th>
            <th>Manual Action</th>
          </tr>
        </thead>
        <tbody id="gateway-tbody">
          <tr><td colspan="9" style="text-align:center;padding:40px;color:#64748b">Loading live bank transactions feed...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <!-- TAB 4: USER BALANCE MANAGER -->
  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <div class="tab-content" id="tab-users">
    <div class="section-header">
      <div class="section-title">👥 Users & Balance Management (INR ₹)</div>
      <div class="controls-row">
        <input type="text" id="user-search" class="search-box" placeholder="🔍 Search user ID or name..." oninput="filterUsers()"/>
        <button class="refresh-btn" onclick="loadUsers()">🔄 Refresh Users</button>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>User ID</th>
            <th>Name</th>
            <th>Username</th>
            <th>Current Balance (₹)</th>
            <th>Orders</th>
            <th>Total Spent (₹)</th>
            <th>Joined At</th>
            <th>Balance Actions</th>
          </tr>
        </thead>
        <tbody id="users-tbody">
          <tr><td colspan="8" style="text-align:center;padding:40px;color:#64748b">Loading users...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <!-- TAB 5: POWER BROADCASTER ENGINE -->
  <!-- ═══════════════════════════════════════════════════════════════════════ -->
  <div class="tab-content" id="tab-broadcast">
    <!-- STATS GRID -->
    <div class="stats-grid">
      <div class="stat-card total">
        <div class="stat-icon">👥</div>
        <div class="stat-label">Total Target Members</div>
        <div class="stat-num" id="stat-bc-total">0</div>
      </div>
      <div class="stat-card available">
        <div class="stat-icon">⚡</div>
        <div class="stat-label">Broadcaster Status</div>
        <div class="stat-num" id="stat-bc-status" style="font-size:18px;color:#10b981;">READY</div>
      </div>
      <div class="stat-card sold">
        <div class="stat-icon">🚀</div>
        <div class="stat-label">Peak Broadcast Speed</div>
        <div class="stat-num" id="stat-bc-speed" style="color:#60a5fa;">0 msg/s</div>
      </div>
      <div class="stat-card monitoring">
        <div class="stat-icon">⏱️</div>
        <div class="stat-label">Last Elapsed Time</div>
        <div class="stat-num" id="stat-bc-time" style="color:#f59e0b;">0s</div>
      </div>
    </div>

    <!-- LIVE BROADCAST RUNNING CARD (Shown while running or completed) -->
    <div id="bc-live-card" style="display:none;background:linear-gradient(135deg,#1e1b4b,#0f172a);border:1px solid rgba(99,102,241,0.4);border-radius:16px;padding:24px;margin-bottom:24px;box-shadow:0 8px 32px rgba(99,102,241,0.2);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:10px;">
        <div style="display:flex;align-items:center;gap:10px;">
          <span style="font-size:24px;" id="bc-spinner-icon">⚡</span>
          <div>
            <h3 style="font-size:17px;font-weight:700;color:#fff;" id="bc-live-title">Turbo Broadcast in Progress...</h3>
            <p style="font-size:12px;color:#94a3b8;" id="bc-live-sub">High-throughput async parallel worker pool active</p>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
          <span class="badge available" id="bc-live-badge" style="font-size:13px;padding:6px 14px;">RUNNING ⚡</span>
          <button class="btn-bal cut" id="bc-cancel-btn" style="padding:6px 14px;font-size:12px;font-weight:700;cursor:pointer;" onclick="cancelBroadcast()">🛑 Cancel Broadcast</button>
        </div>
      </div>

      <!-- PROGRESS BAR -->
      <div style="background:rgba(255,255,255,0.08);border-radius:10px;height:18px;overflow:hidden;margin-bottom:14px;position:relative;">
        <div id="bc-progress-bar" style="background:linear-gradient(90deg,#3b82f6,#6366f1,#10b981);height:100%;width:0%;transition:width 0.3s ease;border-radius:10px;"></div>
      </div>

      <!-- LIVE METRICS -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(130px, 1fr));gap:12px;text-align:center;">
        <div style="background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);padding:10px;border-radius:8px;">
          <div style="font-size:11px;color:#6ee7b7;text-transform:uppercase;font-weight:600;">Sent</div>
          <div style="font-size:20px;font-weight:800;color:#10b981;" id="bc-metric-sent">0</div>
        </div>
        <div style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.25);padding:10px;border-radius:8px;">
          <div style="font-size:11px;color:#fca5a5;text-transform:uppercase;font-weight:600;">Blocked / Dead</div>
          <div style="font-size:20px;font-weight:800;color:#ef4444;" id="bc-metric-blocked">0</div>
        </div>
        <div style="background:rgba(245,158,11,0.1);border:1px solid rgba(245,158,11,0.25);padding:10px;border-radius:8px;">
          <div style="font-size:11px;color:#fde68a;text-transform:uppercase;font-weight:600;">Failed</div>
          <div style="font-size:20px;font-weight:800;color:#f59e0b;" id="bc-metric-failed">0</div>
        </div>
        <div style="background:rgba(96,165,250,0.1);border:1px solid rgba(96,165,250,0.25);padding:10px;border-radius:8px;">
          <div style="font-size:11px;color:#93c5fd;text-transform:uppercase;font-weight:600;">Speed</div>
          <div style="font-size:20px;font-weight:800;color:#60a5fa;" id="bc-metric-speed">0 msg/s</div>
        </div>
        <div style="background:rgba(168,85,247,0.1);border:1px solid rgba(168,85,247,0.25);padding:10px;border-radius:8px;">
          <div style="font-size:11px;color:#d8b4fe;text-transform:uppercase;font-weight:600;">ETA Remaining</div>
          <div style="font-size:20px;font-weight:800;color:#c084fc;" id="bc-metric-eta">0s</div>
        </div>
      </div>
    </div>

    <!-- BROADCAST COMPOSER & PREVIEW GRID -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(320px, 1fr));gap:20px;margin-bottom:24px;" id="bc-grid-wrap">
      <!-- COMPOSER -->
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px;">
        <div class="section-title" style="margin-bottom:16px;">✍️ Compose Power Broadcast</div>
        
        <div class="form-group">
          <label style="display:flex;justify-content:space-between;">
            <span>Message Content (HTML tags supported)</span>
            <span style="font-size:11px;color:var(--muted);">&lt;b&gt;, &lt;i&gt;, &lt;code&gt;, &lt;a href="..."&gt;</span>
          </label>
          <textarea id="bc-text-input" rows="7" placeholder="🔥 Big Restock Alert!&#10;&#10;Over 5,000+ Fresh Indian (+91) and Aged USA accounts added to store!&#10;&#10;⚡ Instant OTP delivery & zero wait time." oninput="updateBroadcastPreview()" style="width:100%;padding:12px;background:#0f172a;border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#fff;font-family:inherit;font-size:13px;line-height:1.5;resize:vertical;"></textarea>
        </div>

        <div class="input-row">
          <div class="form-group">
            <label>🖼️ Photo / Banner URL (Optional)</label>
            <input type="text" id="bc-photo-input" placeholder="https://example.com/banner.jpg" oninput="updateBroadcastPreview()"/>
          </div>
          <div class="form-group">
            <label>⚡ Worker Concurrency</label>
            <select id="bc-concurrency-select">
              <option value="30">🚀 30 Workers (Safe Standard)</option>
              <option value="50" selected>⚡ 50 Workers (Recommended • High Speed)</option>
              <option value="80">🔥 80 Workers (Ultra Turbo)</option>
              <option value="100">👑 100 Workers (Maximum Speed)</option>
            </select>
          </div>
        </div>

        <div class="input-row">
          <div class="form-group">
            <label>🔘 Button Text (Optional)</label>
            <input type="text" id="bc-btn-text-input" placeholder="🛍️ Open Store Now" oninput="updateBroadcastPreview()"/>
          </div>
          <div class="form-group">
            <label>🔗 Button URL (Optional)</label>
            <input type="text" id="bc-btn-url-input" placeholder="https://t.me/your_bot" oninput="updateBroadcastPreview()"/>
          </div>
        </div>

        <div style="display:flex;align-items:center;justify-content:space-between;margin:12px 0 18px 0;background:var(--surface2);padding:10px 14px;border-radius:8px;border:1px solid var(--border);">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;font-weight:600;margin-bottom:0;">
            <input type="checkbox" id="bc-pin-checkbox" style="width:16px;height:16px;accent-color:#6366f1;cursor:pointer;"/>
            📌 Pin Message in User's Chat after sending
          </label>
          <span style="font-size:11px;color:var(--muted);">Silent pin (no notification sound)</span>
        </div>

        <button class="form-btn" id="bc-launch-btn" onclick="startBroadcast()" style="background:linear-gradient(135deg,#3b82f6,#6366f1);font-size:15px;font-weight:700;padding:14px;margin:0;cursor:pointer;">
          🚀 Launch Turbo Broadcast Now
        </button>
      </div>

      <!-- LIVE WYSIWYG TELEGRAM PREVIEW -->
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px;display:flex;flex-direction:column;">
        <div class="section-title" style="margin-bottom:16px;">📱 Live Telegram Message Preview</div>
        <div style="flex:1;background:#0e1621;border-radius:14px;padding:18px;border:1px solid rgba(255,255,255,0.05);display:flex;flex-direction:column;justify-content:center;align-items:center;">
          <!-- TELEGRAM CHAT BUBBLE -->
          <div style="max-width:360px;width:100%;background:#182533;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.06);">
            <!-- OPTIONAL PHOTO BANNER -->
            <div id="bc-preview-photo-wrap" style="display:none;width:100%;max-height:180px;overflow:hidden;background:#000;">
              <img id="bc-preview-img" src="" alt="Banner Preview" style="width:100%;height:100%;object-fit:cover;display:block;"/>
            </div>
            <!-- TEXT CONTENT -->
            <div style="padding:14px 16px;font-size:13px;color:#e6edf3;line-height:1.5;white-space:pre-wrap;word-break:break-word;" id="bc-preview-text">
              Preview will appear here in real-time as you type...
            </div>
            <div style="text-align:right;padding:0 12px 8px 0;font-size:10px;color:#6b7280;" id="bc-preview-time">12:00 PM</div>
            <!-- OPTIONAL INLINE BUTTON -->
            <div id="bc-preview-btn-wrap" style="display:none;padding:8px 12px 12px 12px;background:#182533;border-top:1px solid rgba(255,255,255,0.05);">
              <a id="bc-preview-btn-link" href="#" target="_blank" style="display:block;text-align:center;background:linear-gradient(135deg,#2b5278,#33689a);color:#fff;padding:10px;border-radius:8px;text-decoration:none;font-weight:700;font-size:13px;">
                🛍️ Open Store Now
              </a>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- BROADCAST HISTORY TABLE -->
    <div class="section-header">
      <div class="section-title">📜 Broadcast History & Performance Logs</div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Target Members</th>
            <th>Successfully Sent</th>
            <th>Blocked / Dead</th>
            <th>Failed</th>
            <th>Avg Speed</th>
            <th>Time Taken</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="bc-history-tbody">
          <tr><td colspan="8" style="text-align:center;padding:30px;color:#64748b">No broadcast history logged yet.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>

<!-- ═══════════════════════════════════════════════════════════════════════ -->
<!-- MODALS -->
<!-- ═══════════════════════════════════════════════════════════════════════ -->

<!-- LIVE OTP & 2FA MODAL (FOR ADMIN DEVICE LOGIN) -->
<div class="overlay" id="otp-modal-overlay" onclick="closeOtpModalOutside(event)">
  <div class="modal" style="max-width: 480px;">
    <button class="modal-close" onclick="closeOtpModal()">✕</button>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
      <span style="font-size:24px;">🔑</span>
      <div>
        <h2 style="font-size:18px;font-weight:700;" id="otp-modal-title">Account Login OTP</h2>
        <p style="font-size:12px;color:var(--muted);" id="otp-modal-sub">Real-time OTP & 2FA details for Admin Device Login</p>
      </div>
    </div>
    
    <div style="display:flex;flex-direction:column;gap:14px;">
      <div style="background:var(--surface2);padding:14px;border-radius:10px;border:1px solid var(--border);">
        <span style="font-size:12px;color:var(--muted);text-transform:uppercase;font-weight:600;">Account Phone Number</span>
        <div style="font-size:16px;font-weight:700;margin-top:4px;color:#60a5fa;display:flex;justify-content:space-between;align-items:center;">
          <span id="otp-phone-display">+91XXXXXXXXXX</span>
          <button class="btn-bal set" style="padding:3px 10px;font-size:11px;" onclick="copyPhone()">📋 Copy Phone</button>
        </div>
      </div>

      <div style="background:rgba(16,185,129,0.1);padding:16px;border-radius:12px;border:1px solid rgba(16,185,129,0.3);text-align:center;">
        <span style="font-size:12px;color:#10b981;text-transform:uppercase;font-weight:700;letter-spacing:0.5px;">Latest Telegram Login OTP</span>
        <div style="font-size:34px;font-weight:800;letter-spacing:4px;margin:8px 0;color:#10b981;" id="otp-code-display">--- ---</div>
        <button class="btn-bal add" style="background:#10b981;margin:0 auto;padding:6px 16px;font-size:13px;" onclick="copyOtpCode()">📋 Copy OTP Code</button>
      </div>

      <div style="background:var(--surface2);padding:14px;border-radius:10px;border:1px solid var(--border);" id="otp-2fa-container">
        <span style="font-size:12px;color:var(--muted);text-transform:uppercase;font-weight:600;">Two-Step Verification (2FA Password)</span>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px;">
          <code style="font-size:15px;color:#f59e0b;font-weight:700;" id="otp-2fa-display">None</code>
          <button class="btn-bal set" style="padding:4px 12px;font-size:12px;" onclick="copy2faPass()">📋 Copy 2FA</button>
        </div>
      </div>

      <div style="background:var(--surface2);padding:12px;border-radius:10px;font-size:12px;color:var(--muted);max-height:100px;overflow-y:auto;" id="otp-full-msg">
        Waiting for OTP fetch...
      </div>

      <div style="display:flex;gap:10px;margin-top:6px;">
        <button class="form-btn" style="flex:1;background:linear-gradient(135deg,#3b82f6,#6366f1);margin:0;" onclick="fetchLiveOtp()" id="otp-refresh-btn">🔄 Refresh / Check New OTP</button>
        <button class="refresh-btn" style="padding:10px 16px;" onclick="closeOtpModal()">Close</button>
      </div>
    </div>
  </div>
</div>

<!-- BALANCE MODAL -->
<div class="overlay" id="bal-modal-overlay" onclick="closeBalModalOutside(event)">
  <div class="modal">
    <button class="modal-close" onclick="closeBalModal()">✕</button>
    <div class="modal-title" id="bal-modal-title">Adjust User Balance</div>
    <div class="modal-sub" id="bal-modal-sub">Modify balance for user</div>

    <input type="hidden" id="bal-user-id" />
    <input type="hidden" id="bal-action" />

    <div class="form-group" style="margin-top:16px;">
      <label id="bal-amount-label">Amount (₹)</label>
      <input type="number" id="bal-amount-input" placeholder="100" min="1" />
      <div class="quick-chips">
        <span class="chip" onclick="setQuickAmount(50)">₹50</span>
        <span class="chip" onclick="setQuickAmount(100)">₹100</span>
        <span class="chip" onclick="setQuickAmount(250)">₹250</span>
        <span class="chip" onclick="setQuickAmount(500)">₹500</span>
        <span class="chip" onclick="setQuickAmount(1000)">₹1,000</span>
      </div>
    </div>

    <button class="form-btn" id="bal-submit-btn" onclick="submitBalanceChange()" style="margin-top:20px;">Confirm & Save</button>
  </div>
</div>

<!-- EDIT ACCOUNT & CATEGORY PRICE / AGE MODAL -->
<div class="overlay" id="price-modal-overlay" onclick="closePriceModalOutside(event)">
  <div class="modal" style="max-width: 520px;">
    <button class="modal-close" onclick="closePriceModal()">✕</button>
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
      <span style="font-size:28px;">⚙️</span>
      <div>
        <h2 style="font-size:18px;font-weight:700;" id="price-modal-title">Edit Account & Category Details</h2>
        <p style="font-size:12px;color:var(--muted);" id="price-modal-subtitle">Update Age Quality Tier, Sale Price, and Category settings</p>
      </div>
    </div>

    <!-- TARGET ACCOUNT BANNER -->
    <div id="edit-acc-banner" style="background:rgba(99,102,241,0.1);padding:10px 14px;border-radius:10px;border:1px solid rgba(99,102,241,0.25);display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
      <div style="display:flex;align-items:center;gap:8px;">
        <span style="font-size:18px;" id="edit-banner-flag">🇮🇳</span>
        <div>
          <div style="font-size:13px;font-weight:700;color:#fff;" id="edit-banner-phone">+918302913257</div>
          <div style="font-size:11px;color:#94a3b8;" id="edit-banner-country">India</div>
        </div>
      </div>
      <span class="badge available" id="edit-banner-badge">Single Account</span>
    </div>

    <input type="hidden" id="edit-price-acc-id" />
    <input type="hidden" id="edit-price-country-raw" />
    <input type="hidden" id="edit-price-age-raw" value="0" />

    <!-- TABS: SINGLE ACCOUNT VS BULK CATEGORY -->
    <div style="display:flex;gap:6px;background:var(--surface2);padding:4px;border-radius:8px;margin-bottom:14px;" id="edit-mode-tab-wrap">
      <button type="button" id="tab-btn-single-acc" onclick="setEditModalMode('single')" style="flex:1;padding:8px;border-radius:6px;border:none;font-size:12px;font-weight:700;cursor:pointer;background:#6366f1;color:#fff;">📱 This Account Only</button>
      <button type="button" id="tab-btn-bulk-cat" onclick="setEditModalMode('bulk')" style="flex:1;padding:8px;border-radius:6px;border:none;font-size:12px;font-weight:700;cursor:pointer;background:transparent;color:#94a3b8;">🌐 Bulk Category / Country</button>
    </div>

    <!-- SINGLE ACCOUNT FORM -->
    <div id="modal-single-acc-wrap" style="display:flex;flex-direction:column;gap:14px;">
      <!-- AGE TIER SELECTION CARDS -->
      <div class="form-group" style="margin-bottom:0;">
        <label style="font-weight:700;color:#94a3b8;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;display:flex;justify-content:space-between;">
          <span>⏳ Select Account Age / Quality</span>
          <span style="color:#10b981;font-weight:700;" id="selected-age-label">🌱 Fresh (0-1 Month)</span>
        </label>
        <div class="age-grid">
          <div class="age-card active" id="age-card-0" onclick="selectAgeTier(0, 'fresh')">
            <div class="age-title"><span>🌱 Fresh</span> <span style="font-size:11px;color:#10b981;">0M</span></div>
            <div class="age-sub">0 - 1 Month Old</div>
          </div>
          <div class="age-card" id="age-card-3" onclick="selectAgeTier(3, '3m')">
            <div class="age-title"><span>⏳ 3 Months</span> <span style="font-size:11px;color:#60a5fa;">3M</span></div>
            <div class="age-sub">Aged 3 Months</div>
          </div>
          <div class="age-card" id="age-card-6" onclick="selectAgeTier(6, '6m')">
            <div class="age-title"><span>🏆 6+ Months</span> <span style="font-size:11px;color:#f59e0b;">6M</span></div>
            <div class="age-sub">Aged 6+ Months</div>
          </div>
          <div class="age-card" id="age-card-12" onclick="selectAgeTier(12, '1y')">
            <div class="age-title"><span>👑 1+ Year</span> <span style="font-size:11px;color:#ec4899;">12M</span></div>
            <div class="age-sub">Aged 1+ Year</div>
          </div>
        </div>

        <!-- Custom Months Toggle -->
        <div style="margin-top:6px;display:flex;align-items:center;gap:10px;">
          <label style="font-size:11px;color:#94a3b8;cursor:pointer;display:flex;align-items:center;gap:4px;">
            <input type="checkbox" id="custom-age-check" onchange="toggleCustomAgeInput(this.checked)" />
            <span>Set Custom Age in Months</span>
          </label>
          <input type="number" id="custom-age-number-input" placeholder="0" min="0" max="120" style="display:none;width:80px;padding:4px 8px;font-size:12px;background:#0f172a;border:1px solid rgba(255,255,255,0.2);border-radius:6px;color:#fff;" oninput="onCustomAgeNumberInput(this.value)" />
        </div>
      </div>

      <!-- PRICE INPUT -->
      <div class="form-group" style="margin-bottom:0;">
        <label style="display:flex;justify-content:space-between;align-items:center;">
          <span style="font-weight:700;">Sale Price (₹ INR)</span>
          <span style="font-size:12px;color:#94a3b8;" id="edit-price-usd-preview">≈ $0.30 USD</span>
        </label>
        <input type="number" id="edit-price-input" placeholder="30" min="1" step="1" oninput="updatePriceUsdPreview(this.value)" style="font-size:22px;font-weight:800;color:#10b981;padding:10px 14px;background:#0f172a;border:1px solid rgba(16,185,129,0.4);border-radius:8px;" />
        <div class="quick-chips" style="margin-top:6px;">
          <span class="chip" onclick="setQuickPrice(15)">₹15</span>
          <span class="chip" onclick="setQuickPrice(20)">₹20</span>
          <span class="chip" onclick="setQuickPrice(22)">₹22</span>
          <span class="chip" onclick="setQuickPrice(25)">₹25</span>
          <span class="chip" onclick="setQuickPrice(30)">₹30</span>
          <span class="chip" onclick="setQuickPrice(40)">₹40</span>
          <span class="chip" onclick="setQuickPrice(50)">₹50</span>
          <span class="chip" onclick="setQuickPrice(75)">₹75</span>
          <span class="chip" onclick="setQuickPrice(100)">₹100</span>
          <span class="chip" onclick="setQuickPrice(150)">₹150</span>
        </div>
      </div>
    </div>

    <!-- BULK CATEGORY FORM (Shown in Bulk mode) -->
    <div id="modal-bulk-cat-wrap" style="display:none;flex-direction:column;gap:14px;">
      <div class="form-group" style="margin-bottom:0;">
        <label style="font-weight:700;color:#94a3b8;font-size:12px;text-transform:uppercase;">🎯 Choose Target Scope</label>
        <select id="edit-price-scope-select" onchange="onPriceScopeSelectChange()" style="width:100%;padding:11px 12px;background:#0f172a;border:1px solid rgba(99,102,241,0.5);border-radius:8px;color:#fff;font-size:13px;font-weight:600;">
        </select>
      </div>

      <div id="price-custom-category-wrap" style="display:none;background:var(--surface2);padding:12px;border-radius:10px;border:1px solid var(--border);">
        <div class="input-row" style="margin-bottom:0;">
          <div class="form-group" style="margin-bottom:0;">
            <label style="font-size:11px;">🌍 Country</label>
            <select id="price-custom-country-select" onchange="recalculatePriceAffectedCount()" style="font-size:13px;">
              <option value="all">🌐 All Countries</option>
            </select>
          </div>
          <div class="form-group" style="margin-bottom:0;">
            <label style="font-size:11px;">⏳ Category / Age Tier</label>
            <select id="price-custom-tier-select" onchange="recalculatePriceAffectedCount()" style="font-size:13px;">
              <option value="all">📦 All Categories / All Ages</option>
              <option value="fresh">🌱 Fresh (0-1 Month)</option>
              <option value="3m">⏳ 3 Months (Aged)</option>
              <option value="6m">🏆 6+ Months (Aged)</option>
              <option value="1y">👑 1+ Year (Aged)</option>
            </select>
          </div>
        </div>
      </div>

      <div style="background:rgba(99,102,241,0.1);padding:10px 14px;border-radius:8px;border:1px solid rgba(99,102,241,0.25);display:flex;align-items:center;justify-content:space-between;">
        <span style="font-size:12px;color:#a5b4fc;font-weight:600;" id="price-target-summary">🎯 Target Scope</span>
        <span class="badge available" style="font-size:11px;" id="price-affected-badge">0 Accounts</span>
      </div>

      <div class="form-group" style="margin-bottom:0;">
        <label style="display:flex;justify-content:space-between;align-items:center;">
          <span style="font-weight:700;">Category Sale Price (₹ INR)</span>
          <span style="font-size:12px;color:#94a3b8;" id="bulk-price-usd-preview">≈ $0.30 USD</span>
        </label>
        <input type="number" id="bulk-price-input" placeholder="30" min="1" step="1" oninput="updateBulkPriceUsdPreview(this.value)" style="font-size:22px;font-weight:800;color:#10b981;padding:10px 14px;background:#0f172a;border:1px solid rgba(16,185,129,0.4);border-radius:8px;" />
      </div>
    </div>

    <!-- ACTIONS -->
    <div style="display:flex;gap:10px;margin-top:16px;">
      <button class="form-btn" style="flex:1;background:linear-gradient(135deg,#10b981,#059669);margin:0;font-size:14px;font-weight:700;" onclick="submitAccountPriceChange()" id="edit-price-submit-btn">💾 Save Changes Now</button>
      <button class="refresh-btn" style="padding:10px 16px;" onclick="closePriceModal()">Cancel</button>
    </div>
  </div>
</div>

<!-- ADD ACCOUNT MODAL (196+ COUNTRIES & AGE TIERS) -->
<div class="overlay" id="modal-overlay" onclick="closeModalOutside(event)">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">✕</button>
    <div class="modal-title">➕ Add Telegram Account</div>
    <div class="modal-sub" id="modal-sub">Select country, age tier and enter phone number</div>

    <div class="step-indicators">
      <div class="step-dot active" id="step1-dot"></div>
      <div class="step-dot" id="step2-dot"></div>
    </div>

    <!-- STEP 1: Details & Phone Input -->
    <div id="step1">
      <div class="form-group">
        <label>🌍 Country (196+ Countries with Live Stock)</label>
        <select id="country-select" onchange="onCountrySelected()">
          <option value="India" data-flag="🇮🇳" data-price="22">🇮🇳 India (Default)</option>
        </select>
      </div>

      <div class="input-row">
        <div class="form-group">
          <label>Country Name</label>
          <input type="text" id="country-input" placeholder="India" value="India" readonly style="background:#141b2d;cursor:not-allowed;"/>
        </div>
        <div class="form-group">
          <label>Flag Emoji</label>
          <input type="text" id="flag-input" placeholder="🇮🇳" value="🇮🇳" readonly style="background:#141b2d;text-align:center;cursor:not-allowed;"/>
        </div>
      </div>

      <div class="input-row">
        <div class="form-group">
          <label>⏳ Account Age / Quality Tier</label>
          <select id="age-tier-select" onchange="onAgeTierSelected()">
            <option value="fresh_0" data-mult="1.0">🌱 Fresh (0-1 Month)</option>
            <option value="aged_3" data-mult="1.4">⏳ 3 Months (Aged)</option>
            <option value="aged_6" data-mult="1.8">🏆 6+ Months (Aged)</option>
            <option value="aged_12" data-mult="2.5">👑 1+ Year (Aged)</option>
            <option value="custom" data-mult="1.0">⚙️ Custom Age</option>
          </select>
        </div>
        <div class="form-group" id="custom-age-wrap" style="display:none;">
          <label>Custom Age (Months)</label>
          <input type="number" id="age-input" placeholder="0" value="0" min="0" oninput="calculatePrice()"/>
        </div>
        <div class="form-group">
          <label>Sale Price (₹ INR)</label>
          <input type="number" id="price-input" placeholder="22" value="22" min="1" oninput="onCustomPriceInput()"/>
        </div>
      </div>

      <div class="form-group">
        <label>📱 Phone Number (with Country Code)</label>
        <input type="text" id="phone-input" placeholder="+919876543210" />
      </div>

      <button class="form-btn" id="send-otp-btn" onclick="sendOTP()">📤 Send OTP Code</button>
    </div>

    <!-- STEP 2: OTP Verification -->
    <div id="step2" style="display:none">
      <div class="info-box">
        📩 OTP sent! Open Telegram on <strong id="target-phone-display"></strong> and enter the login code below.
      </div>
      <div class="form-group">
        <label>Enter OTP / Login Code</label>
        <input type="text" id="otp-input" class="otp-input" placeholder="12345" maxlength="6"/>
      </div>
      <div class="form-group" id="twofa-group" style="display:none">
        <label>🔐 2FA Password (if enabled)</label>
        <input type="password" id="twofa-input" placeholder="Enter 2FA password"/>
      </div>
      <button class="form-btn" id="verify-btn" onclick="verifyOTP()">🔐 Verify & Save Account</button>
      <button onclick="goBackStep1()" style="width:100%;background:none;border:none;color:var(--muted);margin-top:12px;cursor:pointer;font-size:13px;text-decoration:underline;">← Back to details</button>
    </div>
  </div>
</div>

<!-- TOAST CONTAINER -->
<div class="toast-wrap" id="toast-wrap"></div>

<script>
// ── Global Auth & Login Handlers (Loaded First) ───────────────────────────
async function checkAuth() {
  const urlParams = new URLSearchParams(window.location.search);
  const urlToken = urlParams.get('token');
  if (urlToken) {
    localStorage.setItem('admin_auth_token', urlToken);
    window.history.replaceState({}, document.title, window.location.pathname);
  }
  const token = localStorage.getItem('admin_auth_token');
  if (token) {
    try {
      const res = await fetch('/api/auth/verify?token=' + encodeURIComponent(token));
      const data = await res.json();
      if (data.ok) {
        document.getElementById('login-screen').style.display = 'none';
        document.getElementById('app-content').style.display = 'block';
        try { initDashboard(); } catch (ie) { console.error("Init Error:", ie); }
        return;
      }
    } catch(e) {
      console.warn("Auth verify network check failed:", e);
    }
  }
  document.getElementById('login-screen').style.display = 'flex';
  document.getElementById('app-content').style.display = 'none';
}

async function doLogin() {
  const pwdInput = document.getElementById('login-pwd');
  const pwd = pwdInput ? pwdInput.value.trim() : '';
  const err = document.getElementById('login-err');
  const btn = document.querySelector('#login-screen button.form-btn');

  if (err) { err.style.display = 'none'; err.innerText = ''; }
  if (!pwd) {
    if (err) { err.innerText = '⚠️ Please enter the admin password!'; err.style.display = 'block'; }
    if (pwdInput) pwdInput.focus();
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Verifying Password...';
  }

  try {
    const fd = new FormData();
    fd.append('password', pwd);
    const res = await fetch('/api/auth/login', { method: 'POST', body: fd });
    const data = await res.json();

    if (data.ok && data.token) {
      localStorage.setItem('admin_auth_token', data.token);
      document.getElementById('login-screen').style.display = 'none';
      document.getElementById('app-content').style.display = 'block';
      toast('Login successful! Welcome back.', 'success');
      try { initDashboard(); } catch (ie) { console.error("Init Error:", ie); }
    } else {
      if (err) {
        err.innerText = '❌ ' + (data.message || 'Incorrect admin password! Please try again.');
        err.style.display = 'block';
      }
      if (pwdInput) { pwdInput.select(); pwdInput.focus(); }
    }
  } catch(e) {
    if (err) {
      err.innerText = '❌ Network error during verification: ' + e.message;
      err.style.display = 'block';
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '🚀 Unlock Dashboard';
    }
  }
}

function doLogout() {
  localStorage.removeItem('admin_auth_token');
  location.reload();
}

let currentPhone = '';
let allAccountsData = [];
let allCountriesData = [];
let allSellerPayoutsData = [];
let allGatewayFeedData = [];
let selectedCountryFilter = 'all';
let currentBasePrice = 22;
let isUserCustomPrice = false;

// Saved data between Step 1 (Phone & Price) and Step 2 (OTP)
let savedLoginCountry = 'India';
let savedLoginFlag = '🇮🇳';
let savedLoginAgeTier = 'fresh_0';
let savedLoginAge = 0;
let savedLoginPrice = 22.0;

// ── Tab Switcher ────────────────────────────────────────────────────────────
function switchTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

  document.getElementById(`tab-btn-${tabId}`).classList.add('active');
  document.getElementById(`tab-${tabId}`).classList.add('active');

  if (tabId === 'sellers') loadSellerPayouts();
  else if (tabId === 'gateway') { loadGatewayFeed(); loadBankSessions(); }
  else if (tabId === 'users') loadUsers();
  else if (tabId === 'broadcast') loadBroadcastDashboard();
  else loadAccounts();
}

// ── Toast Helper ────────────────────────────────────────────────────────────
function toast(msg, type = 'success') {
  const wrap = document.getElementById('toast-wrap');
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `${type === 'success' ? '✅' : '❌'} <span>${msg}</span>`;
  wrap.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 3500);
}

// ── Load Countries & Populate Dropdowns / Ranking ───────────────────────────
async function loadCountries() {
  try {
    const r = await fetch('/api/countries');
    allCountriesData = await r.json();

    renderRankingChips();

    const isModalOpen = document.getElementById('modal-overlay').classList.contains('active');
    const selectEl = document.getElementById('country-select');

    if (!isModalOpen || selectEl.options.length <= 1) {
      const prevVal = selectEl.value || 'India';
      selectEl.innerHTML = allCountriesData.map(c => 
        `<option value="${c.country}" data-flag="${c.flag}" data-price="${c.base_price}" ${c.country === prevVal ? 'selected' : ''}>
          ${c.flag} ${c.country} (${c.stock} in stock • Base ₹${c.base_price})
        </option>`
      ).join('');
      onCountrySelected(false);
    }
  } catch (err) {
    console.error("Error loading countries:", err);
  }
}

function renderRankingChips() {
  const rankingWrap = document.getElementById('country-ranking-chips');
  if (!rankingWrap) return;

  let chipsHtml = `<span class="country-chip ${selectedCountryFilter==='all'?'active':''}" onclick="setCountryFilter('all')">🌐 All Countries (${allAccountsData.length})</span>`;
  allCountriesData.forEach(c => {
    chipsHtml += `<span class="country-chip ${selectedCountryFilter===c.country?'active':''}" onclick="setCountryFilter('${c.country}')">
      ${c.flag} ${c.country} <span class="stock-badge ${c.stock>0?'':'zero'}">${c.stock}</span>
    </span>`;
  });
  rankingWrap.innerHTML = chipsHtml;
}

function setCountryFilter(country) {
  selectedCountryFilter = country;
  renderRankingChips();
  filterAccounts();
}

function onCountrySelected(autoRecalc = true) {
  const sel = document.getElementById('country-select');
  const opt = sel.options[sel.selectedIndex];
  if (!opt) return;

  const country = opt.value;
  const flag = opt.dataset.flag || '🌍';
  currentBasePrice = parseFloat(opt.dataset.price || '22');

  document.getElementById('country-input').value = country;
  document.getElementById('flag-input').value = flag;

  if (autoRecalc) {
    isUserCustomPrice = false;
    calculatePrice();
  }
}

function onAgeTierSelected() {
  const sel = document.getElementById('age-tier-select');
  const val = sel.value;
  const customWrap = document.getElementById('custom-age-wrap');

  if (val === 'custom') {
    customWrap.style.display = 'block';
  } else {
    customWrap.style.display = 'none';
    const parts = val.split('_');
    document.getElementById('age-input').value = parts[1];
  }
  isUserCustomPrice = false;
  calculatePrice();
}

function onCustomPriceInput() {
  isUserCustomPrice = true;
}

function calculatePrice() {
  if (isUserCustomPrice) return;

  const sel = document.getElementById('age-tier-select');
  const val = sel.value;
  let mult = 1.0;

  if (val === 'custom') {
    const age = parseInt(document.getElementById('age-input').value) || 0;
    if (age >= 12) mult = 2.5;
    else if (age >= 6) mult = 1.8;
    else if (age >= 1) mult = 1.4;
  } else {
    const opt = sel.options[sel.selectedIndex];
    mult = parseFloat(opt.dataset.mult || '1.0');
  }

  const finalInr = Math.round(currentBasePrice * mult);
  document.getElementById('price-input').value = finalInr;
}

// ── Load Stats ──────────────────────────────────────────────────────────────
async function loadStats() {
  const r = await fetch('/api/stats');
  const d = await r.json();
  document.getElementById('stat-total').textContent = d.total;
  document.getElementById('stat-available').textContent = d.available;
  document.getElementById('stat-sold').textContent = d.sold;
  document.getElementById('stat-monitoring').textContent = d.monitoring;
}

// ── Load Accounts ───────────────────────────────────────────────────────────
async function loadAccounts() {
  const r = await fetch('/api/accounts');
  allAccountsData = await r.json();
  filterAccounts();
  await loadStats();
  await loadCountries();
  await loadSellerStatsOnly();
  await loadGatewayStatsOnly();
  await loadChannelSettings();
}

function filterAccounts() {
  const search = document.getElementById('acc-search').value.toLowerCase().trim();
  const statusFilter = document.getElementById('acc-status-filter').value;

  const filtered = allAccountsData.filter(a => {
    const matchCountry = selectedCountryFilter === 'all' || a.country.toLowerCase() === selectedCountryFilter.toLowerCase();
    const matchStatus = statusFilter === 'all' || a.status === statusFilter;
    const matchSearch = !search || a.phone.toLowerCase().includes(search) || a.country.toLowerCase().includes(search);
    return matchCountry && matchStatus && matchSearch;
  });

  renderAccounts(filtered);
}

function renderAccounts(accounts) {
  const tbody = document.getElementById('accounts-tbody');

  if (!accounts.length) {
    tbody.innerHTML = `<tr><td colspan="9"><div style="text-align:center;padding:40px;color:var(--muted)"><span style="font-size:36px;display:block;margin-bottom:8px;">📭</span>No matching accounts found.<br>Click "Add Telegram Account" to login and stock up.</div></td></tr>`;
    return;
  }

  tbody.innerHTML = accounts.map((a, i) => {
    const statusBadge = {
      available: `<span class="badge available"><span class="dot"></span>Available</span>`,
      sold: `<span class="badge sold"><span class="dot"></span>Sold</span>`,
      monitoring: `<span class="badge monitoring"><span class="dot"></span>Monitoring OTP</span>`,
    }[a.status] || a.status;

    let ageLabel = '🌱 Fresh';
    if (a.age > 0) {
      if (a.age >= 12) ageLabel = `👑 1+ Year (${a.age}M)`;
      else if (a.age >= 6) ageLabel = `🏆 6+ Months (${a.age}M)`;
      else ageLabel = `⏳ ${a.age} Months (Aged)`;
    }

    const date = a.created_at ? a.created_at.split('.')[0].replace('T',' ') : '–';
    const soldTo = a.sold_to ? `<code style="font-size:12px;color:#60a5fa">TG: ${a.sold_to}</code>` : '–';

    return `<tr>
      <td style="color:var(--muted);font-size:12px">#${i+1}</td>
      <td class="phone-cell">${a.flag} ${a.phone}</td>
      <td><strong>${a.country}</strong></td>
      <td><span style="font-size:13px;color:#cbd5e1">${ageLabel}</span></td>
      <td class="price-cell">
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-weight:700;color:#10b981;font-size:14px;">₹${Math.round(a.price)}</span>
          <button class="btn-bal set" style="padding:3px 7px;font-size:11px;border-radius:5px;cursor:pointer;" onclick="openEditPriceModal(${a.id}, '${a.phone}', '${a.country}', ${a.price}, ${a.age})" title="Edit Price / Category">✏️</button>
        </div>
      </td>
      <td>${statusBadge}</td>
      <td>${soldTo}</td>
      <td style="font-size:12px;color:var(--muted)">${date}</td>
      <td>
        <div style="display:flex;gap:6px;align-items:center;">
          <button class="btn-bal add" style="background:#6366f1;color:#fff;font-size:12px;padding:6px 12px;border-radius:6px;border:none;cursor:pointer;font-weight:600;" onclick="openOtpModal('${a.phone}', '${a.flag} ${a.country}')">📲 Get OTP</button>
          <button class="btn-bal set" style="background:#0284c7;color:#fff;font-size:12px;padding:6px 10px;border-radius:6px;border:none;cursor:pointer;font-weight:600;" onclick="openEditPriceModal(${a.id}, '${a.phone}', '${a.country}', ${a.price}, ${a.age})" title="Change Account / Category Price">🏷️ Price</button>
          <button class="del-btn" style="padding:6px 10px;" onclick="deleteAccount(${a.id}, '${a.phone}')">🗑️</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

// ── SELLER PAYOUTS DASHBOARD FUNCTIONS ───────────────────────────────────────
async function loadSellerStatsOnly() {
  try {
    const r = await fetch('/api/sellers/stats');
    const d = await r.json();
    const badge = document.getElementById('pending-badge');
    if (d.pending_count > 0) {
      badge.textContent = d.pending_count;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }
  } catch (e) {}
}

async function loadSellerPayouts() {
  const tbody = document.getElementById('sellers-tbody');
  tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:40px;color:#64748b">Loading user sold accounts...</td></tr>`;

  try {
    const [rStats, rList] = await Promise.all([
      fetch('/api/sellers/stats'),
      fetch('/api/sellers/payouts')
    ]);
    const stats = await rStats.json();
    allSellerPayoutsData = await rList.json();

    document.getElementById('stat-seller-total').textContent = stats.total_sold;
    document.getElementById('stat-seller-pending').textContent = stats.pending_count;
    document.getElementById('stat-seller-pending-amt').textContent = `₹${Math.round(stats.pending_amt)}`;
    document.getElementById('stat-seller-paid-amt').textContent = `₹${Math.round(stats.paid_amt)}`;

    const badge = document.getElementById('pending-badge');
    if (stats.pending_count > 0) {
      badge.textContent = stats.pending_count;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }

    filterSellerPayouts();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:40px;color:#ef4444">Error loading seller payouts: ${err}</td></tr>`;
  }
}

function filterSellerPayouts() {
  const search = document.getElementById('seller-search').value.toLowerCase().trim();
  const statusFilter = document.getElementById('seller-status-filter').value;

  const filtered = allSellerPayoutsData.filter(p => {
    const matchStatus = statusFilter === 'all' || p.status === statusFilter;
    const matchSearch = !search ||
      p.phone.toLowerCase().includes(search) ||
      (p.seller_username && p.seller_username.toLowerCase().includes(search)) ||
      String(p.seller_user_id).includes(search) ||
      p.upi_id.toLowerCase().includes(search) ||
      p.country.toLowerCase().includes(search);
    return matchStatus && matchSearch;
  });

  renderSellerPayouts(filtered);
}

function renderSellerPayouts(payouts) {
  const tbody = document.getElementById('sellers-tbody');
  if (!payouts.length) {
    tbody.innerHTML = `<tr><td colspan="9"><div style="text-align:center;padding:40px;color:var(--muted)"><span style="font-size:36px;display:block;margin-bottom:8px;">💸</span>No user sold accounts found.</div></td></tr>`;
    return;
  }

  tbody.innerHTML = payouts.map(p => {
    const flag = p.flag_emoji || '🌍';
    const username = p.seller_username ? `<a href="https://t.me/${p.seller_username}" target="_blank" style="color:#60a5fa;text-decoration:none;">@${p.seller_username}</a>` : '<span style="color:var(--muted)">NoUser</span>';
    
    let ageLabel = '🌱 Fresh';
    if (p.age_months >= 12) ageLabel = `👑 1+ Year (${p.age_months}M)`;
    else if (p.age_months >= 6) ageLabel = `🏆 6+ Months (${p.age_months}M)`;
    else if (p.age_months > 0) ageLabel = `⏳ ${p.age_months} Months`;

    const statusBadge = {
      pending: `<span class="badge pending"><span class="dot"></span>Pending Payout</span>`,
      paid: `<span class="badge paid"><span class="dot"></span>Paid</span>`,
      rejected: `<span class="badge rejected"><span class="dot"></span>Rejected</span>`,
    }[p.status] || p.status;

    const date = p.created_at ? p.created_at.split('.')[0].replace('T',' ') : '–';

    return `<tr>
      <td style="color:var(--muted);font-weight:700;">#${p.id}</td>
      <td>
        <div><strong>User ID:</strong> <code style="color:#a5b4fc">${p.seller_user_id}</code></div>
        <div style="font-size:12px;margin-top:2px;">${username}</div>
      </td>
      <td class="phone-cell">${flag} ${p.phone}</td>
      <td>
        <div><strong>${p.country}</strong></div>
        <div style="font-size:12px;color:var(--muted);margin-top:2px;">${ageLabel}</div>
      </td>
      <td class="price-cell">₹${Math.round(p.payout_amount)}</td>
      <td>
        <div style="display:flex;align-items:center;gap:6px;">
          <code style="background:var(--surface2);padding:4px 8px;border-radius:6px;color:#fcd34d;font-weight:700;">${p.upi_id}</code>
          <button class="btn-bal set" style="padding:2px 8px;font-size:11px;" onclick="copyText('${p.upi_id}')">📋</button>
        </div>
      </td>
      <td>${statusBadge}</td>
      <td style="font-size:12px;color:var(--muted);">${date}</td>
      <td>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          <button class="btn-bal add" style="background:#6366f1;color:#fff;font-size:12px;padding:6px 12px;font-weight:600;" onclick="openOtpModal('${p.phone}', '${flag} ${p.country}')">📲 Get OTP</button>
          ${p.status === 'pending' ? `<button class="btn-bal add" onclick="setPayoutStatus(${p.id}, 'paid')">✅ Mark Paid</button>` : ''}
          <button class="del-btn" style="padding:6px 10px;" onclick="deleteSellerPayoutRecord(${p.id})">🗑️</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

async function setPayoutStatus(payoutId, status) {
  if (!confirm(`Are you sure you want to mark Payout #${payoutId} as ${status.toUpperCase()}?`)) return;
  const fd = new FormData();
  fd.append('status', status);

  const r = await fetch(`/api/sellers/payouts/${payoutId}/status`, { method: 'POST', body: fd });
  const d = await r.json();
  if (d.ok) {
    toast(d.message, 'success');
    loadSellerPayouts();
  } else {
    toast(d.message, 'error');
  }
}

async function deleteSellerPayoutRecord(payoutId) {
  if (!confirm(`Delete payout record #${payoutId}?`)) return;
  const r = await fetch(`/api/sellers/payouts/${payoutId}`, { method: 'DELETE' });
  const d = await r.json();
  if (d.ok) {
    toast(d.message, 'success');
    loadSellerPayouts();
  } else {
    toast(d.message, 'error');
  }
}

// ── DIRECT NETBANKING LOGIN & AUTO GATEWAY FUNCTIONS ────────────────────────
let currentBankSessionToken = '';

async function loadBankSessions() {
  try {
    const r = await fetch('/api/netbanking/sessions');
    const sessions = await r.json();
    const listEl = document.getElementById('bank-connected-list');

    if (!sessions.length) {
      listEl.innerHTML = '';
      return;
    }

    listEl.innerHTML = sessions.map(s => {
      const lastSync = s.last_sync ? s.last_sync.split('.')[0] : 'Just now';
      return `<div class="connected-box" style="margin-bottom:12px;">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;">
          <div>
            <div style="display:flex;align-items:center;gap:8px;">
              <span style="width:10px;height:10px;border-radius:50%;background:#10b981;display:inline-block;animation:pulse 1.5s infinite;"></span>
              <strong style="color:#10b981;font-size:14px;">${s.bank_name} NetBanking</strong>
            </div>
            <div style="font-size:13px;margin-top:4px;">
              A/c: <strong>${s.account_number}</strong> • CustID: <code style="color:#60a5fa">${s.customer_id}</code> • Balance: <strong style="color:#34d399">₹${s.balance}</strong>
            </div>
            <div style="font-size:12px;color:var(--muted);margin-top:2px;">
              Last 24/7 Sync: <span style="color:#cbd5e1;">${lastSync}</span>
            </div>
          </div>
          <div style="display:flex;gap:8px;">
            <button class="btn-bal add" onclick="syncBankNow(${s.id})">🔄 Sync Now</button>
            <button class="del-btn" onclick="disconnectBank(${s.id})">🚪 Disconnect</button>
          </div>
        </div>
      </div>`;
    }).join('');
  } catch (err) {}
}

async function startBankLogin() {
  const bankName = document.getElementById('bank-name-select').value;
  const custId = document.getElementById('bank-cust-id').value.trim();
  const password = document.getElementById('bank-password').value.trim();
  const mobile = document.getElementById('bank-mobile').value.trim();

  if (!custId || !password) {
    toast('Please enter Customer ID and Password', 'error');
    return;
  }

  const btn = document.getElementById('btn-start-bank-login');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Connecting to Bank Portal...';

  const fd = new FormData();
  fd.append('bank_name', bankName);
  fd.append('customer_id', custId);
  fd.append('password', password);
  fd.append('mobile', mobile);

  const r = await fetch('/api/netbanking/login/start', { method: 'POST', body: fd });
  const d = await r.json();

  btn.disabled = false;
  btn.innerHTML = '🔐 Connect & Login Bank Server';

  if (d.ok) {
    currentBankSessionToken = d.session_token;
    toast(d.message, 'success');
    document.getElementById('btn-start-bank-login').style.display = 'none';
    document.getElementById('bank-otp-step').style.display = 'block';
    document.getElementById('bank-otp-input').focus();
  } else {
    toast(d.message, 'error');
  }
}

async function verifyBankLoginOtp() {
  const otp = document.getElementById('bank-otp-input').value.trim();
  if (!otp) {
    toast('Please enter the 2FA OTP code from your bank', 'error');
    return;
  }

  const btn = document.getElementById('btn-verify-bank-otp');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Authenticating Bank Session...';

  const fd = new FormData();
  fd.append('session_token', currentBankSessionToken);
  fd.append('otp', otp);

  const r = await fetch('/api/netbanking/login/verify', { method: 'POST', body: fd });
  const d = await r.json();

  btn.disabled = false;
  btn.innerHTML = '🚀 Verify Bank OTP & Start Polling';

  if (d.ok) {
    toast(d.message, 'success');
    resetBankForm();
    loadBankSessions();
    loadGatewayFeed();
  } else {
    toast(d.message, 'error');
  }
}

function resetBankForm() {
  document.getElementById('bank-otp-step').style.display = 'none';
  document.getElementById('btn-start-bank-login').style.display = 'block';
  document.getElementById('bank-otp-input').value = '';
}

// ── FAMPAY CLOUD GATEWAY HANDLERS ──────────────────────────────────────────
let currentFamPaySessionId = '';

async function loadFamPaySession() {
  try {
    const r = await fetch('/api/fampay/session');
    const sess = await r.json();
    const activeBox = document.getElementById('fampay-session-active');
    const loginBox = document.getElementById('fampay-login-box');

    if (sess && sess.auth_token) {
      activeBox.style.display = 'block';
      loginBox.style.display = 'none';
      document.getElementById('fampay-user-display').textContent = sess.account_name || 'FamPay Connected';
      document.getElementById('fampay-upi-display').textContent = sess.upi_id || `${sess.phone}@fam`;
      document.getElementById('fampay-phone-display').textContent = `+91 ${sess.phone}`;
      const lastSync = sess.last_sync ? sess.last_sync.split('.')[0].replace('T', ' ') : 'Active';
      document.getElementById('fampay-last-sync').textContent = lastSync;
    } else {
      activeBox.style.display = 'none';
      loginBox.style.display = 'block';
    }
  } catch (err) {}
}

function switchFamPayMode(mode) {
  if (mode === 'otp') {
    document.getElementById('fampay-mode-otp').style.display = 'block';
    document.getElementById('fampay-mode-token').style.display = 'none';
    document.getElementById('fampay-tab-otp').className = 'btn-bal set';
    document.getElementById('fampay-tab-token').className = 'refresh-btn';
  } else {
    document.getElementById('fampay-mode-otp').style.display = 'none';
    document.getElementById('fampay-mode-token').style.display = 'block';
    document.getElementById('fampay-tab-otp').className = 'refresh-btn';
    document.getElementById('fampay-tab-token').className = 'btn-bal set';
  }
}

async function startFamPayOtp() {
  const phone = document.getElementById('fampay-phone-input').value.trim();
  if (!phone || phone.length !== 10) {
    toast('Please enter a valid 10-digit mobile number', 'error');
    return;
  }

  const btn = document.getElementById('btn-start-fampay-otp');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Requesting FamPay OTP...';

  const fd = new FormData();
  fd.append('phone', phone);

  const r = await fetch('/api/fampay/login/start', { method: 'POST', body: fd });
  const d = await r.json();

  btn.disabled = false;
  btn.innerHTML = '📤 Send FamPay Login OTP';

  if (d.ok) {
    currentFamPaySessionId = d.session_id || '';
    toast(d.message, 'success');
    btn.style.display = 'none';
    document.getElementById('fampay-otp-step').style.display = 'block';
    document.getElementById('fampay-otp-input').focus();
  } else {
    toast(d.message, 'error');
  }
}

async function verifyFamPayOtp() {
  const phone = document.getElementById('fampay-phone-input').value.trim();
  const otp = document.getElementById('fampay-otp-input').value.trim();

  if (!otp) {
    toast('Please enter the OTP code', 'error');
    return;
  }

  const btn = document.getElementById('btn-verify-fampay-otp');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Connecting FamPay Cloud...';

  const fd = new FormData();
  fd.append('phone', phone);
  fd.append('otp', otp);
  fd.append('session_id', currentFamPaySessionId);

  const r = await fetch('/api/fampay/login/verify', { method: 'POST', body: fd });
  const d = await r.json();

  btn.disabled = false;
  btn.innerHTML = '🚀 Verify & Start Auto-Scan';

  if (d.ok) {
    toast(d.message, 'success');
    resetFamPayForm();
    loadFamPaySession();
    loadGatewayFeed();
  } else {
    toast(d.message, 'error');
  }
}

async function saveFamPayToken() {
  const token = document.getElementById('fampay-token-input').value.trim();
  const upi = document.getElementById('fampay-token-upi').value.trim();

  if (!token) {
    toast('Please enter FamPay Bearer Token', 'error');
    return;
  }

  const fd = new FormData();
  fd.append('auth_token', token);
  fd.append('upi_id', upi);

  const r = await fetch('/api/fampay/login/token', { method: 'POST', body: fd });
  const d = await r.json();

  if (d.ok) {
    toast(d.message, 'success');
    loadFamPaySession();
    loadGatewayFeed();
  } else {
    toast(d.message, 'error');
  }
}

function resetFamPayForm() {
  document.getElementById('fampay-otp-step').style.display = 'none';
  document.getElementById('btn-start-fampay-otp').style.display = 'block';
  document.getElementById('fampay-otp-input').value = '';
}

async function syncFamPayNow() {
  const r = await fetch('/api/fampay/sync', { method: 'POST' });
  const d = await r.json();
  toast(d.message || 'FamPay synced!', 'success');
  loadFamPaySession();
  loadGatewayFeed();
}

async function logoutFamPay() {
  if (!confirm('Disconnect FamPay Cloud Gateway?')) return;
  const r = await fetch('/api/fampay/logout', { method: 'POST' });
  const d = await r.json();
  toast(d.message, 'success');
  loadFamPaySession();
}

// ── GMAIL IMAP AUTO-SCANNER HANDLERS ───────────────────────────────────────
async function loadGmailStatus() {
  try {
    const r = await fetch('/api/gmail/status');
    const d = await r.json();
    if (d.ok) {
      document.getElementById('gmail-status-text').textContent = 'Connected (scanning every 6s)';
      document.getElementById('gmail-status-text').style.color = '#34d399';
    } else {
      document.getElementById('gmail-status-text').textContent = d.message || 'Connecting...';
      document.getElementById('gmail-status-text').style.color = '#f59e0b';
    }
    document.getElementById('gmail-last-sync').textContent = d.last_sync || 'Active';
  } catch (err) {}
}

async function syncGmailNow() {
  toast('Scanning Gmail inbox for FamPay payments...', 'info');
  const r = await fetch('/api/gmail/sync', { method: 'POST' });
  const d = await r.json();
  toast(d.message || 'Gmail inbox scanned!', 'success');
  loadGmailStatus();
  loadGatewayFeed();
}

// ── Channel & Force Join Settings ──────────────────────────────────────────
async function loadChannelSettings() {
  try {
    const res = await fetch('/api/channel-settings');
    const data = await res.json();
    document.getElementById('force-channel-id').value = data.channel_id || '@sii_3s';
    document.getElementById('force-channel-link').value = data.channel_link || '';
    document.getElementById('force-sub-toggle').checked = data.enabled;
    document.getElementById('force-join-count').innerText = data.join_requests_count || 0;
    updateForceSubBadge(data.enabled);
  } catch (e) {
    console.error('Error loading channel settings:', e);
  }
}

function updateForceSubBadge(enabled) {
  const badge = document.getElementById('force-sub-badge');
  if (badge) {
    badge.style.background = enabled ? '#10b981' : '#ef4444';
    badge.innerText = enabled ? 'ACTIVE' : 'DISABLED';
  }
}

async function saveChannelSettings() {
  const chId = document.getElementById('force-channel-id').value.trim();
  const chLink = document.getElementById('force-channel-link').value.trim();
  const enabled = document.getElementById('force-sub-toggle').checked;

  const fd = new FormData();
  fd.append('channel_id', chId);
  fd.append('channel_link', chLink);
  fd.append('enabled', enabled);

  try {
    const res = await fetch('/api/save-channel-settings', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.ok) {
      updateForceSubBadge(enabled);
      toast('✅ Channel & Join Request settings saved successfully!', 'success');
    } else {
      toast('❌ Failed to save channel settings.', 'error');
    }
  } catch (e) {
    toast('❌ Error saving channel settings: ' + e.message, 'error');
  }
}

async function syncBankNow(sessId) {
  const r = await fetch('/api/netbanking/sync', { method: 'POST' });
  const d = await r.json();
  toast(d.message || 'Bank statement synced!', 'success');
  loadBankSessions();
  loadGatewayFeed();
}

async function disconnectBank(sessId) {
  if (!confirm('Disconnect this Bank session?')) return;
  const r = await fetch(`/api/netbanking/sessions/${sessId}`, { method: 'DELETE' });
  const d = await r.json();
  toast(d.message, 'success');
  loadBankSessions();
}

async function loadGatewayStatsOnly() {
  try {
    const r = await fetch('/api/gateway/stats');
    const d = await r.json();
    const badge = document.getElementById('gateway-unclaimed-badge');
    if (d.unclaimed_tx > 0) {
      badge.textContent = d.unclaimed_tx;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }
  } catch (e) {}
}

async function loadGatewayFeed() {
  const tbody = document.getElementById('gateway-tbody');
  tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:40px;color:#64748b">Loading live bank transactions feed...</td></tr>`;

  try {
    const [rStats, rFeed] = await Promise.all([
      fetch('/api/gateway/stats'),
      fetch('/api/gateway/feed')
    ]);
    const stats = await rStats.json();
    allGatewayFeedData = await rFeed.json();

    document.getElementById('stat-gw-total-amt').textContent = `₹${Math.round(stats.total_amt)}`;
    document.getElementById('stat-gw-redeemed-amt').textContent = `₹${Math.round(stats.redeemed_amt)}`;
    document.getElementById('stat-gw-unclaimed-amt').textContent = `₹${Math.round(stats.unclaimed_amt)}`;
    document.getElementById('stat-gw-pending-cnt').textContent = stats.pending_deposits_cnt;

    const badge = document.getElementById('gateway-unclaimed-badge');
    if (stats.unclaimed_tx > 0) {
      badge.textContent = stats.unclaimed_tx;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }

    filterGatewayFeed();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:40px;color:#ef4444">Error loading gateway feed: ${err}</td></tr>`;
  }
}

function filterGatewayFeed() {
  const search = document.getElementById('gw-search').value.toLowerCase().trim();
  const statusFilter = document.getElementById('gw-status-filter').value;

  const filtered = allGatewayFeedData.filter(tx => {
    const matchStatus = statusFilter === 'all' ||
      (statusFilter === 'redeemed' && tx.is_redeemed === 1) ||
      (statusFilter === 'unclaimed' && tx.is_redeemed === 0);
    const matchSearch = !search ||
      tx.utr.toLowerCase().includes(search) ||
      (tx.sender_name && tx.sender_name.toLowerCase().includes(search)) ||
      (tx.redeemed_by && String(tx.redeemed_by).includes(search)) ||
      (tx.username && tx.username.toLowerCase().includes(search));
    return matchStatus && matchSearch;
  });

  renderGatewayFeed(filtered);
}

function renderGatewayFeed(feed) {
  const tbody = document.getElementById('gateway-tbody');
  if (!feed.length) {
    tbody.innerHTML = `<tr><td colspan="9"><div style="text-align:center;padding:40px;color:var(--muted)"><span style="font-size:36px;display:block;margin-bottom:8px;">🏦</span>No bank transactions recorded yet.<br>Login your bank account above or test with the simulator tool.</div></td></tr>`;
    return;
  }

  tbody.innerHTML = feed.map(tx => {
    const statusBadge = tx.is_redeemed
      ? `<span class="badge paid"><span class="dot"></span>Auto-Credited</span>`
      : `<span class="badge pending"><span class="dot"></span>Unclaimed In Pool</span>`;

    const userCell = tx.redeemed_by
      ? `<div><strong>ID:</strong> <code style="color:#a5b4fc">${tx.redeemed_by}</code><div style="font-size:12px;color:#60a5fa">${tx.username ? '@'+tx.username : tx.first_name || ''}</div></div>`
      : `<span style="color:var(--muted)">–</span>`;

    const date = tx.created_at ? tx.created_at.split('.')[0].replace('T',' ') : '–';

    return `<tr>
      <td style="color:var(--muted);font-weight:700;">#${tx.id}</td>
      <td>
        <div style="display:flex;align-items:center;gap:6px;">
          <code style="font-size:14px;color:#60a5fa;font-weight:700;letter-spacing:0.5px;">${tx.utr}</code>
          <button class="btn-bal set" style="padding:2px 8px;font-size:11px;" onclick="copyText('${tx.utr}')">📋</button>
        </div>
      </td>
      <td class="price-cell">₹${Math.round(tx.amount)}</td>
      <td><strong>${tx.sender_name || 'UPI Payer'}</strong></td>
      <td><span style="background:var(--surface2);padding:3px 8px;border-radius:6px;font-size:12px;color:#cbd5e1;">${tx.source_app || 'NetBanking'}</span></td>
      <td>${statusBadge}</td>
      <td>${userCell}</td>
      <td style="font-size:12px;color:var(--muted);">${date}</td>
      <td>
        ${!tx.is_redeemed ? `<button class="btn-bal add" style="font-size:12px;" onclick="manualRedeemModal('${tx.utr}', ${tx.amount})">🔗 Link User</button>` : `<span style="color:#10b981;font-size:12px;font-weight:600;">✅ Done</span>`}
      </td>
    </tr>`;
  }).join('');
}

async function simulateBankTx() {
  const utr = document.getElementById('sim-utr').value.trim() || ('TEST' + Math.floor(100000000000 + Math.random() * 900000000000));
  const amt = parseFloat(document.getElementById('sim-amt').value) || 100.0;

  const fd = new FormData();
  fd.append('utr', utr);
  fd.append('amount', amt);
  fd.append('sender', 'Test Bank Payer');
  fd.append('source', 'NetBanking Live Simulator');

  const r = await fetch('/api/upi/notify', { method: 'POST', body: fd });
  const d = await r.json();
  if (d.ok) {
    toast(d.message, 'success');
    loadGatewayFeed();
  } else {
    toast(d.message, 'error');
  }
}

function manualRedeemModal(utr, amt) {
  const uid = prompt(`Enter Telegram User ID to credit ₹${amt} for UTR ${utr}:`);
  if (!uid) return;

  const fd = new FormData();
  fd.append('utr', utr);
  fd.append('user_id', parseInt(uid));
  fd.append('amount', amt);

  fetch('/api/gateway/manual-redeem', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        toast(d.message, 'success');
        loadGatewayFeed();
      } else {
        toast(d.message, 'error');
      }
    });
}

function copyText(text) {
  navigator.clipboard.writeText(text);
  toast(`Copied: ${text}`, 'success');
}

// ── Live OTP Modal Handlers ──────────────────────────────────────────────────
let currentOtpPhone = '';

async function openOtpModal(phone, countryName) {
  currentOtpPhone = phone;
  document.getElementById('otp-modal-overlay').classList.add('active');
  document.getElementById('otp-modal-title').textContent = `Login OTP: ${countryName}`;
  document.getElementById('otp-phone-display').textContent = phone;
  document.getElementById('otp-code-display').textContent = 'Loading...';
  document.getElementById('otp-2fa-display').textContent = 'Loading...';
  document.getElementById('otp-full-msg').textContent = 'Connecting to Telegram session & reading latest service messages...';
  
  await fetchLiveOtp();
}

function closeOtpModal() {
  document.getElementById('otp-modal-overlay').classList.remove('active');
}

function closeOtpModalOutside(e) {
  if (e.target === document.getElementById('otp-modal-overlay')) closeOtpModal();
}

async function fetchLiveOtp() {
  if (!currentOtpPhone) return;
  const btn = document.getElementById('otp-refresh-btn');
  btn.disabled = true;
  btn.textContent = '🔄 Checking Telegram...';

  try {
    const r = await fetch(`/api/accounts/otp?phone=${encodeURIComponent(currentOtpPhone)}`);
    const d = await r.json();
    btn.disabled = false;
    btn.textContent = '🔄 Refresh / Check New OTP';

    if (d.ok) {
      document.getElementById('otp-code-display').textContent = d.otp || '---';
      document.getElementById('otp-2fa-display').textContent = d.two_fa || 'None';
      document.getElementById('otp-full-msg').textContent = d.full_text || 'No message';
      toast('Live OTP updated!', 'success');
    } else {
      document.getElementById('otp-code-display').textContent = 'No OTP';
      document.getElementById('otp-2fa-display').textContent = d.two_fa || 'None';
      document.getElementById('otp-full-msg').textContent = d.message || 'Error';
      toast(d.message, 'error');
    }
  } catch (err) {
    btn.disabled = false;
    btn.textContent = '🔄 Refresh / Check New OTP';
    toast('Failed to fetch OTP: ' + err, 'error');
  }
}

function copyPhone() {
  if (!currentOtpPhone) return;
  navigator.clipboard.writeText(currentOtpPhone);
  toast(`Copied Phone: ${currentOtpPhone}`, 'success');
}

function copyOtpCode() {
  const code = document.getElementById('otp-code-display').textContent.trim();
  if (!code || code === 'Loading...' || code === '---' || code === 'No OTP') return;
  navigator.clipboard.writeText(code);
  toast(`Copied OTP: ${code}`, 'success');
}

function copy2faPass() {
  const pass = document.getElementById('otp-2fa-display').textContent.trim();
  if (!pass || pass === 'Loading...' || pass === 'None') return;
  navigator.clipboard.writeText(pass);
  toast(`Copied 2FA Password!`, 'success');
}

// ── Modal Handlers ──────────────────────────────────────────────────────────
function openModal() {
  document.getElementById('modal-overlay').classList.add('active');
  document.getElementById('phone-input').focus();
}
function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
  goBackStep1();
}
function closeModalOutside(e) {
  if (e.target === document.getElementById('modal-overlay')) closeModal();
}
function goBackStep1() {
  document.getElementById('step1').style.display = 'block';
  document.getElementById('step2').style.display = 'none';
  document.getElementById('step1-dot').className = 'step-dot active';
  document.getElementById('step2-dot').className = 'step-dot';
  document.getElementById('modal-sub').textContent = 'Select country, age tier and enter phone number';
  document.getElementById('otp-input').value = '';
  document.getElementById('twofa-input').value = '';
  document.getElementById('twofa-group').style.display = 'none';
}

// ── Send OTP ─────────────────────────────────────────────────────────────
async function sendOTP() {
  const phone = document.getElementById('phone-input').value.trim();
  if (!phone) { toast('Please enter phone number with country code', 'error'); return; }

  savedLoginCountry = document.getElementById('country-input').value.trim() || 'India';
  savedLoginFlag = document.getElementById('flag-input').value.trim() || '🇮🇳';
  savedLoginAgeTier = document.getElementById('age-tier-select').value;
  savedLoginAge = parseInt(document.getElementById('age-input').value) || 0;
  savedLoginPrice = parseFloat(document.getElementById('price-input').value) || 22.0;

  const btn = document.getElementById('send-otp-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Sending OTP code...';

  const fd = new FormData();
  fd.append('phone', phone);

  const r = await fetch('/api/login/start', { method: 'POST', body: fd });
  const d = await r.json();

  btn.disabled = false;
  btn.innerHTML = '📤 Send OTP Code';

  if (d.ok) {
    currentPhone = phone;
    toast(d.message, 'success');
    document.getElementById('step1').style.display = 'none';
    document.getElementById('step2').style.display = 'block';
    document.getElementById('step1-dot').className = 'step-dot done';
    document.getElementById('step2-dot').className = 'step-dot active';
    document.getElementById('modal-sub').textContent = `Enter the OTP for ${savedLoginFlag} ${savedLoginCountry} (₹${savedLoginPrice})`;
    document.getElementById('target-phone-display').textContent = phone;
    document.getElementById('otp-input').focus();
  } else {
    toast(d.message, 'error');
  }
}

// ── Verify OTP ───────────────────────────────────────────────────────────
async function verifyOTP() {
  const otp = document.getElementById('otp-input').value.trim();
  const twofa = document.getElementById('twofa-input').value.trim();
  if (!otp) { toast('Please enter the OTP code', 'error'); return; }

  const country = savedLoginCountry || document.getElementById('country-input').value.trim() || 'India';
  const flag = savedLoginFlag || document.getElementById('flag-input').value.trim() || '🇮🇳';
  const ageTier = savedLoginAgeTier || document.getElementById('age-tier-select').value;
  const accType = (ageTier && ageTier.startsWith('aged')) ? 'aged' : 'fresh';
  const age = savedLoginAge !== undefined ? savedLoginAge : (parseInt(document.getElementById('age-input').value) || 0);
  const price = (savedLoginPrice !== undefined && !isNaN(savedLoginPrice)) ? savedLoginPrice : (parseFloat(document.getElementById('price-input').value) || 22.0);

  const btn = document.getElementById('verify-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Logging in & saving...';

  const fd = new FormData();
  fd.append('phone', currentPhone);
  fd.append('otp', otp);
  fd.append('two_fa', twofa);
  fd.append('country', country);
  fd.append('flag', flag);
  fd.append('acc_type', accType);
  fd.append('age_months', age);
  fd.append('price', price);

  const r = await fetch('/api/login/verify', { method: 'POST', body: fd });
  const d = await r.json();

  btn.disabled = false;
  btn.innerHTML = '🔐 Verify & Save Account';

  if (d.ok) {
    toast('✅ ' + d.message, 'success');
    closeModal();
    loadAccounts();
  } else if (d.needs_2fa) {
    document.getElementById('twofa-group').style.display = 'block';
    document.getElementById('twofa-input').focus();
    toast('2FA Password required for this account', 'error');
  } else {
    toast(d.message, 'error');
  }
}

// ── Delete Account ──────────────────────────────────────────────────────────
async function deleteAccount(id, phone) {
  if (!confirm(`Are you sure you want to delete ${phone}?`)) return;
  const r = await fetch(`/api/accounts/${id}`, { method: 'DELETE' });
  const d = await r.json();
  if (d.ok) {
    toast(d.message, 'success');
    loadAccounts();
  } else {
    toast(d.message, 'error');
  }
}

// ── Category & Account Price/Age Manager Functions ─────────────────────────
let currentEditModalMode = 'single';

function setEditModalMode(mode) {
  currentEditModalMode = mode;
  const singleBtn = document.getElementById('tab-btn-single-acc');
  const bulkBtn = document.getElementById('tab-btn-bulk-cat');
  const singleWrap = document.getElementById('modal-single-acc-wrap');
  const bulkWrap = document.getElementById('modal-bulk-cat-wrap');
  const bannerEl = document.getElementById('edit-acc-banner');

  if (mode === 'single') {
    singleBtn.style.background = '#6366f1';
    singleBtn.style.color = '#fff';
    bulkBtn.style.background = 'transparent';
    bulkBtn.style.color = '#94a3b8';
    singleWrap.style.display = 'flex';
    bulkWrap.style.display = 'none';
    bannerEl.style.display = 'flex';
  } else {
    singleBtn.style.background = 'transparent';
    singleBtn.style.color = '#94a3b8';
    bulkBtn.style.background = '#6366f1';
    bulkBtn.style.color = '#fff';
    singleWrap.style.display = 'none';
    bulkWrap.style.display = 'flex';
    bannerEl.style.display = 'none';
    recalculatePriceAffectedCount();
  }
}

function selectAgeTier(months, tierCode) {
  document.getElementById('edit-price-age-raw').value = months;
  
  [0, 3, 6, 12].forEach(m => {
    const card = document.getElementById('age-card-' + m);
    if (card) card.classList.remove('active');
  });

  const activeCard = document.getElementById('age-card-' + months);
  if (activeCard) activeCard.classList.add('active');

  const customCheck = document.getElementById('custom-age-check');
  const customNum = document.getElementById('custom-age-number-input');
  if (customCheck) customCheck.checked = false;
  if (customNum) {
    customNum.style.display = 'none';
    customNum.value = months;
  }

  const labelEl = document.getElementById('selected-age-label');
  if (labelEl) {
    if (months === 0) labelEl.textContent = '🌱 Fresh (0-1 Month)';
    else if (months <= 3) labelEl.textContent = `⏳ 3 Months (${months}M)`;
    else if (months <= 6) labelEl.textContent = `🏆 6+ Months (${months}M)`;
    else labelEl.textContent = `👑 1+ Year (${months}M)`;
  }
}

function toggleCustomAgeInput(checked) {
  const customNum = document.getElementById('custom-age-number-input');
  if (checked) {
    customNum.style.display = 'inline-block';
    customNum.focus();
    [0, 3, 6, 12].forEach(m => {
      const card = document.getElementById('age-card-' + m);
      if (card) card.classList.remove('active');
    });
    onCustomAgeNumberInput(customNum.value || 0);
  } else {
    customNum.style.display = 'none';
    selectAgeTier(0, 'fresh');
  }
}

function onCustomAgeNumberInput(val) {
  const num = parseInt(val) || 0;
  document.getElementById('edit-price-age-raw').value = num;
  const labelEl = document.getElementById('selected-age-label');
  if (labelEl) labelEl.textContent = `⚙️ Custom: ${num} Months Old`;
}

function openEditPriceModal(id, phone, countryName, currentPrice, ageMonths) {
  document.getElementById('price-modal-title').textContent = '⚙️ Edit Account Details';
  document.getElementById('price-modal-subtitle').textContent = `Manage Age Quality Tier and Sale Price for ${phone}`;
  document.getElementById('edit-price-acc-id').value = id;
  document.getElementById('edit-price-country-raw').value = countryName || 'India';
  
  const rawMonths = ageMonths !== undefined ? parseInt(ageMonths) : 0;
  document.getElementById('edit-price-age-raw').value = rawMonths;

  document.getElementById('edit-banner-phone').textContent = phone;
  document.getElementById('edit-banner-country').textContent = countryName || 'India';
  
  const foundCountry = (allCountriesData || []).find(c => c.country.toLowerCase() === (countryName || 'india').toLowerCase());
  document.getElementById('edit-banner-flag').textContent = foundCountry ? foundCountry.flag : '📱';

  // Select matching age card
  if ([0, 3, 6, 12].includes(rawMonths)) {
    selectAgeTier(rawMonths, rawMonths === 0 ? 'fresh' : (rawMonths === 3 ? '3m' : (rawMonths === 6 ? '6m' : '1y')));
  } else {
    const customCheck = document.getElementById('custom-age-check');
    const customNum = document.getElementById('custom-age-number-input');
    if (customCheck) customCheck.checked = true;
    if (customNum) {
      customNum.style.display = 'inline-block';
      customNum.value = rawMonths;
    }
    onCustomAgeNumberInput(rawMonths);
  }

  const priceVal = Math.round(currentPrice) || currentPrice || 30;
  document.getElementById('edit-price-input').value = priceVal;
  updatePriceUsdPreview(priceVal);

  const bulkInput = document.getElementById('bulk-price-input');
  if (bulkInput) {
    bulkInput.value = priceVal;
    updateBulkPriceUsdPreview(priceVal);
  }

  document.getElementById('edit-mode-tab-wrap').style.display = 'flex';
  setEditModalMode('single');

  populatePriceScopeOptions(id, phone, countryName, priceVal, rawMonths);
  document.getElementById('price-modal-overlay').classList.add('active');
  setTimeout(() => document.getElementById('edit-price-input').focus(), 100);
}

function openCategoryPriceModal(presetCountry = 'all', presetTier = 'all') {
  document.getElementById('price-modal-title').textContent = '🏷️ Bulk Category Price Manager';
  document.getElementById('price-modal-subtitle').textContent = 'Set and update prices across countries and age tiers in bulk';
  document.getElementById('edit-price-acc-id').value = '';
  document.getElementById('edit-price-country-raw').value = presetCountry;
  document.getElementById('edit-price-age-raw').value = presetTier;

  const currentVal = parseFloat(document.getElementById('edit-price-input').value) || 30;
  const bulkInput = document.getElementById('bulk-price-input');
  if (bulkInput) {
    bulkInput.value = currentVal;
    updateBulkPriceUsdPreview(currentVal);
  }

  populatePriceScopeOptions(null, null, presetCountry, currentVal, presetTier);
  document.getElementById('edit-mode-tab-wrap').style.display = 'none';
  setEditModalMode('bulk');
  document.getElementById('edit-price-scope-select').value = 'custom';
  onPriceScopeSelectChange();

  document.getElementById('price-modal-overlay').classList.add('active');
}

function populatePriceScopeOptions(id, phone, country, currentPrice, ageMonths) {
  const selectEl = document.getElementById('edit-price-scope-select');
  const cleanCountry = country && country !== 'all' ? country : 'India';

  let optionsHtml = `
    <optgroup label="📍 Category in ${cleanCountry}">
      <option value="cat_${cleanCountry}_fresh">🌱 Category: Fresh (0-1 Month) in ${cleanCountry}</option>
      <option value="cat_${cleanCountry}_3m">⏳ Category: 3 Months (Aged) in ${cleanCountry}</option>
      <option value="cat_${cleanCountry}_6m">🏆 Category: 6+ Months (Aged) in ${cleanCountry}</option>
      <option value="cat_${cleanCountry}_1y">👑 Category: 1+ Year (Aged) in ${cleanCountry}</option>
      <option value="country_${cleanCountry}">🌍 All Categories in ${cleanCountry} (Entire Country Stock)</option>
    </optgroup>
    <optgroup label="🌐 Global Category (Across All 196+ Countries)">
      <option value="global_fresh">🌱 Global: All Fresh (0-1 Month) Accounts</option>
      <option value="global_3m">⏳ Global: All 3 Months (Aged) Accounts</option>
      <option value="global_6m">🏆 Global: All 6+ Months (Aged) Accounts</option>
      <option value="global_1y">👑 Global: All 1+ Year (Aged) Accounts</option>
      <option value="global_all">🌐 Global: All Unsold Accounts in Store</option>
    </optgroup>
    <optgroup label="⚙️ Custom Selection">
      <option value="custom">⚙️ Custom Country & Category Pickers...</option>
    </optgroup>
  `;

  selectEl.innerHTML = optionsHtml;

  // Populate custom country select if needed
  const customCountryEl = document.getElementById('price-custom-country-select');
  if (customCountryEl && allCountriesData && allCountriesData.length) {
    const curVal = customCountryEl.value || (country && country !== 'all' ? country : 'all');
    customCountryEl.innerHTML = `<option value="all">🌐 All Countries</option>` + allCountriesData.map(c =>
      `<option value="${c.country}" ${c.country === curVal ? 'selected' : ''}>${c.flag} ${c.country} (${c.stock} in stock)</option>`
    ).join('');
  }

  onPriceScopeSelectChange();
}

function onPriceScopeSelectChange() {
  const scopeVal = document.getElementById('edit-price-scope-select').value;
  const customWrap = document.getElementById('price-custom-category-wrap');

  if (scopeVal === 'custom') {
    customWrap.style.display = 'block';
  } else {
    customWrap.style.display = 'none';
  }

  recalculatePriceAffectedCount();
}

function recalculatePriceAffectedCount() {
  const scopeVal = document.getElementById('edit-price-scope-select').value;
  const summaryEl = document.getElementById('price-target-summary');
  const badgeEl = document.getElementById('price-affected-badge');

  let affectedCount = 0;
  let summaryText = '';

  const unsoldAccounts = allAccountsData.filter(a => a.status === 'available' || !a.sold_to);

  if (scopeVal.startsWith('cat_')) {
    const parts = scopeVal.split('_');
    const c = parts[1];
    const tier = parts[2];
    affectedCount = unsoldAccounts.filter(a => {
      const matchC = a.country.toLowerCase() === c.toLowerCase();
      let matchT = false;
      if (tier === 'fresh') matchT = a.age === 0;
      else if (tier === '3m') matchT = a.age > 0 && a.age <= 3;
      else if (tier === '6m') matchT = a.age > 3 && a.age <= 6;
      else if (tier === '1y') matchT = a.age >= 12;
      return matchC && matchT;
    }).length;
    summaryText = `🎯 Category: ${tier.toUpperCase()} in ${c}`;
  } else if (scopeVal.startsWith('country_')) {
    const c = scopeVal.replace('country_', '');
    affectedCount = unsoldAccounts.filter(a => a.country.toLowerCase() === c.toLowerCase()).length;
    summaryText = `🌍 All Accounts in ${c}`;
  } else if (scopeVal.startsWith('global_')) {
    const tier = scopeVal.replace('global_', '');
    if (tier === 'all') {
      affectedCount = unsoldAccounts.length;
      summaryText = `🌐 All Unsold Accounts in Store`;
    } else {
      affectedCount = unsoldAccounts.filter(a => {
        if (tier === 'fresh') return a.age === 0;
        if (tier === '3m') return a.age > 0 && a.age <= 3;
        if (tier === '6m') return a.age > 3 && a.age <= 6;
        if (tier === '1y') return a.age >= 12;
        return true;
      }).length;
      summaryText = `🌐 Global Category: ${tier.toUpperCase()} (All Countries)`;
    }
  } else if (scopeVal === 'custom') {
    const selC = document.getElementById('price-custom-country-select').value;
    const selT = document.getElementById('price-custom-tier-select').value;

    affectedCount = unsoldAccounts.filter(a => {
      const matchC = selC === 'all' || a.country.toLowerCase() === selC.toLowerCase();
      let matchT = true;
      if (selT === 'fresh') matchT = a.age === 0;
      else if (selT === '3m') matchT = a.age > 0 && a.age <= 3;
      else if (selT === '6m') matchT = a.age > 3 && a.age <= 6;
      else if (selT === '1y') matchT = a.age >= 12;
      return matchC && matchT;
    }).length;
    summaryText = `⚙️ Custom: ${selC === 'all' ? 'All Countries' : selC} • ${selT.toUpperCase()}`;
  }

  summaryEl.textContent = summaryText;
  badgeEl.textContent = `${affectedCount} In-Stock Account${affectedCount === 1 ? '' : 's'}`;
}

function closePriceModal() {
  document.getElementById('price-modal-overlay').classList.remove('active');
}

function closePriceModalOutside(e) {
  if (e.target === document.getElementById('price-modal-overlay')) closePriceModal();
}

function setQuickPrice(amt) {
  document.getElementById('edit-price-input').value = amt;
  updatePriceUsdPreview(amt);
}

function updatePriceUsdPreview(val) {
  const num = parseFloat(val) || 0;
  const usd = (num / 100.0).toFixed(2);
  const el = document.getElementById('edit-price-usd-preview');
  if (el) el.textContent = `≈ $${usd} USD`;
}

function updateBulkPriceUsdPreview(val) {
  const num = parseFloat(val) || 0;
  const usd = (num / 100.0).toFixed(2);
  const el = document.getElementById('bulk-price-usd-preview');
  if (el) el.textContent = `≈ $${usd} USD`;
}

async function submitAccountPriceChange() {
  const btn = document.getElementById('edit-price-submit-btn');

  if (currentEditModalMode === 'single') {
    const accId = document.getElementById('edit-price-acc-id').value;
    const price = parseFloat(document.getElementById('edit-price-input').value);
    const ageMonths = parseInt(document.getElementById('edit-price-age-raw').value) || 0;
    const country = document.getElementById('edit-price-country-raw').value || 'India';

    if (!accId) {
      toast('Invalid account ID', 'error');
      return;
    }
    if (isNaN(price) || price <= 0) {
      toast('Please enter a valid price greater than 0', 'error');
      return;
    }

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Saving...';

    try {
      const fd = new FormData();
      fd.append('account_id', accId);
      fd.append('age_months', ageMonths);
      fd.append('price', price);
      fd.append('country', country);

      const r = await fetch('/api/accounts/edit-details', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.ok) {
        toast(d.message, 'success');
        closePriceModal();
        loadAccounts();
      } else {
        toast(d.message, 'error');
      }
    } catch (e) {
      toast('Error saving changes: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '💾 Save Changes Now';
    }

  } else {
    // Bulk mode
    const scopeVal = document.getElementById('edit-price-scope-select').value;
    const price = parseFloat(document.getElementById('bulk-price-input').value);

    if (isNaN(price) || price <= 0) {
      toast('Please enter a valid price greater than 0', 'error');
      return;
    }

    let backendScope = 'category';
    let country = 'all';
    let ageTier = 'all';

    if (scopeVal.startsWith('cat_')) {
      backendScope = 'category';
      const parts = scopeVal.split('_');
      country = parts[1];
      ageTier = parts[2];
    } else if (scopeVal.startsWith('country_')) {
      backendScope = 'country';
      country = scopeVal.replace('country_', '');
      ageTier = 'all';
    } else if (scopeVal.startsWith('global_')) {
      const tier = scopeVal.replace('global_', '');
      if (tier === 'all') {
        backendScope = 'all';
      } else {
        backendScope = 'global_category';
        ageTier = tier;
      }
    } else if (scopeVal === 'custom') {
      backendScope = 'category';
      country = document.getElementById('price-custom-country-select').value;
      ageTier = document.getElementById('price-custom-tier-select').value;
    }

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Saving...';

    try {
      const fd = new FormData();
      fd.append('scope', backendScope);
      fd.append('country', country);
      fd.append('age_tier', ageTier);
      fd.append('price', price);

      const r = await fetch('/api/accounts/update-price', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.ok) {
        toast(d.message, 'success');
        closePriceModal();
        loadAccounts();
      } else {
        toast(d.message, 'error');
      }
    } catch (e) {
      toast('Error updating price: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = '💾 Save Changes Now';
    }
  }
}

// ── Load & Filter Users ─────────────────────────────────────────────────────
let allUsersData = [];

async function loadUsers() {
  const r = await fetch('/api/users');
  allUsersData = await r.json();
  filterUsers();
}

function filterUsers() {
  const search = document.getElementById('user-search').value.toLowerCase().trim();
  const filtered = allUsersData.filter(u => {
    return !search ||
      String(u.user_id).includes(search) ||
      (u.first_name && u.first_name.toLowerCase().includes(search)) ||
      (u.username && u.username.toLowerCase().includes(search));
  });
  renderUsers(filtered);
}

function renderUsers(users) {
  const tbody = document.getElementById('users-tbody');
  if (!users.length) {
    tbody.innerHTML = `<tr><td colspan="8"><div style="text-align:center;padding:40px;color:var(--muted)">No registered users found.</div></td></tr>`;
    return;
  }

  tbody.innerHTML = users.map(u => {
    const username = u.username ? `<span style="color:#60a5fa">@${u.username}</span>` : '<span style="color:var(--muted)">–</span>';
    const date = u.joined_at ? u.joined_at.split(' ')[0] : '–';

    return `<tr>
      <td><code style="font-size:13px;color:#a5b4fc">${u.user_id}</code></td>
      <td><strong>${u.first_name || '–'}</strong></td>
      <td>${username}</td>
      <td><strong style="color:#10b981;font-size:15px">₹${Math.round(u.balance)}</strong></td>
      <td><span style="font-size:13px;color:#cbd5e1">${u.total_orders || 0}</span></td>
      <td style="color:var(--muted)">₹${Math.round(u.total_spent || 0)}</td>
      <td style="font-size:12px;color:var(--muted)">${date}</td>
      <td>
        <div class="bal-actions">
          <button class="btn-bal add" onclick="openBalModal(${u.user_id}, '${(u.first_name||'User').replace(/'/g,"")}', 'add', ${Math.round(u.balance)})">+ Add</button>
          <button class="btn-bal cut" onclick="openBalModal(${u.user_id}, '${(u.first_name||'User').replace(/'/g,"")}', 'cut', ${Math.round(u.balance)})">- Deduct</button>
          <button class="btn-bal set" onclick="openBalModal(${u.user_id}, '${(u.first_name||'User').replace(/'/g,"")}', 'set', ${Math.round(u.balance)})">= Set</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

// ── Balance Modal ────────────────────────────────────────────────────────
function openBalModal(uid, name, action, currentBal) {
  document.getElementById('bal-user-id').value = uid;
  document.getElementById('bal-action').value = action;
  document.getElementById('bal-amount-input').value = '';

  const titleMap = {
    add: `➕ Add Balance to ${name}`,
    cut: `➖ Deduct Balance from ${name}`,
    set: `✏️ Set Exact Balance for ${name}`,
  };
  const labelMap = {
    add: 'Amount to Credit (₹)',
    cut: 'Amount to Deduct (₹)',
    set: 'New Exact Balance (₹)',
  };

  document.getElementById('bal-modal-title').textContent = titleMap[action] || 'Adjust Balance';
  document.getElementById('bal-modal-sub').textContent = `User ID: ${uid} • Current Balance: ₹${currentBal}`;
  document.getElementById('bal-amount-label').textContent = labelMap[action] || 'Amount (₹)';
  document.getElementById('bal-modal-overlay').classList.add('active');
  document.getElementById('bal-amount-input').focus();
}

function closeBalModal() {
  document.getElementById('bal-modal-overlay').classList.remove('active');
}
function closeBalModalOutside(e) {
  if (e.target === document.getElementById('bal-modal-overlay')) closeBalModal();
}
function setQuickAmount(amt) {
  document.getElementById('bal-amount-input').value = amt;
}

async function submitBalanceChange() {
  const uid = document.getElementById('bal-user-id').value;
  const action = document.getElementById('bal-action').value;
  const amt = parseFloat(document.getElementById('bal-amount-input').value);

  if (isNaN(amt) || (action !== 'set' && amt <= 0) || (action === 'set' && amt < 0)) {
    toast('Please enter a valid amount', 'error');
    return;
  }

  const btn = document.getElementById('bal-submit-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Saving...';

  const fd = new FormData();
  fd.append('user_id', uid);
  fd.append('action', action);
  fd.append('amount', amt);

  const r = await fetch('/api/users/balance', { method: 'POST', body: fd });
  const d = await r.json();

  btn.disabled = false;
  btn.innerHTML = 'Confirm & Save';

  if (d.ok) {
    toast(d.message, 'success');
    closeBalModal();
    loadUsers();
  } else {
    toast(d.message, 'error');
  }
}

// ── Power Broadcaster Dashboard Functions ──────────────────────────────────
let bcPollInterval = null;

async function loadBroadcastDashboard() {
  await fetchBroadcastStatus();
  updateBroadcastPreview();
  if (allUsersData && allUsersData.length) {
    document.getElementById('stat-bc-total').textContent = allUsersData.length.toLocaleString();
  }
}

async function fetchBroadcastStatus() {
  try {
    const r = await fetch('/api/broadcast/status');
    const d = await r.json();

    document.getElementById('stat-bc-total').textContent = d.total ? d.total.toLocaleString() : (allUsersData ? allUsersData.length : 0);
    document.getElementById('stat-bc-status').textContent = (d.status || 'READY').toUpperCase();
    document.getElementById('stat-bc-speed').textContent = `${d.speed_per_sec} msg/s`;
    document.getElementById('stat-bc-time').textContent = `${d.elapsed_seconds}s`;

    const liveCard = document.getElementById('bc-live-card');
    if (d.is_running || (d.status !== 'idle' && d.total > 0)) {
      liveCard.style.display = 'block';
      document.getElementById('bc-progress-bar').style.width = `${d.progress_percent}%`;
      document.getElementById('bc-metric-sent').textContent = d.sent.toLocaleString();
      document.getElementById('bc-metric-blocked').textContent = d.blocked.toLocaleString();
      document.getElementById('bc-metric-failed').textContent = d.failed.toLocaleString();
      document.getElementById('bc-metric-speed').textContent = `${d.speed_per_sec} msg/s`;
      document.getElementById('bc-metric-eta').textContent = `${d.eta_seconds}s`;

      const badge = document.getElementById('bc-live-badge');
      const cancelBtn = document.getElementById('bc-cancel-btn');
      if (d.is_running) {
        badge.className = 'badge monitoring';
        badge.textContent = `RUNNING • ${d.progress_percent}%`;
        cancelBtn.style.display = 'inline-block';
      } else if (d.status === 'completed') {
        badge.className = 'badge available';
        badge.textContent = 'COMPLETED ✅';
        cancelBtn.style.display = 'none';
      } else if (d.status === 'cancelled') {
        badge.className = 'badge sold';
        badge.textContent = 'CANCELLED 🛑';
        cancelBtn.style.display = 'none';
      } else {
        badge.className = 'badge sold';
        badge.textContent = (d.status || 'FINISHED').toUpperCase();
        cancelBtn.style.display = 'none';
      }
    } else {
      liveCard.style.display = 'none';
    }

    // Render history
    renderBroadcastHistory(d.history || []);

    // Manage live polling
    if (d.is_running) {
      if (!bcPollInterval) {
        bcPollInterval = setInterval(fetchBroadcastStatus, 1500);
      }
    } else {
      if (bcPollInterval) {
        clearInterval(bcPollInterval);
        bcPollInterval = null;
      }
    }
  } catch (e) {}
}

function renderBroadcastHistory(history) {
  const tbody = document.getElementById('bc-history-tbody');
  if (!history || !history.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:30px;color:#64748b">No broadcast history logged yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = history.slice().reverse().map(h => {
    const statusBadge = h.status === 'completed'
      ? '<span class="badge available"><span class="dot"></span>Completed</span>'
      : (h.status === 'cancelled'
        ? '<span class="badge sold"><span class="dot"></span>Cancelled</span>'
        : `<span class="badge sold">${h.status}</span>`);

    return `<tr>
      <td style="font-size:12px;color:var(--muted)">${h.timestamp}</td>
      <td><strong>${h.total.toLocaleString()}</strong></td>
      <td style="color:#10b981;font-weight:700;">${h.sent.toLocaleString()}</td>
      <td style="color:#ef4444;">${h.blocked.toLocaleString()}</td>
      <td style="color:#f59e0b;">${h.failed.toLocaleString()}</td>
      <td style="color:#60a5fa;font-weight:700;">${h.speed} msg/s</td>
      <td>${h.elapsed_seconds}s</td>
      <td>${statusBadge}</td>
    </tr>`;
  }).join('');
}

function updateBroadcastPreview() {
  const text = document.getElementById('bc-text-input').value.trim();
  const photo = document.getElementById('bc-photo-input').value.trim();
  const btnText = document.getElementById('bc-btn-text-input').value.trim();
  const btnUrl = document.getElementById('bc-btn-url-input').value.trim();

  const previewText = document.getElementById('bc-preview-text');
  const previewPhotoWrap = document.getElementById('bc-preview-photo-wrap');
  const previewImg = document.getElementById('bc-preview-img');
  const previewBtnWrap = document.getElementById('bc-preview-btn-wrap');
  const previewBtnLink = document.getElementById('bc-preview-btn-link');

  if (text) {
    previewText.innerHTML = text.split(String.fromCharCode(10)).join('<br>');
  } else {
    previewText.innerHTML = '<span style="color:#6b7280">Preview will appear here in real-time as you type...</span>';
  }

  if (photo) {
    previewImg.src = photo;
    previewPhotoWrap.style.display = 'block';
  } else {
    previewPhotoWrap.style.display = 'none';
  }

  if (btnText && btnUrl) {
    previewBtnLink.textContent = btnText;
    previewBtnLink.href = btnUrl;
    previewBtnWrap.style.display = 'block';
  } else {
    previewBtnWrap.style.display = 'none';
  }

  const now = new Date();
  document.getElementById('bc-preview-time').textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

async function startBroadcast() {
  const text = document.getElementById('bc-text-input').value.trim();
  const photo = document.getElementById('bc-photo-input').value.trim();
  const btnText = document.getElementById('bc-btn-text-input').value.trim();
  const btnUrl = document.getElementById('bc-btn-url-input').value.trim();
  const concurrency = parseInt(document.getElementById('bc-concurrency-select').value) || 50;
  const pin = document.getElementById('bc-pin-checkbox').checked;

  if (!text) {
    toast('Please enter a message to broadcast', 'error');
    return;
  }

  if ((btnText && !btnUrl) || (!btnText && btnUrl)) {
    toast('Both Button Text and Button URL are required if using inline buttons', 'error');
    return;
  }

  const btn = document.getElementById('bc-launch-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Launching Turbo Workers...';

  try {
    const fd = new FormData();
    fd.append('text', text);
    if (photo) fd.append('photo_url', photo);
    if (btnText) fd.append('button_text', btnText);
    if (btnUrl) fd.append('button_url', btnUrl);
    fd.append('concurrency', concurrency);
    fd.append('pin', pin ? 'true' : 'false');

    const r = await fetch('/api/broadcast/start', {
      method: 'POST',
      body: fd
    });
    const d = await r.json();
    if (d.ok) {
      toast(d.message, 'success');
      fetchBroadcastStatus();
    } else {
      toast(d.message, 'error');
    }
  } catch (e) {
    toast('Error starting broadcast: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '🚀 Launch Turbo Broadcast Now';
  }
}

async function cancelBroadcast() {
  try {
    const r = await fetch('/api/broadcast/cancel', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      toast(d.message, 'success');
      fetchBroadcastStatus();
    } else {
      toast(d.message, 'error');
    }
  } catch (e) {
    toast('Error cancelling broadcast: ' + e.message, 'error');
  }
}

function initDashboard() {
  loadAccounts();
  loadBankSessions();
  loadFamPaySession();
  loadGmailStatus();
  setInterval(loadAccounts, 15000);
  setInterval(loadSellerStatsOnly, 10000);
  setInterval(loadGatewayStatsOnly, 10000);
  setInterval(loadBankSessions, 10000);
  setInterval(loadFamPaySession, 10000);
  setInterval(loadGmailStatus, 10000);
}

checkAuth();
</script>
</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/api/stats")
async def api_stats():
    return JSONResponse(get_stats())


@app.get("/api/countries")
async def api_countries():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT country, COUNT(*) as stock 
        FROM tg_accounts 
        WHERE is_sold = 0 
        GROUP BY country
    """)
    stock_map = {row["country"].lower(): row["stock"] for row in cursor.fetchall()}
    conn.close()

    res = []
    for c in WORLD_COUNTRIES:
        c_name = c["country"]
        stock = stock_map.get(c_name.lower(), 0)
        base_inr = round(c.get("usd_price", 0.25) * 100)
        res.append({
            "country": c_name,
            "flag": c["flag"],
            "base_price": base_inr,
            "stock": stock
        })
    res.sort(key=lambda x: (-x["stock"], x["country"]))
    return JSONResponse(res)


@app.get("/api/accounts")
async def api_accounts():
    return JSONResponse(get_all_accounts())


@app.get("/api/accounts/otp")
async def api_account_otp(phone: str):
    result = await fetch_account_login_otp(phone)
    return JSONResponse(result)


# ── SELLER PAYOUTS ROUTES ────────────────────────────────────────────────────

@app.get("/api/sellers/stats")
async def api_seller_stats():
    return JSONResponse(get_seller_stats())


@app.get("/api/sellers/payouts")
async def api_seller_payouts():
    return JSONResponse(get_seller_payouts())


@app.post("/api/sellers/payouts/{payout_id}/status")
async def api_seller_payout_status(payout_id: int, status: str = Form(...)):
    result = await update_seller_payout_status(payout_id, status)
    return JSONResponse(result)


@app.delete("/api/sellers/payouts/{payout_id}")
async def api_delete_seller_payout(payout_id: int):
    result = delete_seller_payout(payout_id)
    return JSONResponse(result)


# ── FAMPAY CLOUD GATEWAY ROUTES ──────────────────────────────────────────────

@app.get("/api/fampay/session")
async def api_fampay_session():
    return JSONResponse(get_active_fampay_session() or {})


@app.post("/api/fampay/login/start")
async def api_fampay_login_start(phone: str = Form(...)):
    res = await start_fampay_login(phone)
    return JSONResponse(res)


@app.post("/api/fampay/login/verify")
async def api_fampay_login_verify(
    phone: str = Form(...),
    otp: str = Form(...),
    session_id: str = Form(default="")
):
    res = await verify_fampay_otp(phone=phone, otp=otp, session_id=session_id)
    return JSONResponse(res)


@app.post("/api/fampay/login/token")
async def api_fampay_login_token(
    auth_token: str = Form(...),
    phone: str = Form(default=""),
    upi_id: str = Form(default="")
):
    res = await connect_fampay_by_token(auth_token=auth_token, phone=phone, upi_id=upi_id)
    return JSONResponse(res)


@app.post("/api/fampay/sync")
async def api_fampay_sync():
    res = await sync_fampay_transactions()
    return JSONResponse(res)


@app.post("/api/fampay/logout")
async def api_fampay_logout():
    res = delete_fampay_session()
    return JSONResponse(res)


# ── GMAIL IMAP SCANNER ROUTES ────────────────────────────────────────────────

@app.get("/api/gmail/status")
async def api_gmail_status():
    return JSONResponse(get_gmail_status())


@app.post("/api/gmail/sync")
async def api_gmail_sync():
    new_credits = await scan_and_auto_credit_gmail()
    return JSONResponse({"ok": True, "new_credits": new_credits, "message": f"Gmail scanned! {new_credits} new payment credits processed."})


# ── DIRECT NETBANKING LOGIN ROUTES ───────────────────────────────────────────

@app.get("/api/netbanking/sessions")
async def api_netbanking_sessions():
    return JSONResponse(get_active_bank_sessions())


@app.post("/api/netbanking/login/start")
async def api_netbanking_login_start(
    bank_name: str = Form(...),
    customer_id: str = Form(...),
    password: str = Form(...),
    mobile: str = Form(default=""),
    account_number: str = Form(default="")
):
    res = await initiate_bank_login(
        bank_name=bank_name,
        customer_id=customer_id,
        password=password,
        mobile_number=mobile,
        account_number=account_number
    )
    return JSONResponse(res)


@app.post("/api/netbanking/login/verify")
async def api_netbanking_login_verify(
    session_token: str = Form(...),
    otp: str = Form(...)
):
    res = await verify_bank_login_otp(session_token=session_token, otp_code=otp)
    return JSONResponse(res)


@app.post("/api/netbanking/sync")
async def api_netbanking_sync():
    res = await poll_connected_bank_statements()
    return JSONResponse(res)


@app.delete("/api/netbanking/sessions/{session_id}")
async def api_netbanking_disconnect(session_id: int):
    res = delete_bank_session(session_id)
    return JSONResponse(res)


# ── CLOUD GATEWAY & INCOMING WEBHOOK ROUTES ──────────────────────────────────

@app.get("/api/gateway/stats")
async def api_gateway_stats():
    return JSONResponse(get_gateway_stats())


@app.get("/api/gateway/feed")
async def api_gateway_feed():
    return JSONResponse(get_all_bank_transactions())


@app.post("/api/upi/notify")
@app.post("/api/payment/webhook")
async def api_upi_notify(
    request: Request,
    utr: str = Form(default=""),
    amount: float = Form(default=0.0),
    sender: str = Form(default=""),
    source: str = Form(default="UPI"),
    raw_text: str = Form(default="")
):
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
            utr = body.get("utr") or body.get("ref_no") or body.get("rrn") or utr
            amount = float(body.get("amount") or amount)
            sender = body.get("sender") or body.get("name") or sender
            source = body.get("source") or body.get("package") or source
            raw_text = body.get("raw_text") or body.get("message") or raw_text
    except Exception:
        pass

    if raw_text:
        # 1. Amount Extraction (₹100, Rs. 100, INR 100, credited by 100, received 100)
        if not amount or amount <= 0:
            amt_m = re.search(r'(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)', raw_text, re.IGNORECASE)
            if not amt_m:
                amt_m = re.search(r'credited\s+(?:by|with|for)?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)', raw_text, re.IGNORECASE)
            if not amt_m:
                amt_m = re.search(r'paid\s+you\s+(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)', raw_text, re.IGNORECASE)
            if not amt_m:
                amt_m = re.search(r'Received\s+(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)', raw_text, re.IGNORECASE)
            if amt_m:
                try:
                    amount = float(amt_m.group(1).replace(",", ""))
                except Exception:
                    pass

        # 2. 12-Digit UTR / Ref No Extraction
        if not utr:
            utr_m = re.search(r'UPI\s*/\s*([0-9]{10,14})', raw_text, re.IGNORECASE)
            if not utr_m:
                utr_m = re.search(r'(?:UTR|Ref(?:\s*No)?|RRN|Txn(?:\s*ID)?|UPI\s*Ref(?:\s*No)?)\s*[:#-]?\s*([0-9]{10,14})', raw_text, re.IGNORECASE)
            if not utr_m:
                utr_m = re.search(r'\b([0-9]{12})\b', raw_text)
            if utr_m:
                utr = utr_m.group(1)

        # 3. Sender Name Extraction
        if not sender or sender == "UPI Payer":
            sender_m = re.search(r'from\s+([A-Za-z\s]{3,25})\b', raw_text, re.IGNORECASE)
            if not sender_m:
                sender_m = re.search(r'by\s+UPI\s+user\s+([A-Za-z\s]{3,25})\b', raw_text, re.IGNORECASE)
            if not sender_m:
                sender_m = re.search(r'UPI/[0-9]+/([A-Za-z\s]{3,25})\b', raw_text, re.IGNORECASE)
            if sender_m:
                sender = sender_m.group(1).strip()

    utr = utr.strip().replace(" ", "").upper()
    if not utr or amount <= 0:
        return JSONResponse({"ok": False, "message": "Invalid transaction data. Valid UTR and Amount required."}, status_code=400)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM deposits 
        WHERE (utr = ? OR (utr = '' AND amount = ?)) AND status = 'pending'
        ORDER BY created_at DESC LIMIT 1
    """, (utr, amount))
    pending_dep = cursor.fetchone()
    conn.close()

    if pending_dep:
        target_uid = pending_dep["user_id"]
        record_incoming_bank_tx(utr=utr, amount=amount, sender_name=sender, source_app=source, redeemed_by=target_uid)
        res = verify_and_redeem_utr(user_id=target_uid, utr=utr, expected_amount=amount)

        try:
            msg = (
                f"🎉 <b>Payment Received & Verified Instantly!</b>\n"
                f"{'━' * 28}\n\n"
                f"💵 <b>Amount Credited:</b> {CURRENCY_SYMBOL}{amount:.2f}\n"
                f"💼 <b>New Wallet Balance:</b> {CURRENCY_SYMBOL}{res.get('new_balance', 0):.2f}\n"
                f"🆔 <b>UTR:</b> <code>{utr}</code>\n\n"
                f"✨ <i>Your wallet balance has been updated automatically. Happy shopping with {STORE_NAME}!</i>"
            )
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"chat_id": target_uid, "text": msg, "parse_mode": "HTML"})
        except Exception as err:
            logger.error(f"Error notifying user on Telegram: {err}")

        return JSONResponse({
            "ok": True,
            "matched": True,
            "redeemed_by": target_uid,
            "amount": amount,
            "utr": utr,
            "message": f"✅ Matched with pending deposit for User {target_uid} & credited ₹{amount}!"
        })
    else:
        rec = record_incoming_bank_tx(utr=utr, amount=amount, sender_name=sender, source_app=source)
        return JSONResponse({
            "ok": True,
            "matched": False,
            "amount": amount,
            "utr": utr,
            "message": f"📥 Recorded bank transaction {utr} for ₹{amount} into verified pool."
        })


@app.post("/api/gateway/manual-redeem")
async def api_gateway_manual_redeem(utr: str = Form(...), user_id: int = Form(...), amount: float = Form(...)):
    res = verify_and_redeem_utr(user_id=user_id, utr=utr, expected_amount=amount)
    if res.get("status") == "approved":
        try:
            msg = (
                f"🎉 <b>Deposit Approved & Credited!</b>\n"
                f"{'━' * 28}\n\n"
                f"💵 <b>Amount Credited:</b> {CURRENCY_SYMBOL}{amount:.2f}\n"
                f"💼 <b>New Balance:</b> {CURRENCY_SYMBOL}{res.get('new_balance', 0):.2f}\n"
                f"🆔 <b>UTR:</b> <code>{utr}</code>"
            )
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"chat_id": user_id, "text": msg, "parse_mode": "HTML"})
        except Exception:
            pass
        return JSONResponse({"ok": True, "message": f"Successfully credited ₹{amount} to User {user_id}!"})
    else:
        return JSONResponse({"ok": False, "message": res.get("message", "Could not redeem UTR.")})


# ── USER & AUTH ROUTES ───────────────────────────────────────────────────────

@app.get("/api/users")
async def api_users():
    return JSONResponse(get_all_users())


@app.post("/api/users/balance")
async def api_user_balance(
    user_id: int = Form(...),
    action: str = Form(...),
    amount: float = Form(...)
):
    result = await update_user_balance(user_id=user_id, action=action, amount=amount)
    return JSONResponse(result)


@app.post("/api/login/start")
async def api_login_start(phone: str = Form(...)):
    result = await start_login(phone)
    return JSONResponse(result)


@app.post("/api/login/verify")
async def api_login_verify(
    phone: str = Form(...),
    otp: str = Form(...),
    two_fa: str = Form(default=""),
    country: str = Form(default="India"),
    flag: str = Form(default="🇮🇳"),
    acc_type: str = Form(default="fresh"),
    age_months: int = Form(default=0),
    price: float = Form(default=22.0),
):
    result = await complete_login(
        phone=phone, otp=otp, two_fa_password=two_fa,
        country=country, flag_emoji=flag, account_type=acc_type,
        age_months=age_months, price=price,
    )
    return JSONResponse(result)


@app.delete("/api/accounts/{account_id}")
async def api_delete_account(account_id: int):
    result = delete_account(account_id)
    return JSONResponse(result)


@app.post("/api/accounts/edit-details")
async def api_edit_account_details(
    account_id: int = Form(...),
    age_months: int = Form(default=0),
    price: float = Form(...),
    country: str = Form(default="")
):
    result = edit_account_details(
        account_id=account_id,
        age_months=age_months,
        price=price,
        country=country if country.strip() else None
    )
    return JSONResponse(result)


@app.post("/api/accounts/update-price")
async def api_update_accounts_price(
    scope: str = Form(default="single"),
    account_id: str = Form(default=""),
    country: str = Form(default="all"),
    age_tier: str = Form(default="all"),
    price: float = Form(...)
):
    acc_id_val = None
    if account_id and str(account_id).strip().isdigit():
        acc_id_val = int(str(account_id).strip())

    result = update_category_price(
        scope=scope,
        account_id=acc_id_val,
        country=country,
        age_tier=age_tier,
        new_price=price
    )
    return JSONResponse(result)


@app.post("/api/accounts/{account_id}/price")
async def api_update_account_price(account_id: int, price: float = Form(...)):
    result = update_account_price(account_id, price)
    return JSONResponse(result)


@app.post("/api/countries/price")
async def api_update_country_price(country: str = Form(...), price: float = Form(...)):
    result = update_category_price(scope="country", country=country, new_price=price)
    return JSONResponse(result)


# ── CHANNEL & FORCE JOIN SETTINGS API ──────────────────────────────────────

@app.get("/api/channel-settings")
async def api_get_channel_settings():
    from database import get_setting, get_join_requests_count
    from config import CHANNEL_USERNAME
    enabled = get_setting("force_sub_enabled", "true").lower() in ("true", "1", "yes", "on")
    channel_id = get_setting("force_channel_id", CHANNEL_USERNAME)
    channel_link = get_setting("force_channel_link", "")
    return JSONResponse({
        "enabled": enabled,
        "channel_id": channel_id,
        "channel_link": channel_link,
        "join_requests_count": get_join_requests_count()
    })


@app.post("/api/save-channel-settings")
async def api_save_channel_settings(
    channel_id: str = Form(...),
    channel_link: str = Form(default=""),
    enabled: bool = Form(default=True)
):
    from database import set_setting
    set_setting("force_channel_id", channel_id.strip())
    set_setting("force_channel_link", channel_link.strip())
    set_setting("force_sub_enabled", "true" if enabled else "false")
    return JSONResponse({"ok": True, "message": "Channel & Join Request settings saved successfully!"})


# ── POWER BROADCASTER API ───────────────────────────────────────────────

@app.get("/api/broadcast/status")
async def api_broadcast_status():
    return JSONResponse(get_broadcast_status())


@app.post("/api/broadcast/start")
async def api_broadcast_start(
    text: str = Form(...),
    photo_url: str = Form(default=""),
    button_text: str = Form(default=""),
    button_url: str = Form(default=""),
    pin: str = Form(default="false"),
    concurrency: int = Form(default=50)
):
    from telegram import Bot
    bot_instance = Bot(token=BOT_TOKEN)
    is_pin = pin.lower() in ("true", "1", "yes", "on")
    res = start_broadcast_job(
        bot=bot_instance,
        text=text,
        photo_url=photo_url.strip() if photo_url and photo_url.strip() else None,
        button_text=button_text.strip() if button_text and button_text.strip() else None,
        button_url=button_url.strip() if button_url and button_url.strip() else None,
        pin=is_pin,
        concurrency=concurrency
    )
    return JSONResponse(res)


@app.post("/api/broadcast/cancel")
async def api_broadcast_cancel():
    res = cancel_current_broadcast()
    return JSONResponse(res)


# ── AUTHENTICATION API ───────────────────────────────────────────────────

@app.post("/api/auth/login")
async def api_auth_login(password: str = Form(...)):
    from config import ADMIN_PANEL_PASSWORD, ADMIN_AUTH_SECRET
    if password == ADMIN_PANEL_PASSWORD:
        return JSONResponse({"ok": True, "token": ADMIN_AUTH_SECRET})
    return JSONResponse({"ok": False, "message": "Incorrect admin password!"})


@app.get("/api/auth/verify")
async def api_auth_verify(token: str = Query("")):
    from config import ADMIN_AUTH_SECRET
    if token == ADMIN_AUTH_SECRET:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False})


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "═"*55)
    print("  🤖 TG Account Manager Panel & Direct NetBanking Gateway")
    print("═"*55)
    print("  🌐 Dashboard: http://localhost:8001")
    print("  📱 Login accounts via browser — no terminal needed!")
    print("  💸 User Sold Accounts & Payouts Dashboard!")
    print("  🏦 Direct NetBanking Login & 24/7 Statement Polling!")
    print("═"*55 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
