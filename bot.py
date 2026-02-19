#!/usr/bin/env python3 -u
"""
CypherGrokTrade v3 - Premium SMC Strategy
Multi-timeframe SMC confluence + Grok AI + MM fallback when idle.

Key v3 improvements:
- 15m HTF bias for extra confirmation
- ATR-based dynamic SL/TP
- MM fallback when no futures signals (always generating revenue)
- Max 5 positions (quality over quantity)
- Premium confluence requirement (2+ SMC factors)
- Stricter confidence thresholds

Usage: python3 bot.py start money
"""

import sys
import os
import time
import signal

os.environ["PYTHONUNBUFFERED"] = "1"
from datetime import datetime, timedelta

import config
from smc_engine import SMCEngine
from ma_scalper import MAScalper
from executor import HyperliquidExecutor
from grok_ai import GrokAI
from mm_spot import SpotMarketMaker
from telegram_bot import TelegramNotifier
from trade_logger import TradeLogger
from copy_trading import CopyTradingManager
from arb_lp import ArbitrumLPManager


class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


class CypherGrokTradeBot:
    def __init__(self):
        self.smc = SMCEngine(
            lookback=config.SMC_LOOKBACK,
            ob_threshold=config.ORDER_BLOCK_THRESHOLD,
            fvg_min_gap=config.FVG_MIN_GAP,
            bos_candles=config.BOS_CONFIRMATION_CANDLES,
            displacement_min=config.DISPLACEMENT_MIN,
        )
        self.ma = MAScalper(
            ema_fast=config.EMA_FAST,
            ema_slow=config.EMA_SLOW,
            ema_trend=config.EMA_TREND,
            rsi_period=config.RSI_PERIOD,
            rsi_ob=config.RSI_OVERBOUGHT,
            rsi_os=config.RSI_OVERSOLD,
        )
        self.executor = HyperliquidExecutor()
        self.grok = GrokAI()
        self.mm = SpotMarketMaker() if config.MM_ENABLED else None
        self.telegram = TelegramNotifier()
        self.logger = TradeLogger()
        self.copy_manager = CopyTradingManager(config.HL_WALLET_ADDRESS)
        self.arb_lp = ArbitrumLPManager() if getattr(config, 'ARB_LP_ENABLED', False) else None

        self.running = False
        self.start_balance = 0
        self.trades_taken = 0
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.cooldown_until = None
        self.last_mm_refresh = 0
        self.last_arb_lp_refresh = 0
        self.last_withdraw_check = 0
        self.total_withdrawn = 0.0
        self.idle_scans = 0  # Track scans with no futures entry

    def banner(self):
        print(f"""
{C.CYAN}{C.BOLD}
  ██████╗██╗   ██╗██████╗ ██╗  ██╗███████╗██████╗
 ██╔════╝╚██╗ ██╔╝██╔══██╗██║  ██║██╔════╝██╔══██╗
 ██║      ╚████╔╝ ██████╔╝███████║█████╗  ██████╔╝
 ██║       ╚██╔╝  ██╔═══╝ ██╔══██║██╔══╝  ██╔══██╗
 ╚██████╗   ██║   ██║     ██║  ██║███████╗██║  ██║
  ╚═════╝   ╚═╝   ╚═╝     ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝
 {C.MAGENTA}╔══════════════════════════════════════════════╗
 ║  GROK TRADE AI v3 - PREMIUM SMC STRATEGY     ║
 ║  Multi-TF Confluence + MM Fallback            ║
 ║  Target: 80%+ Win Rate                        ║
 ╚══════════════════════════════════════════════╝{C.RESET}
""")

    def _get_5m_trend(self, coin: str) -> str:
        """Get the 5-minute trend direction using EMA alignment."""
        df_5m = self.executor.get_candles(coin, "5m", 60)
        if df_5m.empty or len(df_5m) < 55:
            return "NEUTRAL"
        ma_result = self.ma.analyze(df_5m)
        return ma_result["signal"]

    def _get_15m_bias(self, coin: str) -> str:
        """Get the 15-minute bias using SMC structure."""
        df_15m = self.executor.get_candles(coin, "15m", 100)
        if df_15m.empty or len(df_15m) < 60:
            return "NEUTRAL"

        # Use SMC for HTF bias (structural analysis)
        smc_result = self.smc.analyze(df_15m)
        return smc_result["signal"]

    def _get_atr_levels(self, ma_result: dict, current_price: float) -> tuple:
        """Calculate ATR-based SL/TP levels.

        Returns (sl_pct, tp_pct) as decimals.
        """
        if not config.USE_ATR_STOPS:
            return config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT

        atr_pct = ma_result.get("atr_pct", 0)
        if atr_pct and atr_pct > 0:
            sl_pct = max(atr_pct * config.ATR_SL_MULTIPLIER, 0.015)  # Min 1.5% SL (era 0.5%)
            tp_pct = max(atr_pct * config.ATR_TP_MULTIPLIER, sl_pct * 2.0)  # Min 2:1 R:R

            # Cap at reasonable levels
            sl_pct = min(sl_pct, 0.04)  # Max 4% SL
            tp_pct = min(tp_pct, 0.10)  # Max 10% TP

            return sl_pct, tp_pct

        return config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT

    def _check_profit_withdrawal(self, balance: float):
        """Send profit to user wallet or LP based on performance.
        - Normal: send to user wallet every WITHDRAW_EVERY_USD
        - When HL >= +200%: bridge profit to Arbitrum LP instead
        """
        now = time.time()
        if now - self.last_withdraw_check < 60:
            return
        self.last_withdraw_check = now

        profit = balance - config.INITIAL_CAPITAL - self.total_withdrawn
        if profit < config.WITHDRAW_EVERY_USD:
            return

        pnl_pct = (profit / config.INITIAL_CAPITAL * 100) if config.INITIAL_CAPITAL > 0 else 0
        withdraw_amount = int(profit / config.WITHDRAW_EVERY_USD) * config.WITHDRAW_EVERY_USD

        # When HL is +200% or more, feed LP instead of user wallet
        if pnl_pct >= 200 and self.arb_lp:
            try:
                if self.arb_lp._bridge_from_hl(withdraw_amount):
                    self.total_withdrawn += withdraw_amount
                    print(f"\n  {C.MAGENTA}{C.BOLD}  PROFIT -> LP: ${withdraw_amount:.2f} bridged to Arbitrum "
                          f"(HL +{pnl_pct:.0f}%){C.RESET}")
                    self.telegram.send(
                        f"LP FEED: ${withdraw_amount:.2f} bridged to Arbitrum LP (HL +{pnl_pct:.0f}%)"
                    )
                else:
                    print(f"  {C.YELLOW}[LP-FEED] Bridge failed, falling back to wallet{C.RESET}")
                    self._withdraw_to_wallet(withdraw_amount)
            except Exception as e:
                print(f"  {C.YELLOW}[LP-FEED] Error: {e}, falling back to wallet{C.RESET}")
                self._withdraw_to_wallet(withdraw_amount)
        else:
            self._withdraw_to_wallet(withdraw_amount)

    def _withdraw_to_wallet(self, amount: float):
        """Send profit to user wallet."""
        try:
            result = self.executor.exchange.usd_transfer(
                amount,
                config.WITHDRAW_WALLET,
            )
            if result.get("status") == "ok":
                self.total_withdrawn += amount
                print(f"\n  {C.GREEN}{C.BOLD}  PROFIT SENT: ${amount:.2f} -> "
                      f"{config.WITHDRAW_WALLET[:10]}...{C.RESET}")
                print(f"  {C.GREEN}Total withdrawn: ${self.total_withdrawn:.2f}{C.RESET}")
                self.telegram.withdrawal(amount, self.total_withdrawn)
            else:
                print(f"  {C.YELLOW}[WITHDRAW] Failed: {result}{C.RESET}")
        except Exception as e:
            print(f"  {C.YELLOW}[WITHDRAW] Error: {e}{C.RESET}")

    def _run_mm_cycle(self, reason: str = "scheduled"):
        """Run a market making cycle."""
        if not self.mm or not config.MM_ENABLED:
            return
        try:
            print(f"\n  {C.CYAN}[MM] Running cycle ({reason})...{C.RESET}")
            self.mm.run_cycle()
            self.last_mm_refresh = time.time()
        except Exception as e:
            print(f"  {C.RED}[MM] Error: {e}{C.RESET}")

    def _run_arb_lp_cycle(self, reason: str = "scheduled"):
        """Run an Arbitrum LP management cycle."""
        if not self.arb_lp or not getattr(config, 'ARB_LP_ENABLED', False):
            return
        try:
            print(f"\n  {C.CYAN}[ARB-LP] Running cycle ({reason})...{C.RESET}")
            self.arb_lp.run_cycle()
            self.last_arb_lp_refresh = time.time()
        except Exception as e:
            print(f"  {C.RED}[ARB-LP] Error: {e}{C.RESET}")

    def start(self):
        """Main entry point."""
        self.banner()
        self.running = True

        try:
            signal.signal(signal.SIGINT, self._shutdown)
            signal.signal(signal.SIGTERM, self._shutdown)
        except ValueError:
            pass  # signal only works in main thread (cloud deploy uses threading)

        self.start_balance = self.executor.get_balance()
        print(f"{C.BOLD}[INIT]{C.RESET} Account Balance: {C.GREEN}${self.start_balance:.2f}{C.RESET}")
        print(f"{C.BOLD}[INIT]{C.RESET} Target: {C.GREEN}${config.TARGET_CAPITAL:.2f}{C.RESET}")
        print(f"{C.BOLD}[INIT]{C.RESET} Leverage: {C.YELLOW}{config.LEVERAGE}x{C.RESET}")
        if config.TRADING_PAIRS:
            print(f"{C.BOLD}[INIT]{C.RESET} Pairs: {C.CYAN}{', '.join(config.TRADING_PAIRS)}{C.RESET}")
        else:
            top = self.executor.get_top_coins(config.TOP_COINS_COUNT, config.MIN_VOLUME_24H)
            print(f"{C.BOLD}[INIT]{C.RESET} Dynamic Pairs (top {config.TOP_COINS_COUNT} by volume):")
            print(f"  {C.CYAN}{', '.join(top)}{C.RESET}")

        extra = getattr(config, "EXTRA_PAIRS", [])
        if extra:
            print(f"{C.BOLD}[INIT]{C.RESET} FX/Commodities/Indices: {C.CYAN}{', '.join(extra)}{C.RESET}")
        print(f"{C.BOLD}[INIT]{C.RESET} SL/TP: {'ATR-based' if config.USE_ATR_STOPS else 'Fixed'} "
              f"(fallback: {config.STOP_LOSS_PCT*100:.1f}%/{config.TAKE_PROFIT_PCT*100:.1f}%)")
        print(f"{C.BOLD}[INIT]{C.RESET} Min Confidence: {config.MIN_CONFIDENCE} | "
              f"5M+15M Filter: ON | Max Positions: {config.MAX_OPEN_POSITIONS}")
        print(f"{C.BOLD}[INIT]{C.RESET} MM Fallback: {'ON' if config.MM_FALLBACK_ENABLED else 'OFF'} | "
              f"MM Pairs: {', '.join(config.MM_PAIRS)}")
        print(f"{C.BOLD}[INIT]{C.RESET} Arbitrum LP: {'ON' if getattr(config, 'ARB_LP_ENABLED', False) else 'OFF'}"
              f" | Alloc: ${getattr(config, 'ARB_LP_ALLOC_USD', 0)}")
        print(f"{C.BOLD}[INIT]{C.RESET} Profit Withdrawal: ${config.WITHDRAW_EVERY_USD} -> {config.WITHDRAW_WALLET[:12]}...")
        learn_summary = self.logger.get_summary()
        print(f"{C.BOLD}[LEARN]{C.RESET} {learn_summary}")
        print()

        if self.start_balance < 1:
            print(f"{C.RED}[ERROR] Balance too low (${self.start_balance:.2f}). Need at least $1.{C.RESET}")
            return

        print(f"{C.CYAN}[GROK] Fetching market sentiment (top 3)...{C.RESET}")
        pairs = config.TRADING_PAIRS or self.executor.get_top_coins(3)
        for coin in pairs[:3]:
            sentiment = self.grok.get_market_sentiment(coin)
            print(f"  {C.BOLD}{coin}{C.RESET}: {sentiment}")
        print()

        # Connect copy manager to telegram and start command listener
        self.telegram.copy_manager = self.copy_manager
        self.telegram.start_command_listener()

        # Connect master LP to copy manager for LP mirroring
        if self.arb_lp:
            self.copy_manager.set_master_lp(self.arb_lp)

        # Start copy trading sync loop
        num_followers = len(self.copy_manager.followers)
        if num_followers > 0:
            print(f"{C.BOLD}[COPY]{C.RESET} {C.GREEN}{num_followers} followers active - sync loop started{C.RESET}")
            self.copy_manager.start_sync_loop(interval_seconds=15)
        else:
            print(f"{C.BOLD}[COPY]{C.RESET} No followers. Add with: ./venv/bin/python3 copy_trading.py add <name> <key>")

        print(f"{C.GREEN}{C.BOLD}[BOT] Starting trading loop (v3 premium SMC)...{C.RESET}")
        print(f"{C.YELLOW}{'='*60}{C.RESET}")

        self._trading_loop()

    def _trading_loop(self):
        """Main trading loop with multi-timeframe analysis + MM fallback."""
        cycle = 0
        while self.running:
            try:
                cycle += 1
                now = datetime.now()

                # Check cooldown
                if self.cooldown_until and now < self.cooldown_until:
                    remaining = (self.cooldown_until - now).seconds
                    print(f"\r{C.YELLOW}[COOLDOWN] {remaining}s remaining...{C.RESET}", end="")
                    # During cooldown, run MM to keep generating revenue
                    if config.MM_FALLBACK_ENABLED:
                        self._run_mm_cycle("cooldown")
                    time.sleep(5)
                    continue

                balance = self.executor.get_balance()
                pnl = balance - self.start_balance
                pnl_pct = (pnl / self.start_balance * 100) if self.start_balance > 0 else 0

                # Target check
                if balance >= config.TARGET_CAPITAL:
                    print(f"\n{C.GREEN}{C.BOLD}  TARGET REACHED! ${balance:.2f}{C.RESET}")
                    self._close_all_positions()
                    return

                # Daily loss LIMIT (ATIVADO - para de abrir novas posicoes)
                if pnl < 0:
                    loss_pct = abs(pnl) / self.start_balance * 100 if self.start_balance > 0 else 0
                    if loss_pct >= config.MAX_DAILY_LOSS_PCT:
                        print(f"  {C.RED}[STOP] Daily loss limit atingido: {loss_pct:.1f}% >= {config.MAX_DAILY_LOSS_PCT}%{C.RESET}")
                        print(f"  {C.YELLOW}[STOP] Bot pausado para novas entradas. Posicoes abertas serao gerenciadas.{C.RESET}")
                        self.telegram.send(f"STOP: Limite diario de loss atingido ({loss_pct:.1f}%). Sem novas entradas.")
                        # Only manage existing positions, don't open new ones
                        if config.MM_FALLBACK_ENABLED:
                            self._run_mm_cycle("daily-loss-limit")
                        time.sleep(60)
                        continue
                    elif loss_pct > 10:
                        print(f"  {C.YELLOW}[WARN] Drawdown: {loss_pct:.1f}% - cuidado{C.RESET}")

                # Trading hours filter
                if getattr(config, 'TRADING_HOURS_ENABLED', False):
                    current_hour = now.hour
                    start_h = getattr(config, 'TRADING_HOURS_START', 0)
                    end_h = getattr(config, 'TRADING_HOURS_END', 23)
                    if current_hour < start_h or current_hour >= end_h:
                        print(f"  {C.YELLOW}[WAIT] Fora do horario operacional ({start_h}h-{end_h}h UTC). Atual: {current_hour}h{C.RESET}")
                        if config.MM_FALLBACK_ENABLED:
                            self._run_mm_cycle("off-hours")
                        time.sleep(60)
                        continue

                # Check profit withdrawal
                self._check_profit_withdrawal(balance)

                # Status
                pnl_color = C.GREEN if pnl >= 0 else C.RED
                wr = (self.wins / self.trades_taken * 100) if self.trades_taken > 0 else 0
                print(f"\n{C.CYAN}[Cycle {cycle}] {now.strftime('%H:%M:%S')} | "
                      f"Bal: ${balance:.2f} | PnL: {pnl_color}${pnl:+.2f}{C.CYAN} | "
                      f"W/L: {self.wins}/{self.losses} ({wr:.0f}%) | "
                      f"Withdrawn: ${self.total_withdrawn:.2f} | "
                      f"Idle: {self.idle_scans}{C.RESET}")

                # Telegram status update (every 5 min)
                open_pos = self.executor.get_open_positions()
                self.telegram.status_update(
                    balance, pnl, self.wins, self.losses,
                    open_pos, self.total_withdrawn, self.idle_scans
                )

                # Check SL/TP
                coins_to_close = self.executor.check_sl_tp()
                for coin in coins_to_close:
                    result = self.executor.close_position(coin)
                    if result["status"] == "ok":
                        self.trades_taken += 1
                        new_balance = self.executor.get_balance()
                        if new_balance > balance:
                            self.wins += 1
                            self.consecutive_losses = 0
                            gain = new_balance - balance
                            print(f"  {C.GREEN}[WIN] {coin} +${gain:.4f}{C.RESET}")
                            self.telegram.trade_closed(coin, "WIN", gain, True)
                            self.logger.log_trade_close(coin, self.executor.get_mid_price(coin), gain, True)
                        else:
                            self.losses += 1
                            self.consecutive_losses += 1
                            loss = balance - new_balance
                            print(f"  {C.RED}[LOSS] {coin} -${loss:.4f}{C.RESET}")
                            self.telegram.trade_closed(coin, "LOSS", -loss, False)
                            self.logger.log_trade_close(coin, self.executor.get_mid_price(coin), -loss, False)

                            if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
                                self.cooldown_until = now + timedelta(seconds=config.COOLDOWN_SECONDS)
                                print(f"  {C.YELLOW}[COOLDOWN] {self.consecutive_losses} losses. "
                                      f"Pausing {config.COOLDOWN_SECONDS}s{C.RESET}")
                        balance = new_balance

                # Check open positions
                open_positions = self.executor.get_open_positions()
                coins_with_positions = {p["coin"] for p in open_positions}

                if len(coins_with_positions) >= config.MAX_OPEN_POSITIONS:
                    pos_str = ", ".join(f"{p['coin']}:{p['unrealized_pnl']:+.4f}" for p in open_positions)
                    print(f"  {C.YELLOW}[HOLD] {pos_str}{C.RESET}")
                    # Run MM while holding max positions
                    if config.MM_FALLBACK_ENABLED:
                        self._run_mm_cycle("max-positions")
                    time.sleep(config.SCAN_INTERVAL)
                    continue

                # Get coins to scan (top by volume + FX/commodities/indices)
                scan_coins = config.TRADING_PAIRS or self.executor.get_top_coins(
                    config.TOP_COINS_COUNT, config.MIN_VOLUME_24H
                )
                # Merge extra pairs (FX, commodities, indices) sem duplicatas
                extra = getattr(config, "EXTRA_PAIRS", [])
                if extra:
                    existing = set(scan_coins)
                    for pair in extra:
                        if pair not in existing:
                            scan_coins.append(pair)
                            existing.add(pair)

                # Scan for new trades
                found_entry = False
                for coin in scan_coins:
                    if not self.running:
                        break
                    if coin in coins_with_positions:
                        continue

                    # LEARNING: skip coins with terrible history
                    if self.logger.should_avoid_coin(coin):
                        continue
                    if len(coins_with_positions) >= config.MAX_OPEN_POSITIONS:
                        break

                    print(f"  {C.BOLD}[SCAN] {coin}{C.RESET}", end=" ")

                    # Rate limit protection: small delay between coin scans
                    time.sleep(0.3)

                    # === MULTI-TIMEFRAME ANALYSIS ===

                    # 1. Get 15m bias (optional)
                    bias_15m = "NEUTRAL"
                    if config.REQUIRE_15M_BIAS:
                        bias_15m = self._get_15m_bias(coin)
                        print(f"15m:{bias_15m}", end=" | ")

                    # 2. Get 5m trend
                    trend_5m = self._get_5m_trend(coin)
                    print(f"5m:{trend_5m}", end=" | ")

                    # 3. Get 1m data and run analysis
                    df_1m = self.executor.get_candles(coin, "1m", 120)
                    if df_1m.empty:
                        print(f"{C.RED}No data{C.RESET}")
                        continue

                    # Pass HTF bias to SMC for confluence scoring
                    smc_result = self.smc.analyze(df_1m, htf_bias=bias_15m)
                    ma_result = self.ma.analyze(df_1m)

                    smc_sig = smc_result["signal"]
                    ma_sig = ma_result["signal"]
                    smc_conf = smc_result["confidence"]
                    ma_conf = ma_result["confidence"]

                    color = C.GREEN if smc_sig == "LONG" else C.RED if smc_sig == "SHORT" else C.YELLOW
                    print(f"SMC:{color}{smc_sig}({smc_conf:.2f}){C.RESET} "
                          f"MA:{color}{ma_sig}({ma_conf:.2f}){C.RESET}")

                    # === FILTERS ===

                    # FILTER 1: Strategies must agree OR one must be strong + other neutral
                    if smc_sig == "NEUTRAL" and ma_sig == "NEUTRAL":
                        continue
                    # If they disagree (one LONG, other SHORT), skip
                    if smc_sig != "NEUTRAL" and ma_sig != "NEUTRAL" and smc_sig != ma_sig:
                        continue
                    # Use the non-neutral signal
                    signal = smc_sig if smc_sig != "NEUTRAL" else ma_sig
                    # Override for downstream filters
                    smc_sig = signal

                    # FILTER 2: 5m trend must agree (or be neutral)
                    if config.REQUIRE_5M_TREND and trend_5m != "NEUTRAL" and trend_5m != smc_sig:
                        print(f"    {C.YELLOW}[SKIP] 5m trend ({trend_5m}) opposes signal ({smc_sig}){C.RESET}")
                        continue

                    # FILTER 3: 15m bias must agree (or be neutral)
                    if config.REQUIRE_15M_BIAS and bias_15m != "NEUTRAL" and bias_15m != smc_sig:
                        print(f"    {C.YELLOW}[SKIP] 15m bias ({bias_15m}) opposes signal ({smc_sig}){C.RESET}")
                        continue

                    # FILTER 4: Minimum confidence (use max of the two)
                    avg_conf = max(smc_conf, ma_conf)
                    if avg_conf < config.MIN_CONFIDENCE:
                        print(f"    {C.YELLOW}[SKIP] Low confidence ({avg_conf:.2f} < {config.MIN_CONFIDENCE}){C.RESET}")
                        continue

                    # FILTER 5: Must have OB or FVG near price
                    if config.REQUIRE_OB_OR_FVG:
                        has_ob_fvg = False
                        current_price = self.executor.get_mid_price(coin)
                        for ob in smc_result.get("order_blocks", []):
                            if not ob.get("mitigated"):
                                proximity = abs(current_price - (ob["high"] + ob["low"]) / 2) / current_price
                                if proximity < 0.002:  # Within 0.2%
                                    has_ob_fvg = True
                                    break
                        if not has_ob_fvg:
                            for fvg in smc_result.get("fvgs", []):
                                if not fvg.get("filled"):
                                    if fvg["bottom"] <= current_price <= fvg["top"]:
                                        has_ob_fvg = True
                                        break
                        if not has_ob_fvg:
                            print(f"    {C.YELLOW}[SKIP] No OB/FVG near price{C.RESET}")
                            continue

                    # FILTER 6: Must have structural confirmation
                    if config.REQUIRE_STRUCTURE:
                        has_structure = len(smc_result.get("bos", [])) > 0 or len(smc_result.get("mss", [])) > 0
                        has_sweep = any(s.get("confirmed") for s in smc_result.get("liquidity", []))
                        if not has_structure and not has_sweep:
                            print(f"    {C.YELLOW}[SKIP] No BOS/MSS/confirmed sweep{C.RESET}")
                            continue

                    # FILTER 7: Volume check
                    vol_ratio = ma_result.get("vol_ratio", 1)
                    if vol_ratio < config.MIN_VOLUME_RATIO and avg_conf < 0.7:
                        print(f"    {C.YELLOW}[SKIP] Low volume ({vol_ratio:.1f}x) + moderate conf{C.RESET}")
                        continue

                    # LEARNING: adjust confidence based on coin history
                    hist_adj = self.logger.get_confidence_adjustment(coin)
                    if hist_adj != 1.0:
                        avg_conf = avg_conf * hist_adj
                        print(f"    {C.CYAN}[LEARN] Confidence adjusted x{hist_adj:.1f} (history){C.RESET}")

                    # === ASK GROK ===
                    price = self.executor.get_mid_price(coin)
                    print(f"    {C.MAGENTA}[GROK] Confirming setup...{C.RESET}")
                    grok_decision = self.grok.confirm_trade(
                        coin, smc_result, ma_result, price, balance, trend_5m, bias_15m
                    )

                    action = grok_decision.get("action", "SKIP")

                    # LOG: every signal (approved or rejected)
                    self.logger.log_signal(
                        coin, smc_sig, avg_conf,
                        smc_result["signal"], smc_conf, smc_result.get("details", ""),
                        ma_result["signal"], ma_conf, ma_result.get("details", ""),
                        trend_5m, bias_15m,
                        action, grok_decision.get("confidence", 0),
                        grok_decision.get("reason", ""),
                        price, action != "SKIP"
                    )

                    if action == "SKIP":
                        print(f"    {C.YELLOW}[SKIP] {grok_decision.get('reason', '')}{C.RESET}")
                        continue

                    # === CALCULATE ATR-BASED SL/TP ===
                    sl_pct, tp_pct = self._get_atr_levels(ma_result, price)

                    # === EXECUTE ===
                    leverage = config.LEVERAGE_MAP.get(coin, config.LEVERAGE_MAP_DEFAULT)
                    size_usd = balance * config.MAX_RISK_PER_TRADE * leverage
                    # Cap at 50% of balance * leverage
                    size_usd = min(size_usd, balance * 0.50 * leverage)
                    # Minimo $11 para Hyperliquid
                    size_usd = max(size_usd, 11.0)

                    is_long = action == "LONG"
                    grok_conf = grok_decision.get("confidence", avg_conf)

                    print(f"    {C.GREEN if is_long else C.RED}{C.BOLD}"
                          f"[EXECUTE] {'LONG' if is_long else 'SHORT'} {coin} | "
                          f"${size_usd:.2f} | Conf: {grok_conf:.2f} | "
                          f"SL: {sl_pct*100:.2f}% | TP: {tp_pct*100:.2f}% | "
                          f"5m: {trend_5m} | 15m: {bias_15m}"
                          f"{C.RESET}")

                    # Telegram: signal found
                    self.telegram.signal_found(
                        coin, action, grok_conf, price, sl_pct, tp_pct,
                        smc_result.get("details", ""),
                        ma_result.get("details", ""),
                        trend_5m,
                        grok_decision.get("reason", ""),
                    )

                    # Override executor SL/TP with ATR-based levels
                    original_sl = config.STOP_LOSS_PCT
                    original_tp = config.TAKE_PROFIT_PCT
                    config.STOP_LOSS_PCT = sl_pct
                    config.TAKE_PROFIT_PCT = tp_pct

                    result = self.executor.open_position(coin, is_long, size_usd)

                    # Restore
                    config.STOP_LOSS_PCT = original_sl
                    config.TAKE_PROFIT_PCT = original_tp

                    if result["status"] == "ok":
                        self.trades_taken += 1
                        coins_with_positions.add(coin)
                        found_entry = True
                        self.idle_scans = 0
                        self.telegram.trade_opened(coin, action, size_usd, result['price'], leverage)
                        # LOG: trade opened
                        self.logger.log_trade_open(
                            coin, action, result['price'], size_usd, leverage,
                            sl_pct, tp_pct, smc_conf, ma_conf, grok_conf,
                            smc_result.get("details", ""), trend_5m
                        )
                        print(f"    {C.GREEN}[OK] @ ${result['price']:.2f} | "
                              f"SL: ${result['price'] * (1 - sl_pct if is_long else 1 + sl_pct):.2f} | "
                              f"TP: ${result['price'] * (1 + tp_pct if is_long else 1 - tp_pct):.2f}{C.RESET}")
                    else:
                        print(f"    {C.RED}[FAIL] {result.get('msg', '')}{C.RESET}")

                    time.sleep(2)

                # === MM FALLBACK: When no futures entries found ===
                if not found_entry:
                    self.idle_scans += 1

                if config.MM_FALLBACK_ENABLED and self.idle_scans >= config.MM_FALLBACK_AFTER_SCANS:
                    self._run_mm_cycle(f"idle-fallback (scan #{self.idle_scans})")
                    if config.MM_AGGRESSIVE_ON_IDLE and self.idle_scans >= config.MM_FALLBACK_AFTER_SCANS * 2:
                        print(f"  {C.CYAN}[MM] Aggressive mode: extra MM cycle{C.RESET}")
                        time.sleep(2)
                        self._run_mm_cycle("aggressive-idle")

                # Regular scheduled MM
                elif self.mm and config.MM_ENABLED:
                    now_ts = time.time()
                    if now_ts - self.last_mm_refresh >= config.MM_REFRESH_INTERVAL:
                        self._run_mm_cycle("scheduled")

                # Arbitrum LP management (master + followers)
                if self.arb_lp and getattr(config, 'ARB_LP_ENABLED', False):
                    now_ts = time.time()
                    if now_ts - self.last_arb_lp_refresh >= getattr(config, 'ARB_LP_REFRESH_INTERVAL', 300):
                        self._run_arb_lp_cycle("scheduled")
                        # Sync LP to followers
                        try:
                            self.copy_manager.sync_lp_all_followers()
                        except Exception as e:
                            print(f"  {C.RED}[COPY-LP] Sync error: {e}{C.RESET}")

                time.sleep(config.SCAN_INTERVAL)

            except KeyboardInterrupt:
                self._shutdown()
            except Exception as e:
                print(f"\n{C.RED}[ERROR] {e}{C.RESET}")
                time.sleep(15)

    def _close_all_positions(self):
        """Close all open positions."""
        positions = self.executor.get_open_positions()
        for pos in positions:
            print(f"  [CLOSING] {pos['coin']}...")
            self.executor.close_position(pos["coin"])
        print(f"{C.GREEN}All positions closed.{C.RESET}")

    def _shutdown(self, *args):
        """Graceful shutdown."""
        print(f"\n{C.YELLOW}{C.BOLD}[SHUTDOWN] Closing positions and stopping...{C.RESET}")
        self.running = False
        if self.arb_lp:
            print(f"  [ARB-LP] Removing liquidity positions...")
            self.arb_lp.shutdown()
            print(f"  [COPY-LP] Removing follower LP positions...")
            self.copy_manager.shutdown_all_follower_lps()
        if self.mm:
            print(f"  [MM] Cancelling all spot orders...")
            self.mm.cancel_all_orders()
        self._close_all_positions()

        balance = self.executor.get_balance()
        pnl = balance - self.start_balance
        pnl_pct = (pnl / self.start_balance * 100) if self.start_balance > 0 else 0

        print(f"\n{C.BOLD}{'='*50}")
        print(f"  SESSION SUMMARY (v3 Premium SMC)")
        print(f"{'='*50}")
        print(f"  Start Balance:  ${self.start_balance:.2f}")
        print(f"  Final Balance:  ${balance:.2f}")
        print(f"  PnL:            {C.GREEN if pnl >= 0 else C.RED}${pnl:+.2f} ({pnl_pct:+.1f}%){C.BOLD}")
        print(f"  Trades:         {self.trades_taken}")
        print(f"  Wins/Losses:    {self.wins}/{self.losses}")
        wr = (self.wins / self.trades_taken * 100) if self.trades_taken > 0 else 0
        print(f"  Win Rate:       {wr:.1f}%")
        print(f"  Total Withdrawn: ${self.total_withdrawn:.2f}")
        print(f"{'='*50}{C.RESET}")
        self.telegram.shutdown(balance, pnl, self.wins, self.losses, self.total_withdrawn)
        sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "start" and sys.argv[2] == "money":
        bot = CypherGrokTradeBot()
        bot.start()
    elif len(sys.argv) >= 2 and sys.argv[1] == "status":
        executor = HyperliquidExecutor()
        balance = executor.get_balance()
        positions = executor.get_open_positions()
        print(f"Balance: ${balance:.2f}")
        print(f"Open positions: {len(positions)}")
        for p in positions:
            print(f"  {p['coin']}: {p['size']} @ ${p['entry_price']:.2f} | PnL: ${p['unrealized_pnl']:.2f}")
    else:
        print("Usage:")
        print("  python3 bot.py start money   - Start the trading bot")
        print("  python3 bot.py status        - Check account status")
