"""
CypherGrokTrade - Grok AI Integration v3
Ultra-strict filtering for premium SMC setups only.
"""

import json
import requests
import config


class GrokAI:
    def __init__(self):
        self.api_url = config.GROK_API_URL
        self.api_key = config.GROK_API_KEY
        self.model = config.GROK_MODEL

    def confirm_trade(self, coin: str, smc_analysis: dict, ma_analysis: dict,
                      current_price: float, balance: float,
                      trend_5m: str = "NEUTRAL", bias_15m: str = "NEUTRAL") -> dict:
        """Ask Grok to confirm or reject a trade setup with premium SMC context."""

        # Build detailed SMC context
        smc_details = smc_analysis.get("details", "N/A")
        ob_info = ""
        fvg_info = ""
        sweep_info = ""
        mss_info = ""

        for ob in smc_analysis.get("order_blocks", []):
            if not ob.get("mitigated"):
                ob_info += f"\n  - {ob['type']} [{ob['low']:.2f}-{ob['high']:.2f}] (strength: {ob['strength']:.4f})"

        for fvg in smc_analysis.get("fvgs", []):
            if not fvg.get("filled"):
                fvg_info += f"\n  - {fvg['type']} [{fvg['bottom']:.2f}-{fvg['top']:.2f}] (unfilled)"

        for sweep in smc_analysis.get("liquidity", []):
            sweep_info += f"\n  - {sweep['type']} at {sweep['level']:.2f} ({'CONFIRMED' if sweep.get('confirmed') else 'pending'})"

        for m in smc_analysis.get("mss", []):
            mss_info += f"\n  - {m['type']} at {m['level']:.2f} (displacement: {m.get('displacement')})"

        # Detect contradictions in the SMC data
        has_bull_signals = any(x for x in [ob_info, fvg_info, sweep_info, mss_info]
                              if "BULLISH" in x)
        has_bear_signals = any(x for x in [ob_info, fvg_info, sweep_info, mss_info]
                              if "BEARISH" in x)
        contradiction_warning = ""
        if has_bull_signals and has_bear_signals:
            contradiction_warning = "\nWARNING: Mixed bullish AND bearish signals detected. This is a CONTRADICTION - you should SKIP."

        prompt = f"""You are a crypto trade evaluator. Be selective but not overly cautious.
We need to make $3/day with aggressive but smart entries. Approve good setups, reject trash.

COIN: {coin} | PRICE: ${current_price:.2f} | BAL: ${balance:.2f}
5M TREND: {trend_5m} | 15M BIAS: {bias_15m}
{contradiction_warning}

SMC: {smc_analysis['signal']} (conf: {smc_analysis['confidence']:.2f}) | Trend: {smc_analysis.get('trend', 'N/A')}
Details: {smc_details}
OBs: {ob_info or 'None'} | FVGs: {fvg_info or 'None'}
Sweeps: {sweep_info or 'None'} | MSS: {mss_info or 'None'}

MA: {ma_analysis['signal']} (conf: {ma_analysis['confidence']:.2f})
RSI: {ma_analysis.get('rsi', 'N/A')} | Vol: {ma_analysis.get('vol_ratio', 'N/A')}x avg

=== HARD SKIP (any one = SKIP) ===
1. RSI > 72 for LONG or RSI < 28 for SHORT (chasing extremes)
2. SMC and MA disagree on direction (one LONG, other SHORT)
3. Mixed bullish AND bearish SMC signals (contradiction)
4. SMC confidence < 0.5 AND MA confidence < 0.5 (no signal)
5. 5M trend OPPOSES signal direction

=== APPROVE IF ===
- Engines agree (or one strong + other neutral)
- At least 1 OB or FVG supports the entry
- Confidence >= 0.6 from at least one engine
- No contradictory signals
- Volume reasonable (>1.0x avg)

Give higher confidence (0.8+) when: confirmed sweep + OB, MSS with displacement, or 3+ factors align.
Give moderate confidence (0.65-0.79) when: 2 factors agree with trend.

Respond ONLY with valid JSON:
{{"action": "LONG" or "SHORT" or "SKIP", "confidence": 0.0-1.0, "reason": "brief reason"}}"""

        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                json={
                    "messages": [
                        {"role": "system", "content": "You are a crypto trade evaluator. Approve GOOD setups with confluence, reject obvious trash. We need 40%+ win rate with 2:1 R:R. Be selective but not scared - missing good setups is also costly. When engines agree with OB/FVG support, approve confidently. Respond only with valid JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    "model": self.model,
                    "stream": False,
                    "temperature": 0.02,  # Even more deterministic
                },
                timeout=15,
            )

            if response.status_code != 200:
                print(f"[GROK] API error {response.status_code}: {response.text[:200]}")
                return self._fallback_decision(smc_analysis, ma_analysis, balance, trend_5m, bias_15m)

            data = response.json()
            content = data["choices"][0]["message"]["content"].strip()

            # Clean potential markdown wrapping
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

            result = json.loads(content)
            action = result.get("action", "SKIP")
            conf = result.get("confidence", 0)
            reason = result.get("reason", "N/A")

            print(f"[GROK] Decision: {action} | Confidence: {conf} | Reason: {reason}")

            # Extra safety: reject low confidence
            if action != "SKIP" and conf < 0.65:
                print(f"[GROK] Overriding to SKIP (confidence {conf} < 0.65)")
                return {"action": "SKIP", "confidence": conf, "reason": f"Low Grok confidence: {reason}"}

            return result

        except json.JSONDecodeError as e:
            print(f"[GROK] JSON parse error: {e}")
            return self._fallback_decision(smc_analysis, ma_analysis, balance, trend_5m, bias_15m)
        except Exception as e:
            print(f"[GROK] Error: {e}")
            return self._fallback_decision(smc_analysis, ma_analysis, balance, trend_5m, bias_15m)

    def _fallback_decision(self, smc_analysis: dict, ma_analysis: dict,
                           balance: float, trend_5m: str = "NEUTRAL",
                           bias_15m: str = "NEUTRAL") -> dict:
        """Fallback when Grok API is unavailable.
        The signal already passed 7 filters in bot.py before reaching here,
        so we trust the setup if SMC+MA agree or one is strong enough."""
        smc_sig = smc_analysis["signal"]
        ma_sig = ma_analysis["signal"]
        smc_conf = smc_analysis["confidence"]
        ma_conf = ma_analysis["confidence"]

        # Determine direction: use the non-neutral signal
        if smc_sig in ("LONG", "SHORT") and ma_sig in ("LONG", "SHORT"):
            if smc_sig != ma_sig:
                return {"action": "SKIP", "confidence": 0, "reason": "Fallback: SMC/MA disagree"}
            direction = smc_sig
        elif smc_sig in ("LONG", "SHORT"):
            direction = smc_sig
        elif ma_sig in ("LONG", "SHORT"):
            direction = ma_sig
        else:
            return {"action": "SKIP", "confidence": 0, "reason": "Fallback: no direction"}

        # 5m trend must not oppose
        if trend_5m != "NEUTRAL" and trend_5m != direction:
            return {"action": "SKIP", "confidence": 0, "reason": f"Fallback: 5m trend {trend_5m} opposes {direction}"}

        # Need reasonable confidence from at least one engine
        best_conf = max(smc_conf, ma_conf)
        if best_conf < config.MIN_CONFIDENCE:
            return {"action": "SKIP", "confidence": 0, "reason": f"Fallback: low conf {best_conf:.2f}"}

        # Need OB or FVG support
        has_ob_fvg = False
        for ob in smc_analysis.get("order_blocks", []):
            if not ob.get("mitigated"):
                has_ob_fvg = True
                break
        if not has_ob_fvg:
            for fvg in smc_analysis.get("fvgs", []):
                if not fvg.get("filled"):
                    has_ob_fvg = True
                    break

        if not has_ob_fvg:
            return {"action": "SKIP", "confidence": 0, "reason": "Fallback: no OB/FVG"}

        return {
            "action": direction,
            "confidence": best_conf * 0.9,  # Slight discount vs Grok-confirmed
            "reason": f"Fallback: {direction} conf={best_conf:.2f} (Grok offline)",
        }

    def get_market_sentiment(self, coin: str) -> str:
        """Quick sentiment check from Grok."""
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                json={
                    "messages": [
                        {"role": "system", "content": "You are a crypto market analyst. Be very brief."},
                        {"role": "user", "content": f"In 1-2 sentences, what's the current sentiment and key level for {coin}? Just the key info."},
                    ],
                    "model": self.model,
                    "stream": False,
                    "temperature": 0.3,
                },
                timeout=10,
            )
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"].strip()
            return "Sentiment unavailable"
        except Exception:
            return "Sentiment unavailable"
