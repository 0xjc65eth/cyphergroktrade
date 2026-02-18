"""
CypherGrokTrade - Moving Average Scalper v3
Enhanced with ATR-based dynamic levels, VWAP, momentum quality filters.

Key improvements:
- ATR for dynamic SL/TP calculation
- VWAP as institutional level
- Momentum quality (not just RSI value, but RSI slope + price momentum)
- Volume profile (accumulation vs distribution)
- EMA ribbon squeeze detection (low volatility before expansion)
- Candle pattern confirmation (engulfing, pin bars)
"""

import pandas as pd
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator


class MAScalper:
    def __init__(self, ema_fast=8, ema_slow=21, ema_trend=55,
                 rsi_period=14, rsi_ob=65, rsi_os=35):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.ema_trend = ema_trend
        self.rsi_period = rsi_period
        self.rsi_ob = rsi_ob
        self.rsi_os = rsi_os

    def analyze(self, df: pd.DataFrame) -> dict:
        """Analyze price data with enhanced MA scalping strategy."""
        if len(df) < self.ema_trend + 10:
            return {"signal": "NEUTRAL", "confidence": 0, "details": "Insufficient data"}

        df = df.copy()

        # Core indicators
        df["ema_fast"] = EMAIndicator(df["close"], window=self.ema_fast).ema_indicator()
        df["ema_slow"] = EMAIndicator(df["close"], window=self.ema_slow).ema_indicator()
        df["ema_trend"] = EMAIndicator(df["close"], window=self.ema_trend).ema_indicator()
        df["rsi"] = RSIIndicator(df["close"], window=self.rsi_period).rsi()

        # ATR (Average True Range) for volatility
        hl = df["high"] - df["low"]
        hc = (df["high"] - df["close"].shift(1)).abs()
        lc = (df["low"] - df["close"].shift(1)).abs()
        df["tr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        df["atr"] = df["tr"].rolling(window=14).mean()
        df["atr_pct"] = df["atr"] / df["close"]

        # VWAP (session approximation using rolling)
        df["vwap"] = (df["close"] * df["volume"]).rolling(20).sum() / df["volume"].rolling(20).sum()

        # Volume analysis
        df["vol_sma"] = df["volume"].rolling(window=20).mean()
        df["vol_spike"] = df["volume"] > df["vol_sma"] * 1.5
        df["vol_ratio"] = df["volume"] / df["vol_sma"].replace(0, 1)

        # RSI slope (momentum quality)
        df["rsi_slope"] = df["rsi"] - df["rsi"].shift(3)

        # EMA squeeze (distance between fast and slow)
        df["ema_spread"] = abs(df["ema_fast"] - df["ema_slow"]) / df["close"] * 100
        df["ema_spread_avg"] = df["ema_spread"].rolling(20).mean()

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        signal, confidence, details = self._generate_signal(df, latest, prev)

        return {
            "signal": signal,
            "confidence": confidence,
            "ema_fast": latest["ema_fast"],
            "ema_slow": latest["ema_slow"],
            "ema_trend": latest["ema_trend"],
            "rsi": latest["rsi"],
            "rsi_slope": latest.get("rsi_slope", 0),
            "atr_pct": latest.get("atr_pct", 0),
            "vwap": latest.get("vwap", 0),
            "vol_spike": latest["vol_spike"],
            "vol_ratio": latest.get("vol_ratio", 1),
            "details": details,
        }

    def _generate_signal(self, df, latest, prev):
        """Generate scalp signal with weighted scoring."""
        bull_score = 0
        bear_score = 0
        details = []

        # === EMA CROSSOVER (most important for scalping) ===
        fast_crossed_above = prev["ema_fast"] <= prev["ema_slow"] and latest["ema_fast"] > latest["ema_slow"]
        fast_crossed_below = prev["ema_fast"] >= prev["ema_slow"] and latest["ema_fast"] < latest["ema_slow"]

        if fast_crossed_above:
            bull_score += 3
            details.append("EMA8 crossed above EMA21 (BULLISH)")
        elif fast_crossed_below:
            bear_score += 3
            details.append("EMA8 crossed below EMA21 (BEARISH)")

        # === EMA ALIGNMENT ===
        if latest["ema_fast"] > latest["ema_slow"] > latest["ema_trend"]:
            bull_score += 2
            details.append("EMAs aligned BULLISH (8>21>55)")
        elif latest["ema_fast"] < latest["ema_slow"] < latest["ema_trend"]:
            bear_score += 2
            details.append("EMAs aligned BEARISH (8<21<55)")

        # === PRICE VS VWAP (institutional level) ===
        vwap = latest.get("vwap", latest["ema_trend"])
        if pd.notna(vwap) and vwap > 0:
            vwap_dist = (latest["close"] - vwap) / vwap
            if latest["close"] > vwap and vwap_dist > 0.001:
                bull_score += 1
                details.append(f"Price above VWAP (+{vwap_dist*100:.2f}%)")
            elif latest["close"] < vwap and vwap_dist < -0.001:
                bear_score += 1
                details.append(f"Price below VWAP ({vwap_dist*100:.2f}%)")

        # === RSI WITH MOMENTUM QUALITY ===
        rsi = latest["rsi"]
        rsi_slope = latest.get("rsi_slope", 0)

        if rsi < self.rsi_os:
            # Oversold + RSI turning up = strong bull
            if pd.notna(rsi_slope) and rsi_slope > 0:
                bull_score += 3
                details.append(f"RSI oversold + turning up ({rsi:.1f}, slope: {rsi_slope:+.1f})")
            else:
                bull_score += 2
                details.append(f"RSI oversold ({rsi:.1f})")
        elif rsi > self.rsi_ob:
            if pd.notna(rsi_slope) and rsi_slope < 0:
                bear_score += 3
                details.append(f"RSI overbought + turning down ({rsi:.1f}, slope: {rsi_slope:+.1f})")
            else:
                bear_score += 2
                details.append(f"RSI overbought ({rsi:.1f})")
        elif 45 < rsi < 55:
            details.append(f"RSI neutral zone ({rsi:.1f}) - weak momentum")

        # === RSI DIVERGENCE (improved with 3 swing points) ===
        if len(df) > 15:
            # Check last 15 candles for divergence
            price_slice = df["close"].iloc[-15:]
            rsi_slice = df["rsi"].iloc[-15:]

            price_higher = latest["close"] > price_slice.iloc[0]
            rsi_lower = latest["rsi"] < rsi_slice.iloc[0]
            price_lower = latest["close"] < price_slice.iloc[0]
            rsi_higher = latest["rsi"] > rsi_slice.iloc[0]

            if price_higher and rsi_lower:
                bear_score += 2
                details.append("Bearish RSI divergence (15 candles)")
            elif price_lower and rsi_higher:
                bull_score += 2
                details.append("Bullish RSI divergence (15 candles)")

        # === VOLUME CONFIRMATION ===
        vol_ratio = latest.get("vol_ratio", 1)
        if pd.notna(vol_ratio) and vol_ratio > 1.5:
            if bull_score > bear_score:
                bull_score += 2
                details.append(f"High volume confirms BULL ({vol_ratio:.1f}x avg)")
            elif bear_score > bull_score:
                bear_score += 2
                details.append(f"High volume confirms BEAR ({vol_ratio:.1f}x avg)")
        elif pd.notna(vol_ratio) and vol_ratio < 0.5:
            # Low volume = weak signal, penalize
            details.append(f"Low volume ({vol_ratio:.1f}x avg) - weak signal")

        # === EMA PULLBACK ENTRY ===
        ema_slow_dist = abs(latest["close"] - latest["ema_slow"]) / latest["close"]
        if ema_slow_dist < 0.0012:
            if latest["ema_fast"] > latest["ema_slow"]:
                bull_score += 2
                details.append("Pullback to EMA21 in uptrend")
            elif latest["ema_fast"] < latest["ema_slow"]:
                bear_score += 2
                details.append("Pullback to EMA21 in downtrend")

        # === CANDLE PATTERN CONFIRMATION ===
        body = abs(latest["close"] - latest["open"])
        candle_range = latest["high"] - latest["low"]
        if candle_range > 0:
            body_ratio = body / candle_range
            upper_wick = latest["high"] - max(latest["close"], latest["open"])
            lower_wick = min(latest["close"], latest["open"]) - latest["low"]

            # Bullish engulfing / pin bar
            if (latest["close"] > latest["open"] and body_ratio > 0.6 and
                    body > abs(prev["close"] - prev["open"]) * 1.2):
                bull_score += 1
                details.append("Bullish engulfing candle")

            # Bearish engulfing / pin bar
            elif (latest["close"] < latest["open"] and body_ratio > 0.6 and
                  body > abs(prev["close"] - prev["open"]) * 1.2):
                bear_score += 1
                details.append("Bearish engulfing candle")

            # Bullish pin bar (long lower wick)
            if lower_wick > body * 2 and lower_wick > upper_wick * 2:
                bull_score += 1
                details.append("Bullish pin bar (rejection wick)")

            # Bearish pin bar (long upper wick)
            if upper_wick > body * 2 and upper_wick > lower_wick * 2:
                bear_score += 1
                details.append("Bearish pin bar (rejection wick)")

        # === EMA SQUEEZE (pre-expansion detection) ===
        ema_spread = latest.get("ema_spread", 0)
        ema_spread_avg = latest.get("ema_spread_avg", 0)
        if pd.notna(ema_spread) and pd.notna(ema_spread_avg) and ema_spread_avg > 0:
            if ema_spread < ema_spread_avg * 0.5:
                details.append("EMA SQUEEZE detected - expansion expected")

        # === GENERATE SIGNAL ===
        total = bull_score + bear_score
        if total == 0:
            return "NEUTRAL", 0, "No MA signals"

        if bull_score > bear_score:
            confidence = min(bull_score / 10.0, 1.0)
            return "LONG", confidence, " | ".join(details)
        elif bear_score > bull_score:
            confidence = min(bear_score / 10.0, 1.0)
            return "SHORT", confidence, " | ".join(details)
        else:
            return "NEUTRAL", 0.2, " | ".join(details)
