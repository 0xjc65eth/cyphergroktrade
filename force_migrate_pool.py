#!/usr/bin/env python3 -u
"""
FORCE MIGRATE: Remove ALL existing LP positions and mount ETH/USDC.
Run this DIRECTLY from your Mac terminal:
    cd cyphergroktrade
    ./venv/bin/python3 force_migrate_pool.py

This script does NOT touch Hyperliquid. It only:
1. Scans all Uniswap V3 NFT positions in the wallet
2. Removes liquidity from ALL of them (decreaseLiquidity + collect)
3. Converts all tokens to WETH + USDC (50/50 split)
4. Mints a new concentrated LP position in WETH/USDC pool
"""

import os
import sys
import time

os.environ["PYTHONUNBUFFERED"] = "1"

import config
from web3 import Web3
from eth_account import Account
from arb_abi import (
    ERC20_ABI, WETH_ABI,
    UNISWAP_V3_FACTORY_ABI, UNISWAP_V3_POOL_ABI,
    UNISWAP_V3_NFT_MANAGER_ABI, UNISWAP_V3_SWAP_ROUTER_ABI,
    ARBITRUM_CONTRACTS,
)


def connect_rpc():
    rpcs = [
        getattr(config, "ARB_RPC_URL", "https://arb1.arbitrum.io/rpc"),
        "https://arbitrum.llamarpc.com",
        "https://rpc.ankr.com/arbitrum",
        "https://arbitrum.drpc.org",
    ]
    for rpc in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                print(f"[OK] Connected to {rpc}")
                return w3
        except Exception:
            continue
    print("[FAIL] Could not connect to any RPC")
    sys.exit(1)


def send_tx(w3, account, private_key, tx_func, value=0, chain_id=42161):
    """Build, sign, send a transaction. Returns receipt."""
    addr = account.address
    tx = tx_func.build_transaction({
        "from": addr,
        "nonce": w3.eth.get_transaction_count(addr),
        "gas": 1_000_000,
        "maxFeePerGas": w3.eth.gas_price * 3,
        "maxPriorityFeePerGas": w3.to_wei(0.05, "gwei"),
        "chainId": chain_id,
        "value": value,
    })
    try:
        estimated = w3.eth.estimate_gas(tx)
        tx["gas"] = int(estimated * 1.5)
        print(f"  Gas estimate: {estimated} (using {tx['gas']})")
    except Exception as e:
        print(f"  Gas estimate failed: {e} (using default 1M)")

    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Tx sent: {tx_hash.hex()[:16]}...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise Exception(f"TX REVERTED: {tx_hash.hex()}")
    print(f"  Tx confirmed! Block: {receipt['blockNumber']}")
    return receipt


def main():
    print("=" * 60)
    print("  FORCE MIGRATE: Remove ALL LPs -> Mount ETH/USDC")
    print("=" * 60)

    w3 = connect_rpc()
    pk = config.HL_PRIVATE_KEY
    account = Account.from_key(pk)
    addr = account.address
    print(f"Wallet: {addr}")

    nft_mgr = w3.eth.contract(
        address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["uniswap_v3_nft_manager"]),
        abi=UNISWAP_V3_NFT_MANAGER_ABI,
    )
    factory = w3.eth.contract(
        address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["uniswap_v3_factory"]),
        abi=UNISWAP_V3_FACTORY_ABI,
    )
    swap_router = w3.eth.contract(
        address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["uniswap_v3_swap_router"]),
        abi=UNISWAP_V3_SWAP_ROUTER_ABI,
    )
    weth_contract = w3.eth.contract(
        address=Web3.to_checksum_address(ARBITRUM_CONTRACTS["weth"]),
        abi=WETH_ABI,
    )

    tokens = getattr(config, "ARB_TOKENS", {})
    weth_addr = ARBITRUM_CONTRACTS["weth"]
    usdc_addr = tokens.get("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831")

    eth_bal = float(Web3.from_wei(w3.eth.get_balance(addr), "ether"))
    print(f"ETH balance: {eth_bal:.6f}")

    # ─── STEP 1: Find and remove ALL LP positions ───
    print("\n--- STEP 1: Remove ALL LP positions ---")
    nft_count = nft_mgr.functions.balanceOf(addr).call()
    print(f"Found {nft_count} NFT position(s)")

    for i in range(nft_count):
        token_id = nft_mgr.functions.tokenOfOwnerByIndex(addr, i).call()
        pos = nft_mgr.functions.positions(token_id).call()
        liquidity = pos[7]
        t0, t1, fee = pos[2], pos[3], pos[4]

        # Lookup names
        t0_name = "?"
        t1_name = "?"
        for name, taddr in tokens.items():
            if taddr.lower() == t0.lower():
                t0_name = name
            if taddr.lower() == t1.lower():
                t1_name = name

        print(f"\nPosition #{token_id}: {t0_name}/{t1_name} fee={fee} liquidity={liquidity}")

        if liquidity == 0:
            print(f"  -> No liquidity, skipping")
            continue

        # decreaseLiquidity
        print(f"  Removing liquidity...")
        deadline = int(time.time()) + 600
        params = (token_id, liquidity, 0, 0, deadline)
        tx_func = nft_mgr.functions.decreaseLiquidity(params)
        try:
            send_tx(w3, account, pk, tx_func)
        except Exception as e:
            print(f"  FAILED decreaseLiquidity: {e}")
            print(f"  Trying to continue anyway...")
            continue

        # collect
        print(f"  Collecting tokens...")
        max_uint128 = 2 ** 128 - 1
        collect_params = (token_id, addr, max_uint128, max_uint128)
        tx_func = nft_mgr.functions.collect(collect_params)
        try:
            send_tx(w3, account, pk, tx_func)
        except Exception as e:
            print(f"  FAILED collect: {e}")
            continue

        print(f"  Position #{token_id} fully removed!")

    # ─── STEP 2: Convert ALL tokens to WETH + USDC ───
    print("\n--- STEP 2: Convert all tokens to WETH + USDC ---")

    # Wrap ETH (keep gas reserve)
    eth_bal = float(Web3.from_wei(w3.eth.get_balance(addr), "ether"))
    gas_reserve = 0.0005
    wrappable = eth_bal - gas_reserve
    if wrappable > 0.0001:
        print(f"Wrapping {wrappable:.6f} ETH -> WETH")
        tx_func = weth_contract.functions.deposit()
        value = w3.to_wei(wrappable, "ether")
        try:
            send_tx(w3, account, pk, tx_func, value=value)
        except Exception as e:
            print(f"  Wrap failed: {e}")

    # Swap non-pool tokens to WETH
    def get_balance(token_addr):
        token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
        return token.functions.balanceOf(addr).call()

    def approve(token_addr, spender, amount):
        token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
        current = token.functions.allowance(addr, Web3.to_checksum_address(spender)).call()
        if current >= amount:
            return
        print(f"  Approving {token_addr[:10]}...")
        max_approval = 2 ** 256 - 1
        tx_func = token.functions.approve(Web3.to_checksum_address(spender), max_approval)
        send_tx(w3, account, pk, tx_func)

    def swap(token_in, token_out, amount_in, fee=3000):
        approve(token_in, ARBITRUM_CONTRACTS["uniswap_v3_swap_router"], amount_in)
        deadline = int(time.time()) + 300
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee, addr, deadline, amount_in, 0, 0,
        )
        tx_func = swap_router.functions.exactInputSingle(params)
        send_tx(w3, account, pk, tx_func)

    for token_name, token_addr in tokens.items():
        if token_addr.lower() in (weth_addr.lower(), usdc_addr.lower()):
            continue
        bal = get_balance(token_addr)
        if bal == 0:
            continue
        # Quick USD estimate
        token_c = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
        decimals = token_c.functions.decimals().call()
        human_bal = bal / (10 ** decimals)
        print(f"\n{token_name}: {human_bal:.6f} (raw={bal})")
        if human_bal < 0.000001:
            print(f"  Dust, skipping")
            continue

        # Find pool with WETH
        swapped = False
        for fee in [3000, 10000, 500]:
            pool_addr = factory.functions.getPool(
                Web3.to_checksum_address(token_addr),
                Web3.to_checksum_address(weth_addr),
                fee,
            ).call()
            if pool_addr != "0x0000000000000000000000000000000000000000":
                print(f"  Swapping {token_name} -> WETH (fee={fee})...")
                try:
                    swap(token_addr, weth_addr, bal, fee)
                    swapped = True
                    break
                except Exception as e:
                    print(f"  Swap failed (fee={fee}): {e}")
                    continue
        if not swapped:
            print(f"  Could not swap {token_name}, no pool found")

    # ─── STEP 3: Balance WETH/USDC 50/50 ───
    print("\n--- STEP 3: Balance WETH/USDC 50/50 ---")

    weth_bal = get_balance(weth_addr)
    usdc_bal = get_balance(usdc_addr)
    weth_dec = 18
    usdc_dec = 6
    weth_human = weth_bal / (10 ** weth_dec)
    usdc_human = usdc_bal / (10 ** usdc_dec)
    print(f"WETH: {weth_human:.6f}")
    print(f"USDC: {usdc_human:.2f}")

    # Get ETH price from pool
    eth_price = 2000.0
    try:
        pool_addr = factory.functions.getPool(
            Web3.to_checksum_address(weth_addr),
            Web3.to_checksum_address(usdc_addr),
            500,
        ).call()
        if pool_addr != "0x0000000000000000000000000000000000000000":
            pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=UNISWAP_V3_POOL_ABI)
            slot0 = pool.functions.slot0().call()
            sqrt_price = slot0[0]
            price = (sqrt_price / (2 ** 96)) ** 2
            token0 = pool.functions.token0().call()
            if token0.lower() == weth_addr.lower():
                eth_price = price * (10 ** 12)
            else:
                eth_price = (1 / price) * (10 ** 12) if price > 0 else 2000.0
            print(f"ETH price: ${eth_price:.2f}")
    except:
        pass

    weth_usd = weth_human * eth_price
    usdc_usd = usdc_human
    total_usd = weth_usd + usdc_usd
    print(f"Total value: WETH=${weth_usd:.2f} + USDC=${usdc_usd:.2f} = ${total_usd:.2f}")

    if weth_usd > usdc_usd * 1.5 and weth_bal > 0:
        swap_amount = weth_bal // 2
        print(f"Swapping ~50% WETH -> USDC...")
        try:
            swap(weth_addr, usdc_addr, swap_amount, 500)
        except Exception as e:
            print(f"  Balance swap failed: {e}")
    elif usdc_usd > weth_usd * 1.5 and usdc_bal > 0:
        swap_amount = usdc_bal // 2
        print(f"Swapping ~50% USDC -> WETH...")
        try:
            swap(usdc_addr, weth_addr, swap_amount, 500)
        except Exception as e:
            print(f"  Balance swap failed: {e}")

    # ─── STEP 4: Mint new ETH/USDC LP position ───
    print("\n--- STEP 4: Mint ETH/USDC LP position ---")
    import math

    # Find the pool
    pool_addr = None
    pool_fee = None
    for fee in [500, 3000, 10000]:
        pa = factory.functions.getPool(
            Web3.to_checksum_address(weth_addr),
            Web3.to_checksum_address(usdc_addr),
            fee,
        ).call()
        if pa != "0x0000000000000000000000000000000000000000":
            pool_addr = pa
            pool_fee = fee
            break

    if not pool_addr:
        print("FATAL: No WETH/USDC pool found!")
        sys.exit(1)

    pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=UNISWAP_V3_POOL_ABI)
    slot0 = pool.functions.slot0().call()
    current_tick = slot0[1]
    tick_spacing = pool.functions.tickSpacing().call()

    # Wider range for volatile pair (+/- 200 ticks)
    width = max(200 * tick_spacing, tick_spacing * 20)
    tick_lower = int(math.floor((current_tick - width) / tick_spacing)) * tick_spacing
    tick_upper = int(math.ceil((current_tick + width) / tick_spacing)) * tick_spacing
    if tick_lower >= tick_upper:
        tick_upper = tick_lower + tick_spacing

    print(f"Pool: {pool_addr[:12]}... fee={pool_fee}")
    print(f"Current tick: {current_tick}")
    print(f"Range: [{tick_lower}, {tick_upper}]")

    # Sort tokens (Uniswap requires token0 < token1 by address)
    t0 = weth_addr
    t1 = usdc_addr
    if int(t0, 16) > int(t1, 16):
        t0, t1 = t1, t0

    bal0 = get_balance(t0)
    bal1 = get_balance(t1)
    print(f"Token0 ({t0[:10]}...): {bal0}")
    print(f"Token1 ({t1[:10]}...): {bal1}")

    if bal0 == 0 and bal1 == 0:
        print("No tokens to provide!")
        sys.exit(1)

    # Approve
    nft_addr = ARBITRUM_CONTRACTS["uniswap_v3_nft_manager"]
    if bal0 > 0:
        approve(t0, nft_addr, bal0)
    if bal1 > 0:
        approve(t1, nft_addr, bal1)

    # Mint
    deadline = int(time.time()) + 600
    mint_params = (
        Web3.to_checksum_address(t0),
        Web3.to_checksum_address(t1),
        pool_fee,
        tick_lower,
        tick_upper,
        bal0,
        bal1,
        0, 0,
        addr,
        deadline,
    )
    print(f"Minting LP position...")
    tx_func = nft_mgr.functions.mint(mint_params)
    try:
        receipt = send_tx(w3, account, pk, tx_func)
        # Extract token ID
        transfer_hash = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        nft_lower = nft_addr.lower()
        token_id = None
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            log_addr = log.get("address", "").lower()
            if len(topics) >= 4 and log_addr == nft_lower:
                t0_hex = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
                if transfer_hash in t0_hex:
                    raw = topics[3].hex() if hasattr(topics[3], "hex") else str(topics[3])
                    raw = raw.replace("0x", "")
                    token_id = int(raw, 16)
                    break
        if token_id:
            print(f"\n{'='*60}")
            print(f"  SUCCESS! New ETH/USDC LP Position #{token_id}")
            print(f"  Pool: WETH/USDC (fee={pool_fee})")
            print(f"  Range: [{tick_lower}, {tick_upper}]")
            print(f"{'='*60}")
        else:
            print(f"\n  Position minted but could not extract token ID")
            print(f"  Check Arbiscan for your wallet: {addr}")
    except Exception as e:
        print(f"\n  MINT FAILED: {e}")
        print(f"  Tokens are in your wallet, you can retry or mint manually")

    print("\nDone!")


if __name__ == "__main__":
    main()
