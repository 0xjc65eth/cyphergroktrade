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
                                   "[HOLD]", "[COOLDOWN]", "[GROK]", "[Cycle", "[COPY]",
                                   "[ERROR]", "[SKIP]", "[BYPASS]", "[INIT]", "Error"]):
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
        elif self.path == "/api/trades":
            self._serve_trades()
        elif self.path == "/api/signals":
            self._serve_signals()
        elif self.path == "/api/config":
            self._serve_config()
        elif self.path == "/api/pnl":
            self._serve_pnl()
        elif self.path == "/api/learning":
            self._serve_learning()
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

                # Arbitrum LP (master) - with diagnostics
                if bot.arb_lp:
                    lp = bot.arb_lp
                    pos = lp.active_position  # singular dict or None
                    lp_data = {
                        "active": bool(pos),
                        "enabled": True,
                        "pool": pos.get("pool", None) if pos else None,
                        "token_id": pos.get("token_id", None) if pos else None,
                        "fees_collected": getattr(lp, "total_fees_collected", 0),
                    }
                    # Diagnostics
                    try:
                        lp_data["address"] = lp.address[:8] + "..." + lp.address[-4:] if hasattr(lp, 'address') else "?"
                        eth_bal = lp.w3.eth.get_balance(lp.address) if hasattr(lp, 'w3') else 0
                        lp_data["eth_balance"] = round(eth_bal / 1e18, 6)
                        lp_data["rpc_connected"] = lp.w3.is_connected() if hasattr(lp, 'w3') else False
                    except Exception as e:
                        lp_data["diag_error"] = str(e)
                    data["lp"] = lp_data
                else:
                    data["lp"] = {
                        "active": False, "enabled": getattr(config, 'ARB_LP_ENABLED', False),
                        "pool": None, "token_id": None, "fees_collected": 0,
                        "reason": "ArbitrumLPManager not initialized"
                    }

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

    def _json_response(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _serve_trades(self):
        """Return trade history from trades_history.json."""
        trades = []
        try:
            trades_path = os.path.join(os.path.dirname(__file__), "trades_history.json")
            if os.path.exists(trades_path):
                with open(trades_path, "r") as f:
                    trades = json.load(f)
        except Exception as e:
            trades = [{"error": str(e)}]
        self._json_response(trades)

    def _serve_signals(self):
        """Return signal history from signals_history.json."""
        signals = []
        try:
            signals_path = os.path.join(os.path.dirname(__file__), "signals_history.json")
            if os.path.exists(signals_path):
                with open(signals_path, "r") as f:
                    signals = json.load(f)
        except Exception as e:
            signals = [{"error": str(e)}]
        self._json_response(signals)

    def _serve_config(self):
        """Return full config as JSON."""
        try:
            import config
            cfg = {}
            for key in dir(config):
                if key.isupper() and not key.startswith("_"):
                    val = getattr(config, key)
                    if isinstance(val, (str, int, float, bool, list, dict, type(None))):
                        # Hide secrets
                        if any(s in key for s in ["KEY", "TOKEN", "SECRET", "PASSWORD", "WALLET", "PRIVATE"]):
                            cfg[key] = "***HIDDEN***"
                        else:
                            cfg[key] = val
            self._json_response(cfg)
        except Exception as e:
            self._json_response({"error": str(e)})

    def _serve_pnl(self):
        """Return accumulated PnL data with daily/hourly breakdown."""
        result = {"total_pnl": 0, "daily": {}, "hourly": {}, "by_coin": {}, "cumulative": []}
        try:
            trades_path = os.path.join(os.path.dirname(__file__), "trades_history.json")
            if os.path.exists(trades_path):
                with open(trades_path, "r") as f:
                    trades = json.load(f)

                running_pnl = 0
                for t in trades:
                    pnl = t.get("pnl")
                    if pnl is None:
                        continue
                    running_pnl += pnl
                    result["cumulative"].append({
                        "ts": t.get("timestamp_close", t.get("timestamp_open")),
                        "pnl": round(pnl, 4),
                        "cumulative": round(running_pnl, 4),
                        "coin": t.get("coin"),
                        "result": t.get("result"),
                    })

                    # Daily breakdown
                    day = (t.get("timestamp_close") or t.get("timestamp_open", ""))[:10]
                    if day:
                        if day not in result["daily"]:
                            result["daily"][day] = {"pnl": 0, "wins": 0, "losses": 0, "trades": 0}
                        result["daily"][day]["pnl"] = round(result["daily"][day]["pnl"] + pnl, 4)
                        result["daily"][day]["trades"] += 1
                        if t.get("result") == "WIN":
                            result["daily"][day]["wins"] += 1
                        elif t.get("result") == "LOSS":
                            result["daily"][day]["losses"] += 1

                    # Hourly breakdown
                    hour = str(t.get("hour_open", "?"))
                    if hour not in result["hourly"]:
                        result["hourly"][hour] = {"pnl": 0, "wins": 0, "losses": 0, "trades": 0}
                    result["hourly"][hour]["pnl"] = round(result["hourly"][hour]["pnl"] + pnl, 4)
                    result["hourly"][hour]["trades"] += 1
                    if t.get("result") == "WIN":
                        result["hourly"][hour]["wins"] += 1
                    elif t.get("result") == "LOSS":
                        result["hourly"][hour]["losses"] += 1

                    # By coin
                    coin = t.get("coin", "?")
                    if coin not in result["by_coin"]:
                        result["by_coin"][coin] = {"pnl": 0, "wins": 0, "losses": 0, "trades": 0}
                    result["by_coin"][coin]["pnl"] = round(result["by_coin"][coin]["pnl"] + pnl, 4)
                    result["by_coin"][coin]["trades"] += 1
                    if t.get("result") == "WIN":
                        result["by_coin"][coin]["wins"] += 1
                    elif t.get("result") == "LOSS":
                        result["by_coin"][coin]["losses"] += 1

                result["total_pnl"] = round(running_pnl, 4)

        except Exception as e:
            result["error"] = str(e)

        self._json_response(result)

    def _serve_learning(self):
        """Return learning stats."""
        try:
            stats_path = os.path.join(os.path.dirname(__file__), "learning_stats.json")
            if os.path.exists(stats_path):
                with open(stats_path, "r") as f:
                    stats = json.load(f)
                self._json_response(stats)
            else:
                self._json_response({"message": "No learning data yet"})
        except Exception as e:
            self._json_response({"error": str(e)})

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
