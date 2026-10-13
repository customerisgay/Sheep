"""
main.py — Master Entrypoint for Tg store on GodHosting / Pterodactyl / VPS
===========================================================================
Automatically starts both the Web Admin Panel (FastAPI/Uvicorn), the
Telegram Bot polling, and sets up a free live Cloudflare HTTPS URL.
"""
import multiprocessing
import os
import sys
import time

def run_panel():
    """Start the Web Admin Dashboard and auto-generate live public HTTPS link."""
    try:
        import uvicorn
        from account_panel import app
        port = int(os.environ.get("SERVER_PORT", os.environ.get("PORT", "8001")))
        print(f"🌐 [PANEL] Starting Tg store Web Dashboard on 0.0.0.0:{port}...")

        # Background thread to auto-generate Cloudflare tunnel for remote access
        def start_tunnel():
            time.sleep(2)
            try:
                from pycloudflared import try_cloudflare
                from database import set_setting
                print("☁️ [TUNNEL] Generating Free Public Cloudflare URL for Admin Panel...")
                tunnel = try_cloudflare(port=port)
                if tunnel and getattr(tunnel, "tunnel", None):
                    print(f"\n{'━'*55}\n✅ [LIVE ADMIN URL]: {tunnel.tunnel}\n{'━'*55}\n")
                    set_setting("panel_url", tunnel.tunnel)
            except Exception as te:
                print(f"ℹ️ [TUNNEL INFO]: {te}")

        import threading
        t = threading.Thread(target=start_tunnel, daemon=True)
        t.start()

        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except Exception as e:
        print(f"❌ [PANEL ERROR] {e}")

def run_bot():
    """Start the Telegram Bot polling."""
    try:
        print("🤖 [BOT] Starting Tg store Telegram Bot polling...")
        from bot import main as bot_main
        bot_main()
    except Exception as e:
        print(f"❌ [BOT ERROR] {e}")

if __name__ == "__main__":
    print("\n" + "═" * 55)
    print("  🚀 Tg store System Starting (Bot + Admin Panel)...")
    print("═" * 55 + "\n")

    p1 = multiprocessing.Process(target=run_panel, name="AdminPanelProcess")
    p2 = multiprocessing.Process(target=run_bot, name="TelegramBotProcess")

    p1.start()
    time.sleep(1.5)
    p2.start()

    try:
        p1.join()
        p2.join()
    except KeyboardInterrupt:
        print("\nStopping all services...")
        p1.terminate()
        p2.terminate()
        sys.exit(0)
