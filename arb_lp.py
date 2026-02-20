"""
CypherGrokTrade - Arbitrum DEX Liquidity Provision Manager
Auto-selects best pool, provides concentrated liquidity on Uniswap V3 (Arbitrum).

Lifecycle: discover pools -> select best -> approve tokens -> add liquidity ->
           monitor -> collect fees -> rebalance when out of range
"""

from __future__ import annotations

import time
import math
import json
import requests
from web3 import Web3
from eth_account import Account

import config
from arb_abi import (
    ERC20_ABI,
    WETH_ABI,
    UNISWAP_V3_FACTORY_ABI,
    UNISWAP_V3_POOL_ABI,
    UNISWAP_V3_NFT_MANAGER_ABI,
    UNISWAP_V3_SWAP_ROUTER_ABI,
    ARBITRUM_CONTRACTS,
)


class ArbitrumLPManager:
    """Manages concentrated liquidity positions on Uniswap V3 (Arbitrum).

    Can operate on behalf of any wallet by passing a private_key.
    Default: uses master wallet from config.HL_PRIVATE_KEY.
    """

    # Multiple RPCs for reliability
    RPC_ENDPOINTS = [
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum.llamarpc.com",
        "https://rpc.ankr.com/arbitrum",
        "https://arbitrum.drpc.org",
    ]

    def __init__(self, private_key: str = None, label: str = "MASTER", w3: Web3 = None):
        if w3:
            self.w3 = w3
        else:
            self.w3 = self._connect_rpc()

        self._private_key = private_key or config.HL_PRIVATE_KEY
        self.account = Account.from_key(self._private_key)
        self.address = self.account.address
        self.label = label
        self.chain_id = getattr(config, "ARB_CHAIN_ID", 42161)

        # Contracts
        self.factory = self.w3.eth.contract(
            address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["uniswap_v3_factory"]),
            abi=UNISWAP_V3_FACTORY_ABI,
        )
        self.nft_manager = self.w3.eth.contract(
            address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["uniswap_v3_nft_manager"]),
            abi=UNISWAP_V3_NFT_MANAGER_ABI,
        )
        self.swap_router = self.w3.eth.contract(
            address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["uniswap_v3_swap_router"]),
            abi=UNISWAP_V3_SWAP_ROUTER_ABI,
        )
        self.weth_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["weth"]),
            abi=WETH_ABI,
        )

        # State
        self.active_position = None  # {token_id, pool, entry_time, tick_lower, tick_upper}
        self.last_fee_collection = 0
        self._oor_since = None  # Timestamp when position first went out of range
        self._pool_cache = None
        self._pool_cache_time = 0
        self._token_decimals_cache = {}

    # ─── RPC Helpers ───

    def _connect_rpc(self) -> Web3:
        """Try connecting to multiple RPC endpoints."""
        rpcs = list(self.RPC_ENDPOINTS)
        custom = getattr(config, "ARB_RPC_URL", None)
        if custom and custom not in rpcs:
            rpcs.insert(0, custom)
        fallback = getattr(config, "ARB_RPC_FALLBACK", None)
        if fallback and fallback not in rpcs:
            rpcs.insert(1, fallback)

        for rpc in rpcs:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                if w3.is_connected():
                    return w3
            except Exception:
                continue
        # Last resort
        return Web3(Web3.HTTPProvider(rpcs[0], request_kwargs={"timeout": 15}))

    def _reconnect_rpc(self):
        """Reconnect to a working RPC (called after connection errors)."""
        self.w3 = self._connect_rpc()
        # Rebuild contracts with new w3
        self.factory = self.w3.eth.contract(
            address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["uniswap_v3_factory"]),
            abi=UNISWAP_V3_FACTORY_ABI,
        )
        self.nft_manager = self.w3.eth.contract(
            address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["uniswap_v3_nft_manager"]),
            abi=UNISWAP_V3_NFT_MANAGER_ABI,
        )
        self.swap_router = self.w3.eth.contract(
            address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["uniswap_v3_swap_router"]),
            abi=UNISWAP_V3_SWAP_ROUTER_ABI,
        )
        self.weth_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["weth"]),
            abi=WETH_ABI,
        )

    # ─── Helpers ───

    def _get_eth_balance(self) -> float:
        """Get native ETH balance on Arbitrum (for gas)."""
        wei = self.w3.eth.get_balance(self.address)
        return float(Web3.from_wei(wei, "ether"))

    def _get_arb_total_usd(self) -> float:
        """Estimate total USD value available on Arbitrum (ETH + USDC + WETH)."""
        tokens = getattr(config, "ARB_TOKENS", {})
        total = 0.0
        # ETH
        eth_bal = self._get_eth_balance()
        eth_price = self._get_eth_price()
        total += eth_bal * eth_price
        # USDC
        usdc_addr = tokens.get("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
        total += self._token_to_human(self._get_token_balance(usdc_addr), usdc_addr)
        # USDC.e
        usdce_addr = tokens.get("USDC.e")
        if usdce_addr:
            total += self._token_to_human(self._get_token_balance(usdce_addr), usdce_addr)
        # WETH
        weth_addr = tokens.get("WETH", ARBITRUM_CONTRACTS["weth"])
        weth_bal = self._token_to_human(self._get_token_balance(weth_addr), weth_addr)
        total += weth_bal * eth_price
        return total

    def _bridge_from_hl(self, amount_usd: float) -> bool:
        """Withdraw USDC from Hyperliquid to this wallet on Arbitrum.
        Uses the HL SDK withdraw_from_bridge (L1 -> Arbitrum)."""
        try:
            from hyperliquid.exchange import Exchange
            from hyperliquid import constants
            from eth_account import Account as EthAccount

            account = EthAccount.from_key(self._private_key)
            exchange = Exchange(
                account,
                constants.MAINNET_API_URL,
                vault_address=None,
                account_address=self.address,
            )
            result = exchange.withdraw_from_bridge(amount_usd, self.address)
            if result.get("status") == "ok":
                print(f"[ARB-LP:{self.label}] Bridge OK: ${amount_usd:.2f} USDC from HL -> Arbitrum")
                print(f"[ARB-LP:{self.label}] Wait ~2 min for USDC to arrive on Arbitrum")
                return True
            else:
                print(f"[ARB-LP:{self.label}] Bridge failed: {result}")
                return False
        except Exception as e:
            print(f"[ARB-LP:{self.label}] Bridge error: {e}")
            return False

    def _get_token_balance(self, token_address: str) -> int:
        """Get ERC20 token balance (raw units)."""
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address), abi=ERC20_ABI
        )
        return token.functions.balanceOf(self.address).call()

    def _get_token_decimals(self, token_address: str) -> int:
        """Get token decimals (cached)."""
        addr = token_address.lower()
        if addr not in self._token_decimals_cache:
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_address), abi=ERC20_ABI
            )
            self._token_decimals_cache[addr] = token.functions.decimals().call()
        return self._token_decimals_cache[addr]

    def _token_to_human(self, raw_amount: int, token_address: str) -> float:
        """Convert raw token amount to human-readable."""
        decimals = self._get_token_decimals(token_address)
        return raw_amount / (10 ** decimals)

    def _human_to_token(self, amount: float, token_address: str) -> int:
        """Convert human-readable amount to raw token units."""
        decimals = self._get_token_decimals(token_address)
        return int(amount * (10 ** decimals))

    def _token_value_usd(self, token_address: str, raw_amount: int) -> float:
        """Estimate USD value of a raw token amount."""
        human = self._token_to_human(raw_amount, token_address)
        if human == 0:
            return 0
        addr_lower = token_address.lower()
        tokens = getattr(config, "ARB_TOKENS", {})
        # Stablecoins: 1:1
        stables = [tokens.get(s, "").lower() for s in ("USDC", "USDT", "USDC.e", "DAI") if tokens.get(s)]
        if addr_lower in stables:
            return human
        # WETH or native ETH
        weth_lower = ARBITRUM_CONTRACTS["weth"].lower()
        eth_price = self._get_eth_price()
        if addr_lower == weth_lower:
            return human * eth_price
        # Other tokens: try to get price via WETH pool on Uniswap V3
        try:
            for fee in [3000, 10000, 500]:
                pool_addr = self.factory.functions.getPool(
                    Web3.to_checksum_address(token_address),
                    Web3.to_checksum_address(ARBITRUM_CONTRACTS["weth"]),
                    fee,
                ).call()
                if pool_addr != "0x0000000000000000000000000000000000000000":
                    pool = self.w3.eth.contract(
                        address=Web3.to_checksum_address(pool_addr),
                        abi=UNISWAP_V3_POOL_ABI,
                    )
                    slot0 = pool.functions.slot0().call()
                    sqrt_price = slot0[0]
                    token0_addr = pool.functions.token0().call().lower()
                    dec_token = self._get_token_decimals(token_address)
                    dec_weth = 18
                    price_ratio = (sqrt_price / (2 ** 96)) ** 2
                    if addr_lower == token0_addr:
                        # price = token1/token0 = WETH per token
                        eth_per_token = price_ratio * (10 ** dec_token) / (10 ** dec_weth)
                    else:
                        # price = token0/token1 = token per WETH, invert
                        eth_per_token = (1 / price_ratio) * (10 ** dec_token) / (10 ** dec_weth) if price_ratio > 0 else 0
                    return human * eth_per_token * eth_price
        except Exception:
            pass
        # Fallback: assume $1 per token (better than $0.01)
        return human * 1.0

    def _estimate_gas_cost_usd(self, gas_estimate: int) -> float:
        """Estimate gas cost in USD."""
        gas_price = self.w3.eth.gas_price
        cost_eth = float(Web3.from_wei(gas_price * gas_estimate, "ether"))
        # Rough ETH price estimate from WETH/USDC pool or fallback
        eth_price = self._get_eth_price()
        return cost_eth * eth_price

    def _get_eth_price(self) -> float:
        """Get approximate ETH price from Uniswap V3 WETH/USDC pool."""
        try:
            tokens = getattr(config, "ARB_TOKENS", {})
            usdc_addr = tokens.get("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
            weth_addr = ARBITRUM_CONTRACTS["weth"]
            pool_addr = self.factory.functions.getPool(
                Web3.to_checksum_address(weth_addr),
                Web3.to_checksum_address(usdc_addr),
                500,  # 0.05% fee tier
            ).call()
            if pool_addr == "0x0000000000000000000000000000000000000000":
                return 2000.0  # fallback
            pool = self.w3.eth.contract(
                address=Web3.to_checksum_address(pool_addr), abi=UNISWAP_V3_POOL_ABI
            )
            slot0 = pool.functions.slot0().call()
            sqrt_price_x96 = slot0[0]
            price = (sqrt_price_x96 / (2 ** 96)) ** 2
            # Adjust for decimals (WETH=18, USDC=6)
            token0 = pool.functions.token0().call()
            if token0.lower() == weth_addr.lower():
                return price * (10 ** 12)  # 18-6=12
            else:
                return (1 / price) * (10 ** 12) if price > 0 else 2000.0
        except Exception:
            return 2000.0

    def _send_tx(self, tx_func, value=0) -> dict:
        """Build, sign and send a transaction. Returns receipt or raises."""
        tx = tx_func.build_transaction({
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address),
            "gas": 800_000,
            "maxFeePerGas": self.w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": self.w3.to_wei(0.01, "gwei"),
            "chainId": self.chain_id,
            "value": value,
        })
        # Estimate actual gas
        try:
            estimated = self.w3.eth.estimate_gas(tx)
            tx["gas"] = int(estimated * 1.3)
        except Exception:
            pass  # Use default 800k

        signed = self.w3.eth.account.sign_transaction(tx, self._private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] != 1:
            raise Exception(f"Tx reverted: {tx_hash.hex()}")
        return receipt

    # ─── Pool Discovery ───

    # Hardcoded fallback pools on Arbitrum (used when DeFiLlama API is down)
    FALLBACK_POOLS = [
        {"symbol": "USDC-USDT", "project": "uniswap-v3", "apy": 15.0, "tvl": 5_000_000,
         "stable_coin": True, "il7d": 0, "apy_base": 15.0, "apy_reward": 0, "pool_id": "fallback-usdc-usdt"},
        {"symbol": "USDC-USDC.e", "project": "uniswap-v3", "apy": 8.0, "tvl": 3_000_000,
         "stable_coin": True, "il7d": 0, "apy_base": 8.0, "apy_reward": 0, "pool_id": "fallback-usdc-usdce"},
        {"symbol": "WETH-USDC", "project": "uniswap-v3", "apy": 25.0, "tvl": 50_000_000,
         "stable_coin": False, "il7d": 2.0, "apy_base": 25.0, "apy_reward": 0, "pool_id": "fallback-weth-usdc"},
        {"symbol": "WETH-ARB", "project": "uniswap-v3", "apy": 30.0, "tvl": 10_000_000,
         "stable_coin": False, "il7d": 3.0, "apy_base": 30.0, "apy_reward": 0, "pool_id": "fallback-weth-arb"},
    ]

    def _fetch_pool_yields(self) -> list:
        """Fetch pool APY data from DeFiLlama yields API, with hardcoded fallback."""
        now = time.time()
        if self._pool_cache and now - self._pool_cache_time < 600:
            return self._pool_cache

        try:
            resp = requests.get("https://yields.llama.fi/pools", timeout=15)
            resp.raise_for_status()
            all_pools = resp.json().get("data", [])

            pools = []
            for p in all_pools:
                if p.get("chain") != "Arbitrum":
                    continue
                if p.get("project") not in ("uniswap-v3", "camelot-v3"):
                    continue
                tvl = p.get("tvlUsd", 0) or 0
                if tvl < 1000:
                    continue
                apy = p.get("apyMean30d") or p.get("apy") or 0
                if apy < getattr(config, "ARB_LP_MIN_APY", 5.0):
                    continue
                pools.append({
                    "pool_id": p.get("pool", ""),
                    "project": p.get("project", ""),
                    "symbol": p.get("symbol", ""),
                    "tvl": tvl,
                    "apy": apy,
                    "apy_base": p.get("apyBase") or 0,
                    "apy_reward": p.get("apyReward") or 0,
                    "il7d": p.get("il7d") or 0,
                    "stable_coin": p.get("stablecoin", False),
                })

            # Sort by APY descending
            pools.sort(key=lambda x: x["apy"], reverse=True)
            self._pool_cache = pools
            self._pool_cache_time = now
            return pools
        except Exception as e:
            print(f"[ARB-LP:{self.label}] DeFiLlama API unavailable, using fallback pools")
            if self._pool_cache:
                return self._pool_cache
            # Use hardcoded fallback pools
            return self.FALLBACK_POOLS

    def _select_best_pool(self, pools: list) -> dict | None:
        """Score and select the best pool for our capital."""
        if not pools:
            return None

        prefer_stables = getattr(config, "ARB_LP_PREFER_STABLES", True)
        alloc = getattr(config, "ARB_LP_ALLOC_USD", 2.50)
        known_tokens = {k.upper() for k in getattr(config, "ARB_TOKENS", {}).keys()}

        scored = []
        for p in pools:
            # Only consider pools where we know both tokens
            symbol = p.get("symbol", "")
            parts = symbol.replace("/", "-").split("-")
            if len(parts) < 2:
                continue
            if parts[0].strip().upper() not in known_tokens or parts[1].strip().upper() not in known_tokens:
                continue
            # Score components
            apy_score = min(p["apy"] / 100, 1.0)  # Normalize to 0-1
            tvl_score = min(p["tvl"] / 1_000_000, 1.0)  # Higher TVL = more stable
            il_score = 1.0 - min(abs(p.get("il7d", 0)) / 10, 1.0)  # Lower IL = better
            stable_bonus = 0.3 if p.get("stable_coin") and prefer_stables else 0

            # For tiny capital, prefer high-fee stablecoin pools
            score = apy_score * 0.5 + tvl_score * 0.1 + il_score * 0.2 + stable_bonus

            # Penalize pools where our capital is insignificant
            if alloc < 5 and p["tvl"] > 10_000_000:
                score *= 0.5  # Our $2.50 is meaningless in a $10M+ pool

            scored.append({**p, "score": score})

        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0] if scored else None

        if best:
            print(f"[ARB-LP:{self.label}] Best pool: {best['symbol']} ({best['project']}) "
                  f"APY: {best['apy']:.1f}% TVL: ${best['tvl']:,.0f} Score: {best['score']:.2f}")

        return best

    # ─── Pool Resolution ───

    def _resolve_pool_tokens(self, pool_info: dict) -> dict | None:
        """Resolve pool symbol to on-chain token addresses and fee tier."""
        symbol = pool_info.get("symbol", "")
        tokens = getattr(config, "ARB_TOKENS", {})

        # Parse symbol like "USDC-USDT" or "WETH-USDC"
        parts = symbol.replace("/", "-").split("-")
        if len(parts) < 2:
            print(f"[ARB-LP:{self.label}] Cannot parse pool symbol: {symbol}")
            return None

        token0_name = parts[0].strip().upper()
        token1_name = parts[1].strip().upper()

        token0_addr = tokens.get(token0_name)
        token1_addr = tokens.get(token1_name)

        if not token0_addr or not token1_addr:
            print(f"[ARB-LP:{self.label}] Unknown tokens: {token0_name}={token0_addr}, {token1_name}={token1_addr}")
            return None

        # Try fee tiers ordered by likelihood of success.
        # 500 (0.05%) and 3000 (0.30%) are the most liquid for volatile pairs.
        # 100 (0.01%) only for stablecoin pairs (tight spacing can cause mint issues).
        is_stable = token0_name in ("USDC", "USDT", "USDC.e", "DAI") and \
                    token1_name in ("USDC", "USDT", "USDC.e", "DAI")
        fee_tiers = [100, 500, 3000, 10000] if is_stable else [500, 3000, 10000, 100]
        for fee in fee_tiers:
            pool_addr = self.factory.functions.getPool(
                Web3.to_checksum_address(token0_addr),
                Web3.to_checksum_address(token1_addr),
                fee,
            ).call()
            if pool_addr != "0x0000000000000000000000000000000000000000":
                return {
                    "pool_address": pool_addr,
                    "token0_name": token0_name,
                    "token1_name": token1_name,
                    "token0": token0_addr,
                    "token1": token1_addr,
                    "fee": fee,
                }

        print(f"[ARB-LP:{self.label}] No Uniswap V3 pool found for {token0_name}/{token1_name}")
        return None

    # ─── Target Pool Resolution ───

    def _resolve_target_pool(self, target: str = "WETH-USDC") -> dict | None:
        """Resolve a specific target pool (e.g. 'WETH-USDC') to on-chain addresses.

        This bypasses the DeFiLlama scoring and directly resolves the pool.
        Used for forced pool targeting (config.ARB_LP_TARGET_POOL).
        """
        pool_info = {"symbol": target}
        resolved = self._resolve_pool_tokens(pool_info)
        if resolved:
            print(f"[ARB-LP:{self.label}] Resolved target pool {target}: "
                  f"{resolved['token0_name']}/{resolved['token1_name']} fee={resolved['fee']}")
        return resolved

    # ─── Tick Math ───

    def _price_to_tick(self, price: float) -> int:
        """Convert price to nearest tick."""
        if price <= 0:
            return 0
        return int(math.floor(math.log(price) / math.log(1.0001)))

    def _tick_to_price(self, tick: int) -> float:
        """Convert tick to price."""
        return 1.0001 ** tick

    def _align_tick(self, tick: int, tick_spacing: int, round_up: bool = False) -> int:
        """Align tick to tick spacing."""
        if round_up:
            return int(math.ceil(tick / tick_spacing)) * tick_spacing
        return int(math.floor(tick / tick_spacing)) * tick_spacing

    def _calculate_tick_range(self, pool_contract, pool_resolved: dict) -> tuple:
        """Calculate optimal tick range for the position.

        For stablecoins: very tight range for max fee capture.
        For volatile pairs: wider range to stay in range longer.
        """
        slot0 = pool_contract.functions.slot0().call()
        current_tick = slot0[1]
        tick_spacing = pool_contract.functions.tickSpacing().call()

        is_stable = pool_resolved["token0_name"] in ("USDC", "USDT", "USDC.e", "DAI") and \
                    pool_resolved["token1_name"] in ("USDC", "USDT", "USDC.e", "DAI")

        if is_stable:
            # Stablecoin pair: very tight range (+/- 10 ticks)
            width = max(10 * tick_spacing, tick_spacing)
        else:
            # Volatile pair: wider range (+/- 200 ticks)
            width = max(200 * tick_spacing, tick_spacing * 20)

        tick_lower = self._align_tick(current_tick - width, tick_spacing, round_up=False)
        tick_upper = self._align_tick(current_tick + width, tick_spacing, round_up=True)

        # Ensure they're different
        if tick_lower >= tick_upper:
            tick_upper = tick_lower + tick_spacing

        return tick_lower, tick_upper, current_tick

    # ─── Token Management ───

    def _approve_token(self, token_address: str, spender: str, amount: int):
        """Approve ERC20 spending if needed."""
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address), abi=ERC20_ABI
        )
        current = token.functions.allowance(self.address, Web3.to_checksum_address(spender)).call()
        if current >= amount:
            return  # Already approved

        print(f"[ARB-LP:{self.label}] Approving token {token_address[:10]}... for {spender[:10]}...")
        max_approval = 2 ** 256 - 1
        tx_func = token.functions.approve(Web3.to_checksum_address(spender), max_approval)
        self._send_tx(tx_func)
        print(f"[ARB-LP:{self.label}] Approved")

    def _wrap_eth(self, amount_eth: float):
        """Wrap ETH to WETH."""
        value = self.w3.to_wei(amount_eth, "ether")
        tx_func = self.weth_contract.functions.deposit()
        self._send_tx(tx_func, value=value)
        print(f"[ARB-LP:{self.label}] Wrapped {amount_eth:.6f} ETH -> WETH")

    def _swap_for_tokens(self, token_in: str, token_out: str, amount_in: int, fee: int = 3000) -> int:
        """Swap tokens via Uniswap V3 router."""
        self._approve_token(token_in, ARBITRUM_CONTRACTS["uniswap_v3_swap_router"], amount_in)

        deadline = int(time.time()) + 300
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
            self.address,
            deadline,
            amount_in,
            0,  # amountOutMinimum (accept any for small amounts)
            0,  # sqrtPriceLimitX96
        )
        tx_func = self.swap_router.functions.exactInputSingle(params)
        receipt = self._send_tx(tx_func)
        print(f"[ARB-LP:{self.label}] Swapped tokens (tx: {receipt['transactionHash'].hex()[:12]}...)")
        return 0  # Actual amount from logs, but we check balance after

    def _convert_all_to_pool_tokens(self, pool_resolved: dict):
        """Convert ALL non-pool tokens in wallet to pool tokens (WETH + USDC).

        Scans every token in ARB_TOKENS config. Any token that is NOT one of
        the two pool tokens gets swapped into WETH (easiest route for most tokens).
        Then wraps any native ETH (keeping a small gas reserve).

        This ensures maximum capital goes into the LP position.
        """
        tokens = getattr(config, "ARB_TOKENS", {})
        weth = ARBITRUM_CONTRACTS["weth"]
        pool_token0 = pool_resolved["token0"].lower()
        pool_token1 = pool_resolved["token1"].lower()

        # 1. Wrap native ETH first (keep gas reserve)
        eth_balance = self._get_eth_balance()
        gas_reserve = 0.0003  # ~$0.60 at $2000/ETH — enough for several txs
        wrappable = eth_balance - gas_reserve
        if wrappable > 0.00005:
            print(f"[ARB-LP:{self.label}] Wrapping {wrappable:.6f} ETH -> WETH (keeping {gas_reserve} for gas)")
            self._wrap_eth(wrappable)

        # 2. Swap ALL non-pool tokens to WETH (best liquidity route)
        target_token = weth  # Most tokens have WETH pairs with deep liquidity
        for token_name, token_addr in tokens.items():
            addr_lower = token_addr.lower()

            # Skip if it's already a pool token
            if addr_lower == pool_token0 or addr_lower == pool_token1:
                continue

            # Check balance
            raw_bal = self._get_token_balance(token_addr)
            if raw_bal == 0:
                continue

            usd_val = self._token_value_usd(token_addr, raw_bal)
            if usd_val < 0.20:  # Not worth swapping dust < $0.20
                continue

            # Determine best fee tier for swap (try 3000 first, then 10000, then 500)
            for fee in [3000, 10000, 500]:
                try:
                    pool_addr = self.factory.functions.getPool(
                        Web3.to_checksum_address(token_addr),
                        Web3.to_checksum_address(target_token),
                        fee,
                    ).call()
                    if pool_addr != "0x0000000000000000000000000000000000000000":
                        print(f"[ARB-LP:{self.label}] Converting {token_name} (${usd_val:.2f}) -> WETH (fee={fee})")
                        self._swap_for_tokens(token_addr, target_token, raw_bal, fee)
                        break
                except Exception as e:
                    print(f"[ARB-LP:{self.label}] Failed to swap {token_name}: {e}")
                    continue

        # 3. Now we should have WETH + USDC. If pool is WETH-USDC, ensure 50/50 split
        weth_bal = self._get_token_balance(weth)
        weth_usd = self._token_value_usd(weth, weth_bal)

        # Find which pool token is USDC and which is WETH
        usdc_addr = tokens.get("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831")
        usdc_bal = self._get_token_balance(usdc_addr)
        usdc_usd = self._token_value_usd(usdc_addr, usdc_bal)

        total_usd = weth_usd + usdc_usd
        print(f"[ARB-LP:{self.label}] After conversion: WETH=${weth_usd:.2f} USDC=${usdc_usd:.2f} Total=${total_usd:.2f}")

        # Swap half of the dominant token to get a ~50/50 split
        if weth_usd > usdc_usd * 1.5 and weth_bal > 0:
            # Too much WETH, swap ~half to USDC
            swap_amount = weth_bal // 2
            if swap_amount > 0:
                print(f"[ARB-LP:{self.label}] Balancing: swapping ~50% WETH -> USDC")
                try:
                    self._swap_for_tokens(weth, usdc_addr, swap_amount, pool_resolved.get("fee", 500))
                except Exception as e:
                    print(f"[ARB-LP:{self.label}] Balance swap WETH->USDC failed: {e}")
        elif usdc_usd > weth_usd * 1.5 and usdc_bal > 0:
            # Too much USDC, swap ~half to WETH
            swap_amount = usdc_bal // 2
            if swap_amount > 0:
                print(f"[ARB-LP:{self.label}] Balancing: swapping ~50% USDC -> WETH")
                try:
                    self._swap_for_tokens(usdc_addr, weth, swap_amount, pool_resolved.get("fee", 500))
                except Exception as e:
                    print(f"[ARB-LP:{self.label}] Balance swap USDC->WETH failed: {e}")

        # Final state
        final_weth = self._token_value_usd(weth, self._get_token_balance(weth))
        final_usdc = self._token_value_usd(usdc_addr, self._get_token_balance(usdc_addr))
        print(f"[ARB-LP:{self.label}] Final split: WETH=${final_weth:.2f} USDC=${final_usdc:.2f}")

    def _ensure_tokens(self, pool_resolved: dict, alloc_usd: float):
        """Ensure we have both tokens for the pool. Split allocation 50/50."""
        token0 = pool_resolved["token0"]
        token1 = pool_resolved["token1"]
        weth = ARBITRUM_CONTRACTS["weth"]
        tokens = getattr(config, "ARB_TOKENS", {})
        usdc_addr = tokens.get("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831")

        bal0 = self._get_token_balance(token0)
        bal1 = self._get_token_balance(token1)
        bal0_human = self._token_to_human(bal0, token0)
        bal1_human = self._token_to_human(bal1, token1)

        print(f"[ARB-LP:{self.label}] Token0 ({pool_resolved['token0_name']}): {bal0_human:.6f}")
        print(f"[ARB-LP:{self.label}] Token1 ({pool_resolved['token1_name']}): {bal1_human:.6f}")

        # For stablecoin pairs, check if we already have enough
        half_alloc = alloc_usd / 2

        # If we don't have token0, try to get it
        need_token0 = bal0_human < 0.01
        need_token1 = bal1_human < 0.01

        if not need_token0 and not need_token1:
            return  # Have both tokens

        # Check what we have to swap from
        eth_balance = self._get_eth_balance()
        usdc_balance = self._token_to_human(self._get_token_balance(usdc_addr), usdc_addr)

        # Strategy: use whatever we have (ETH or USDC) to get what we need
        if eth_balance > 0.0005 and (need_token0 or need_token1):
            # Wrap some ETH to WETH first
            wrap_amount = eth_balance * 0.7  # Keep 30% for gas
            if wrap_amount > 0.0001:
                self._wrap_eth(wrap_amount)
                weth_bal = self._get_token_balance(weth)

                if need_token0 and token0.lower() != weth.lower():
                    swap_amount = weth_bal // 2 if need_token1 else weth_bal
                    if swap_amount > 0:
                        self._swap_for_tokens(weth, token0, swap_amount, pool_resolved.get("fee", 3000))

                if need_token1 and token1.lower() != weth.lower():
                    weth_bal = self._get_token_balance(weth)
                    if weth_bal > 0:
                        self._swap_for_tokens(weth, token1, weth_bal, pool_resolved.get("fee", 3000))

        elif usdc_balance > 0.5:
            # Use USDC to swap
            if need_token0 and token0.lower() != usdc_addr.lower():
                amount = self._human_to_token(usdc_balance / 2, usdc_addr)
                self._swap_for_tokens(usdc_addr, token0, amount)

            if need_token1 and token1.lower() != usdc_addr.lower():
                usdc_raw = self._get_token_balance(usdc_addr)
                if usdc_raw > 0:
                    self._swap_for_tokens(usdc_addr, token1, usdc_raw)

    # ─── Liquidity Provision ───

    def _add_liquidity(self, pool_resolved: dict) -> int | None:
        """Mint a new concentrated liquidity position. Returns token_id."""
        pool_addr = pool_resolved["pool_address"]
        pool = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=UNISWAP_V3_POOL_ABI
        )

        tick_lower, tick_upper, current_tick = self._calculate_tick_range(pool, pool_resolved)

        token0 = pool_resolved["token0"]
        token1 = pool_resolved["token1"]

        # Sort tokens (Uniswap requires token0 < token1)
        if int(token0, 16) > int(token1, 16):
            token0, token1 = token1, token0
            pool_resolved["token0"], pool_resolved["token1"] = token1, token0

        bal0 = self._get_token_balance(token0)
        bal1 = self._get_token_balance(token1)

        if bal0 == 0 and bal1 == 0:
            print(f"[ARB-LP:{self.label}] No tokens to provide as liquidity")
            return None

        # Check minimum value to avoid revert on dust amounts
        t0_usd = self._token_value_usd(token0, bal0)
        t1_usd = self._token_value_usd(token1, bal1)
        total_usd = t0_usd + t1_usd
        if total_usd < 0.30:
            print(f"[ARB-LP:{self.label}] Token value too low (${total_usd:.2f}), skipping mint")
            return None

        nft_addr = ARBITRUM_CONTRACTS["uniswap_v3_nft_manager"]

        # Approve both tokens
        if bal0 > 0:
            self._approve_token(token0, nft_addr, bal0)
        if bal1 > 0:
            self._approve_token(token1, nft_addr, bal1)

        # Check gas cost
        gas_cost = self._estimate_gas_cost_usd(350_000)
        max_gas_pct = getattr(config, "ARB_LP_MAX_GAS_PCT", 0.10)
        alloc = getattr(config, "ARB_LP_ALLOC_USD", 2.50)
        if gas_cost > alloc * max_gas_pct:
            print(f"[ARB-LP:{self.label}] Gas too expensive: ${gas_cost:.4f} > {max_gas_pct*100}% of ${alloc}")
            return None

        deadline = int(time.time()) + 300
        params = (
            Web3.to_checksum_address(token0),
            Web3.to_checksum_address(token1),
            pool_resolved["fee"],
            tick_lower,
            tick_upper,
            bal0,
            bal1,
            0,  # amount0Min
            0,  # amount1Min
            self.address,
            deadline,
        )

        print(f"[ARB-LP:{self.label}] Minting position: ticks [{tick_lower}, {tick_upper}] "
              f"current: {current_tick}")

        tx_func = self.nft_manager.functions.mint(params)
        receipt = self._send_tx(tx_func)

        # Extract tokenId from Transfer event logs (ERC721 Transfer from 0x0 = mint)
        token_id = None
        transfer_hash = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        nft_addr_lower = ARBITRUM_CONTRACTS["uniswap_v3_nft_manager"].lower()
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            log_addr = log.get("address", "").lower()
            if len(topics) >= 4 and log_addr == nft_addr_lower:
                t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
                # .hex() may or may not include 0x prefix depending on web3 version
                if transfer_hash in t0:
                    raw = topics[3].hex() if hasattr(topics[3], "hex") else str(topics[3])
                    raw = raw.replace("0x", "")
                    token_id = int(raw, 16)
                    break

        if token_id:
            print(f"[ARB-LP:{self.label}] Position minted! Token ID: {token_id}")
        else:
            print(f"[ARB-LP:{self.label}] Position minted (tx: {receipt['transactionHash'].hex()[:12]}...) but could not extract token ID")

        return token_id

    def _increase_liquidity(self) -> bool:
        """Add more liquidity to the existing position using available wallet tokens.
        If only one token is available and the position is in-range, swaps half to
        get the other token first. Returns True if liquidity was added."""
        pos = self.active_position
        if not pos or not pos.get("token_id"):
            return False

        token0 = pos.get("token0")
        token1 = pos.get("token1")
        fee = None
        if not token0 or not token1:
            # Read from on-chain position
            try:
                on_chain = self.nft_manager.functions.positions(pos["token_id"]).call()
                token0 = on_chain[2]
                token1 = on_chain[3]
                fee = on_chain[4]
                pos["token0"] = token0
                pos["token1"] = token1
            except Exception:
                print(f"[ARB-LP:{self.label}] Cannot read position tokens for increase")
                return False

        # Get fee tier from position or pool info
        if fee is None:
            pool_info = pos.get("pool", {})
            fee = pool_info.get("fee", 500) if isinstance(pool_info, dict) else 500

        token_id = pos["token_id"]

        # Convert any USDC/other stablecoins to pool tokens if we have them
        tokens = getattr(config, "ARB_TOKENS", {})
        weth = ARBITRUM_CONTRACTS["weth"]
        for stable_name in ("USDC", "USDC.e", "USDT"):
            stable_addr = tokens.get(stable_name)
            if not stable_addr or stable_addr.lower() in (token0.lower(), token1.lower()):
                continue
            stable_bal = self._get_token_balance(stable_addr)
            stable_usd = self._token_value_usd(stable_addr, stable_bal)
            if stable_usd > 0.30:
                # Swap stablecoins to WETH (or whichever pool token is easier)
                target = token1 if token1.lower() == weth.lower() else token0 if token0.lower() == weth.lower() else token1
                print(f"[ARB-LP:{self.label}] Converting {stable_name} (${stable_usd:.2f}) -> pool token for LP")
                try:
                    self._swap_for_tokens(stable_addr, target, stable_bal, 500)
                except Exception as e:
                    print(f"[ARB-LP:{self.label}] Stable swap failed: {e}")

        bal0 = self._get_token_balance(token0)
        bal1 = self._get_token_balance(token1)

        # Check minimum value
        t0_usd = self._token_value_usd(token0, bal0)
        t1_usd = self._token_value_usd(token1, bal1)
        total_usd = t0_usd + t1_usd

        t0_name = self._addr_to_token_name(token0) or token0[:10]
        t1_name = self._addr_to_token_name(token1) or token1[:10]
        print(f"[ARB-LP:{self.label}] Free tokens: {t0_name}=${t0_usd:.4f} {t1_name}=${t1_usd:.4f} (total=${total_usd:.4f})")

        if total_usd < 0.30:
            print(f"[ARB-LP:{self.label}] Skipping increase: free tokens < $0.30")
            return False

        # If only one token available, swap half to get the other (needed for in-range positions)
        has_t0 = bal0 > 0 and t0_usd > 0.05
        has_t1 = bal1 > 0 and t1_usd > 0.05
        if has_t0 and not has_t1:
            swap_amount = bal0 // 2
            print(f"[ARB-LP:{self.label}] Swapping half {t0_name} -> {t1_name} for balanced add")
            try:
                self._swap_for_tokens(token0, token1, swap_amount, fee)
            except Exception as e:
                print(f"[ARB-LP:{self.label}] Swap failed: {e}")
                return False
            bal0 = self._get_token_balance(token0)
            bal1 = self._get_token_balance(token1)
        elif has_t1 and not has_t0:
            swap_amount = bal1 // 2
            print(f"[ARB-LP:{self.label}] Swapping half {t1_name} -> {t0_name} for balanced add")
            try:
                self._swap_for_tokens(token1, token0, swap_amount, fee)
            except Exception as e:
                print(f"[ARB-LP:{self.label}] Swap failed: {e}")
                return False
            bal0 = self._get_token_balance(token0)
            bal1 = self._get_token_balance(token1)

        if bal0 == 0 and bal1 == 0:
            print(f"[ARB-LP:{self.label}] No tokens after swap, skipping")
            return False

        # Approve tokens
        nft_addr = ARBITRUM_CONTRACTS["uniswap_v3_nft_manager"]
        if bal0 > 0:
            self._approve_token(token0, nft_addr, bal0)
        if bal1 > 0:
            self._approve_token(token1, nft_addr, bal1)

        # Check gas
        gas_cost = self._estimate_gas_cost_usd(250_000)
        if gas_cost > total_usd * 0.20:
            print(f"[ARB-LP:{self.label}] Gas too high for increase (${gas_cost:.4f} vs ${total_usd:.2f})")
            return False

        t0_usd_new = self._token_value_usd(token0, bal0)
        t1_usd_new = self._token_value_usd(token1, bal1)
        deadline = int(time.time()) + 300
        params = (
            token_id,
            bal0,
            bal1,
            0,  # amount0Min
            0,  # amount1Min
            deadline,
        )

        print(f"[ARB-LP:{self.label}] Increasing liquidity on #{token_id}: "
              f"token0=${t0_usd_new:.2f} token1=${t1_usd_new:.2f} (total +${t0_usd_new + t1_usd_new:.2f})")

        try:
            tx_func = self.nft_manager.functions.increaseLiquidity(params)
            receipt = self._send_tx(tx_func)
            print(f"[ARB-LP:{self.label}] Liquidity increased! tx: {receipt['transactionHash'].hex()[:12]}...")
            return True
        except Exception as e:
            print(f"[ARB-LP:{self.label}] Increase liquidity failed: {e}")
            return False

    def _check_position(self) -> dict | None:
        """Check active position status."""
        if not self.active_position or not self.active_position.get("token_id"):
            return None

        token_id = self.active_position["token_id"]
        try:
            pos = self.nft_manager.functions.positions(token_id).call()
            # pos = (nonce, operator, token0, token1, fee, tickLower, tickUpper, liquidity,
            #        feeGrowthInside0LastX128, feeGrowthInside1LastX128, tokensOwed0, tokensOwed1)
            liquidity = pos[7]
            tick_lower = pos[5]
            tick_upper = pos[6]
            tokens_owed0 = pos[10]
            tokens_owed1 = pos[11]

            # Get current tick
            pool_addr = self.active_position.get("pool_address")
            if pool_addr:
                pool = self.w3.eth.contract(
                    address=Web3.to_checksum_address(pool_addr), abi=UNISWAP_V3_POOL_ABI
                )
                slot0 = pool.functions.slot0().call()
                current_tick = slot0[1]
                in_range = tick_lower <= current_tick <= tick_upper
            else:
                current_tick = 0
                in_range = True

            return {
                "token_id": token_id,
                "liquidity": liquidity,
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "current_tick": current_tick,
                "in_range": in_range,
                "tokens_owed0": tokens_owed0,
                "tokens_owed1": tokens_owed1,
            }
        except Exception as e:
            print(f"[ARB-LP:{self.label}] Error checking position: {e}")
            return None

    def _collect_fees(self, token_id: int):
        """Collect accumulated fees and auto-compound back into the position."""
        max_uint128 = 2 ** 128 - 1
        params = (token_id, self.address, max_uint128, max_uint128)

        # Gas for collect + increaseLiquidity (compound)
        gas_cost = self._estimate_gas_cost_usd(300_000)
        min_collect = getattr(config, "ARB_LP_FEE_COLLECT_MIN_USD", 0.02)
        if gas_cost > min_collect:
            print(f"[ARB-LP:{self.label}] Fee collection gas (${gas_cost:.4f}) > min threshold (${min_collect})")
            return

        # Get token addresses from position
        pos = self.nft_manager.functions.positions(token_id).call()
        token0 = pos[2]
        token1 = pos[3]

        # Snapshot balances before collect
        bal0_before = self._get_token_balance(token0)
        bal1_before = self._get_token_balance(token1)

        print(f"[ARB-LP:{self.label}] Collecting fees for position {token_id}...")
        tx_func = self.nft_manager.functions.collect(params)
        self._send_tx(tx_func)
        self.last_fee_collection = time.time()

        # Calculate collected amounts
        bal0_after = self._get_token_balance(token0)
        bal1_after = self._get_token_balance(token1)
        collected0 = bal0_after - bal0_before
        collected1 = bal1_after - bal1_before

        if collected0 <= 0 and collected1 <= 0:
            print(f"[ARB-LP:{self.label}] No fees to compound")
            return

        print(f"[ARB-LP:{self.label}] Fees collected, compounding back into position...")

        # Auto-compound: add collected fees back as liquidity
        try:
            nft_addr = ARBITRUM_CONTRACTS["uniswap_v3_nft_manager"]
            if collected0 > 0:
                self._approve_token(token0, nft_addr, collected0)
            if collected1 > 0:
                self._approve_token(token1, nft_addr, collected1)

            deadline = int(time.time()) + 300
            increase_params = (token_id, collected0, collected1, 0, 0, deadline)
            tx_func = self.nft_manager.functions.increaseLiquidity(increase_params)
            self._send_tx(tx_func)
            print(f"[ARB-LP:{self.label}] Auto-compounded fees into position")
        except Exception as e:
            print(f"[ARB-LP:{self.label}] Compound failed (fees kept in wallet): {e}")

    def _remove_liquidity(self, token_id: int):
        """Remove all liquidity from position."""
        try:
            pos = self.nft_manager.functions.positions(token_id).call()
            liquidity = pos[7]

            if liquidity == 0:
                print(f"[ARB-LP:{self.label}] Position {token_id} has no liquidity")
                return

            deadline = int(time.time()) + 300
            params = (token_id, liquidity, 0, 0, deadline)

            print(f"[ARB-LP:{self.label}] Removing liquidity from position {token_id}...")
            tx_func = self.nft_manager.functions.decreaseLiquidity(params)
            self._send_tx(tx_func)

            # Collect remaining tokens
            max_uint128 = 2 ** 128 - 1
            collect_params = (token_id, self.address, max_uint128, max_uint128)
            tx_func = self.nft_manager.functions.collect(collect_params)
            self._send_tx(tx_func)

            print(f"[ARB-LP:{self.label}] Liquidity removed and tokens collected")
        except Exception as e:
            print(f"[ARB-LP:{self.label}] Error removing liquidity: {e}")

    def _should_rebalance(self, status: dict) -> bool:
        """Determine if position needs rebalancing.

        Tracks the actual time the position has been OUT OF RANGE (not entry time).
        Only rebalances after ARB_LP_REBALANCE_AFTER_OOR_MIN minutes continuously OOR.
        """
        if status.get("in_range", True):
            # Position is back in range, reset OOR timer
            self._oor_since = None
            return False

        # Position is out of range — start tracking OOR time if not already
        now = time.time()
        if self._oor_since is None:
            self._oor_since = now
            print(f"[ARB-LP:{self.label}] Position went OUT OF RANGE, starting OOR timer")
            return False

        # Check how long we've been continuously out of range
        oor_threshold = getattr(config, "ARB_LP_REBALANCE_AFTER_OOR_MIN", 30) * 60
        oor_duration = now - self._oor_since

        if oor_duration < oor_threshold:
            remaining_min = (oor_threshold - oor_duration) / 60
            print(f"[ARB-LP:{self.label}] OOR for {oor_duration/60:.1f}min, rebalance in {remaining_min:.1f}min")
            return False

        # Check if gas cost is reasonable
        gas_cost = self._estimate_gas_cost_usd(500_000)  # Remove + new mint
        alloc = getattr(config, "ARB_LP_ALLOC_USD", 2.50)
        max_gas_pct = getattr(config, "ARB_LP_MAX_GAS_PCT", 0.10)

        if gas_cost > alloc * max_gas_pct * 2:  # 2x threshold for rebalance
            print(f"[ARB-LP:{self.label}] Rebalance gas too high: ${gas_cost:.4f}")
            return False

        print(f"[ARB-LP:{self.label}] OOR for {oor_duration/60:.1f}min (threshold: {oor_threshold/60:.0f}min) -> REBALANCING")
        return True

    # ─── Position Recovery ───

    def _addr_to_token_name(self, addr: str) -> str | None:
        """Reverse-lookup a token address to its name in ARB_TOKENS."""
        addr_lower = addr.lower()
        for name, token_addr in getattr(config, "ARB_TOKENS", {}).items():
            if token_addr.lower() == addr_lower:
                return name
        return None

    def _recover_existing_positions(self):
        """Check for existing LP NFT positions owned by this wallet.
        Restores active_position state if a position with liquidity is found.
        Retries with RPC reconnect on failure."""
        for attempt in range(3):
            try:
                nft_count = self.nft_manager.functions.balanceOf(self.address).call()
                if nft_count == 0:
                    return
                print(f"[ARB-LP:{self.label}] Found {nft_count} existing NFT position(s), checking...")
                for i in range(nft_count):
                    token_id = self.nft_manager.functions.tokenOfOwnerByIndex(self.address, i).call()
                    pos = self.nft_manager.functions.positions(token_id).call()
                    liquidity = pos[7]
                    if liquidity > 0:
                        token0 = pos[2]
                        token1 = pos[3]
                        fee = pos[4]
                        pool_addr = self.factory.functions.getPool(
                            Web3.to_checksum_address(token0),
                            Web3.to_checksum_address(token1),
                            fee,
                        ).call()
                        t0_name = self._addr_to_token_name(token0) or "UNKNOWN"
                        t1_name = self._addr_to_token_name(token1) or "UNKNOWN"
                        symbol = f"{t0_name}-{t1_name}"
                        self.active_position = {
                            "token_id": token_id,
                            "pool": {"symbol": symbol, "apy": 0, "tvl": 0, "fee": fee},
                            "pool_address": pool_addr,
                            "token0": token0,
                            "token1": token1,
                            "entry_time": time.time(),
                        }
                        print(f"[ARB-LP:{self.label}] Recovered position #{token_id} ({symbol} fee={fee}, liquidity={liquidity})")
                        return
                return  # No positions with liquidity
            except Exception as e:
                print(f"[ARB-LP:{self.label}] Recovery attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    self._reconnect_rpc()
                    time.sleep(2)

    # ─── Main Cycle ───

    def run_cycle(self):
        """Run one LP management cycle. Called from bot.py main loop."""
        try:
            # 0. Recover existing positions if we lost state (restart)
            if not self.active_position:
                self._recover_existing_positions()

            # 0b. If recovered position is NOT in the target pool, dismantle it
            #     This handles migration from old pools (e.g. ZRO/WETH -> ETH/USDC)
            if self.active_position:
                target_pool = getattr(config, "ARB_LP_TARGET_POOL", "WETH-USDC")
                current_symbol = self.active_position.get("pool", {}).get("symbol", "")
                # Normalize: "ZRO-WETH" vs target "WETH-USDC"
                target_parts = set(target_pool.upper().replace("/", "-").split("-"))
                current_parts = set(current_symbol.upper().replace("/", "-").split("-"))

                if current_parts and current_parts != target_parts:
                    print(f"[ARB-LP:{self.label}] Current pool {current_symbol} != target {target_pool}")
                    print(f"[ARB-LP:{self.label}] DISMANTLING old position to migrate to {target_pool}...")
                    token_id = self.active_position.get("token_id")
                    if token_id:
                        # Collect any pending fees first
                        try:
                            self._collect_fees(token_id)
                        except Exception as e:
                            print(f"[ARB-LP:{self.label}] Fee collection before migration failed: {e}")
                        # Remove all liquidity
                        self._remove_liquidity(token_id)
                    self.active_position = None
                    self._oor_since = None
                    print(f"[ARB-LP:{self.label}] Old position dismantled, proceeding to mount {target_pool}")

            # 1. Check gas availability
            eth_balance = self._get_eth_balance()
            if eth_balance < 0.00005:  # ~$0.10 at $2000/ETH
                print(f"[ARB-LP:{self.label}] Insufficient ETH for gas: {eth_balance:.6f} ETH")
                return

            print(f"[ARB-LP:{self.label}] ETH balance: {eth_balance:.6f} (~${eth_balance * self._get_eth_price():.2f})")

            # 2. If no active position, convert ALL tokens and enter target pool
            if not self.active_position:
                arb_usd = self._get_arb_total_usd()
                print(f"[ARB-LP:{self.label}] Arbitrum total balance: ~${arb_usd:.2f}")

                if arb_usd < 0.50:
                    print(f"[ARB-LP:{self.label}] Insufficient funds on Arbitrum (need >$0.50), skipping")
                    return

                # Use target pool directly (no more DeFiLlama scoring)
                target_pool = getattr(config, "ARB_LP_TARGET_POOL", "WETH-USDC")
                print(f"[ARB-LP:{self.label}] Target pool: {target_pool}")

                resolved = self._resolve_target_pool(target_pool)
                if not resolved:
                    print(f"[ARB-LP:{self.label}] Could not resolve target pool {target_pool}")
                    return

                # Convert ALL tokens in wallet to pool tokens (WETH + USDC)
                print(f"[ARB-LP:{self.label}] Converting ALL tokens to pool tokens...")
                self._convert_all_to_pool_tokens(resolved)

                token_id = self._add_liquidity(resolved)
                if token_id:
                    pool_info = {"symbol": target_pool, "apy": 0, "tvl": 0, "fee": resolved["fee"]}
                    self.active_position = {
                        "token_id": token_id,
                        "pool": pool_info,
                        "pool_address": resolved["pool_address"],
                        "token0": resolved["token0"],
                        "token1": resolved["token1"],
                        "entry_time": time.time(),
                    }
                    print(f"[ARB-LP:{self.label}] Position active in {target_pool} (fee tier: {resolved['fee']})")
                return

            # 3. Monitor existing position
            status = self._check_position()
            if not status:
                print(f"[ARB-LP:{self.label}] Could not check position, clearing state")
                self.active_position = None
                return

            range_str = "IN RANGE" if status["in_range"] else "OUT OF RANGE"
            print(f"[ARB-LP:{self.label}] Position {status['token_id']}: {range_str} | "
                  f"Liquidity: {status['liquidity']} | "
                  f"Tick: {status['current_tick']} [{status['tick_lower']}, {status['tick_upper']}]")

            # 3b. Auto-compound: add free tokens to existing position
            self._increase_liquidity()

            # 4. Collect fees if worthwhile
            if status["tokens_owed0"] > 0 or status["tokens_owed1"] > 0:
                fee_interval = 3600  # Collect at most once per hour
                if time.time() - self.last_fee_collection >= fee_interval:
                    self._collect_fees(self.active_position["token_id"])

            # 5. Rebalance if needed — remove old position and IMMEDIATELY remint
            if self._should_rebalance(status):
                print(f"[ARB-LP:{self.label}] Rebalancing position (out of range)")
                self._remove_liquidity(self.active_position["token_id"])
                self.active_position = None
                self._oor_since = None  # Reset OOR timer

                # === IMMEDIATE REMINT: don't wait for next cycle ===
                print(f"[ARB-LP:{self.label}] Reminting position immediately after rebalance...")
                try:
                    arb_usd = self._get_arb_total_usd()
                    if arb_usd >= 0.50:
                        # Force ETH/USDC pool (configured target)
                        target_pool = getattr(config, "ARB_LP_TARGET_POOL", "WETH-USDC")
                        resolved = self._resolve_target_pool(target_pool)

                        if resolved:
                            self._convert_all_to_pool_tokens(resolved)
                            token_id = self._add_liquidity(resolved)
                            if token_id:
                                pool_info = {"symbol": target_pool, "apy": 0, "tvl": 0, "fee": resolved["fee"]}
                                self.active_position = {
                                    "token_id": token_id,
                                    "pool": pool_info,
                                    "pool_address": resolved["pool_address"],
                                    "token0": resolved["token0"],
                                    "token1": resolved["token1"],
                                    "entry_time": time.time(),
                                }
                                print(f"[ARB-LP:{self.label}] Reminted! New position #{token_id} in {target_pool}")
                            else:
                                print(f"[ARB-LP:{self.label}] Remint failed, will retry next cycle")
                        else:
                            print(f"[ARB-LP:{self.label}] Could not resolve {target_pool}, will retry next cycle")
                    else:
                        print(f"[ARB-LP:{self.label}] Insufficient funds (${arb_usd:.2f}) for remint")
                except Exception as e:
                    print(f"[ARB-LP:{self.label}] Remint error: {e}, will retry next cycle")

        except Exception as e:
            err_str = str(e)
            print(f"[ARB-LP:{self.label}] Error: {err_str}")
            if "Connection" in err_str or "Remote" in err_str or "timeout" in err_str.lower():
                self._reconnect_rpc()

    def get_active_pool_info(self) -> dict | None:
        """Return the active position's pool info for copy trading."""
        if not self.active_position:
            return None
        pool = self.active_position.get("pool")
        if not pool:
            return None
        return {
            "pool": pool,
            "pool_address": self.active_position.get("pool_address"),
            "has_position": True,
        }

    def _collect_lp_copy_fee(self, fee_recipient: str, alloc_usd: float) -> float:
        """Collect LP copy fee from follower and transfer to master.

        Charges ARB_LP_COPY_FEE_PCT of allocation in USDC.
        Returns the fee amount in USD (deducted from allocation).
        """
        fee_pct = getattr(config, "ARB_LP_COPY_FEE_PCT", 0.05)
        if fee_pct <= 0:
            return 0.0

        fee_usd = alloc_usd * fee_pct
        if fee_usd < 0.01:
            return 0.0

        usdc_addr = getattr(config, "ARB_TOKENS", {}).get("USDC")
        if not usdc_addr:
            print(f"[ARB-LP:{self.label}] No USDC address for fee collection")
            return 0.0

        try:
            usdc = self.w3.eth.contract(
                address=Web3.to_checksum_address(usdc_addr), abi=ERC20_ABI
            )
            decimals = usdc.functions.decimals().call()
            fee_amount = int(fee_usd * (10 ** decimals))

            balance = usdc.functions.balanceOf(self.address).call()
            if balance < fee_amount:
                print(f"[ARB-LP:{self.label}] Insufficient USDC for LP fee: have {balance / 10**decimals:.4f}, need {fee_usd:.4f}")
                return 0.0

            # Transfer fee to master
            tx_func = usdc.functions.transfer(
                Web3.to_checksum_address(fee_recipient), fee_amount
            )
            receipt = self._send_tx(tx_func)
            print(f"[ARB-LP:{self.label}] LP copy fee: ${fee_usd:.4f} USDC -> {fee_recipient[:10]}... (tx: {receipt['transactionHash'].hex()[:12]}...)")
            return fee_usd

        except Exception as e:
            print(f"[ARB-LP:{self.label}] LP copy fee error: {e}")
            return 0.0

    def mirror_master_pool(self, pool_info: dict, fee_recipient: str = None) -> float:
        """Enter the same pool as the master (used by followers).

        pool_info: dict from master's get_active_pool_info()
        fee_recipient: master wallet address to receive LP copy fee
        Returns: fee amount collected (0.0 if no fee or failed)
        """
        if self.active_position:
            return 0.0  # Already in a position

        pool = pool_info.get("pool")
        if not pool:
            return 0.0

        resolved = self._resolve_pool_tokens(pool)
        if not resolved:
            return 0.0

        # Check gas
        eth_balance = self._get_eth_balance()
        if eth_balance < 0.00005:
            print(f"[ARB-LP:{self.label}] Insufficient ETH for gas: {eth_balance:.6f}")
            return 0.0

        alloc = getattr(config, "ARB_LP_ALLOC_USD", 2.50)

        # Collect LP copy fee before minting
        fee_taken = 0.0
        if fee_recipient:
            fee_taken = self._collect_lp_copy_fee(fee_recipient, alloc)
            if fee_taken > 0:
                alloc -= fee_taken  # Reduce allocation by fee amount
                print(f"[ARB-LP:{self.label}] Allocation after fee: ${alloc:.4f}")

        self._ensure_tokens(resolved, alloc)

        token_id = self._add_liquidity(resolved)
        if token_id:
            self.active_position = {
                "token_id": token_id,
                "pool": pool,
                "pool_address": resolved["pool_address"],
                "entry_time": time.time(),
            }
            print(f"[ARB-LP:{self.label}] Mirrored master pool: {pool['symbol']} (fee: ${fee_taken:.4f})")

        return fee_taken

    def shutdown(self):
        """Remove all positions on bot shutdown."""
        if self.active_position and self.active_position.get("token_id"):
            print(f"[ARB-LP:{self.label}] Shutdown: removing position {self.active_position['token_id']}...")
            try:
                self._remove_liquidity(self.active_position["token_id"])
            except Exception as e:
                print(f"[ARB-LP:{self.label}] Shutdown error: {e}")
            self.active_position = None
