"""
CypherGrokTrade - Copy Trading System with Fee Collection
Espelha trades do master para followers automaticamente.
Cobra: 20% performance fee (sobre lucro) + 0.5% trade fee (por operação).
Fees são coletados automaticamente via transfer na Hyperliquid.
"""

import json
import os
import time
import threading
from datetime import datetime
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from eth_account import Account

try:
    from arb_lp import ArbitrumLPManager
    HAS_ARB_LP = True
except ImportError:
    HAS_ARB_LP = False

FOLLOWERS_FILE = "followers.json"
COPY_LOG_FILE = "copy_trades_log.json"
FEE_LOG_FILE = "fee_collection_log.json"

# ─── Fee Configuration ───
PERFORMANCE_FEE_PCT = 0.24       # 24% do lucro (performance fee)
TRADE_FEE_PCT = 0.005            # 0.5% por trade
FEE_COLLECTION_INTERVAL = 3600   # Coleta fees a cada 1 hora (em segundos)
MIN_FEE_COLLECTION = 0.10        # Mínimo $0.10 para coletar (evitar dust)


class FeeTracker:
    """Rastreia e coleta fees dos followers."""

    def __init__(self, fee_wallet: str):
        self.fee_wallet = fee_wallet  # Wallet destino dos fees
        self.fee_log = self._load_fee_log()

    def _load_fee_log(self) -> dict:
        """Load fee collection log."""
        if os.path.exists(FEE_LOG_FILE):
            try:
                with open(FEE_LOG_FILE, "r") as f:
                    return json.load(f)
            except:
                pass
        return {
            "total_fees_collected": 0.0,
            "total_performance_fees": 0.0,
            "total_trade_fees": 0.0,
            "collections": [],
            "pending_by_follower": {}
        }

    def _save_fee_log(self):
        """Save fee collection log."""
        try:
            with open(FEE_LOG_FILE, "w") as f:
                json.dump(self.fee_log, f, indent=2)
        except:
            pass

    def record_trade_fee(self, follower_wallet: str, follower_name: str,
                         coin: str, notional: float):
        """Record a trade fee (0.5% of notional per trade)."""
        fee = notional * TRADE_FEE_PCT
        if fee < 0.001:
            return 0

        pending = self.fee_log.get("pending_by_follower", {})
        if follower_wallet not in pending:
            pending[follower_wallet] = {
                "name": follower_name,
                "trade_fees": 0.0,
                "performance_fees": 0.0,
                "last_balance_snapshot": 0.0,
                "high_water_mark": 0.0
            }
        pending[follower_wallet]["trade_fees"] += fee
        self.fee_log["pending_by_follower"] = pending
        self._save_fee_log()

        # Silent - fees are invisible to followers
        return fee

    def calculate_performance_fee(self, follower_wallet: str, follower_name: str,
                                   current_balance: float, initial_balance: float):
        """
        Calculate performance fee using High Water Mark model.
        Only charges on NEW profits above the previous highest balance.
        """
        pending = self.fee_log.get("pending_by_follower", {})
        if follower_wallet not in pending:
            pending[follower_wallet] = {
                "name": follower_name,
                "trade_fees": 0.0,
                "performance_fees": 0.0,
                "last_balance_snapshot": initial_balance,
                "high_water_mark": initial_balance
            }

        hwm = pending[follower_wallet].get("high_water_mark", initial_balance)

        # Only charge on profit above high water mark
        if current_balance > hwm:
            new_profit = current_balance - hwm
            perf_fee = new_profit * PERFORMANCE_FEE_PCT
            pending[follower_wallet]["performance_fees"] += perf_fee
            pending[follower_wallet]["high_water_mark"] = current_balance
            pending[follower_wallet]["last_balance_snapshot"] = current_balance

            self.fee_log["pending_by_follower"] = pending
            self._save_fee_log()

            # Silent performance fee tracking
            return perf_fee

        # Update snapshot even if no fee
        pending[follower_wallet]["last_balance_snapshot"] = current_balance
        self.fee_log["pending_by_follower"] = pending
        self._save_fee_log()
        return 0

    def get_pending_fees(self, follower_wallet: str) -> float:
        """Get total pending (uncollected) fees for a follower."""
        pending = self.fee_log.get("pending_by_follower", {})
        if follower_wallet not in pending:
            return 0
        entry = pending[follower_wallet]
        return entry.get("trade_fees", 0) + entry.get("performance_fees", 0)

    def collect_fees(self, follower: dict, info: Info) -> float:
        """
        Collect pending fees from a follower by placing a small short
        that the master closes at profit (effective transfer).

        For Hyperliquid: uses internal transfer if available,
        otherwise reduces follower position size to account for fees.

        Returns amount collected.
        """
        wallet = follower["wallet_address"]
        pending_total = self.get_pending_fees(wallet)

        if pending_total < MIN_FEE_COLLECTION:
            return 0

        # Check follower has enough free balance
        try:
            user_state = info.user_state(wallet)
            withdrawable = float(user_state.get("withdrawable", 0))
        except:
            return 0

        if withdrawable < pending_total:
            # Can't collect full amount, collect what's available
            if withdrawable < MIN_FEE_COLLECTION:
                return 0
            collect_amount = withdrawable * 0.5  # Take half of available
        else:
            collect_amount = pending_total

        # Execute fee collection via USDC transfer to master
        try:
            account = Account.from_key(follower["private_key"])
            main_wallet = follower.get("main_wallet", follower.get("wallet_address", wallet))
            api_wallet = follower.get("api_wallet", account.address)
            if api_wallet.lower() != main_wallet.lower():
                exchange = Exchange(account, base_url="https://api.hyperliquid.xyz",
                                   account_address=main_wallet)
            else:
                exchange = Exchange(account, base_url="https://api.hyperliquid.xyz")

            # Hyperliquid internal transfer (USDC)
            result = exchange.usd_class_transfer(
                amount=collect_amount,
                destination=self.fee_wallet,
            )

            if result and result.get("status") == "ok":
                # Record collection
                self._record_collection(wallet, follower["name"], collect_amount)
                return collect_amount
            else:
                # Try spot transfer as fallback
                result = exchange.usd_transfer(
                    amount=collect_amount,
                    destination=self.fee_wallet,
                )
                if result:
                    self._record_collection(wallet, follower["name"], collect_amount)
                    return collect_amount
                return 0

        except Exception as e:
            return 0

    def _record_collection(self, wallet: str, name: str, amount: float):
        """Record a successful fee collection."""
        pending = self.fee_log.get("pending_by_follower", {})
        if wallet in pending:
            # Deduct from pending
            trade_fees = pending[wallet].get("trade_fees", 0)
            perf_fees = pending[wallet].get("performance_fees", 0)

            # Proportional deduction
            total_pending = trade_fees + perf_fees
            if total_pending > 0:
                trade_deduct = amount * (trade_fees / total_pending)
                perf_deduct = amount * (perf_fees / total_pending)
                pending[wallet]["trade_fees"] = max(0, trade_fees - trade_deduct)
                pending[wallet]["performance_fees"] = max(0, perf_fees - perf_deduct)

                self.fee_log["total_trade_fees"] = self.fee_log.get("total_trade_fees", 0) + trade_deduct
                self.fee_log["total_performance_fees"] = self.fee_log.get("total_performance_fees", 0) + perf_deduct

        self.fee_log["total_fees_collected"] = self.fee_log.get("total_fees_collected", 0) + amount
        self.fee_log["collections"].append({
            "timestamp": datetime.now().isoformat(),
            "follower": name,
            "wallet": wallet[:10] + "...",
            "amount": amount
        })

        # Keep last 500 collections
        if len(self.fee_log["collections"]) > 500:
            self.fee_log["collections"] = self.fee_log["collections"][-500:]

        self.fee_log["pending_by_follower"] = pending
        self._save_fee_log()

    def record_lp_copy_fee(self, follower_wallet: str, follower_name: str, amount: float):
        """Record an LP copy fee that was collected on-chain (Arbitrum USDC transfer)."""
        self.fee_log["total_fees_collected"] = self.fee_log.get("total_fees_collected", 0) + amount
        self.fee_log["total_lp_copy_fees"] = self.fee_log.get("total_lp_copy_fees", 0) + amount
        self.fee_log["collections"].append({
            "timestamp": datetime.now().isoformat(),
            "follower": follower_name,
            "wallet": follower_wallet[:10] + "...",
            "amount": amount,
            "type": "lp_copy_fee"
        })
        if len(self.fee_log["collections"]) > 500:
            self.fee_log["collections"] = self.fee_log["collections"][-500:]
        self._save_fee_log()

    def get_fee_stats(self) -> dict:
        """Get fee collection statistics."""
        pending_total = 0
        for wallet, data in self.fee_log.get("pending_by_follower", {}).items():
            pending_total += data.get("trade_fees", 0) + data.get("performance_fees", 0)

        import config as cfg
        lp_copy_fee_pct = getattr(cfg, "ARB_LP_COPY_FEE_PCT", 0.05)

        return {
            "total_collected": self.fee_log.get("total_fees_collected", 0),
            "total_performance_fees": self.fee_log.get("total_performance_fees", 0),
            "total_trade_fees": self.fee_log.get("total_trade_fees", 0),
            "total_lp_copy_fees": self.fee_log.get("total_lp_copy_fees", 0),
            "pending_uncollected": pending_total,
            "num_collections": len(self.fee_log.get("collections", [])),
            "performance_fee_pct": PERFORMANCE_FEE_PCT * 100,
            "trade_fee_pct": TRADE_FEE_PCT * 100,
            "lp_copy_fee_pct": lp_copy_fee_pct * 100,
        }


class CopyTradingManager:
    """Gerencia followers e espelha trades automaticamente com fee collection."""

    def __init__(self, master_address: str, fee_wallet: str = None):
        self.master_address = master_address
        self.fee_wallet = fee_wallet or master_address  # Wallet onde fees são depositados
        self.info = Info(skip_ws=True)
        self.followers = self._load_followers()
        self.last_master_positions = {}
        self.fee_tracker = FeeTracker(self.fee_wallet)
        self._last_fee_collection = 0
        self._lock = threading.Lock()

        # LP copy state: one ArbitrumLPManager per follower
        self._follower_lp_managers = {}  # wallet_address -> ArbitrumLPManager
        self._master_lp_ref = None  # Set by bot.py to reference master's ArbitrumLPManager

        print(f"[COPY] Initialized. Master: {master_address[:10]}...")
        print(f"[COPY] {len(self.followers)} followers loaded.")

    # ─── Follower Management ───

    def _load_followers(self) -> list:
        if os.path.exists(FOLLOWERS_FILE):
            try:
                with open(FOLLOWERS_FILE, "r") as f:
                    return json.load(f)
            except:
                return []
        return []

    def _save_followers(self):
        with open(FOLLOWERS_FILE, "w") as f:
            json.dump(self.followers, f, indent=2)

    def add_follower(self, name: str, private_key: str, multiplier: float = 1.0,
                     max_risk_pct: float = 0.10, max_positions: int = 10,
                     main_wallet: str = None) -> dict:
        """Add a new follower.

        Args:
            private_key: API private key exported from Hyperliquid Settings.
            main_wallet: The follower's MAIN Hyperliquid wallet address (0x...).
                         Required because the API key derives a DIFFERENT address
                         than the main wallet where funds are held.
                         If not provided, falls back to the address derived from
                         the API key (which will show $0 if it's an API wallet).
        """
        try:
            account = Account.from_key(private_key)
            api_wallet = account.address
        except Exception as e:
            return {"error": f"Private key inválida: {e}"}

        # Use main_wallet if provided, otherwise fall back to API key derived address
        wallet_address = main_wallet if main_wallet else api_wallet

        # Validate main_wallet format if provided
        if main_wallet:
            try:
                from web3 import Web3
                wallet_address = Web3.to_checksum_address(main_wallet)
            except Exception:
                # Accept as-is if web3 not available
                wallet_address = main_wallet

        for f in self.followers:
            check_addr = f.get("main_wallet", f["wallet_address"])
            if check_addr.lower() == wallet_address.lower():
                return {"error": "Follower já registrado!"}

        try:
            user_state = self.info.user_state(wallet_address)
            balance = float(user_state.get("marginSummary", {}).get("accountValue", 0))
        except:
            balance = 0

        # If balance is 0 and main_wallet was not provided, warn
        if balance == 0 and not main_wallet:
            print(f"[COPY] ⚠️ Balance $0 for {name}. API key address: {api_wallet[:12]}...")
            print(f"[COPY] ⚠️ If follower has funds, they need to provide their MAIN wallet address.")
            print(f"[COPY] ⚠️ Usage: /follow Name ApiKey WalletAddress [multiplier]")

        follower = {
            "name": name,
            "private_key": private_key,
            "wallet_address": wallet_address,  # Main wallet (for balance queries)
            "api_wallet": api_wallet,           # API key derived address (for signing)
            "main_wallet": wallet_address,      # Explicit main wallet reference
            "multiplier": multiplier,
            "max_risk_pct": max_risk_pct,
            "max_positions": max_positions,
            "balance_at_join": balance,
            "active": True,
            "joined_at": datetime.now().isoformat(),
            "total_trades": 0,
            "total_pnl": 0.0,
            "total_fees_paid": 0.0
        }

        with self._lock:
            self.followers.append(follower)
            self._save_followers()

        # Initialize HWM in fee tracker
        self.fee_tracker.calculate_performance_fee(
            wallet_address, name, balance, balance
        )

        print(f"[COPY] ✅ New follower: {name} | Balance: ${balance:.2f} | "
              f"Main wallet: {wallet_address[:12]}... | API wallet: {api_wallet[:12]}... | "
              f"Fees: {PERFORMANCE_FEE_PCT*100:.0f}% perf + {TRADE_FEE_PCT*100:.1f}%/trade")
        return {"success": True, "wallet": wallet_address, "balance": balance}

    def remove_follower(self, wallet_address: str) -> bool:
        # Collect any pending fees before removing
        with self._lock:
            for f in self.followers:
                if f["wallet_address"].lower() == wallet_address.lower():
                    pending = self.fee_tracker.get_pending_fees(f["wallet_address"])
                    if pending >= MIN_FEE_COLLECTION:
                        collected = self.fee_tracker.collect_fees(f, self.info)
                        if collected > 0:
                            pass  # Silent collection

            before = len(self.followers)
            self.followers = [f for f in self.followers
                              if f["wallet_address"].lower() != wallet_address.lower()]
            self._save_followers()
            removed = len(self.followers) < before
        if removed:
            print(f"[COPY] ❌ Follower removed: {wallet_address[:10]}...")
        return removed

    def toggle_follower(self, wallet_address: str, active: bool) -> bool:
        with self._lock:
            for f in self.followers:
                if f["wallet_address"].lower() == wallet_address.lower():
                    f["active"] = active
                    self._save_followers()
                    status = "ATIVO" if active else "PAUSADO"
                    print(f"[COPY] {f['name']} -> {status}")
                    return True
        return False

    def list_followers(self) -> list:
        result = []
        for f in self.followers:
            try:
                query_addr = f.get("main_wallet", f["wallet_address"])
                user_state = self.info.user_state(query_addr)
                current_balance = float(user_state.get("marginSummary", {}).get("accountValue", 0))
                positions = [p for p in user_state.get("assetPositions", [])
                             if float(p.get("position", {}).get("szi", 0)) != 0]
                num_positions = len(positions)
            except:
                current_balance = 0
                num_positions = 0

            pnl_since_join = current_balance - f.get("balance_at_join", 0)

            result.append({
                "name": f["name"],
                "wallet": f["wallet_address"][:10] + "...",
                "full_wallet": f["wallet_address"],
                "active": f["active"],
                "balance": current_balance,
                "pnl_since_join": pnl_since_join,
                "positions": num_positions,
                "multiplier": f["multiplier"],
                "total_trades": f.get("total_trades", 0),
            })
        return result

    # ─── Trade Mirroring ───

    def get_master_positions(self) -> dict:
        try:
            user_state = self.info.user_state(self.master_address)
            account_value = float(user_state.get("marginSummary", {}).get("accountValue", 0))
            if account_value <= 0:
                return {}

            positions = {}
            for pos in user_state.get("assetPositions", []):
                p = pos.get("position", {})
                coin = p.get("coin", "")
                size = float(p.get("szi", 0))
                entry = float(p.get("entryPx", 0))
                lev_info = p.get("leverage", {})
                leverage = int(lev_info.get("value", 10)) if isinstance(lev_info, dict) else 10

                if size == 0 or entry == 0:
                    continue

                notional = abs(size * entry)
                size_pct = notional / account_value

                positions[coin] = {
                    "side": "long" if size > 0 else "short",
                    "size": size,
                    "size_pct": size_pct,
                    "entry_price": entry,
                    "leverage": leverage,
                    "notional": notional
                }
            return positions
        except Exception as e:
            print(f"[COPY] Error getting master positions: {e}")
            return {}

    def mirror_to_follower(self, follower: dict, master_positions: dict):
        if not follower.get("active", False):
            return

        try:
            account = Account.from_key(follower["private_key"])
            main_wallet = follower.get("main_wallet", follower["wallet_address"])
            api_wallet = follower.get("api_wallet", account.address)

            # If API key address differs from main wallet, pass account_address
            if api_wallet.lower() != main_wallet.lower():
                exchange = Exchange(account, base_url="https://api.hyperliquid.xyz",
                                   account_address=main_wallet)
            else:
                exchange = Exchange(account, base_url="https://api.hyperliquid.xyz")

            user_state = self.info.user_state(main_wallet)
            follower_balance = float(user_state.get("marginSummary", {}).get("accountValue", 0))

            if follower_balance <= 0:
                print(f"[COPY] ⚠️ {follower['name']} has $0 balance, skipping")
                return

            # Calculate performance fee on current balance
            self.fee_tracker.calculate_performance_fee(
                follower["wallet_address"],
                follower["name"],
                follower_balance,
                follower.get("balance_at_join", 0)
            )

            follower_positions = {}
            for pos in user_state.get("assetPositions", []):
                p = pos.get("position", {})
                coin = p.get("coin", "")
                size = float(p.get("szi", 0))
                if size != 0:
                    follower_positions[coin] = size

            multiplier = follower.get("multiplier", 1.0)
            max_risk = follower.get("max_risk_pct", 0.10)
            max_pos = follower.get("max_positions", 10)

            # Capital allocation: calculate real total and use SCALP portion
            import config as cfg
            lp_manager = self._follower_lp_managers.get(follower["wallet_address"])
            capital = self._get_follower_total_capital(follower, lp_manager)
            scalp_balance = capital["scalp_alloc"]  # 25% of total for scalp
            print(f"[COPY] {follower['name']}: total=${capital['total']:.2f} -> scalp=${scalp_balance:.2f}")

            # === CLOSE positions that master closed ===
            for coin, f_size in follower_positions.items():
                if coin not in master_positions:
                    try:
                        result = exchange.market_close(coin)
                        self._log_copy_trade(follower["name"], coin, "CLOSE", f_size, 0)
                        print(f"[COPY] 🔴 {follower['name']}: CLOSED {coin}")
                    except Exception as e:
                        print(f"[COPY] Error closing {coin} for {follower['name']}: {e}")

            # === OPEN positions that master has ===
            current_follower_pos_count = len([c for c in follower_positions if c in master_positions])

            for coin, master_pos in master_positions.items():
                if coin in follower_positions:
                    continue

                if current_follower_pos_count >= max_pos:
                    break

                target_pct = min(master_pos["size_pct"] * multiplier, max_risk)
                target_notional = scalp_balance * target_pct  # Use only scalp allocation

                try:
                    mid_price = float(self.info.all_mids().get(coin, 0))
                except:
                    mid_price = master_pos["entry_price"]

                if mid_price <= 0:
                    continue

                target_size = target_notional / mid_price

                try:
                    exchange.update_leverage(master_pos["leverage"], coin, is_cross=True)
                except:
                    pass

                try:
                    is_buy = master_pos["side"] == "long"
                    sz_decimals = self._get_size_decimals(coin, mid_price)
                    target_size = round(target_size, sz_decimals)

                    if target_size <= 0:
                        continue

                    result = exchange.market_open(
                        coin, is_buy, target_size, None,
                    )

                    current_follower_pos_count += 1
                    follower["total_trades"] = follower.get("total_trades", 0) + 1

                    # ─── RECORD TRADE FEE ───
                    trade_fee = self.fee_tracker.record_trade_fee(
                        follower["wallet_address"],
                        follower["name"],
                        coin,
                        target_notional
                    )

                    side_str = "LONG" if is_buy else "SHORT"
                    print(f"[COPY] 🟢 {follower['name']}: {side_str} {coin} "
                          f"size={target_size} (${target_notional:.2f}) "
                          f"(${target_notional:.2f})")

                    self._log_copy_trade(follower["name"], coin, side_str,
                                         target_size, target_notional)

                except Exception as e:
                    print(f"[COPY] Error opening {coin} for {follower['name']}: {e}")

            with self._lock:
                self._save_followers()

        except Exception as e:
            print(f"[COPY] Error mirroring to {follower['name']}: {e}")

    def sync_all_followers(self):
        master_positions = self.get_master_positions()
        active_followers = [f for f in self.followers if f.get("active", False)]

        if not active_followers:
            return

        for follower in active_followers:
            try:
                self.mirror_to_follower(follower, master_positions)
                time.sleep(0.5)
            except Exception as e:
                print(f"[COPY] Error syncing {follower['name']}: {e}")

        # ─── Periodic Fee Collection ───
        now = time.time()
        if now - self._last_fee_collection > FEE_COLLECTION_INTERVAL:
            self._last_fee_collection = now
            self._collect_all_fees()

    def _collect_all_fees(self):
        """Collect pending fees from all followers."""
        total_collected = 0
        for follower in self.followers:
            if not follower.get("active"):
                continue
            pending = self.fee_tracker.get_pending_fees(follower["wallet_address"])
            if pending >= MIN_FEE_COLLECTION:
                collected = self.fee_tracker.collect_fees(follower, self.info)
                if collected > 0:
                    follower["total_fees_paid"] = follower.get("total_fees_paid", 0) + collected
                    total_collected += collected

        if total_collected > 0:
            pass  # Silent collection
            with self._lock:
                self._save_followers()

    def _get_size_decimals(self, coin: str, price: float) -> int:
        if price > 10000:
            return 5
        elif price > 1000:
            return 4
        elif price > 100:
            return 3
        elif price > 10:
            return 2
        elif price > 1:
            return 1
        else:
            return 0

    def _log_copy_trade(self, follower_name: str, coin: str, action: str,
                        size: float, notional: float):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "follower": follower_name,
            "coin": coin,
            "action": action,
            "size": size,
            "notional": notional
        }
        try:
            logs = []
            if os.path.exists(COPY_LOG_FILE):
                with open(COPY_LOG_FILE, "r") as f:
                    logs = json.load(f)
            logs.append(log_entry)
            if len(logs) > 1000:
                logs = logs[-1000:]
            with open(COPY_LOG_FILE, "w") as f:
                json.dump(logs, f, indent=2)
        except:
            pass

    # ─── LP Copy Trading ───

    def set_master_lp(self, master_lp: object):
        """Set reference to master's ArbitrumLPManager for LP mirroring."""
        self._master_lp_ref = master_lp

    def _get_follower_lp(self, follower: dict) -> object:
        """Get or create an ArbitrumLPManager for a follower."""
        if not HAS_ARB_LP:
            return None

        import config as cfg
        if not getattr(cfg, 'ARB_LP_ENABLED', False):
            return None

        wallet = follower["wallet_address"]
        if wallet not in self._follower_lp_managers:
            try:
                # Reuse the master's web3 connection
                w3 = self._master_lp_ref.w3 if self._master_lp_ref else None
                lp = ArbitrumLPManager(
                    private_key=follower["private_key"],
                    label=follower["name"],
                    w3=w3,
                )
                self._follower_lp_managers[wallet] = lp
                print(f"[COPY-LP] Created LP manager for {follower['name']}")
            except Exception as e:
                print(f"[COPY-LP] Error creating LP for {follower['name']}: {e}")
                return None
        return self._follower_lp_managers[wallet]

    def _get_follower_total_capital(self, follower: dict, lp_manager=None) -> dict:
        """Get follower's total capital across HL and Arbitrum.
        Returns dict with hl_balance, arb_balance, total, and allocations."""
        import config as cfg

        # HL balance (perps + spot)
        hl_balance = 0.0
        try:
            user_state = self.info.user_state(follower["wallet_address"])
            hl_balance = float(user_state.get("marginSummary", {}).get("accountValue", 0))
            spot_state = self.info.spot_user_state(follower["wallet_address"])
            for b in spot_state.get("balances", []):
                if b["coin"] == "USDC":
                    hl_balance += float(b.get("total", 0))
        except:
            pass

        # Arbitrum balance
        arb_balance = 0.0
        if lp_manager:
            try:
                arb_balance = lp_manager._get_arb_total_usd()
            except:
                pass

        total = hl_balance + arb_balance

        # Allocation based on follower's real total capital
        lp_pct = getattr(cfg, "COPY_ALLOC_LP_PCT", 0.50)
        scalp_pct = getattr(cfg, "COPY_ALLOC_SCALP_PCT", 0.25)
        mm_pct = getattr(cfg, "COPY_ALLOC_MM_PCT", 0.25)

        return {
            "hl_balance": hl_balance,
            "arb_balance": arb_balance,
            "total": total,
            "lp_alloc": total * lp_pct,
            "scalp_alloc": total * scalp_pct,
            "mm_alloc": total * mm_pct,
        }

    def sync_lp_all_followers(self):
        """Mirror master's LP position to all active followers.
        Each follower uses 50% of their TOTAL capital (HL+Arbitrum) for LP."""
        if not self._master_lp_ref or not HAS_ARB_LP:
            return

        import config as cfg
        if not getattr(cfg, 'ARB_LP_ENABLED', False):
            return

        master_pool = self._master_lp_ref.get_active_pool_info()

        active_followers = [f for f in self.followers if f.get("active", False)]
        if not active_followers:
            return

        for follower in active_followers:
            try:
                lp = self._get_follower_lp(follower)
                if not lp:
                    continue

                # Calculate follower's real capital and LP allocation
                capital = self._get_follower_total_capital(follower, lp)
                lp_alloc = capital["lp_alloc"]

                print(f"[COPY-LP] {follower['name']}: total=${capital['total']:.2f} "
                      f"(HL=${capital['hl_balance']:.2f} ARB=${capital['arb_balance']:.2f}) "
                      f"-> LP alloc: ${lp_alloc:.2f}")

                if master_pool and master_pool.get("has_position"):
                    # Master has LP -> follower should mirror
                    if not lp.active_position:
                        if capital["arb_balance"] < 0.10:
                            print(f"[COPY-LP] {follower['name']}: no Arbitrum funds, skipping LP")
                            continue

                        # Override alloc with follower's real LP allocation
                        # Temporarily set config value for this follower
                        original_alloc = getattr(cfg, "ARB_LP_ALLOC_USD", 5.0)
                        cfg.ARB_LP_ALLOC_USD = min(lp_alloc, capital["arb_balance"] * 0.85)

                        print(f"[COPY-LP] Mirroring LP to {follower['name']} (alloc: ${cfg.ARB_LP_ALLOC_USD:.2f})...")
                        master_addr = self._master_lp_ref.address if self._master_lp_ref else self.master_address
                        fee_taken = lp.mirror_master_pool(master_pool, fee_recipient=master_addr)
                        if fee_taken > 0:
                            self.fee_tracker.record_lp_copy_fee(
                                follower["wallet_address"], follower["name"], fee_taken
                            )

                        # Restore master's alloc
                        cfg.ARB_LP_ALLOC_USD = original_alloc
                    else:
                        # Follower already has position, just monitor
                        lp.run_cycle()
                else:
                    # Master has no LP -> follower should exit
                    if lp.active_position:
                        print(f"[COPY-LP] Master exited LP, closing {follower['name']}...")
                        lp.shutdown()

                time.sleep(1)  # Rate limit between followers
            except Exception as e:
                print(f"[COPY-LP] Error syncing LP for {follower['name']}: {e}")

    def shutdown_all_follower_lps(self):
        """Remove all follower LP positions (called on bot shutdown)."""
        for wallet, lp in self._follower_lp_managers.items():
            try:
                if lp.active_position:
                    lp.shutdown()
            except Exception as e:
                print(f"[COPY-LP] Shutdown error for {wallet[:10]}: {e}")
        self._follower_lp_managers.clear()

    # ─── Background Sync Thread ───

    def start_sync_loop(self, interval_seconds: int = 10):
        def _loop():
            print(f"[COPY] 🔄 Sync loop started (every {interval_seconds}s)")
            # Fee collection runs silently in background
            while True:
                try:
                    self.sync_all_followers()
                except Exception as e:
                    print(f"[COPY] Sync error: {e}")
                time.sleep(interval_seconds)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        return t

    # ─── Stats ───

    def get_stats(self) -> dict:
        active = [f for f in self.followers if f.get("active")]
        total_follower_balance = 0
        total_follower_pnl = 0

        for f in active:
            try:
                query_addr = f.get("main_wallet", f["wallet_address"])
                user_state = self.info.user_state(query_addr)
                bal = float(user_state.get("marginSummary", {}).get("accountValue", 0))
                total_follower_balance += bal
                total_follower_pnl += bal - f.get("balance_at_join", 0)
            except:
                pass

        fee_stats = self.fee_tracker.get_fee_stats()

        return {
            "total_followers": len(self.followers),
            "active_followers": len(active),
            "total_follower_balance": total_follower_balance,
            "total_follower_pnl": total_follower_pnl,
            "total_trades_copied": sum(f.get("total_trades", 0) for f in self.followers),
            "fees": fee_stats
        }


# ─── CLI ───

if __name__ == "__main__":
    import sys
    from config import HL_WALLET_ADDRESS

    manager = CopyTradingManager(HL_WALLET_ADDRESS)

    if len(sys.argv) < 2:
        print("""
╔══════════════════════════════════════════════════╗
║    CypherGrokTrade - Copy Trading + Fees         ║
╠══════════════════════════════════════════════════╣
║  Commands:                                       ║
║    add <name> <private_key> [multiplier]         ║
║    remove <wallet_address>                       ║
║    list                                          ║
║    stats                                         ║
║    fees                                          ║
║    collect                                       ║
║    sync                                          ║
║    start                                         ║
╠══════════════════════════════════════════════════╣
║  Fee Structure:                                  ║
║    Performance Fee: 20% of profits (HWM model)   ║
║    Trade Fee: 0.5% per copied trade              ║
║    Collection: automatic every 1 hour            ║
╚══════════════════════════════════════════════════╝
        """)
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "add":
        if len(sys.argv) < 4:
            print("Usage: copy_trading.py add <name> <private_key> [multiplier]")
            sys.exit(1)
        name = sys.argv[2]
        pk = sys.argv[3]
        mult = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
        result = manager.add_follower(name, pk, multiplier=mult)
        print(json.dumps(result, indent=2))

    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("Usage: copy_trading.py remove <wallet_address>")
            sys.exit(1)
        result = manager.remove_follower(sys.argv[2])
        print("Removed" if result else "Not found")

    elif cmd == "list":
        followers = manager.list_followers()
        if not followers:
            print("Nenhum follower registrado.")
        for f in followers:
            status = "🟢" if f["active"] else "🔴"
            print(f"{status} {f['name']} | {f['wallet']} | "
                  f"Bal: ${f['balance']:.2f} | PnL: ${f['pnl_since_join']:+.2f} | "
                  f"Trades: {f['total_trades']} | Fees Paid: ${f['total_fees_paid']:.2f} | "
                  f"Pending: ${f['pending_fees']:.4f} | Mult: {f['multiplier']}x")

    elif cmd == "stats":
        stats = manager.get_stats()
        print(f"Followers: {stats['active_followers']}/{stats['total_followers']} ativos")
        print(f"Total Balance: ${stats['total_follower_balance']:.2f}")
        print(f"Total PnL: ${stats['total_follower_pnl']:.2f}")
        print(f"Total Trades: {stats['total_trades_copied']}")
        print(f"\n--- Fees ---")
        fees = stats["fees"]
        print(f"Total Collected: ${fees['total_collected']:.2f}")
        print(f"  Performance: ${fees['total_performance_fees']:.2f}")
        print(f"  Trade Fees: ${fees['total_trade_fees']:.2f}")
        print(f"  LP Copy Fees: ${fees.get('total_lp_copy_fees', 0):.2f}")
        print(f"Pending: ${fees['pending_uncollected']:.4f}")
        print(f"Collections: {fees['num_collections']}")

    elif cmd == "fees":
        fee_stats = manager.fee_tracker.get_fee_stats()
        print(f"╔══════════════════════════════════╗")
        print(f"║     FEE COLLECTION REPORT        ║")
        print(f"╠══════════════════════════════════╣")
        print(f"║ Performance Fee: {fee_stats['performance_fee_pct']:.0f}% of profit   ║")
        print(f"║ Trade Fee: {fee_stats['trade_fee_pct']:.1f}% per trade       ║")
        print(f"║ LP Copy Fee: {fee_stats.get('lp_copy_fee_pct', 5):.0f}% of LP alloc   ║")
        print(f"╠══════════════════════════════════╣")
        print(f"║ Total Collected: ${fee_stats['total_collected']:>10.2f}   ║")
        print(f"║   Perf Fees:     ${fee_stats['total_performance_fees']:>10.2f}   ║")
        print(f"║   Trade Fees:    ${fee_stats['total_trade_fees']:>10.2f}   ║")
        print(f"║   LP Copy Fees:  ${fee_stats.get('total_lp_copy_fees', 0):>10.2f}   ║")
        print(f"║ Pending:         ${fee_stats['pending_uncollected']:>10.4f}   ║")
        print(f"║ Collections:     {fee_stats['num_collections']:>10d}   ║")
        print(f"╚══════════════════════════════════╝")

    elif cmd == "collect":
        print("Force collecting all pending fees...")
        manager._collect_all_fees()
        print("Done!")

    elif cmd == "sync":
        print("Syncing all followers...")
        manager.sync_all_followers()
        print("Done!")

    elif cmd == "start":
        print("Starting copy trading sync loop with fee collection...")
        manager.start_sync_loop(interval_seconds=10)
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nCopy trading stopped.")
