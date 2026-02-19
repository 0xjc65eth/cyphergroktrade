"""
CypherGrokTrade - Arbitrum Contract ABIs (minimal)
Only the function signatures needed for LP operations.
"""

# ERC20 standard functions
ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
]

# Uniswap V3 Factory
UNISWAP_V3_FACTORY_ABI = [
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}, {"name": "fee", "type": "uint24"}], "name": "getPool", "outputs": [{"name": "pool", "type": "address"}], "stateMutability": "view", "type": "function"},
]

# Uniswap V3 Pool
UNISWAP_V3_POOL_ABI = [
    {"inputs": [], "name": "slot0", "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"}, {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"}, {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"}, {"name": "unlocked", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity", "outputs": [{"name": "", "type": "uint128"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "fee", "outputs": [{"name": "", "type": "uint24"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "tickSpacing", "outputs": [{"name": "", "type": "int24"}], "stateMutability": "view", "type": "function"},
]

# Uniswap V3 NonfungiblePositionManager
UNISWAP_V3_NFT_MANAGER_ABI = [
    # mint
    {"inputs": [{"components": [{"name": "token0", "type": "address"}, {"name": "token1", "type": "address"}, {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"}, {"name": "tickUpper", "type": "int24"}, {"name": "amount0Desired", "type": "uint256"}, {"name": "amount1Desired", "type": "uint256"}, {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"}, {"name": "recipient", "type": "address"}, {"name": "deadline", "type": "uint256"}], "name": "params", "type": "tuple"}], "name": "mint", "outputs": [{"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"}, {"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}], "stateMutability": "payable", "type": "function"},
    # decreaseLiquidity
    {"inputs": [{"components": [{"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"}, {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"}, {"name": "deadline", "type": "uint256"}], "name": "params", "type": "tuple"}], "name": "decreaseLiquidity", "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}], "stateMutability": "payable", "type": "function"},
    # collect
    {"inputs": [{"components": [{"name": "tokenId", "type": "uint256"}, {"name": "recipient", "type": "address"}, {"name": "amount0Max", "type": "uint128"}, {"name": "amount1Max", "type": "uint128"}], "name": "params", "type": "tuple"}], "name": "collect", "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}], "stateMutability": "payable", "type": "function"},
    # positions
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "positions", "outputs": [{"name": "nonce", "type": "uint96"}, {"name": "operator", "type": "address"}, {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"}, {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"}, {"name": "tickUpper", "type": "int24"}, {"name": "liquidity", "type": "uint128"}, {"name": "feeGrowthInside0LastX128", "type": "uint256"}, {"name": "feeGrowthInside1LastX128", "type": "uint256"}, {"name": "tokensOwed0", "type": "uint128"}, {"name": "tokensOwed1", "type": "uint128"}], "stateMutability": "view", "type": "function"},
    # balanceOf (ERC721)
    {"inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    # tokenOfOwnerByIndex (ERC721Enumerable)
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "index", "type": "uint256"}], "name": "tokenOfOwnerByIndex", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

# Uniswap V3 SwapRouter
UNISWAP_V3_SWAP_ROUTER_ABI = [
    {"inputs": [{"components": [{"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"}, {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"}, {"name": "deadline", "type": "uint256"}, {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"}, {"name": "sqrtPriceLimitX96", "type": "uint160"}], "name": "params", "type": "tuple"}], "name": "exactInputSingle", "outputs": [{"name": "amountOut", "type": "uint256"}], "stateMutability": "payable", "type": "function"},
]

# WETH deposit/withdraw
WETH_ABI = [
    {"inputs": [], "name": "deposit", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "wad", "type": "uint256"}], "name": "withdraw", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
] + ERC20_ABI

# Contract addresses on Arbitrum One
ARBITRUM_CONTRACTS = {
    "uniswap_v3_factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    "uniswap_v3_nft_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
    "uniswap_v3_swap_router": "0xE592427A0AEce92De3Edee1F18E0157C05861564",
    "weth": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
}
