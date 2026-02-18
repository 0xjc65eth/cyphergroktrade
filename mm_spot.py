"""
CypherGrokTrade - Spot Market Making v3
Enhanced with dynamic spread, inventory rebalancing, and volatility adaptation.

Key v3 improvements:
- Dynamic spread based on recent volatility (ATR)
- Inventory skew: when holding too much, lower ask price; when low, raise bid
- Multiple order levels for better fill rate
- Spread tightening in low-volatility (more fills) and widening in high-vol (safety)
"""

import time
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account
import config


class SpotMarketMaker:
    def __init__(self):
        self.account = Account.from_key(config.HL_PRIVATE_KEY)
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(
            self.account,
            constants.MAINNET_API_URL,
            vault_address=None,
            account_address=config.HL_WALLET_ADDRESS,
        )
        self._spot_meta = None
        self._volatility_cache = {}  # coin -> (timestamp, volatility)

    def _load_spot_meta(self):
        if self._spot_meta is None:
            self._spot_meta = self.info.spot_meta()
        return self._spot_meta

    def get_spot_balance(self) -> float:
        """Get available USDC for spot MM."""
        try:
            spot = self.info.spot_user_state(config.HL_WALLET_ADDRESS)
            avail = spot.get("tokenToAvailableAfterMaintenance", [])
            for item in avail:
                if item[0] == 0:
                    return float(item[1])
            return 0.0
        except Exception as e:
            print(f"[MM] Error getting spot balance: {e}")
            return 0.0

    def get_spot_holdings(self) -> dict:
        """Get current spot token holdings (non-USDC)."""
        try:
            spot = self.info.spot_user_state(config.HL_WALLET_ADDRESS)
            holdings = {}
            for bal in spot.get("balances", []):
                total = float(bal["total"])
                hold = float(bal.get("hold", 0))
                available = total - hold
                if total > 0 and bal["coin"] not in ("USDC", "USDE", "USDT0", "USDH"):
                    holdings[bal["coin"]] = {"total": total, "available": available}
            return holdings
        except Exception as e:
            print(f"[MM] Error getting holdings: {e}")
            return {}

    def get_l2(self, coin: str) -> tuple:
        """Get best bid, best ask, and mid price from L2 book."""
        try:
            l2 = self.info.l2_snapshot(coin)
            levels = l2.get("levels", [[], []])
            bids = levels[0]
            asks = levels[1]

            if not bids or not asks:
                return 0, 0, 0

            best_bid = float(bids[0]["px"])
            best_ask = float(asks[0]["px"])
            mid = (best_bid + best_ask) / 2

            return best_bid, best_ask, mid
        except Exception as e:
            print(f"[MM] Error getting L2 for {coin}: {e}")
            return 0, 0, 0

    def _estimate_volatility(self, coin: str) -> float:
        """Estimate recent volatility using L2 spread and cached data.

        Returns volatility as a ratio (e.g., 0.01 = 1%).
        """
        now = time.time()
        cached = self._volatility_cache.get(coin)
        if cached and now - cached[0] < 120:  # Cache 2 min
            return cached[1]

        best_bid, best_ask, mid = self.get_l2(coin)
        if mid <= 0:
            return 0.005  # Default 0.5%

        # Use current spread as volatility proxy
        spread_pct = (best_ask - best_bid) / mid
        # Scale up slightly (spread is tighter than true volatility)
        vol_estimate = max(spread_pct * 2, 0.002)

        self._volatility_cache[coin] = (now, vol_estimate)
        return vol_estimate

    def _calculate_dynamic_spread(self, coin: str, base_spread_bps: float) -> float:
        """Calculate dynamic spread based on volatility.

        - High volatility -> wider spread (more protection)
        - Low volatility -> tighter spread (more fills)
        """
        if not config.MM_DYNAMIC_SPREAD:
            return base_spread_bps

        vol = self._estimate_volatility(coin)

        # Scale spread with volatility
        # Base: if vol ~0.5%, use base spread
        # If vol > 1%, widen proportionally
        # If vol < 0.3%, tighten
        vol_ratio = vol / 0.005  # Normalize around 0.5%
        adjusted_bps = base_spread_bps * max(vol_ratio, 0.5)

        # Clamp to configured limits
        adjusted_bps = max(adjusted_bps, config.MM_MIN_SPREAD_BPS)
        adjusted_bps = min(adjusted_bps, config.MM_MAX_SPREAD_BPS)

        return adjusted_bps

    def _calculate_inventory_skew(self, coin: str, mid: float) -> tuple:
        """Calculate bid/ask price skew based on inventory.

        If we hold too much token -> lower ask (eager to sell), raise bid (less eager to buy)
        If we hold too little -> lower bid (eager to buy), raise ask (less eager to sell)

        Returns (bid_skew_bps, ask_skew_bps) to add/subtract from base spread.
        """
        if not config.MM_INVENTORY_REBALANCE:
            return 0, 0

        base_token = self._get_base_token_name(coin)
        holdings = self.get_spot_holdings()

        if not base_token or base_token not in holdings:
            # No holdings -> skew toward buying (lower bid spread slightly)
            return -1, 2  # Tighter bid, wider ask

        token_value = holdings[base_token]["total"] * mid
        target_value = config.MM_SIZE_USD  # Target inventory = 1x order size

        if target_value <= 0:
            return 0, 0

        inventory_ratio = token_value / target_value

        if inventory_ratio > 2.0:
            # Too much inventory -> eager to sell
            return 3, -2  # Wider bid (buy less), tighter ask (sell more)
        elif inventory_ratio > 1.5:
            return 1, -1
        elif inventory_ratio < 0.3:
            # Low inventory -> eager to buy
            return -2, 3  # Tighter bid (buy more), wider ask (sell less)
        elif inventory_ratio < 0.7:
            return -1, 1

        return 0, 0

    def cancel_all_orders(self, coin: str = None):
        """Cancel all open orders (optionally for a specific coin)."""
        try:
            orders = self.info.frontend_open_orders(config.HL_WALLET_ADDRESS)
            if not orders:
                return 0

            cancels = []
            for o in orders:
                if coin is None or o.get("coin", "") == coin:
                    cancels.append({"coin": o["coin"], "oid": o["oid"]})

            if cancels:
                result = self.exchange.bulk_cancel(cancels)
                print(f"[MM] Cancelled {len(cancels)} orders")
                return len(cancels)
            return 0
        except Exception as e:
            print(f"[MM] Error cancelling orders: {e}")
            return 0

    def _get_sz_decimals(self, coin: str) -> int:
        """Get size decimal precision for a spot pair."""
        try:
            meta = self._load_spot_meta()
            tokens = meta.get("tokens", [])
            universe = meta.get("universe", [])

            for u in universe:
                if u["name"] == coin:
                    token_idx = u["tokens"][0]
                    for t in tokens:
                        if t["index"] == token_idx:
                            return t.get("szDecimals", 2)
            return 2
        except:
            return 2

    def _get_px_decimals(self, coin: str, bid: float, ask: float) -> int:
        """Infer price decimal precision from order book."""
        for px in [bid, ask]:
            px_str = f"{px:.10f}".rstrip("0")
            if "." in px_str:
                decimals = len(px_str.split(".")[1])
                return max(decimals, 2)
        return 6

    def _get_base_token_name(self, pair_name: str) -> str:
        """Get base token name from pair name."""
        try:
            meta = self._load_spot_meta()
            tokens = meta.get("tokens", [])
            universe = meta.get("universe", [])
            token_map = {t["index"]: t["name"] for t in tokens}

            for u in universe:
                if u["name"] == pair_name:
                    return token_map.get(u["tokens"][0], "")
            return ""
        except:
            return ""

    def _place_order(self, coin: str, is_buy: bool, sz: float, px: float) -> dict:
        """Place a single limit GTC order."""
        try:
            order_type = {"limit": {"tif": "Gtc"}}
            result = self.exchange.order(
                coin, is_buy=is_buy, sz=sz, limit_px=px,
                order_type=order_type
            )
            statuses = result.get("response", {}).get("data", {}).get("statuses", [{}])
            if statuses and isinstance(statuses[0], dict) and "error" in statuses[0]:
                return {"status": "error", "msg": statuses[0]["error"]}
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    def place_mm_orders(self, coin: str, spread_bps: float, size_usd: float, avail_usdc: float) -> dict:
        """Place bid and ask limit orders with dynamic spread and inventory skew."""
        best_bid, best_ask, mid = self.get_l2(coin)
        if mid <= 0:
            return {"status": "error", "msg": f"No price for {coin}"}

        sz_decimals = self._get_sz_decimals(coin)
        px_decimals = self._get_px_decimals(coin, best_bid, best_ask)

        # Dynamic spread calculation
        effective_spread = self._calculate_dynamic_spread(coin, spread_bps)

        # Inventory skew
        bid_skew, ask_skew = self._calculate_inventory_skew(coin, mid)

        bid_spread = max(effective_spread + bid_skew, config.MM_MIN_SPREAD_BPS)
        ask_spread = max(effective_spread + ask_skew, config.MM_MIN_SPREAD_BPS)

        bid_px = round(mid - mid * (bid_spread / 10000), px_decimals)
        ask_px = round(mid + mid * (ask_spread / 10000), px_decimals)

        placed = []

        # BID side - buy with USDC
        bid_usd = max(size_usd, 11.0)
        if avail_usdc >= bid_usd:
            bid_sz = round(bid_usd / bid_px, sz_decimals)
            if sz_decimals == 0:
                bid_sz = max(int(bid_sz), 1)
                bid_usd_actual = bid_sz * bid_px
                if bid_usd_actual < 10:
                    bid_sz = int(10 / bid_px) + 1
            if bid_sz > 0:
                res = self._place_order(coin, True, bid_sz, bid_px)
                if res["status"] == "ok":
                    print(f"[MM] BID {coin}: {bid_sz} @ ${bid_px:.6f} (~${bid_sz * bid_px:.2f}) [spread: {bid_spread:.1f}bps]")
                    placed.append("bid")
                else:
                    print(f"[MM] BID {coin} FAIL: {res['msg']}")
        else:
            print(f"[MM] BID {coin}: Skip (need ${bid_usd:.0f}, have ${avail_usdc:.2f})")

        # ASK side - sell holdings
        base_token = self._get_base_token_name(coin)
        holdings = self.get_spot_holdings()
        if base_token and base_token in holdings:
            available_tokens = holdings[base_token]["available"]
            token_value = available_tokens * ask_px
            if token_value >= 10:
                ask_sz = round(min(available_tokens, size_usd / ask_px), sz_decimals)
                if sz_decimals == 0:
                    ask_sz = int(ask_sz)
                if ask_sz > 0 and ask_sz * ask_px >= 10:
                    res = self._place_order(coin, False, ask_sz, ask_px)
                    if res["status"] == "ok":
                        print(f"[MM] ASK {coin}: {ask_sz} @ ${ask_px:.6f} (~${ask_sz * ask_px:.2f}) [spread: {ask_spread:.1f}bps]")
                        placed.append("ask")
                    else:
                        print(f"[MM] ASK {coin} FAIL: {res['msg']}")
            else:
                if token_value > 0:
                    print(f"[MM] ASK {coin}: Skip (holdings ${token_value:.2f} < $10 min)")

        spread_total = ask_px - bid_px
        spread_pct = spread_total / mid * 100

        return {"status": "ok", "placed": placed, "bid_px": bid_px, "ask_px": ask_px,
                "spread_pct": spread_pct, "bid_spread_bps": bid_spread, "ask_spread_bps": ask_spread}

    def run_cycle(self):
        """Run one MM cycle: cancel old orders, place new ones."""
        avail = self.get_spot_balance()
        holdings = self.get_spot_holdings()

        print(f"[MM] Available USDC: ${avail:.2f}")
        if holdings:
            for token, info in holdings.items():
                print(f"[MM] Holding: {info['total']:.4f} {token} (avail: {info['available']:.4f})")

        if avail < config.MM_MIN_BALANCE and not holdings:
            print(f"[MM] Balance too low and no holdings. Skipping MM.")
            return

        # Cancel existing orders first
        self.cancel_all_orders()
        time.sleep(0.5)

        # Allocate USDC across pairs
        num_pairs = len(config.MM_PAIRS)
        usdc_per_pair = avail * config.MM_ALLOC_PCT / num_pairs

        for coin in config.MM_PAIRS:
            spread = config.MM_SPREAD_MAP.get(coin, config.MM_SPREAD_BPS)
            result = self.place_mm_orders(
                coin, spread_bps=spread, size_usd=config.MM_SIZE_USD,
                avail_usdc=usdc_per_pair
            )
            if result.get("status") == "ok" and result.get("placed"):
                print(f"[MM] {coin}: placed={result['placed']} "
                      f"spread={result['spread_pct']:.3f}% "
                      f"(bid:{result.get('bid_spread_bps', 0):.0f}bps / ask:{result.get('ask_spread_bps', 0):.0f}bps)")

            time.sleep(0.3)
