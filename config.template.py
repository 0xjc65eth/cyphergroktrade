"""
CypherGrokTrade - Configuration Template v3 (Aggressive-Safe Strategy)
Copy to config.py and fill in your secrets, OR set environment variables for cloud deploy.
Optimized for high win-rate SMC confluence setups + MM fallback.
"""

import os

# === Hyperliquid Credentials (REQUIRED) ===
HL_PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")
HL_WALLET_ADDRESS = os.environ.get("HL_WALLET_ADDRESS", "")

# === Profit Withdrawal ===
WITHDRAW_WALLET = os.environ.get("WITHDRAW_WALLET", "")
WITHDRAW_EVERY_USD = 10.0       # Send profit every $10 gained

# === Telegram Notifications ===
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# === Grok AI (xAI) ===
GROK_API_KEY = os.environ.get("GROK_API_KEY", "")
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4-1-fast-non-reasoning")
GROK_API_URL = "https://api.x.ai/v1/chat/completions"

# === Trading Parameters ===
INITIAL_CAPITAL = 6.0             # USD starting capital
TARGET_CAPITAL = 50000.0          # USD target
LEVERAGE = 15                     # Agressivo: 15x default
LEVERAGE_MAP = {"BTC": 10, "ETH": 10, "SOL": 12}
MAX_RISK_PER_TRADE = 0.08        # 8% risk per trade - agressivo mas controlado
MAX_ENTRIES_PER_CYCLE = 2         # Max 2 entries per scan cycle
MIN_SECONDS_BETWEEN_ENTRIES = 30  # 30s entre entries (rapido mas nao spam)

# === SL/TP (ATR-based dynamic is primary, these are fallbacks) ===
STOP_LOSS_PCT = 0.020            # 2.0% SL - tight para limitar perdas
TAKE_PROFIT_PCT = 0.045          # 4.5% TP (2.25:1 R:R)
TRAILING_STOP_PCT = 0.012        # 1.2% trailing (ativa apos 50% do TP)
USE_ATR_STOPS = True             # Use ATR for dynamic SL/TP
ATR_SL_MULTIPLIER = 2.5          # SL = 2.5 * ATR
ATR_TP_MULTIPLIER = 5.5          # TP = 5.5 * ATR (2.2:1 R:R minimum)

# === Trading Pairs ===
TRADING_PAIRS = []                # Empty = dynamic
TOP_COINS_COUNT = 200             # Scan ALL coins da Hyperliquid (~191 ativas)
MIN_VOLUME_24H = 50_000           # Baixo para incluir mais coins no scan
LEVERAGE_MAP_DEFAULT = 15

# === Extra Pairs (sempre incluidos no scan) ===
EXTRA_PAIRS = ["PAXG", "SPX"]
LEVERAGE_MAP.update({"PAXG": 10, "SPX": 10})

# === SMC Parameters (premium setup detection) ===
SMC_LOOKBACK = 100               # More data for better swing detection
ORDER_BLOCK_THRESHOLD = 0.0015   # Slightly higher for premium OBs
FVG_MIN_GAP = 0.0003
BOS_CONFIRMATION_CANDLES = 3
DISPLACEMENT_MIN = 0.003         # Min 0.3% for displacement candle
MIN_CONFLUENCE_FACTORS = 2       # Require at least 2 SMC factors to agree

# === Moving Average Parameters ===
EMA_FAST = 8                     # Slightly faster for crypto
EMA_SLOW = 21
EMA_TREND = 55                   # 55 better than 50 for crypto
RSI_PERIOD = 14
RSI_OVERBOUGHT = 65
RSI_OVERSOLD = 35

# === Timeframes (multi-timeframe analysis) ===
SCALP_TIMEFRAME = "1m"           # Entry timeframe
TREND_TIMEFRAME = "5m"           # Trend confirmation
HTF_TIMEFRAME = "15m"            # Higher timeframe bias

# === Risk Management ===
MAX_DAILY_LOSS_PCT = 25.0         # 25% max daily loss - agressivo mas com trava
MAX_CONSECUTIVE_LOSSES = 3        # 3 losses = cooldown
COOLDOWN_SECONDS = 180            # 3 min cooldown (rapido para voltar)
MAX_OPEN_POSITIONS = 3            # 3 posicoes simultaneas
SCAN_INTERVAL = 20                # 20s entre scans - rapido para pegar moves

# === Horario Operacional ===
TRADING_HOURS_ENABLED = False     # DESATIVADO - opera 24h

# === Signal Quality Filters ===
MIN_CONFIDENCE = 0.65             # Confianca minima (agressivo mas filtrado)
REQUIRE_5M_TREND = True           # Filtro 5m: exige alinhamento OU high-conf bypass
HIGH_CONF_5M_BYPASS = 0.80       # Se conf >= 0.80, aceita 5m NEUTRAL (momentum forte)
REQUIRE_15M_BIAS = True           # Aceita NEUTRAL, mas rejeita oposicao
MIN_VOLUME_RATIO = 1.3            # Volume acima da media
REQUIRE_OB_OR_FVG = True          # Precisa ter zona de entry definida
REQUIRE_STRUCTURE = False          # Bonus, nao obrigatorio (mais entries)

# === MM Fallback (when no futures signals) ===
MM_FALLBACK_ENABLED = True        # Auto-switch to MM when no signals
MM_FALLBACK_AFTER_SCANS = 2       # After 2 full scans with no entry, do MM
MM_AGGRESSIVE_ON_IDLE = True      # Place more MM orders when futures idle

# === Spot Market Making (Bid/Ask) ===
MM_ENABLED = True
MM_PAIRS = ["PURR/USDC", "@107"]
MM_SPREAD_BPS = 10
MM_SPREAD_MAP = {"PURR/USDC": 30, "@107": 5}
MM_SIZE_USD = 11.0
MM_MIN_BALANCE = 1.0
MM_ALLOC_PCT = 0.30
MM_REFRESH_INTERVAL = 30
# Dynamic spread adjustment
MM_DYNAMIC_SPREAD = True          # Widen spread in volatility, tighten in calm
MM_MIN_SPREAD_BPS = 3             # Never go below 3 bps
MM_MAX_SPREAD_BPS = 50            # Never go above 50 bps
MM_INVENTORY_REBALANCE = True     # Skew orders to rebalance inventory

# === Arbitrum LP (DEX Liquidity Provision) ===
ARB_LP_ENABLED = True
ARB_RPC_URL = "https://arb1.arbitrum.io/rpc"
ARB_RPC_FALLBACK = "https://arbitrum.llamarpc.com"
ARB_LP_ALLOC_USD = 5.00               # Max USD to deploy as LP (~50% of capital)
ARB_LP_REFRESH_INTERVAL = 60           # Check position every 1 min (faster migration/rebalance)
ARB_LP_MIN_APY = 5.0                   # Minimum 5% APY to enter pool
ARB_LP_PREFER_STABLES = False          # Degen mode: go for highest APY pools
ARB_LP_TARGET_POOL = "WETH-USDC"       # Pool alvo fixa (bypassa DeFiLlama scoring)
ARB_LP_MAX_GAS_PCT = 0.10              # Never spend >10% of position on gas per tx
ARB_LP_REBALANCE_AFTER_OOR_MIN = 30    # Rebalance after 30 min out of range
ARB_LP_FEE_COLLECT_MIN_USD = 0.02      # Min fees to justify collection tx
ARB_LP_COPY_FEE_PCT = 0.05             # 5% fee on follower LP allocation (paid to master)

# === Copy Trading Allocation (follower capital split) ===
COPY_ALLOC_LP_PCT = 0.50              # 50% of follower capital -> Arbitrum LP
COPY_ALLOC_SCALP_PCT = 0.25           # 25% of follower capital -> Perp scalp trading
COPY_ALLOC_MM_PCT = 0.25              # 25% of follower capital -> Spot MM bid/ask
ARB_CHAIN_ID = 42161
ARB_TOKENS = {
    # Blue chips
    "WETH":   "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    "USDC":   "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    "USDT":   "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    "ARB":    "0x912CE59144191C1204E64559FE8253a0e49E6548",
    "USDC.e": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
    "WBTC":   "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
    "DAI":    "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    # DeFi majors
    "PENDLE": "0x0c880f6761F1af8d9Aa9C466984b80DAb9a8c9e8",
    "GMX":    "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a",
    "GNS":    "0x18c11FD286C5EC11c3b683Caa813B77f5163A122",
    "CRV":    "0x11cDb42B0EB46D95f990BeDD4695A6e3fA034978",
    "LPT":    "0x289ba1701C2F088cf0faf8B3705246331cB8A839",
    "ZRO":    "0x6985884C4392D348587B19cb9eAAf157F13271cd",
    "AAVE":   "0xba5DdD1f9d7F570dc94a51479a000E3BCE967196",
    # Arbitrum DeFi ecosystem
    "LINK":   "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",
    "UNI":    "0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0",
    "MAGIC":  "0x539bdE0d7Dbd336b79148AA742883198BBF60342",
    "RDNT":   "0x3082CC23568eA640225c2467653dB90e9250AaA0",
    "DPX":    "0x6C2C06790b3E3E3c38e12Ee22F8183b37a13EE55",
    "GRAIL":  "0x3d9907F9a368ad0a51Be60f7Da3b97cf940982D8",
    "JONES":  "0x10393c20975cF177a3513071bC110f7962CD67da",
    "SUSHI":  "0xd4d42F0b6DEF4CE0383636770eF773390d85c61A",
    "STG":    "0x6694340fc020c5E6B96567843da2df01b2CE1eb6",
    "LODE":   "0xF19547f9ED24aA66b03c3a552D181Ae334FBb8DB",
    "PREMIA": "0x51fC0f6660482Ea73330E414eFd7808811a57Fa2",
    "WSTETH": "0x5979D7b546E38E9Ab8C56282F4cE1e1e5c1af3C7",
    "RETH":   "0xEC70Dcb4A1EFa46b8F2D97C310C9c4790ba5ffA8",
}
