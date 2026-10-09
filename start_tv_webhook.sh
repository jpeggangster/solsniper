#!/bin/bash
# Starts the TradingView -> Solana auto-buy webhook bot + an ngrok tunnel
# so TradingView's cloud alert servers can reach your local machine.
#
# Requires: `ngrok config add-authtoken <token>` run once beforehand.
#
# Usage: ./start_tv_webhook.sh

cd "$(dirname "$0")"

echo "Starting tv_webhook_bot.py on port 7071..."
nohup /opt/homebrew/bin/python3.14 tv_webhook_bot.py >> tv_webhook.log 2>&1 &
echo "  pid: $!"

sleep 2

echo "Starting ngrok tunnel to port 7071..."
nohup ngrok http 7071 --log=stdout > ngrok.log 2>&1 &
echo "  pid: $!"

sleep 3

echo ""
echo "Fetching public ngrok URL..."
PUBLIC_URL=$(curl -s http://127.0.0.1:4040/api/tunnels | /opt/homebrew/bin/python3.14 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])" 2>/dev/null)

if [ -n "$PUBLIC_URL" ]; then
  echo "Webhook URL for TradingView alerts: ${PUBLIC_URL}/tv-webhook"
else
  echo "Could not auto-fetch ngrok URL — check ngrok.log or http://127.0.0.1:4040"
fi
