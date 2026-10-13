"""
generate_session.py — Admin helper to generate Pyrogram StringSession
=======================================================================
Run this ONCE per phone number to generate a session string.
The session string is then stored in tg_accounts table via /addaccount command.

Usage:
    cd tgapi
    ./venv/bin/python generate_session.py
"""

import asyncio
from pyrogram import Client

# ─── Default API credentials (Telegram official app) ────────────────────────
# You can replace with your own from https://my.telegram.org/apps
DEFAULT_API_ID   = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

async def generate():
    print("\n" + "═" * 55)
    print("   TGAPI — Pyrogram Session String Generator")
    print("═" * 55)
    print("\nThis tool generates a session string for a Telegram account.")
    print("You will need the phone number and OTP code from Telegram.\n")

    api_id_input = input(f"API ID (press Enter for default {DEFAULT_API_ID}): ").strip()
    api_id = int(api_id_input) if api_id_input else DEFAULT_API_ID

    api_hash_input = input(f"API Hash (press Enter for default): ").strip()
    api_hash = api_hash_input if api_hash_input else DEFAULT_API_HASH

    phone = input("Phone number (with country code, e.g. +919876543210): ").strip()

    async with Client(
        name="session_gen_temp",
        api_id=api_id,
        api_hash=api_hash,
        phone_number=phone,
        in_memory=True
    ) as app:
        session_string = await app.export_session_string()

    print("\n" + "═" * 55)
    print("✅ SESSION STRING GENERATED SUCCESSFULLY!")
    print("═" * 55)
    print(f"\n📱 Phone: {phone}")
    print(f"\n🔑 Session String (copy everything below):\n")
    print(session_string)
    print("\n" + "═" * 55)
    print("📋 Now add this account to the store bot:")
    price_str = input("\nSet price for this account (e.g. 99): ").strip()
    price = float(price_str) if price_str else 99.0
    country = input("Country name (e.g. India): ").strip() or "India"
    flag = input("Country flag emoji (e.g. 🇮🇳): ").strip() or "🇮🇳"
    acc_type = input("Account type — fresh or aged (default: fresh): ").strip() or "fresh"
    age_months = input("Account age in months (0 for fresh): ").strip() or "0"

    print("\n🤖 Run this command in your ADMIN bot chat:\n")
    print(f"/addaccount {phone} {price} {country} {flag} {acc_type} {age_months} {api_id} {api_hash}")
    print("\nThen separately, paste session string when prompted by the bot.")
    print("\n─── OR ── Add directly to DB ─────────────────────────────────────")
    print("python3 -c \"")
    print("import sqlite3, sys")
    print("conn = sqlite3.connect('tgapi_store.db')")
    print(f"conn.execute(\\\"INSERT OR REPLACE INTO tg_accounts (phone, session_string, api_id, api_hash, price, country, flag_emoji, account_type, age_months) VALUES ('{phone}', '...<paste session here>...', {api_id}, '{api_hash}', {price}, '{country}', '{flag}', '{acc_type}', {age_months})\\\")")
    print("conn.commit(); conn.close(); print('Done!')")
    print("\"")
    print("═" * 55)


if __name__ == "__main__":
    asyncio.run(generate())
