#!/bin/bash
echo "══════════════════════════════════════════════════════════════"
echo "  🚀 Starting Telegram Store & Web Admin Panel Deployment"
echo "══════════════════════════════════════════════════════════════"

# Create python virtual environment if not exists
if [ ! -d "venv" ]; then
    echo "📦 Creating Python Virtual Environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install / Upgrade dependencies
echo "📥 Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Start main application
echo "⚡ Launching Bot & FastAPI Web Admin Panel..."
python3 main.py
