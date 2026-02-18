"""
CypherGrokTrade - Trade Logger & Learning System
Grava todas as operacoes (sinais, entradas, saidas, PnL) em JSON para analise.
O bot usa o historico para aprender padroes de win/loss e refinar decisoes.
"""

import json
import os
import time
from datetime import datetime


TRADES_FILE = os.path.join(os.path.dirname(__file__), "trades_history.json")
SIGNALS_FILE = os.path.join(os.path.dirname(__file__), "signals_history.json")
STATS_FILE = os.path.join(os.path.dirname(__file__), "learning_stats.json")


class TradeLogger:
    def __init__(self):
        self.trades = self._load(TRADES_FILE)
        self.signals = self._load(SIGNALS_FILE)
        self.stats = self._load_stats()

    def _load(self, path: str) -> list:
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    return json.load(f)
        except:
            pass
        return []

    def _save(self, data, path: str):
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            print(f"[LOGGER] Save error: {e}")

    def _load_stats(self) -> dict:
        try:
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE, "r") as f:
                    return json.load(f)
        except:
            pass
        return {
            "coin_stats": {},       # win rate per coin
            "signal_stats": {},     # win rate per signal type
            "timeframe_stats": {},  # win rate by time of day
            "grok_accuracy": {},    # grok confidence vs actual result
            "avoid_patterns": [],   # patterns that consistently lose
            "prefer_patterns": [],  # patterns that consistently win
        }

    def _save_stats(self):
        self._save(self.stats, STATS_FILE)

    # === SIGNAL LOGGING ===

    def log_signal(self, coin: str, direction: str, confidence: float,
                   smc_signal: str, smc_conf: float, smc_details: str,
                   ma_signal: str, ma_conf: float, ma_details: str,
                   trend_5m: str, bias_15m: str,
                   grok_action: str, grok_conf: float, grok_reason: str,
                   price: float, approved: bool):
        """Log every signal found (approved or rejected)."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "epoch": time.time(),
            "coin": coin,
            "direction": direction,
            "confidence": confidence,
            "price": price,
            "approved": approved,
            "smc": {"signal": smc_signal, "confidence": smc_conf, "details": smc_details[:200]},
            "ma": {"signal": ma_signal, "confidence": ma_conf, "details": ma_details[:200]},
            "trend_5m": trend_5m,
            "bias_15m": bias_15m,
            "grok": {"action": grok_action, "confidence": grok_conf, "reason": grok_reason[:100]},
            "hour": datetime.now().hour,
        }
        self.signals.append(entry)

        # Keep last 1000 signals
        if len(self.signals) > 1000:
            self.signals = self.signals[-1000:]

        self._save(self.signals, SIGNALS_FILE)

    # === TRADE LOGGING ===

    def log_trade_open(self, coin: str, direction: str, entry_price: float,
                       size_usd: float, leverage: int, sl_pct: float, tp_pct: float,
                       smc_conf: float, ma_conf: float, grok_conf: float,
                       smc_details: str, trend_5m: str):
        """Log when a trade is opened."""
        trade = {
            "id": f"{coin}_{int(time.time())}",
            "timestamp_open": datetime.now().isoformat(),
            "epoch_open": time.time(),
            "coin": coin,
            "direction": direction,
            "entry_price": entry_price,
            "size_usd": size_usd,
            "leverage": leverage,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
            "smc_conf": smc_conf,
            "ma_conf": ma_conf,
            "grok_conf": grok_conf,
            "smc_details": smc_details[:200],
            "trend_5m": trend_5m,
            "hour_open": datetime.now().hour,
            # Will be filled on close
            "exit_price": None,
            "pnl": None,
            "result": None,  # "WIN" or "LOSS"
            "duration_seconds": None,
            "timestamp_close": None,
        }
        self.trades.append(trade)
        self._save(self.trades, TRADES_FILE)
        return trade["id"]

    def log_trade_close(self, coin: str, exit_price: float, pnl: float, is_win: bool):
        """Log when a trade is closed. Updates the last open trade for this coin."""
        # Find the last open trade for this coin
        for trade in reversed(self.trades):
            if trade["coin"] == coin and trade["result"] is None:
                trade["exit_price"] = exit_price
                trade["pnl"] = pnl
                trade["result"] = "WIN" if is_win else "LOSS"
                trade["timestamp_close"] = datetime.now().isoformat()
                trade["duration_seconds"] = time.time() - trade["epoch_open"]
                break

        self._save(self.trades, TRADES_FILE)

        # Update learning stats
        self._update_stats(coin, is_win, pnl)

    # === LEARNING STATS ===

    def _update_stats(self, coin: str, is_win: bool, pnl: float):
        """Update cumulative stats for learning."""
        # Per-coin stats
        if coin not in self.stats["coin_stats"]:
            self.stats["coin_stats"][coin] = {"wins": 0, "losses": 0, "total_pnl": 0}
        cs = self.stats["coin_stats"][coin]
        if is_win:
            cs["wins"] += 1
        else:
            cs["losses"] += 1
        cs["total_pnl"] += pnl
        total = cs["wins"] + cs["losses"]
        cs["win_rate"] = cs["wins"] / total if total > 0 else 0

        # Time-of-day stats
        hour = str(datetime.now().hour)
        if hour not in self.stats["timeframe_stats"]:
            self.stats["timeframe_stats"][hour] = {"wins": 0, "losses": 0}
        ts = self.stats["timeframe_stats"][hour]
        if is_win:
            ts["wins"] += 1
        else:
            ts["losses"] += 1
        total_h = ts["wins"] + ts["losses"]
        ts["win_rate"] = ts["wins"] / total_h if total_h > 0 else 0

        # Detect avoid patterns (coins with < 30% win rate after 5+ trades)
        self.stats["avoid_patterns"] = []
        self.stats["prefer_patterns"] = []
        for c, s in self.stats["coin_stats"].items():
            total_trades = s["wins"] + s["losses"]
            if total_trades >= 5:
                if s["win_rate"] < 0.30:
                    self.stats["avoid_patterns"].append({
                        "coin": c, "win_rate": s["win_rate"],
                        "trades": total_trades, "reason": "Low win rate"
                    })
                elif s["win_rate"] >= 0.65:
                    self.stats["prefer_patterns"].append({
                        "coin": c, "win_rate": s["win_rate"],
                        "trades": total_trades, "reason": "High win rate"
                    })

        self._save_stats()

    # === LEARNING QUERIES ===

    def should_avoid_coin(self, coin: str) -> bool:
        """Check if we should avoid this coin based on history."""
        for pattern in self.stats.get("avoid_patterns", []):
            if pattern["coin"] == coin:
                return True
        return False

    def is_preferred_coin(self, coin: str) -> bool:
        """Check if this coin has good history."""
        for pattern in self.stats.get("prefer_patterns", []):
            if pattern["coin"] == coin:
                return True
        return False

    def get_coin_win_rate(self, coin: str) -> float:
        """Get historical win rate for a coin. Returns -1 if no data."""
        cs = self.stats.get("coin_stats", {}).get(coin)
        if cs:
            total = cs["wins"] + cs["losses"]
            if total >= 3:
                return cs["win_rate"]
        return -1

    def get_confidence_adjustment(self, coin: str) -> float:
        """Returns a multiplier (0.5-1.5) to adjust confidence based on history."""
        wr = self.get_coin_win_rate(coin)
        if wr < 0:
            return 1.0  # No data, no adjustment
        if wr >= 0.7:
            return 1.3  # Boost confidence for winning coins
        elif wr >= 0.5:
            return 1.1
        elif wr >= 0.3:
            return 0.8  # Reduce confidence for losing coins
        else:
            return 0.5  # Strong penalty for bad coins

    def get_best_hours(self) -> list:
        """Get hours with best win rates."""
        good_hours = []
        for hour, s in self.stats.get("timeframe_stats", {}).items():
            total = s["wins"] + s["losses"]
            if total >= 3 and s["win_rate"] >= 0.6:
                good_hours.append(int(hour))
        return good_hours

    def get_summary(self) -> str:
        """Get a readable summary of learning stats."""
        total_trades = len([t for t in self.trades if t.get("result")])
        wins = len([t for t in self.trades if t.get("result") == "WIN"])
        losses = len([t for t in self.trades if t.get("result") == "LOSS"])
        total_pnl = sum(t.get("pnl", 0) for t in self.trades if t.get("pnl") is not None)
        wr = (wins / total_trades * 100) if total_trades > 0 else 0

        avoid = ", ".join(p["coin"] for p in self.stats.get("avoid_patterns", []))
        prefer = ", ".join(p["coin"] for p in self.stats.get("prefer_patterns", []))

        return (
            f"Total: {total_trades} trades | WR: {wr:.0f}% ({wins}W/{losses}L) | PnL: ${total_pnl:.2f}\n"
            f"Avoid: {avoid or 'none yet'}\n"
            f"Prefer: {prefer or 'none yet'}"
        )
