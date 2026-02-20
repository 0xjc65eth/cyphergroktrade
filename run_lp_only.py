#!/usr/bin/env python3 -u
"""
CypherGrokTrade - Standalone Arbitrum LP Runner
Mounts ETH/USDC LP position using ALL Arbitrum spot tokens.
Does NOT touch Hyperliquid positions or balances.

Usage: python3 run_lp_only.py
"""

import os
import sys
import time
import signal

os.environ["PYTHONUNBUFFERED"] = "1"

import config
from arb_lp import ArbitrumLPManager


class LPOnlyRunner:
    """Runs only the Arbitrum LP manager in a loop, no HL interaction."""

    def __init__(self):
        print("=" * 60)
        print("  CypherGrokTrade - LP Only Mode (ETH/USDC)")
        print("  NO Hyperliquid interaction")
        print("=" * 60)
        print()

        self.lp = ArbitrumLPManager()
        self.running = True

        try:
            signal.signal(signal.SIGINT, self._shutdown)
            signal.signal(signal.SIGTERM, self._shutdown)
        except ValueError:
            pass

    def start(self):
        """Mount LP and monitor in a loop."""
        print(f"[LP-ONLY] Wallet: {self.lp.address}")
        print(f"[LP-ONLY] Target pool: {getattr(config, 'ARB_LP_TARGET_POOL', 'WETH-USDC')}")
        print(f"[LP-ONLY] Refresh interval: {getattr(config, 'ARB_LP_REFRESH_INTERVAL', 300)}s")
        print(f"[LP-ONLY] OOR rebalance after: {getattr(config, 'ARB_LP_REBALANCE_AFTER_OOR_MIN', 30)}min")
        print()

        # Initial balance check
        try:
            eth_bal = self.lp._get_eth_balance()
            eth_price = self.lp._get_eth_price()
            arb_usd = self.lp._get_arb_total_usd()
            print(f"[LP-ONLY] ETH balance: {eth_bal:.6f} (~${eth_bal * eth_price:.2f})")
            print(f"[LP-ONLY] Total Arbitrum value: ~${arb_usd:.2f}")
            print()
        except Exception as e:
            print(f"[LP-ONLY] Balance check error: {e}")

        # Run first cycle immediately
        print("[LP-ONLY] Running initial LP cycle...")
        self.lp.run_cycle()

        # Monitor loop
        refresh = getattr(config, 'ARB_LP_REFRESH_INTERVAL', 300)
        while self.running:
            try:
                time.sleep(refresh)
                if not self.running:
                    break
                print(f"\n[LP-ONLY] Running LP cycle...")
                self.lp.run_cycle()
            except KeyboardInterrupt:
                self._shutdown()
            except Exception as e:
                print(f"[LP-ONLY] Cycle error: {e}")
                time.sleep(30)

    def _shutdown(self, *args):
        """Graceful shutdown — does NOT remove LP (keeps position active)."""
        print(f"\n[LP-ONLY] Shutting down (LP position stays active)...")
        self.running = False
        # Note: we do NOT call self.lp.shutdown() — position stays on-chain
        sys.exit(0)


if __name__ == "__main__":
    runner = LPOnlyRunner()
    runner.start()
