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

    def __init__(self, private_key: str = None, label: str = "MASTER", w3: Web3 = None):
        if w3:
            self.w3 = w3
        else:
            rpc_url = getattr(config, "ARB_RPC_URL", "https://arb1.arbitrum.io/rpc")
            self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
            if not self.w3.is_connected():
                fallback = getattr(config, "ARB_RPC_FALLBACK", "https://arbitrum.llamarpc.com")
                self.w3 = Web3(Web3.HTTPProvider(fallback, request_kwargs={"timeout": 15}))

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
        self._pool_cache = None
        self._pool_cache_time = 0
        self._token_decimals_cache = {}

    # ─── Helpers ───

    def _get_eth_balance(self) -> float:
        """Get native ETH balance on Arbitrum (for gas)."""
        wei = self.w3.eth.get_balance(self.address)
        return float(Web3.from_wei(wei, "ether"))

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
            "gas": 500_000,
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
            pass  # Use default 500k

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

        scored = []
        for p in pools:
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

        # Try common fee tiers: 100 (0.01%), 500 (0.05%), 3000 (0.30%), 10000 (1%)
        for fee in [100, 500, 3000, 10000]:
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

        # Extract tokenId from Transfer event logs
        token_id = None
        for log in receipt.get("logs", []):
            if len(log.get("topics", [])) >= 4:
                # Transfer event: topic[0]=Transfer, topic[1]=from, topic[2]=to, topic[3]=tokenId
                topic0 = log["topics"][0].hex() if hasattr(log["topics"][0], "hex") else log["topics"][0]
                if topic0 == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef":
                    token_id = int(log["topics"][3].hex(), 16) if hasattr(log["topics"][3], "hex") else int(log["topics"][3], 16)
                    break

        if token_id:
            print(f"[ARB-LP:{self.label}] Position minted! Token ID: {token_id}")
        else:
            print(f"[ARB-LP:{self.label}] Position minted (tx: {receipt['transactionHash'].hex()[:12]}...) but could not extract token ID")

        return token_id

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
        """Collect accumulated fees from position."""
        max_uint128 = 2 ** 128 - 1
        params = (token_id, self.address, max_uint128, max_uint128)

        gas_cost = self._estimate_gas_cost_usd(150_000)
        min_collect = getattr(config, "ARB_LP_FEE_COLLECT_MIN_USD", 0.02)
        if gas_cost > min_collect:
            print(f"[ARB-LP:{self.label}] Fee collection gas (${gas_cost:.4f}) > min threshold (${min_collect})")
            return

        print(f"[ARB-LP:{self.label}] Collecting fees for position {token_id}...")
        tx_func = self.nft_manager.functions.collect(params)
        self._send_tx(tx_func)
        self.last_fee_collection = time.time()
        print(f"[ARB-LP:{self.label}] Fees collected")

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
        """Determine if position needs rebalancing."""
        if status.get("in_range", True):
            return False

        # Check how long we've been out of range
        entry_time = self.active_position.get("entry_time", time.time())
        oor_threshold = getattr(config, "ARB_LP_REBALANCE_AFTER_OOR_MIN", 30) * 60

        if time.time() - entry_time < oor_threshold:
            return False  # Give it time

        # Check if gas cost is reasonable
        gas_cost = self._estimate_gas_cost_usd(500_000)  # Remove + new mint
        alloc = getattr(config, "ARB_LP_ALLOC_USD", 2.50)
        max_gas_pct = getattr(config, "ARB_LP_MAX_GAS_PCT", 0.10)

        if gas_cost > alloc * max_gas_pct * 2:  # 2x threshold for rebalance
            print(f"[ARB-LP:{self.label}] Rebalance gas too high: ${gas_cost:.4f}")
            return False

        return True

    # ─── Main Cycle ───

    def run_cycle(self):
        """Run one LP management cycle. Called from bot.py main loop."""
        try:
            # 1. Check gas availability
            eth_balance = self._get_eth_balance()
            if eth_balance < 0.00005:  # ~$0.10 at $2000/ETH
                print(f"[ARB-LP:{self.label}] Insufficient ETH for gas: {eth_balance:.6f} ETH")
                return

            print(f"[ARB-LP:{self.label}] ETH balance: {eth_balance:.6f} (~${eth_balance * self._get_eth_price():.2f})")

            # 2. If no active position, discover and enter
            if not self.active_position:
                pools = self._fetch_pool_yields()
                if not pools:
                    print(f"[ARB-LP:{self.label}] No pools found meeting criteria")
                    return

                best = self._select_best_pool(pools)
                if not best:
                    print(f"[ARB-LP:{self.label}] No suitable pool found")
                    return

                # Resolve to on-chain addresses
                resolved = self._resolve_pool_tokens(best)
                if not resolved:
                    return

                alloc = getattr(config, "ARB_LP_ALLOC_USD", 2.50)
                self._ensure_tokens(resolved, alloc)

                token_id = self._add_liquidity(resolved)
                if token_id:
                    self.active_position = {
                        "token_id": token_id,
                        "pool": best,
                        "pool_address": resolved["pool_address"],
                        "entry_time": time.time(),
                    }
                    print(f"[ARB-LP:{self.label}] Position active: {best['symbol']} (APY: {best['apy']:.1f}%)")
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

            # 4. Collect fees if worthwhile
            if status["tokens_owed0"] > 0 or status["tokens_owed1"] > 0:
                fee_interval = 3600  # Collect at most once per hour
                if time.time() - self.last_fee_collection >= fee_interval:
                    self._collect_fees(self.active_position["token_id"])

            # 5. Rebalance if needed
            if self._should_rebalance(status):
                print(f"[ARB-LP:{self.label}] Rebalancing position (out of range)")
                self._remove_liquidity(self.active_position["token_id"])
                self.active_position = None
                # Next cycle will re-enter with fresh pool selection

        except Exception as e:
            print(f"[ARB-LP:{self.label}] Error: {e}")

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

    def mirror_master_pool(self, pool_info: dict):
        """Enter the same pool as the master (used by followers).

        pool_info: dict from master's get_active_pool_info()
        """
        if self.active_position:
            return  # Already in a position

        pool = pool_info.get("pool")
        if not pool:
            return

        resolved = self._resolve_pool_tokens(pool)
        if not resolved:
            return

        # Check gas
        eth_balance = self._get_eth_balance()
        if eth_balance < 0.00005:
            print(f"[ARB-LP:{self.label}] Insufficient ETH for gas: {eth_balance:.6f}")
            return

        alloc = getattr(config, "ARB_LP_ALLOC_USD", 2.50)
        self._ensure_tokens(resolved, alloc)

        token_id = self._add_liquidity(resolved)
        if token_id:
            self.active_position = {
                "token_id": token_id,
                "pool": pool,
                "pool_address": resolved["pool_address"],
                "entry_time": time.time(),
            }
            print(f"[ARB-LP:{self.label}] Mirrored master pool: {pool['symbol']}")

    def shutdown(self):
        """Remove all positions on bot shutdown."""
        if self.active_position and self.active_position.get("token_id"):
            print(f"[ARB-LP:{self.label}] Shutdown: removing position {self.active_position['token_id']}...")
            try:
                self._remove_liquidity(self.active_position["token_id"])
            except Exception as e:
                print(f"[ARB-LP:{self.label}] Shutdown error: {e}")
            self.active_position = None
