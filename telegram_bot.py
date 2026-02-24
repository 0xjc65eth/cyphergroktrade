"""
CypherGrokTrade - Telegram Signal Notifier
Sends trade signals, open positions, and status updates to Telegram.
Also handles copy trading commands via Telegram.
"""

import requests
import time
import os
import json
import threading
from datetime import datetime
import config

SIGNATURE = "\n\n`0xjc65.btc` — *CEO Cypher*"
DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━"


class TelegramNotifier:
    def __init__(self):
        self.token = getattr(config, "TELEGRAM_BOT_TOKEN", "")
        self.chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self._last_status_time = 0
        self._last_update_id = 0
        self.copy_manager = None  # Set externally after init

        if self.enabled:
            self._send(
                f"*CYPHER GROK TRADE v3*\n"
                f"{DIVIDER}\n"
                f"\n"
                f"*System Online*\n"
                f"\n"
                f"  Capital:    `${getattr(config, 'INITIAL_CAPITAL', 0):.2f}`\n"
                f"  Target:     `${getattr(config, 'TARGET_CAPITAL', 0):.2f}`\n"
                f"  Leverage:   `{getattr(config, 'LEVERAGE', 0)}x`\n"
                f"  Scan Pool:  `{getattr(config, 'TOP_COINS_COUNT', 0)} assets`\n"
                f"\n"
                f"{DIVIDER}\n"
                f"Modules: SMC | MA Scalper | MM | Arb LP\n"
                f"Status: *ARMED*"
                f"{SIGNATURE}"
            )
        else:
            print("[TELEGRAM] Disabled - set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config.py")

    def _send(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send a message to master chat."""
        return self._send_to(self.chat_id, text, parse_mode)

    def _send_to(self, chat_id: str, text: str, parse_mode: str = "Markdown") -> bool:
        """Send a message to any chat."""
        if not self.token:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            resp = requests.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            print(f"[TELEGRAM] Error: {e}")
            return False

    def signal_found(self, coin: str, direction: str, confidence: float,
                     price: float, sl_pct: float, tp_pct: float,
                     smc_details: str, ma_details: str,
                     trend_5m: str, grok_reason: str):
        """Notify when a trade signal is found and about to execute."""
        side_label = "LONG" if direction == "LONG" else "SHORT"
        side_icon = "+" if direction == "LONG" else "-"

        sl_price = price * (1 - sl_pct) if direction == "LONG" else price * (1 + sl_pct)
        tp_price = price * (1 + tp_pct) if direction == "LONG" else price * (1 - tp_pct)
        rr = tp_pct / sl_pct if sl_pct > 0 else 0

        msg = (
            f"*NEW SIGNAL — {side_label} {coin}*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Entry:       `${price:.4f}`\n"
            f"  Stop Loss:   `${sl_price:.4f}`  ({sl_pct*100:.2f}%)\n"
            f"  Take Profit: `${tp_price:.4f}`  ({tp_pct*100:.2f}%)\n"
            f"  Risk/Reward: `1:{rr:.1f}`\n"
            f"  Confidence:  `{confidence:.0%}`\n"
            f"  5m Trend:    `{trend_5m}`\n"
            f"\n"
            f"{DIVIDER}\n"
            f"*Analysis*\n"
            f"  SMC: {smc_details[:100]}\n"
            f"  MA:  {ma_details[:100]}\n"
            f"  AI:  {grok_reason[:80]}\n"
            f"\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}`"
            f"{SIGNATURE}"
        )
        self._send(msg)

    def trade_opened(self, coin: str, direction: str, size_usd: float,
                     price: float, leverage: int):
        """Notify when a trade is actually opened."""
        msg = (
            f"*TRADE OPENED — {direction} {coin}*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Price:    `${price:.4f}`\n"
            f"  Size:     `${size_usd:.2f}`\n"
            f"  Leverage: `{leverage}x`\n"
            f"\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}`"
            f"{SIGNATURE}"
        )
        self._send(msg)

    def trade_closed(self, coin: str, direction: str, pnl: float, is_win: bool):
        """Notify when a trade is closed."""
        result = "PROFIT" if is_win else "LOSS"
        msg = (
            f"*TRADE CLOSED — {result}*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Pair:   `{coin}`\n"
            f"  Side:   `{direction}`\n"
            f"  PnL:    `${pnl:+.4f}`\n"
            f"\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}`"
            f"{SIGNATURE}"
        )
        self._send(msg)

    def status_update(self, balance: float, pnl: float, wins: int, losses: int,
                      open_positions: list, withdrawn: float, idle_scans: int):
        """Send periodic status update (max every 5 min)."""
        now = time.time()
        if now - self._last_status_time < 300:
            return
        self._last_status_time = now

        wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        pnl_sign = "+" if pnl >= 0 else ""

        pos_text = ""
        if open_positions:
            for p in open_positions:
                upnl = p.get("unrealized_pnl", 0)
                sign = "+" if upnl >= 0 else ""
                pos_text += f"  {p['coin']:>8}  `${sign}{upnl:.4f}`\n"
        else:
            pos_text = "  No open positions\n"

        msg = (
            f"*STATUS REPORT*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Balance:    `${balance:.2f}`\n"
            f"  PnL:        `${pnl_sign}{pnl:.2f}`\n"
            f"  Win Rate:   `{wr:.0f}%` ({wins}W / {losses}L)\n"
            f"  Withdrawn:  `${withdrawn:.2f}`\n"
            f"  Idle Scans: `{idle_scans}`\n"
            f"\n"
            f"{DIVIDER}\n"
            f"*Open Positions*\n"
            f"{pos_text}"
            f"\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}`"
            f"{SIGNATURE}"
        )
        self._send(msg)

    def scan_summary(self, total_scanned: int, signals_found: int, next_scan_seconds: int):
        """Notify scan results when no entries found."""
        if signals_found > 0:
            return
        now = time.time()
        if now - self._last_status_time < 300:
            return

        msg = (
            f"*SCAN COMPLETE*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Assets Scanned: `{total_scanned}`\n"
            f"  Signals Found:  `{signals_found}`\n"
            f"  Next Scan:      `{next_scan_seconds}s`\n"
            f"  MM Fallback:    `Active`"
            f"{SIGNATURE}"
        )
        self._send(msg)

    def withdrawal(self, amount: float, total: float):
        """Notify profit withdrawal."""
        msg = (
            f"*PROFIT WITHDRAWAL*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Amount:          `${amount:.2f}`\n"
            f"  Total Withdrawn: `${total:.2f}`"
            f"{SIGNATURE}"
        )
        self._send(msg)

    def error(self, error_msg: str):
        """Notify errors."""
        msg = (
            f"*SYSTEM ALERT*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  {error_msg[:200]}\n"
            f"\n"
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}`"
            f"{SIGNATURE}"
        )
        self._send(msg)

    def shutdown(self, balance: float, pnl: float, wins: int, losses: int, withdrawn: float):
        """Notify shutdown."""
        wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        pnl_sign = "+" if pnl >= 0 else ""
        msg = (
            f"*SYSTEM SHUTDOWN*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Final Balance: `${balance:.2f}`\n"
            f"  Session PnL:   `${pnl_sign}{pnl:.2f}`\n"
            f"  Win Rate:      `{wr:.0f}%` ({wins}W / {losses}L)\n"
            f"  Withdrawn:     `${withdrawn:.2f}`\n"
            f"\n"
            f"Bot terminated gracefully."
            f"{SIGNATURE}"
        )
        self._send(msg)

    # ─── Copy Trading Notifications ───

    def copy_trade_executed(self, follower_name: str, coin: str, direction: str,
                            size_usd: float):
        """Notify when a copy trade is executed for a follower."""
        msg = (
            f"*COPY TRADE EXECUTED*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Follower: `{follower_name}`\n"
            f"  Pair:     `{coin}`\n"
            f"  Side:     `{direction}`\n"
            f"  Size:     `${size_usd:.2f}`"
            f"{SIGNATURE}"
        )
        self._send(msg)

    def new_follower(self, name: str, wallet: str, balance: float):
        """Notify when a new follower joins."""
        msg = (
            f"*NEW FOLLOWER JOINED*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Name:    `{name}`\n"
            f"  Wallet:  `{wallet[:10]}...`\n"
            f"  Balance: `${balance:.2f}`\n"
            f"\n"
            f"Auto-copy enabled."
            f"{SIGNATURE}"
        )
        self._send(msg)

    def follower_stats(self):
        """Send copy trading stats."""
        if not self.copy_manager:
            return

        stats = self.copy_manager.get_stats()
        followers = self.copy_manager.list_followers()

        follower_lines = ""
        for f in followers:
            status = "ON" if f["active"] else "OFF"
            pnl_sign = "+" if f["pnl_since_join"] >= 0 else ""
            follower_lines += (
                f"  [{status}] *{f['name']}*\n"
                f"       Bal: `${f['balance']:.2f}` | "
                f"PnL: `${pnl_sign}{f['pnl_since_join']:.2f}` | "
                f"Pos: `{f['positions']}` | "
                f"Mult: `{f['multiplier']}x`\n"
            )

        if not follower_lines:
            follower_lines = "  No followers yet.\n"

        msg = (
            f"*COPY TRADING REPORT*\n"
            f"{DIVIDER}\n"
            f"\n"
            f"  Active Followers: `{stats['active_followers']}/{stats['total_followers']}`\n"
            f"  Total AUM:        `${stats['total_follower_balance']:.2f}`\n"
            f"  Trades Copied:    `{stats['total_trades_copied']}`\n"
            f"\n"
            f"{DIVIDER}\n"
            f"*Followers*\n"
            f"{follower_lines}"
            f"{SIGNATURE}"
        )
        self._send(msg)

    # ─── Telegram Command Listener ───

    def start_command_listener(self):
        """Start listening for Telegram commands in background."""
        if not self.enabled:
            return

        def _listener():
            print("[TELEGRAM] Command listener started")
            while True:
                try:
                    self._poll_commands()
                except Exception as e:
                    print(f"[TELEGRAM] Listener error: {e}")
                time.sleep(3)

        t = threading.Thread(target=_listener, daemon=True)
        t.start()

    def _poll_commands(self):
        """Poll for new Telegram messages/commands."""
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates"
            params = {"offset": self._last_update_id + 1, "timeout": 5}
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                return

            data = resp.json()
            if not data.get("ok"):
                return

            for update in data.get("result", []):
                self._last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                user_name = msg.get("from", {}).get("first_name", "Unknown")

                if not text.startswith("/"):
                    continue

                # Public commands - anyone can use
                public_cmds = ["/start", "/join", "/follow", "/add_follower",
                               "/addfollower", "/my_status", "/mystatus", "/stop_copy",
                               "/stopcopy"]
                cmd_lower = text.split()[0].lower()

                if cmd_lower in public_cmds:
                    self._handle_public_command(text, chat_id, user_name)
                elif chat_id == self.chat_id:
                    self._handle_command(text)

        except Exception:
            pass

    def _handle_public_command(self, text: str, chat_id: str, user_name: str):
        """Handle commands from ANY user (public commands for followers)."""
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/start" or cmd == "/join":
            self._send_to(chat_id,
                f"*CYPHER GROK TRADE — Copy Trading*\n"
                f"{DIVIDER}\n"
                f"\n"
                f"Mirror trades automatically from our\n"
                f"SMC + AI powered strategy.\n"
                f"\n"
                f"*Getting Started:*\n"
                f"  1. Create a Hyperliquid account\n"
                f"  2. Deposit USDC\n"
                f"  3. Export your API Private Key (Settings)\n"
                f"  4. Copy your wallet address (top right)\n"
                f"  5. Send here:\n"
                f"     `/follow Name ApiKey WalletAddress`\n"
                f"\n"
                f"{DIVIDER}\n"
                f"*Commands*\n"
                f"  /follow `name` `key` `wallet` — Start copying\n"
                f"  /my\\_status — View your positions\n"
                f"  /stop\\_copy — Stop copying\n"
                f"\n"
                f"Allocation: 50% LP | 25% Scalp | 25% MM"
                f"{SIGNATURE}"
            )

        elif cmd in ("/follow", "/add_follower", "/addfollower"):
            if len(parts) < 4:
                self._send_to(chat_id,
                    f"*Usage:* `/follow Name ApiKey WalletAddress`\n"
                    f"\n"
                    f"*Steps:*\n"
                    f"  1. Go to app.hyperliquid.xyz\n"
                    f"  2. Settings > Export API Private Key\n"
                    f"  3. Copy your wallet address (top right)\n"
                    f"\n"
                    f"*Example:*\n"
                    f"  `/follow John 0xApiKey... 0xWallet...`\n"
                    f"\n"
                    f"*Optional multiplier:*\n"
                    f"  `/follow John 0xKey 0xWallet 0.5`\n"
                    f"  `/follow John 0xKey 0xWallet 2.0`"
                    f"{SIGNATURE}"
                )
                return

            name = parts[1]
            key = parts[2]
            wallet_addr = parts[3]
            mult = float(parts[4]) if len(parts) > 4 else 1.0

            # Validate wallet address format
            if not wallet_addr.startswith("0x") or len(wallet_addr) < 40:
                self._send_to(chat_id,
                    f"*Invalid wallet address*\n"
                    f"\n"
                    f"The 3rd parameter must be your Hyperliquid\n"
                    f"wallet address (starts with 0x...).\n"
                    f"\n"
                    f"Find it at the top right of app.hyperliquid.xyz"
                    f"{SIGNATURE}"
                )
                return

            if not self.copy_manager:
                self._send_to(chat_id,
                    f"*System Unavailable*\n"
                    f"Copy trading is not active at the moment."
                    f"{SIGNATURE}"
                )
                return

            result = self.copy_manager.add_follower(name, key, multiplier=mult,
                                                     main_wallet=wallet_addr)

            if result.get("success"):
                self._send_to(chat_id,
                    f"*Welcome, {name}!*\n"
                    f"{DIVIDER}\n"
                    f"\n"
                    f"  Balance:    `${result['balance']:.2f}`\n"
                    f"  Multiplier: `{mult}x`\n"
                    f"  Status:     `Active`\n"
                    f"\n"
                    f"Your trades are now being copied\n"
                    f"automatically in real-time.\n"
                    f"\n"
                    f"*Commands:*\n"
                    f"  /my\\_status — View your positions\n"
                    f"  /stop\\_copy — Stop copying"
                    f"{SIGNATURE}"
                )
                self._send(
                    f"*NEW FOLLOWER JOINED*\n"
                    f"{DIVIDER}\n"
                    f"\n"
                    f"  Name:    `{name}`\n"
                    f"  Balance: `${result['balance']:.2f}`\n"
                    f"  Mult:    `{mult}x`\n"
                    f"  Chat:    `{chat_id}`"
                    f"{SIGNATURE}"
                )
                self._save_follower_chat(result["wallet"], chat_id)
            else:
                self._send_to(chat_id,
                    f"*Error:* {result.get('error', 'Unknown')}"
                    f"{SIGNATURE}"
                )

        elif cmd in ("/my_status", "/mystatus"):
            if not self.copy_manager:
                self._send_to(chat_id,
                    f"*System Unavailable*"
                    f"{SIGNATURE}"
                )
                return

            follower_wallet = self._get_wallet_by_chat(chat_id)
            if not follower_wallet:
                self._send_to(chat_id,
                    f"*Not Registered*\n"
                    f"Use `/follow YourName YourPrivateKey` to start."
                    f"{SIGNATURE}"
                )
                return

            followers = self.copy_manager.list_followers()
            for f in followers:
                if f.get("full_wallet", "").lower() == follower_wallet.lower():
                    pnl_sign = "+" if f["pnl_since_join"] >= 0 else ""
                    self._send_to(chat_id,
                        f"*YOUR STATUS — {f['name']}*\n"
                        f"{DIVIDER}\n"
                        f"\n"
                        f"  Balance:    `${f['balance']:.2f}`\n"
                        f"  PnL:        `${pnl_sign}{f['pnl_since_join']:.2f}`\n"
                        f"  Positions:  `{f['positions']}`\n"
                        f"  Trades:     `{f['total_trades']}`\n"
                        f"  Multiplier: `{f['multiplier']}x`\n"
                        f"  Status:     `{'Active' if f['active'] else 'Paused'}`"
                        f"{SIGNATURE}"
                    )
                    return

            self._send_to(chat_id,
                f"*Not Found*\n"
                f"Use /follow to register."
                f"{SIGNATURE}"
            )

        elif cmd in ("/stop_copy", "/stopcopy"):
            if not self.copy_manager:
                self._send_to(chat_id,
                    f"*System Unavailable*"
                    f"{SIGNATURE}"
                )
                return

            follower_wallet = self._get_wallet_by_chat(chat_id)
            if not follower_wallet:
                self._send_to(chat_id,
                    f"*Not Registered*"
                    f"{SIGNATURE}"
                )
                return

            ok = self.copy_manager.toggle_follower(follower_wallet, False)
            if ok:
                self._send_to(chat_id,
                    f"*COPY TRADING PAUSED*\n"
                    f"{DIVIDER}\n"
                    f"\n"
                    f"Existing positions remain open.\n"
                    f"Use `/follow` to reactivate."
                    f"{SIGNATURE}"
                )
                self._send(
                    f"*FOLLOWER PAUSED*\n"
                    f"  Wallet: `{follower_wallet[:10]}...`"
                    f"{SIGNATURE}"
                )
            else:
                self._send_to(chat_id,
                    f"*Error pausing copy trading.*"
                    f"{SIGNATURE}"
                )

    def _save_follower_chat(self, wallet: str, chat_id: str):
        """Save mapping of wallet -> telegram chat_id."""
        chat_map_file = "follower_chats.json"
        chat_map = {}
        if os.path.exists(chat_map_file):
            try:
                with open(chat_map_file, "r") as f:
                    chat_map = json.load(f)
            except:
                pass
        chat_map[wallet.lower()] = chat_id
        with open(chat_map_file, "w") as f:
            json.dump(chat_map, f, indent=2)

    def _get_wallet_by_chat(self, chat_id: str) -> str:
        """Get wallet address by telegram chat_id."""
        chat_map_file = "follower_chats.json"
        if not os.path.exists(chat_map_file):
            return ""
        try:
            with open(chat_map_file, "r") as f:
                chat_map = json.load(f)
            for wallet, cid in chat_map.items():
                if str(cid) == str(chat_id):
                    return wallet
        except:
            pass
        return ""

    def _handle_command(self, text: str):
        """Handle Telegram commands from master."""
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/help":
            self._send(
                f"*CYPHER GROK TRADE — Commands*\n"
                f"{DIVIDER}\n"
                f"\n"
                f"  /status          — Current status\n"
                f"  /followers       — List followers\n"
                f"  /copy\\_stats     — Copy trading stats\n"
                f"  /fees            — Fee report\n"
                f"  /collect\\_fees   — Collect pending fees\n"
                f"  /join\\_link      — Follower onboarding\n"
                f"\n"
                f"{DIVIDER}\n"
                f"*Follower Management*\n"
                f"  /add\\_follower `name` `key` `mult`\n"
                f"  /remove\\_follower `wallet`\n"
                f"  /pause\\_follower `wallet`\n"
                f"  /resume\\_follower `wallet`"
                f"{SIGNATURE}"
            )

        elif cmd == "/followers":
            self.follower_stats()

        elif cmd in ("/copy_stats", "/copystats"):
            self.follower_stats()

        elif cmd in ("/add_follower", "/addfollower"):
            if len(parts) < 3:
                self._send(
                    f"*Usage:* `/add_follower name private_key [multiplier]`"
                    f"{SIGNATURE}"
                )
                return
            name = parts[1]
            key = parts[2]
            mult = float(parts[3]) if len(parts) > 3 else 1.0
            if self.copy_manager:
                result = self.copy_manager.add_follower(name, key, multiplier=mult)
                if result.get("success"):
                    self.new_follower(name, result["wallet"], result["balance"])
                else:
                    self._send(
                        f"*Error:* {result.get('error', 'Unknown')}"
                        f"{SIGNATURE}"
                    )

        elif cmd in ("/remove_follower", "/removefollower"):
            if len(parts) < 2:
                self._send(
                    f"*Usage:* `/remove_follower wallet_address`"
                    f"{SIGNATURE}"
                )
                return
            if self.copy_manager:
                removed = self.copy_manager.remove_follower(parts[1])
                self._send(
                    f"{'*Follower removed.*' if removed else '*Not found.*'}"
                    f"{SIGNATURE}"
                )

        elif cmd in ("/pause_follower", "/pausefollower"):
            if len(parts) < 2:
                self._send(
                    f"*Usage:* `/pause_follower wallet_address`"
                    f"{SIGNATURE}"
                )
                return
            if self.copy_manager:
                ok = self.copy_manager.toggle_follower(parts[1], False)
                self._send(
                    f"{'*Follower paused.*' if ok else '*Not found.*'}"
                    f"{SIGNATURE}"
                )

        elif cmd in ("/resume_follower", "/resumefollower"):
            if len(parts) < 2:
                self._send(
                    f"*Usage:* `/resume_follower wallet_address`"
                    f"{SIGNATURE}"
                )
                return
            if self.copy_manager:
                ok = self.copy_manager.toggle_follower(parts[1], True)
                self._send(
                    f"{'*Follower resumed.*' if ok else '*Not found.*'}"
                    f"{SIGNATURE}"
                )

        elif cmd in ("/join_link", "/joinlink"):
            self._send(
                f"*CYPHER GROK TRADE — Join as Follower*\n"
                f"{DIVIDER}\n"
                f"\n"
                f"*Steps:*\n"
                f"  1. Create a Hyperliquid account\n"
                f"  2. Deposit USDC\n"
                f"  3. Export your Private Key\n"
                f"  4. Send:\n"
                f"     `/add_follower Name PrivateKey 1.0`\n"
                f"\n"
                f"{DIVIDER}\n"
                f"*Multiplier Guide*\n"
                f"  `1.0` — Same % as master\n"
                f"  `0.5` — Half risk\n"
                f"  `2.0` — Double risk\n"
                f"\n"
                f"Capital Allocation:\n"
                f"  50% Arbitrum LP | 25% Scalp | 25% MM\n"
                f"\n"
                f"Trades are copied in real-time."
                f"{SIGNATURE}"
            )

        elif cmd == "/fees":
            if self.copy_manager:
                fees = self.copy_manager.fee_tracker.get_fee_stats()
                followers = self.copy_manager.list_followers()

                fee_lines = ""
                for f in followers:
                    if f.get("pending_fees", 0) > 0 or f.get("total_fees_paid", 0) > 0:
                        fee_lines += (
                            f"  *{f['name']}*\n"
                            f"    Paid: `${f['total_fees_paid']:.2f}` | "
                            f"Pending: `${f['pending_fees']:.4f}`\n"
                        )

                if not fee_lines:
                    fee_lines = "  No fees collected yet.\n"

                self._send(
                    f"*FEE REPORT*\n"
                    f"{DIVIDER}\n"
                    f"\n"
                    f"  Performance Fee: `{fees['performance_fee_pct']:.0f}%` of profit\n"
                    f"  Trade Fee:       `{fees['trade_fee_pct']:.1f}%` per trade\n"
                    f"  LP Copy Fee:     `{fees.get('lp_copy_fee_pct', 5):.0f}%` of LP alloc\n"
                    f"\n"
                    f"{DIVIDER}\n"
                    f"*Totals*\n"
                    f"  Collected:  `${fees['total_collected']:.2f}`\n"
                    f"    Perf:     `${fees['total_performance_fees']:.2f}`\n"
                    f"    Trade:    `${fees['total_trade_fees']:.2f}`\n"
                    f"    LP Copy:  `${fees.get('total_lp_copy_fees', 0):.2f}`\n"
                    f"  Pending:    `${fees['pending_uncollected']:.4f}`\n"
                    f"  Collections: `{fees['num_collections']}`\n"
                    f"\n"
                    f"{DIVIDER}\n"
                    f"*By Follower*\n"
                    f"{fee_lines}"
                    f"{SIGNATURE}"
                )

        elif cmd in ("/collect_fees", "/collectfees"):
            if self.copy_manager:
                self._send(
                    f"*Collecting pending fees...*"
                )
                self.copy_manager._collect_all_fees()
                fees = self.copy_manager.fee_tracker.get_fee_stats()
                self._send(
                    f"*FEE COLLECTION COMPLETE*\n"
                    f"{DIVIDER}\n"
                    f"\n"
                    f"  Total Collected: `${fees['total_collected']:.2f}`\n"
                    f"  Remaining:       `${fees['pending_uncollected']:.4f}`"
                    f"{SIGNATURE}"
                )
