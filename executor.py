"""
CypherGrokTrade - Hyperliquid Trade Executor
Handles order placement, position management, and market data fetching.
"""

import time
import pandas as pd
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
import config


class HyperliquidExecutor:
    def __init__(self):
        self.account = Account.from_key(config.HL_PRIVATE_KEY)
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(
            self.account,
            constants.MAINNET_API_URL,
            vault_address=None,
            account_address=config.HL_WALLET_ADDRESS,
        )
        self.positions = {}
        self._coin_cache = []
        self._coin_cache_time = 0

    def get_balance(self) -> float:
        """Get current total equity (perp + spot USDC)."""
        try:
            perp_state = self.info.user_state(config.HL_WALLET_ADDRESS)
            perp_val = float(perp_state["marginSummary"]["accountValue"])

            spot_state = self.info.spot_user_state(config.HL_WALLET_ADDRESS)
            spot_val = 0.0
            for bal in spot_state.get("balances", []):
                if bal["coin"] == "USDC":
                    spot_val = float(bal["total"])
                    break

            return perp_val + spot_val
        except Exception as e:
            print(f"[EXECUTOR] Error fetching balance: {e}")
            return 0.0

    def ensure_perp_balance(self):
        """Move any idle Spot USDC to Perp for trading."""
        try:
            spot_state = self.info.spot_user_state(config.HL_WALLET_ADDRESS)
            spot_usdc = 0.0
            for bal in spot_state.get("balances", []):
                if bal["coin"] == "USDC":
                    spot_usdc = float(bal["total"])
                    break

            if spot_usdc > 0.5:
                result = self.exchange.usd_class_transfer(spot_usdc, True)
                if result.get("status") == "ok":
                    print(f"[EXECUTOR] Moved ${spot_usdc:.2f} from Spot to Perp")
                return spot_usdc
            return 0.0
        except Exception as e:
            print(f"[EXECUTOR] Error moving funds to perp: {e}")
            return 0.0

    def _get_sz_decimals(self, coin: str) -> int:
        """Get size decimal places for a coin from Hyperliquid meta."""
        try:
            meta = self.info.meta()
            for u in meta.get("universe", []):
                if u["name"] == coin:
                    return u.get("szDecimals", 2)
            return 2
        except:
            return 2

    def get_open_positions(self) -> list:
        """Get all open positions."""
        try:
            state = self.info.user_state(config.HL_WALLET_ADDRESS)
            positions = []
            for pos in state.get("assetPositions", []):
                p = pos["position"]
                if float(p["szi"]) != 0:
                    positions.append({
                        "coin": p["coin"],
                        "size": float(p["szi"]),
                        "entry_price": float(p["entryPx"]),
                        "unrealized_pnl": float(p["unrealizedPnl"]),
                        "liquidation_px": float(p.get("liquidationPx", 0) or 0),
                        "leverage": int(p["leverage"]["value"]),
                    })
            return positions
        except Exception as e:
            print(f"[EXECUTOR] Error fetching positions: {e}")
            return []

    def get_candles(self, coin: str, interval: str = "1m", count: int = 100) -> pd.DataFrame:
        """Fetch OHLCV candle data."""
        try:
            end_time = int(time.time() * 1000)
            # For 1m candles, go back count minutes
            interval_ms = {"1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000}
            ms = interval_ms.get(interval, 60000)
            start_time = end_time - (count * ms)

            candles = self.info.candles_snapshot(coin, interval, start_time, end_time)

            if not candles:
                return pd.DataFrame()

            df = pd.DataFrame(candles)
            df = df.rename(columns={
                "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"
            })
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
        except Exception as e:
            print(f"[EXECUTOR] Error fetching candles for {coin}: {e}")
            return pd.DataFrame()

    def get_top_coins(self, count: int = 20, min_volume: float = 1_000_000) -> list:
        """Get top perp coins by 24h volume. Cached for 5 minutes."""
        now = time.time()
        if self._coin_cache and now - self._coin_cache_time < 300:
            return self._coin_cache

        try:
            ctxs = self.info.meta_and_asset_ctxs()
            meta = ctxs[0]
            asset_ctxs = ctxs[1] if len(ctxs) > 1 else []
            universe = meta.get("universe", [])

            coins = []
            for i, u in enumerate(universe):
                name = u["name"]
                if i < len(asset_ctxs):
                    vol = float(asset_ctxs[i].get("dayNtlVlm", 0))
                    if vol >= min_volume:
                        coins.append((name, vol))

            coins.sort(key=lambda x: x[1], reverse=True)
            result = [c[0] for c in coins[:count]]
            self._coin_cache = result
            self._coin_cache_time = now
            return result
        except Exception as e:
            print(f"[EXECUTOR] Error fetching top coins: {e}")
            return self._coin_cache or ["BTC", "ETH", "SOL"]

    def get_mid_price(self, coin: str) -> float:
        """Get current mid price for a coin."""
        try:
            mids = self.info.all_mids()
            return float(mids.get(coin, 0))
        except Exception as e:
            print(f"[EXECUTOR] Error fetching price for {coin}: {e}")
            return 0.0

    def set_leverage(self, coin: str, leverage: int):
        """Set leverage for a coin."""
        try:
            result = self.exchange.update_leverage(leverage, coin, is_cross=True)
            print(f"[EXECUTOR] Set {coin} leverage to {leverage}x: {result}")
            return result
        except Exception as e:
            print(f"[EXECUTOR] Error setting leverage for {coin}: {e}")
            return None

    def open_position(self, coin: str, is_long: bool, size_usd: float) -> dict:
        """Open a market position."""
        try:
            price = self.get_mid_price(coin)
            if price <= 0:
                return {"status": "error", "msg": f"Invalid price for {coin}"}

            # Hyperliquid minimum order is $10 notional
            size_usd = max(size_usd, 11.0)

            # Get size decimals from meta for this coin
            sz_decimals = self._get_sz_decimals(coin)
            sz = round(size_usd / price, sz_decimals)

            # Ensure minimum notional $11
            min_sz = round(11.0 / price, sz_decimals)
            if min_sz == 0:
                min_sz = 10 ** (-sz_decimals) if sz_decimals > 0 else 1
            sz = max(sz, min_sz)

            leverage = config.LEVERAGE_MAP.get(coin, config.LEVERAGE)
            self.set_leverage(coin, leverage)

            result = self.exchange.market_open(
                coin, is_buy=is_long, sz=sz, slippage=0.01
            )

            if result.get("status") == "ok":
                fill_data = result.get("response", {}).get("data", {})
                statuses = fill_data.get("statuses", [{}])
                filled_info = statuses[0] if statuses else {}

                # Check if order actually filled (not just accepted)
                if isinstance(filled_info, dict) and "error" in filled_info:
                    print(f"[EXECUTOR] Order rejected: {filled_info['error']}")
                    return {"status": "error", "msg": filled_info["error"]}

                self.positions[coin] = {
                    "side": "LONG" if is_long else "SHORT",
                    "size": sz,
                    "entry_price": price,
                    "open_time": time.time(),
                    "sl": price * (1 - config.STOP_LOSS_PCT) if is_long else price * (1 + config.STOP_LOSS_PCT),
                    "tp": price * (1 + config.TAKE_PROFIT_PCT) if is_long else price * (1 - config.TAKE_PROFIT_PCT),
                }

                print(f"[EXECUTOR] OPENED {'LONG' if is_long else 'SHORT'} {coin} | "
                      f"Size: {sz} | Price: {price:.2f} | "
                      f"SL: {self.positions[coin]['sl']:.2f} | TP: {self.positions[coin]['tp']:.2f}")

                return {"status": "ok", "coin": coin, "side": "LONG" if is_long else "SHORT",
                        "size": sz, "price": price, "result": result}
            else:
                print(f"[EXECUTOR] Failed to open {coin}: {result}")
                return {"status": "error", "msg": str(result)}

        except Exception as e:
            print(f"[EXECUTOR] Error opening position for {coin}: {e}")
            return {"status": "error", "msg": str(e)}

    def close_position(self, coin: str) -> dict:
        """Close a position at market."""
        try:
            result = self.exchange.market_close(coin, slippage=0.01)
            if coin in self.positions:
                del self.positions[coin]
            print(f"[EXECUTOR] CLOSED {coin}: {result}")
            return {"status": "ok", "coin": coin, "result": result}
        except Exception as e:
            print(f"[EXECUTOR] Error closing {coin}: {e}")
            return {"status": "error", "msg": str(e)}

    def check_sl_tp(self) -> list:
        """Check if any position hit SL or TP. Returns list of coins to close."""
        to_close = []
        for coin, pos in list(self.positions.items()):
            price = self.get_mid_price(coin)
            if price <= 0:
                continue

            if pos["side"] == "LONG":
                if price <= pos["sl"]:
                    print(f"[SL HIT] {coin} LONG | Entry: {pos['entry_price']:.2f} | Current: {price:.2f}")
                    to_close.append(coin)
                elif price >= pos["tp"]:
                    print(f"[TP HIT] {coin} LONG | Entry: {pos['entry_price']:.2f} | Current: {price:.2f}")
                    to_close.append(coin)
                else:
                    # Trailing stop: move SL up if price moved favorably
                    new_sl = price * (1 - config.TRAILING_STOP_PCT)
                    if new_sl > pos["sl"]:
                        pos["sl"] = new_sl
            else:  # SHORT
                if price >= pos["sl"]:
                    print(f"[SL HIT] {coin} SHORT | Entry: {pos['entry_price']:.2f} | Current: {price:.2f}")
                    to_close.append(coin)
                elif price <= pos["tp"]:
                    print(f"[TP HIT] {coin} SHORT | Entry: {pos['entry_price']:.2f} | Current: {price:.2f}")
                    to_close.append(coin)
                else:
                    # Trailing stop: move SL down
                    new_sl = price * (1 + config.TRAILING_STOP_PCT)
                    if new_sl < pos["sl"]:
                        pos["sl"] = new_sl

        return to_close
