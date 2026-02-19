"""
Web wrapper for cloud deploy (Render free tier).
Runs the trading bot in a background thread and exposes:
- / → Dashboard HTML (terminal hacker UI)
- /api/status → JSON with bot state for dashboard polling
- /health → Plain text health check
Also pings itself every 10 min to stay awake.
"""

import os
import sys
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Force unbuffered output for cloud logs
os.environ["PYTHONUNBUFFERED"] = "1"

BOT_STATUS = {"running": False, "started_at": None, "errors": 0}
BOT_INSTANCE = None  # Reference to CypherGrokTradeBot instance
SCAN_LOG = []        # Recent scan log lines (max 100)
SCAN_COUNT = 0

# Capture print output for scan log
_original_print = print
def _capturing_print(*args, **kwargs):
    global SCAN_COUNT
    msg = " ".join(str(a) for a in args)
    _original_print(*args, **kwargs)
    # Capture bot scan/trade lines
    if any(tag in msg for tag in ["[SCAN]", "[ENTRY]", "[WIN]", "[LOSS]", "[MM]", "[ARB-LP]",
                                   "[HOLD]", "[COOLDOWN]", "[GROK]", "[Cycle", "[COPY]"]):
        # Strip ANSI codes
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', msg).strip()
        if clean:
            SCAN_LOG.append(clean)
            while len(SCAN_LOG) > 100:
                SCAN_LOG.pop(0)
            if "[Cycle" in msg:
                SCAN_COUNT += 1

import builtins
builtins.print = _capturing_print


# Read dashboard HTML once
DASHBOARD_HTML = ""
dashboard_paths = [
    os.path.join(os.path.dirname(__file__), "dashboard.html"),
    "/app/dashboard.html",
]
for p in dashboard_paths:
    if os.path.exists(p):
        with open(p, "r") as f:
            DASHBOARD_HTML = f.read()
        break


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            self._serve_dashboard()
        elif self.path == "/api/status":
            self._serve_api()
        elif self.path == "/health":
            self._serve_health()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_dashboard(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(DASHBOARD_HTML.encode())

    def _serve_api(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        data = {
            "running": BOT_STATUS["running"],
            "uptime": int(time.time() - BOT_STATUS["started_at"]) if BOT_STATUS["started_at"] else 0,
            "errors": BOT_STATUS["errors"],
            "scan_count": SCAN_COUNT,
            "recent_logs": SCAN_LOG[-30:],
        }

        bot = BOT_INSTANCE
        if bot:
            try:
                # Balance & PnL
                balance = bot.executor.get_balance()
                data["balance"] = balance
                data["pnl"] = balance - bot.start_balance if bot.start_balance > 0 else 0

                # Win rate
                total = bot.wins + bot.losses
                data["win_rate"] = round(bot.wins / total * 100, 1) if total > 0 else 0
                data["wins"] = bot.wins
                data["losses"] = bot.losses
                data["trades_taken"] = bot.trades_taken

                # Open positions
                positions = bot.executor.get_open_positions()
                data["open_positions"] = len(positions)
                data["positions"] = []
                for p in positions:
                    data["positions"].append({
                        "coin": p.get("coin", "?"),
                        "side": "LONG" if p.get("size", 0) > 0 else "SHORT",
                        "size": abs(p.get("size", 0)),
                        "entry_price": p.get("entry_price", 0),
                        "pnl": p.get("unrealized_pnl", 0),
                        "leverage": p.get("leverage", 0),
                    })

                # Config summary
                import config
                data["config"] = {
                    "leverage": config.LEVERAGE,
                    "pairs_count": len(config.TRADING_PAIRS) or config.TOP_COINS_COUNT,
                    "min_confidence": config.MIN_CONFIDENCE,
                    "scan_interval": config.SCAN_INTERVAL,
                }

                # Arbitrum LP (master)
                if bot.arb_lp:
                    lp = bot.arb_lp
                    pos = lp.active_position  # singular dict or None
                    data["lp"] = {
                        "active": bool(pos),
                        "pool": pos.get("pool", None) if pos else None,
                        "token_id": pos.get("token_id", None) if pos else None,
                        "fees_collected": getattr(lp, "total_fees_collected", 0),
                    }
                else:
                    data["lp"] = {"active": False, "pool": None, "token_id": None, "fees_collected": 0}

                # Copy Trading
                cm = bot.copy_manager
                data["copy"] = {
                    "followers": [],
                    "total_fees_collected": 0,
                }
                if cm:
                    data["copy"]["total_fees_collected"] = cm.fee_tracker.fee_log.get("total_fees_collected", 0)
                    for f in cm.followers:
                        wallet = f.get("wallet_address", "")
                        fname = f.get("name", "?")
                        # Follower HL balance
                        hl_bal = 0
                        try:
                            us = cm.info.user_state(wallet)
                            hl_bal = float(us.get("marginSummary", {}).get("accountValue", 0))
                        except:
                            pass
                        # Follower LP status
                        flp = cm._follower_lp_managers.get(wallet)
                        flp_data = None
                        if flp and flp.active_position:
                            fp = flp.active_position
                            flp_data = {
                                "pool": fp.get("pool"),
                                "token_id": fp.get("token_id"),
                                "fees": getattr(flp, "total_fees_collected", 0),
                            }
                        # Pending fees
                        pending_fees = cm.fee_tracker.get_pending_fees(wallet)
                        data["copy"]["followers"].append({
                            "name": fname,
                            "wallet": wallet[:6] + "..." + wallet[-4:] if len(wallet) > 10 else wallet,
                            "hl_balance": round(hl_bal, 2),
                            "lp": flp_data,
                            "pending_fees": round(pending_fees, 4),
                        })

            except Exception as e:
                data["api_error"] = str(e)

        self.wfile.write(json.dumps(data).encode())

    def _serve_health(self):
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
    global BOT_INSTANCE
    try:
        # Patch sys.argv so bot.py thinks it was called with 'start money'
        sys.argv = ["bot.py", "start", "money"]

        from bot import CypherGrokTradeBot

        BOT_STATUS["running"] = True
        BOT_STATUS["started_at"] = time.time()

        bot = CypherGrokTradeBot()
        BOT_INSTANCE = bot
        bot.start()
    except Exception as e:
        BOT_STATUS["errors"] += 1
        _original_print(f"[WEB-WRAPPER] Bot error: {e}")
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
        _original_print("[WEB-WRAPPER] No RENDER_EXTERNAL_URL, self-ping disabled")
        return

    _original_print(f"[WEB-WRAPPER] Self-ping enabled: {url}")
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
    _original_print(f"[WEB-WRAPPER] Dashboard + API on port {port}")
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    server.serve_forever()
