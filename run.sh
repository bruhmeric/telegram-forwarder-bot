#!/usr/bin/env bash
# Convenience launcher. Make sure .env is filled in and you've run
# `python login.py` once before starting the bot for the first time.
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "ERROR: .env not found. Copy .env.example to .env and fill it in."
    exit 1
fi

if [ ! -f user_session.session ] && grep -q "API_ID=1234567" .env 2>/dev/null; then
    echo "Reminder: API_ID is still the placeholder. Have you run 'python login.py'?"
fi

exec python bot.py
