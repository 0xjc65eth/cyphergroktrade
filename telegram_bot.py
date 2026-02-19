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
                "🤖 *CypherGrokTrade v3 - ONLINE*\n"
                f"💰 Balance: ${getattr(config, 'INITIAL_CAPITAL', 0):.2f}\n"
                f"🎯 Target: ${getattr(config, 'TARGET_CAPITAL', 0):.2f}\n"
                f"⚡ Leverage: {getattr(config, 'LEVERAGE', 0)}x\n"
                f"📊 Scanning top {getattr(config, 'TOP_COINS_COUNT', 0)} coins\n"
                "━━━━━━━━━━━━━━━━━━━━"
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
        emoji = "🟢" if direction == "LONG" else "🔴"
        arrow = "📈" if direction == "LONG" else "📉"

        sl_price = price * (1 - sl_pct) if direction == "LONG" else price * (1 + sl_pct)
        tp_price = price * (1 + tp_pct) if direction == "LONG" else price * (1 - tp_pct)
        rr = tp_pct / sl_pct if sl_pct > 0 else 0

        msg = (
            f"{emoji} *SIGNAL: {direction} {coin}* {arrow}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💲 Entry: `${price:.4f}`\n"
            f"🛑 SL: `${sl_price:.4f}` ({sl_pct*100:.2f}%)\n"
            f"🎯 TP: `${tp_price:.4f}` ({tp_pct*100:.2f}%)\n"
            f"📊 R:R = 1:{rr:.1f}\n"
            f"🔥 Confidence: {confidence:.0%}\n"
            f"📉 5m Trend: {trend_5m}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 *SMC:* {smc_details[:100]}\n"
            f"📊 *MA:* {ma_details[:100]}\n"
            f"🤖 *Grok:* {grok_reason[:80]}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        self._send(msg)

    def trade_opened(self, coin: str, direction: str, size_usd: float,
                     price: float, leverage: int):
        """Notify when a trade is actually opened."""
        emoji = "✅" if direction == "LONG" else "✅"
        msg = (
            f"{emoji} *ABRIU {direction} {coin}*\n"
            f"💲 Preco: `${price:.4f}`\n"
            f"💵 Size: ${size_usd:.2f} ({leverage}x)\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        self._send(msg)

    def trade_closed(self, coin: str, direction: str, pnl: float, is_win: bool):
        """Notify when a trade is closed."""
        emoji = "💰" if is_win else "💸"
        color = "WIN" if is_win else "LOSS"
        msg = (
            f"{emoji} *{color}: {coin} {direction}*\n"
            f"{'📈' if is_win else '📉'} PnL: `${pnl:+.4f}`\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        self._send(msg)

    def status_update(self, balance: float, pnl: float, wins: int, losses: int,
                      open_positions: list, withdrawn: float, idle_scans: int):
        """Send periodic status update (max every 5 min)."""
        now = time.time()
        if now - self._last_status_time < 300:  # 5 min interval
            return
        self._last_status_time = now

        wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        pos_text = ""
        if open_positions:
            for p in open_positions:
                pos_emoji = "🟢" if p.get("unrealized_pnl", 0) >= 0 else "🔴"
                pos_text += f"  {pos_emoji} {p['coin']}: ${p.get('unrealized_pnl', 0):+.4f}\n"
        else:
            pos_text = "  Nenhuma posicao aberta\n"

        msg = (
            f"📊 *STATUS UPDATE*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance: `${balance:.2f}`\n"
            f"{pnl_emoji} PnL: `${pnl:+.2f}`\n"
            f"🏆 Win Rate: {wr:.0f}% ({wins}W/{losses}L)\n"
            f"💸 Withdrawn: ${withdrawn:.2f}\n"
            f"⏳ Idle Scans: {idle_scans}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 *Posicoes Abertas:*\n"
            f"{pos_text}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        self._send(msg)

    def scan_summary(self, total_scanned: int, signals_found: int, next_scan_seconds: int):
        """Notify scan results when no entries found."""
        if signals_found > 0:
            return  # Only notify when idle
        now = time.time()
        if now - self._last_status_time < 300:
            return

        msg = (
            f"🔍 *Scan completo*\n"
            f"Escaneados: {total_scanned} ativos\n"
            f"Sinais: {signals_found}\n"
            f"⏳ Proximo scan em ~{next_scan_seconds}s\n"
            f"📋 MM ativo como fallback"
        )
        self._send(msg)

    def withdrawal(self, amount: float, total: float):
        """Notify profit withdrawal."""
        msg = (
            f"💸 *PROFIT WITHDRAWAL*\n"
            f"Enviado: ${amount:.2f}\n"
            f"Total retirado: ${total:.2f}"
        )
        self._send(msg)

    def error(self, error_msg: str):
        """Notify errors."""
        msg = f"⚠️ *ERRO:* {error_msg[:200]}"
        self._send(msg)

    def shutdown(self, balance: float, pnl: float, wins: int, losses: int, withdrawn: float):
        """Notify shutdown."""
        wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        msg = (
            f"🔴 *BOT DESLIGADO*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance Final: ${balance:.2f}\n"
            f"{'📈' if pnl >= 0 else '📉'} PnL: ${pnl:+.2f}\n"
            f"🏆 Win Rate: {wr:.0f}% ({wins}W/{losses}L)\n"
            f"💸 Total Withdrawn: ${withdrawn:.2f}"
        )
        self._send(msg)

    # ─── Copy Trading Notifications ───

    def copy_trade_executed(self, follower_name: str, coin: str, direction: str,
                            size_usd: float):
        """Notify when a copy trade is executed for a follower."""
        msg = (
            f"👥 *COPY TRADE*\n"
            f"Follower: {follower_name}\n"
            f"{'🟢' if direction == 'LONG' else '🔴'} {direction} {coin}\n"
            f"💵 Size: ${size_usd:.2f}"
        )
        self._send(msg)

    def new_follower(self, name: str, wallet: str, balance: float):
        """Notify when a new follower joins."""
        msg = (
            f"🆕 *NOVO FOLLOWER!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Nome: {name}\n"
            f"🔑 Wallet: `{wallet[:10]}...`\n"
            f"💰 Balance: ${balance:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Trades serão copiados automaticamente!"
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
            status = "🟢" if f["active"] else "🔴"
            pnl_emoji = "📈" if f["pnl_since_join"] >= 0 else "📉"
            follower_lines += (
                f"  {status} *{f['name']}* | ${f['balance']:.2f} | "
                f"{pnl_emoji} ${f['pnl_since_join']:+.2f} | "
                f"{f['positions']} pos | {f['multiplier']}x\n"
            )

        if not follower_lines:
            follower_lines = "  Nenhum follower ainda\n"

        msg = (
            f"👥 *COPY TRADING STATUS*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Followers: {stats['active_followers']}/{stats['total_followers']}\n"
            f"💰 Total Balance: ${stats['total_follower_balance']:.2f}\n"
            f"📊 Total Trades Copied: {stats['total_trades_copied']}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{follower_lines}"
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
                    # Private commands - only master
                    self._handle_command(text)

        except Exception as e:
            pass  # Silent fail on poll errors

    def _handle_public_command(self, text: str, chat_id: str, user_name: str):
        """Handle commands from ANY user (public commands for followers)."""
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/start" or cmd == "/join":
            self._send_to(chat_id,
                "🤖 *CypherGrokTrade - Copy Trading*\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Copie trades automaticamente!\n\n"
                "*Para começar:*\n"
                "1️⃣ Crie conta na Hyperliquid\n"
                "2️⃣ Deposite USDC\n"
                "3️⃣ Exporte sua Private Key\n"
                "4️⃣ Envie aqui:\n"
                "`/follow SeuNome SuaPrivateKey`\n\n"
                "*Comandos:*\n"
                "/follow `nome` `key` - Começar a copiar\n"
                "/my\\_status - Ver seu status\n"
                "/stop\\_copy - Parar de copiar\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚡ Trades copiados em tempo real!"
            )

        elif cmd == "/follow" or cmd == "/add_follower" or cmd == "/addfollower":
            if len(parts) < 3:
                self._send_to(chat_id,
                    "❌ *Uso:* `/follow SeuNome SuaPrivateKey`\n\n"
                    "Exemplo:\n"
                    "`/follow João 0xSuaPrivateKey123...`\n\n"
                    "Opcional - multiplicador de risco:\n"
                    "`/follow João 0xKey 0.5` (metade do risco)\n"
                    "`/follow João 0xKey 2.0` (dobro do risco)"
                )
                return

            name = parts[1]
            key = parts[2]
            mult = float(parts[3]) if len(parts) > 3 else 1.0

            if not self.copy_manager:
                self._send_to(chat_id, "⚠️ Sistema de copy trading não está ativo no momento.")
                return

            result = self.copy_manager.add_follower(name, key, multiplier=mult)

            if result.get("success"):
                # Notify the follower
                self._send_to(chat_id,
                    f"✅ *Bem-vindo, {name}!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Seu balance: ${result['balance']:.2f}\n"
                    f"📊 Multiplicador: {mult}x\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Suas trades estão sendo copiadas automaticamente!\n\n"
                    f"Comandos:\n"
                    f"/my\\_status - Ver suas posições\n"
                    f"/stop\\_copy - Parar de copiar"
                )
                # Notify YOU (the master) - privately
                self._send(
                    f"🆕 *NOVO FOLLOWER!*\n"
                    f"👤 {name} | 💰 ${result['balance']:.2f} | "
                    f"📊 {mult}x | Chat: {chat_id}"
                )
                # Save chat_id for future notifications to this follower
                self._save_follower_chat(result["wallet"], chat_id)
            else:
                self._send_to(chat_id, f"❌ Erro: {result.get('error', 'Desconhecido')}")

        elif cmd == "/my_status" or cmd == "/mystatus":
            if not self.copy_manager:
                self._send_to(chat_id, "⚠️ Sistema não ativo.")
                return

            # Find follower by chat_id
            follower_wallet = self._get_wallet_by_chat(chat_id)
            if not follower_wallet:
                self._send_to(chat_id,
                    "❌ Você não está registrado.\n"
                    "Use `/follow SeuNome SuaPrivateKey` para começar."
                )
                return

            followers = self.copy_manager.list_followers()
            for f in followers:
                if f.get("full_wallet", "").lower() == follower_wallet.lower():
                    pnl_emoji = "📈" if f["pnl_since_join"] >= 0 else "📉"
                    self._send_to(chat_id,
                        f"📊 *Seu Status - {f['name']}*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💰 Balance: ${f['balance']:.2f}\n"
                        f"{pnl_emoji} PnL: ${f['pnl_since_join']:+.2f}\n"
                        f"📂 Posições: {f['positions']}\n"
                        f"📊 Trades copiados: {f['total_trades']}\n"
                        f"⚡ Multiplicador: {f['multiplier']}x\n"
                        f"🟢 Status: {'Ativo' if f['active'] else 'Pausado'}"
                    )
                    return

            self._send_to(chat_id, "❌ Não encontrado. Use /follow para se registrar.")

        elif cmd == "/stop_copy" or cmd == "/stopcopy":
            if not self.copy_manager:
                self._send_to(chat_id, "⚠️ Sistema não ativo.")
                return

            follower_wallet = self._get_wallet_by_chat(chat_id)
            if not follower_wallet:
                self._send_to(chat_id, "❌ Você não está registrado.")
                return

            ok = self.copy_manager.toggle_follower(follower_wallet, False)
            if ok:
                self._send_to(chat_id,
                    "⏸ *Copy trading pausado.*\n"
                    "Suas posições atuais continuam abertas.\n"
                    "Use `/follow` novamente para reativar."
                )
                self._send(f"⏸ Follower {follower_wallet[:10]}... pausou o copy trading.")
            else:
                self._send_to(chat_id, "❌ Erro ao pausar.")

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
        """Handle Telegram commands."""
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/help":
            self._send(
                "🤖 *CypherGrokTrade Commands:*\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📊 /status - Status atual\n"
                "👥 /followers - Lista de followers\n"
                "➕ /add\\_follower `name` `key` `mult` - Adicionar follower\n"
                "❌ /remove\\_follower `wallet` - Remover follower\n"
                "⏸ /pause\\_follower `wallet` - Pausar follower\n"
                "▶️ /resume\\_follower `wallet` - Retomar follower\n"
                "📈 /copy\\_stats - Estatísticas copy trading\n"
                "🔗 /join\\_link - Link para followers\n"
            )

        elif cmd == "/followers":
            self.follower_stats()

        elif cmd == "/copy_stats" or cmd == "/copystats":
            self.follower_stats()

        elif cmd == "/add_follower" or cmd == "/addfollower":
            if len(parts) < 3:
                self._send("❌ Uso: /add\\_follower `nome` `private_key` `[multiplicador]`")
                return
            name = parts[1]
            key = parts[2]
            mult = float(parts[3]) if len(parts) > 3 else 1.0
            if self.copy_manager:
                result = self.copy_manager.add_follower(name, key, multiplier=mult)
                if result.get("success"):
                    self.new_follower(name, result["wallet"], result["balance"])
                else:
                    self._send(f"❌ Erro: {result.get('error', 'Desconhecido')}")

        elif cmd == "/remove_follower" or cmd == "/removefollower":
            if len(parts) < 2:
                self._send("❌ Uso: /remove\\_follower `wallet_address`")
                return
            if self.copy_manager:
                removed = self.copy_manager.remove_follower(parts[1])
                self._send("✅ Follower removido" if removed else "❌ Não encontrado")

        elif cmd == "/pause_follower" or cmd == "/pausefollower":
            if len(parts) < 2:
                self._send("❌ Uso: /pause\\_follower `wallet_address`")
                return
            if self.copy_manager:
                ok = self.copy_manager.toggle_follower(parts[1], False)
                self._send("⏸ Follower pausado" if ok else "❌ Não encontrado")

        elif cmd == "/resume_follower" or cmd == "/resumefollower":
            if len(parts) < 2:
                self._send("❌ Uso: /resume\\_follower `wallet_address`")
                return
            if self.copy_manager:
                ok = self.copy_manager.toggle_follower(parts[1], True)
                self._send("▶️ Follower retomado" if ok else "❌ Não encontrado")

        elif cmd == "/join_link" or cmd == "/joinlink":
            self._send(
                "🔗 *Como seguir o CypherGrokTrade:*\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ Crie conta na Hyperliquid\n"
                "2️⃣ Deposite USDC\n"
                "3️⃣ Exporte sua Private Key\n"
                "4️⃣ Envie aqui:\n"
                "`/add_follower SeuNome SuaPrivateKey 1.0`\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "O multiplicador (1.0) define o % do capital:\n"
                "• 1.0 = mesmo % que o master\n"
                "• 0.5 = metade do risco\n"
                "• 2.0 = dobro do risco\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ Trades são copiados automaticamente em tempo real!"
            )

        elif cmd == "/fees":
            if self.copy_manager:
                fees = self.copy_manager.fee_tracker.get_fee_stats()
                followers = self.copy_manager.list_followers()

                fee_lines = ""
                for f in followers:
                    if f.get("pending_fees", 0) > 0 or f.get("total_fees_paid", 0) > 0:
                        fee_lines += (
                            f"  👤 *{f['name']}*: "
                            f"pago=${f['total_fees_paid']:.2f} | "
                            f"pendente=${f['pending_fees']:.4f}\n"
                        )

                if not fee_lines:
                    fee_lines = "  Nenhuma fee ainda\n"

                self._send(
                    f"💰 *FEE REPORT*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 Performance Fee: {fees['performance_fee_pct']:.0f}% do lucro\n"
                    f"📊 Trade Fee: {fees['trade_fee_pct']:.1f}% por trade\n"
                    f"📊 LP Copy Fee: {fees.get('lp_copy_fee_pct', 5):.0f}% da alocacao LP\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Total Coletado: `${fees['total_collected']:.2f}`\n"
                    f"  Perf Fees: ${fees['total_performance_fees']:.2f}\n"
                    f"  Trade Fees: ${fees['total_trade_fees']:.2f}\n"
                    f"  LP Copy Fees: ${fees.get('total_lp_copy_fees', 0):.2f}\n"
                    f"⏳ Pendente: `${fees['pending_uncollected']:.4f}`\n"
                    f"📦 Coletas: {fees['num_collections']}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"*Por Follower:*\n"
                    f"{fee_lines}"
                )

        elif cmd == "/collect_fees" or cmd == "/collectfees":
            if self.copy_manager:
                self._send("💸 Coletando fees pendentes...")
                self.copy_manager._collect_all_fees()
                fees = self.copy_manager.fee_tracker.get_fee_stats()
                self._send(
                    f"✅ Coleta concluída!\n"
                    f"Total coletado: `${fees['total_collected']:.2f}`\n"
                    f"Pendente: `${fees['pending_uncollected']:.4f}`"
                )
