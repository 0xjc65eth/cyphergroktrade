"""
Web wrapper for cloud deploy (Render free tier).
Runs the trading bot in a background thread and exposes an HTTP health endpoint
to keep the free tier service alive (prevents sleep after 15 min inactivity).
Also pings itself every 10 min to stay awake.
"""

import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Force unbuffered output for cloud logs
os.environ["PYTHONUNBUFFERED"] = "1"

BOT_STATUS = {"running": False, "started_at": None, "errors": 0}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        uptime = ""
        if BOT_STATUS["started_at"]:
            uptime = f" | uptime: {int(time.time() - BOT_STATUS['started_at'])}s"
        msg = f"CypherGrokTrade OK | running: {BOT_STATUS['running']}{uptime}\n"
        self.wfile.write(msg.encode())

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def run_bot():
    """Run the trading bot in a thread."""
    try:
        # Patch sys.argv so bot.py thinks it was called with 'start money'
        sys.argv = ["bot.py", "start", "money"]

        from bot import CypherGrokTradeBot

        BOT_STATUS["running"] = True
        BOT_STATUS["started_at"] = time.time()

        bot = CypherGrokTradeBot()
        bot.start()
    except Exception as e:
        BOT_STATUS["errors"] += 1
        print(f"[WEB-WRAPPER] Bot error: {e}")
        # Restart after 30s
        time.sleep(30)
        run_bot()


def self_ping():
    """Ping own health endpoint every 10 min to prevent Render sleep."""
    import requests

    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        # Try to build from service name
        service = os.environ.get("RENDER_SERVICE_NAME", "")
        if service:
            url = f"https://{service}.onrender.com"

    if not url:
        print("[WEB-WRAPPER] No RENDER_EXTERNAL_URL, self-ping disabled")
        return

    print(f"[WEB-WRAPPER] Self-ping enabled: {url}")
    while True:
        time.sleep(600)  # 10 min
        try:
            requests.get(url, timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Start self-ping in background thread
    ping_thread = threading.Thread(target=self_ping, daemon=True)
    ping_thread.start()

    # Start HTTP server (foreground - this is what Render monitors)
    print(f"[WEB-WRAPPER] Health server on port {port}")
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()
