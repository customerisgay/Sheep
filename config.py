import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Telegram Bot Token (from @BotFather)
BOT_TOKEN = os.getenv("BOT_TOKEN", "8244568420:AAEm_FISmcGz7u3iv1fPSdD7UZYlEgETl6E")

# Admin Telegram User IDs — @sii_3s (ID: 8207882848)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "8207882848").split(",") if x.strip()]

# Currency and Store Settings
CURRENCY_SYMBOL = "₹"
STORE_NAME = "Tg store"
SUPPORT_USERNAME = "@sii_3s"
CHANNEL_USERNAME = "@sii_3s"

# Exchange Rate: 1 USDT = X INR
USDT_RATE = float(os.getenv("USDT_RATE", "100.0"))

# CryptoBot API Token (Optional)
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "")

# UPI Settings (For deposits & QR payments)
UPI_ID   = os.getenv("UPI_ID", "pradip960@fam")
UPI_NAME = os.getenv("UPI_NAME", "Pradip")

# Referral Commission (%)
REFERRAL_PERCENT = 0.10

# Gmail IMAP Auto-Verification Settings (For FamPay & Bank Email Alerts)
GMAIL_EMAIL    = os.getenv("GMAIL_EMAIL", "chinupatraq@gmail.com")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD", "uukhhygbgodrhboh")

# Admin Panel Security & Direct Chrome Link Settings
PANEL_BASE_URL = os.getenv("PANEL_BASE_URL", "http://localhost:8001")
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "admin123")
ADMIN_AUTH_SECRET = os.getenv("ADMIN_AUTH_SECRET", "tgstore_admin_secret_99281")
