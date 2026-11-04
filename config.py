"""
Hydranet DEX Pure DLMM Market Maker Configuration.
Designed for turnkey, non-custodial community market making with automated
Cashu Ecash Lightning liquidity management (no sovereign node required).
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Union


def load_env_file(filepath: Optional[str] = None):
    """Loads key-value pairs from .env if present without external dependencies."""
    if filepath is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(base_dir, ".env")
        filepath = candidate if os.path.exists(candidate) else ".env"
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("\"'")
                if key and (key not in os.environ or not os.environ[key]):
                    os.environ[key] = val


# Load environment variables from .env
load_env_file()


# ==================== MARKET METADATA ====================

@dataclass
class CurrencyConfig:
    protocol: str
    network_id: str
    asset_id: str
    symbol: str
    network_name: str
    decimals: int
    precision: int
    min_amount: float

    @property
    def display_decimals(self) -> int:
        return self.precision

    def to_orderbook_currency(self) -> Dict[str, str]:
        return {
            "protocol": self.protocol,
            "networkId": self.network_id,
            "assetId": self.asset_id,
        }

    def to_network(self) -> Dict[str, str]:
        return {
            "protocol": self.protocol,
            "id": self.network_id,
        }


@dataclass
class MarketConfig:
    pair_name: str
    base: CurrencyConfig
    quote: CurrencyConfig
    default_order_size_base: float = 0.01
    max_order_size_base: Optional[float] = None
    maker_fee_rebate: float = -0.0005
    price_precision: Optional[int] = None

    @property
    def pair(self) -> str:
        return self.pair_name

    @property
    def base_asset(self) -> str:
        return self.base.symbol

    @property
    def quote_asset(self) -> str:
        return self.quote.symbol

    @property
    def base_network(self) -> str:
        return self.base.network_name

    @property
    def quote_network(self) -> str:
        return self.quote.network_name

    @property
    def base_asset_id(self) -> str:
        return self.base.asset_id

    @property
    def quote_asset_id(self) -> str:
        return self.quote.asset_id

    @property
    def base_precision(self) -> int:
        return self.base.precision

    @property
    def quote_precision(self) -> int:
        return self.quote.precision

    @property
    def min_order_size(self) -> float:
        return self.base.min_amount

    @property
    def lot_size(self) -> float:
        return self.base.min_amount

    @property
    def is_peg_pair(self) -> bool:
        return self.base.symbol == self.quote.symbol

    def get_price_precision(self) -> int:
        if self.price_precision is not None:
            return self.price_precision
        if self.is_peg_pair:
            return 5
        if self.base.symbol == "HDN":
            return 6
        return self.quote.precision


# Standard active DEX markets on Hydranet
MARKETS: Dict[str, MarketConfig] = {
    "HDN/USDC": MarketConfig(
        pair_name="HDN/USDC",
        base=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="42161",
            asset_id="erc20:0xb0f66bdb39acbb043308eb9dbe78f5bb47ea5430",
            symbol="HDN",
            network_name="Arbitrum",
            decimals=18,
            precision=2,
            min_amount=20.00,
        ),
        quote=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="42161",
            asset_id="erc20:0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            symbol="USDC",
            network_name="Arbitrum",
            decimals=6,
            precision=2,
            min_amount=1.20,
        ),
        default_order_size_base=75.0,
        maker_fee_rebate=-0.0005,
        price_precision=6,
    ),
    "BTC/USDC": MarketConfig(
        pair_name="BTC/USDC",
        base=CurrencyConfig(
            protocol="PROTOCOL_BITCOIN",
            network_id="f9beb4d9",
            asset_id="0x0000000000000000000000000000000000000000000000000000000000000000",
            symbol="BTC",
            network_name="Bitcoin",
            decimals=8,
            precision=7,
            min_amount=0.00001,
        ),
        quote=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="42161",
            asset_id="erc20:0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            symbol="USDC",
            network_name="Arbitrum",
            decimals=6,
            precision=2,
            min_amount=1.20,
        ),
        default_order_size_base=0.0005,
        maker_fee_rebate=-0.0010,
        price_precision=2,
    ),
    "ETH/USDC": MarketConfig(
        pair_name="ETH/USDC",
        base=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="1",
            asset_id="0x0000000000000000000000000000000000000000",
            symbol="ETH",
            network_name="Ethereum",
            decimals=18,
            precision=6,
            min_amount=0.0010,
        ),
        quote=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="42161",
            asset_id="erc20:0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            symbol="USDC",
            network_name="Arbitrum",
            decimals=6,
            precision=2,
            min_amount=1.20,
        ),
        default_order_size_base=0.01,
        maker_fee_rebate=-0.00075,
        price_precision=2,
    ),
    "ETH/BTC": MarketConfig(
        pair_name="ETH/BTC",
        base=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="1",
            asset_id="0x0000000000000000000000000000000000000000",
            symbol="ETH",
            network_name="Ethereum",
            decimals=18,
            precision=6,
            min_amount=0.0010,
        ),
        quote=CurrencyConfig(
            protocol="PROTOCOL_BITCOIN",
            network_id="f9beb4d9",
            asset_id="0x0000000000000000000000000000000000000000000000000000000000000000",
            symbol="BTC",
            network_name="Bitcoin",
            decimals=8,
            precision=7,
            min_amount=0.00001,
        ),
        default_order_size_base=0.01,
        maker_fee_rebate=-0.0010,
        price_precision=6,
    ),
    "ETH(Arb)/ETH(Eth)": MarketConfig(
        pair_name="ETH(Arb)/ETH(Eth)",
        base=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="42161",
            asset_id="0x0000000000000000000000000000000000000000",
            symbol="ETH",
            network_name="Arbitrum",
            decimals=18,
            precision=6,
            min_amount=0.0010,
        ),
        quote=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="1",
            asset_id="0x0000000000000000000000000000000000000000",
            symbol="ETH",
            network_name="Ethereum",
            decimals=18,
            precision=5,
            min_amount=0.0010,
        ),
        default_order_size_base=0.01,
        maker_fee_rebate=-0.00025,
        price_precision=5,
    ),
    "USDC(Arb)/USDC(Eth)": MarketConfig(
        pair_name="USDC(Arb)/USDC(Eth)",
        base=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="42161",
            asset_id="erc20:0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            symbol="USDC",
            network_name="Arbitrum",
            decimals=6,
            precision=2,
            min_amount=1.00,
        ),
        quote=CurrencyConfig(
            protocol="PROTOCOL_EVM",
            network_id="1",
            asset_id="erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            symbol="USDC",
            network_name="Ethereum",
            decimals=6,
            precision=5,
            min_amount=1.00,
        ),
        default_order_size_base=15.0,
        maker_fee_rebate=-0.00025,
        price_precision=5,
    ),
}

# Hub Public Keys for channel opening and rentals (Mainnet)
HUB_NODE_PUBKEYS: Dict[str, str] = {
    "Arbitrum": "0x53f1067c4d85000f24d8019b8d676fbf7f321ca3",
    "Ethereum": "0x53f1067c4d85000f24d8019b8d676fbf7f321ca3",
    "Bitcoin": "03eaace825811aee04ea8e56d31a8927104d5f301ff2aa8998cc431903ec26289a",
}

# Hub Peer WebSocket URLs for establishing active peer sessions before channel operations (Mainnet)
HUB_PEER_URLS: Dict[str, str] = {
    "Bitcoin": "03eaace825811aee04ea8e56d31a8927104d5f301ff2aa8998cc431903ec26289a@wss://lightning-ws.hydranet.app:443",
    "Arbitrum": "0x53f1067c4d85000f24d8019b8d676fbf7f321ca3@wss://arbitrum-ws.hydranet.app:443",
    "Ethereum": "0x53f1067c4d85000f24d8019b8d676fbf7f321ca3@wss://ethereum-ws.hydranet.app:443",
}

# Official Hydranet Staging / Testnet Hub Public Keys & Peer WebSocket URLs
TESTNET_HUB_NODE_PUBKEYS: Dict[str, str] = {
    "Arbitrum": "0xd78f26fdc770e4041960d3e06a58fb9b91fa5ff6",
    "Ethereum": "0xd78f26fdc770e4041960d3e06a58fb9b91fa5ff6",
    "Bitcoin": "03726edb9778282abf3b08cbac5114fe45e8b0d302ad278bd1dc7af5d3bb134083",
}

TESTNET_HUB_PEER_URLS: Dict[str, str] = {
    "Bitcoin": "03726edb9778282abf3b08cbac5114fe45e8b0d302ad278bd1dc7af5d3bb134083@wss://lightning-ws.staging.hydranet.ai:443",
    "Arbitrum": "0xd78f26fdc770e4041960d3e06a58fb9b91fa5ff6@wss://arbitrum-ws.staging.hydranet.ai:443",
    "Ethereum": "0xd78f26fdc770e4041960d3e06a58fb9b91fa5ff6@wss://ethereum-ws.staging.hydranet.ai:443",
}

# Official Hydranet Staging / Testnet Token Contracts (verified via orderbook_getMarketsInfo on hydra-app-testnet)
TESTNET_TOKEN_ASSETS: Dict[tuple, str] = {
    ("Arbitrum", "HDN"): "erc20:0x67e6a7eae40107ff676908b28c3fc632a38f1499",
    ("Arbitrum", "USDC"): "erc20:0x1baabb04529d43a73232b713c0fe471f7c7334d5",
    ("Ethereum", "USDC"): "erc20:0x8cd0da3d001b013336918b8bc4e56d9dda1347e0",
}

# ==================== MAINNET VS TESTNET (STAGING) RAILS ====================
# Hydranet Staging/Testnet uses Bitcoin Signet (0a03cf40), Arbitrum Sepolia (421614),
# and Ethereum Sepolia (11155111).
NETWORK_RAILS: Dict[str, Dict[str, Dict[str, str]]] = {
    "mainnet": {
        "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": "f9beb4d9", "label": "Bitcoin Mainnet (f9beb4d9)"},
        "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": "42161", "label": "Arbitrum One (42161)"},
        "Ethereum": {"protocol": "PROTOCOL_EVM", "id": "1", "label": "Ethereum Mainnet (1)"},
    },
    "testnet": {
        "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": "0a03cf40", "label": "Bitcoin Signet (0a03cf40)"},
        "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": "421614", "label": "Arbitrum Sepolia (421614)"},
        "Ethereum": {"protocol": "PROTOCOL_EVM", "id": "11155111", "label": "Ethereum Sepolia (11155111)"},
    },
}

CASHU_MINT_PRESETS: Dict[str, Dict[str, str]] = {
    "mainnet_minibits": {
        "name": "Minibits Mainnet Mint",
        "url": "https://mint.minibits.cash/Bitcoin",
        "rail": "mainnet",
        "description": "Production Bitcoin Lightning Cashu Mint (NUT-00..06)",
    },
    "testnet_testnut": {
        "name": "Cashu Public Testnut (cdk-mintd)",
        "url": "https://testnut.cashu.space",
        "rail": "testnet",
        "description": "Official public Cashu Testnet FakeWallet Mint for instant BDHKE token mint/melt",
    },
    "testnet_local_signet": {
        "name": "Hydranet Cloud Signet Cashu Mint",
        "url": "http://136.113.240.168:3338",
        "rail": "testnet",
        "description": "Google Cloud Bitcoin Signet (0a03cf40) Cashu Mint Server (LND Signet + NUT-00..06)",
    },
}


def get_network_id(network_name: str, testnet: bool = False) -> str:
    """Returns the Hydranet networkId for Bitcoin, Arbitrum, or Ethereum on Mainnet or Testnet."""
    rail = "testnet" if testnet else "mainnet"
    return NETWORK_RAILS[rail].get(network_name, NETWORK_RAILS[rail]["Arbitrum"])["id"]


def get_hub_node_pubkey(network_name: str, testnet: bool = False) -> str:
    """Returns the Hydranet Hub Node public key / EVM address for Mainnet or Testnet."""
    table = TESTNET_HUB_NODE_PUBKEYS if testnet else HUB_NODE_PUBKEYS
    return table.get(network_name, table["Arbitrum"])


def get_hub_peer_url(network_name: str, testnet: bool = False) -> str:
    """Returns the Hydranet Hub Peer WebSocket URL for Mainnet or Testnet."""
    table = TESTNET_HUB_PEER_URLS if testnet else HUB_PEER_URLS
    return table.get(network_name, table["Arbitrum"])


def build_testnet_markets() -> Dict[str, MarketConfig]:
    """Builds the Hydranet Testnet (Bitcoin Signet + Arbitrum/Ethereum Sepolia) market definitions.
    Note: On Hydranet Staging (orderbook.staging.hydranet.ai), the quote USDC for BTC/USDC, ETH/USDC,
    and HDN/USDC is hosted on Ethereum Sepolia (11155111, erc20:0x8cd0da3d001b013336918b8bc4e56d9dda1347e0).
    """
    testnet_markets: Dict[str, MarketConfig] = {}
    for pair_name, m in MARKETS.items():
        base_net_name = m.base.network_name
        base_net_id = get_network_id(base_net_name, testnet=True)
        base_asset_id = TESTNET_TOKEN_ASSETS.get((base_net_name, m.base.symbol), m.base.asset_id)

        # On Hydranet Staging, quote USDC is on Ethereum Sepolia (11155111, 18 decimals)
        if m.quote.symbol == "USDC":
            quote_net_name = "Ethereum"
            quote_net_id = get_network_id("Ethereum", testnet=True)
            quote_asset_id = TESTNET_TOKEN_ASSETS[("Ethereum", "USDC")]
            quote_decimals = 18
        else:
            quote_net_name = m.quote.network_name
            quote_net_id = get_network_id(quote_net_name, testnet=True)
            quote_asset_id = TESTNET_TOKEN_ASSETS.get((quote_net_name, m.quote.symbol), m.quote.asset_id)
            quote_decimals = m.quote.decimals

        testnet_markets[pair_name] = MarketConfig(
            pair_name=pair_name,
            base=CurrencyConfig(
                protocol=m.base.protocol,
                network_id=base_net_id,
                asset_id=base_asset_id,
                symbol=m.base.symbol,
                network_name=base_net_name,
                decimals=m.base.decimals,
                precision=m.base.precision,
                min_amount=m.base.min_amount,
            ),
            quote=CurrencyConfig(
                protocol=m.quote.protocol,
                network_id=quote_net_id,
                asset_id=quote_asset_id,
                symbol=m.quote.symbol,
                network_name=quote_net_name,
                decimals=quote_decimals,
                precision=m.quote.precision,
                min_amount=m.quote.min_amount,
            ),
            default_order_size_base=m.default_order_size_base,
            max_order_size_base=m.max_order_size_base,
            maker_fee_rebate=m.maker_fee_rebate,
            price_precision=m.price_precision,
        )
    return testnet_markets


TESTNET_MARKETS: Dict[str, MarketConfig] = build_testnet_markets()


def get_active_markets(testnet: bool = False) -> Dict[str, MarketConfig]:
    """Returns TESTNET_MARKETS when testnet=True, otherwise Mainnet MARKETS."""
    return TESTNET_MARKETS if testnet else MARKETS


# ==================== BOT CONFIGURATION ====================

@dataclass
class BotConfig:
    """Configuration for Hydranet Pure DLMM Market Maker."""

    # Node RPC & Network Rail (Mainnet vs Testnet/Signet)
    node_url: str = os.getenv("HYDRA_RPC_URL", "http://localhost:5003")
    testnet: bool = os.getenv("HYDRA_TESTNET", "false").lower() in ("true", "1")
    start_local_signet_mint: bool = os.getenv("START_LOCAL_SIGNET_MINT", "false").lower() in ("true", "1")
    local_signet_mint_port: int = int(os.getenv("CASHU_SIGNET_MINT_PORT", "3338"))

    # Active Pair(s)
    active_pair: str = os.getenv("ACTIVE_PAIR", "BTC/USDC")
    active_pairs_all: List[str] = field(default_factory=lambda: list(MARKETS.keys()))
    all_pairs: bool = os.getenv("ALL_PAIRS", "true").lower() in ("true", "1")

    # Execution Mode
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1")

    # Supervisor & Set-It-And-Forget-It 50/50 Portfolio Ratio
    target_base_ratio: float = 0.50             # Automatic 50% Base / 50% Quote equilibrium
    min_target_base_ratio: float = 0.0          # Floor
    max_target_base_ratio: float = 1.0          # Ceiling
    max_skew_price_shift_pct: float = 0.015     # Max 1.5% Avellaneda-Stoikov quote shading

    # DLMM (Dynamic Liquidity Market Maker) Bell Curve Parameters
    rungs_per_side: int = 15                    # 15 layered limit orders per side (30 orders per book)
    bell_curve_sigma: float = 1.2               # Gaussian standard deviation for liquidity concentration
    rebalance_drift_threshold_pct: float = 0.0035  # 0.35% sticky deadband before shifting resting orders
    hub_min_placement_floor_usd: float = 0.75   # Minimum order placement floor ($0.75 USD)
    channel_order_allocation_pct: float = 0.95  # Target % of available channel capacity committed to DLMM orders

    # Market Making Spread & Pricing
    base_half_spread_pct: float = 0.008         # 0.8% half spread (1.6% full spread)
    min_half_spread_pct: float = 0.003          # 0.3% floor
    max_half_spread_pct: float = 0.035          # 3.5% ceiling in volatile regimes
    peg_base_half_spread_pct: float = 0.0004    # 4 bps (0.04%) inner rung #0 for 1:1 cross-chain peg pairs
    peg_max_half_spread_pct: float = 0.0025     # 25 bps (0.25%) outer rung #14 for 1:1 cross-chain peg pairs
    peg_max_skew_shift_pct: float = 0.0004      # ±4 bps max Reservation Center shift (places overweight Rung #0 at 1.00000 par)
    min_order_interval_secs: float = 15.0       # Loop tick interval

    # Circuit Breakers & Price Protection
    use_external_oracle: bool = True            # Use tier-1 external spot feeds (Coinbase, Kraken, DexScreener)
    external_oracle_cache_ttl_secs: float = 2.0 # Update interval for background external spot feeds
    external_oracle_max_divergence_pct: float = 0.025 # 2.5% max divergence before alert
    max_oracle_divergence_pct: float = 0.08     # 8% hard pause if DEX mid diverges from spot oracle

    # Automated Channel Capacity Management (3 Independent Automation Toggles)
    auto_manage_channels: bool = True           # Manage channel capacity when advised
    auto_cashu: bool = os.getenv("AUTO_CASHU", "true").lower() in ("true", "1")
    auto_btc_channels: bool = os.getenv("AUTO_BTC_CHANNELS", "false").lower() in ("true", "1")
    auto_lithium_channels: bool = os.getenv("AUTO_LITHIUM_CHANNELS", "false").lower() in ("true", "1")
    cashu_expansion_threshold: int = int(os.getenv("CASHU_EXPANSION_THRESHOLD", "4"))
    channel_mgmt_log_path: str = os.getenv("CHANNEL_MGMT_LOG_PATH", "data/channel_mgmt_log.json")
    default_lease_duration_secs: int = int(os.getenv("DEFAULT_LEASE_DURATION_SECS", "86400"))  # 24 hours
    target_capacity_increase_pct: float = 0.10  # 10% increase per rental
    min_auto_rental_amount_usd: float = 10.0    # Minimum rental floor
    target_auto_rental_amount_usd: float = 25.0
    max_auto_rental_fee_usd: float = float(os.getenv("MAX_AUTO_RENTAL_FEE_USD", "10.00"))      # Safety fee ceiling (USD) for Hub leases
    max_auto_rental_amount_usd: float = 250.0   # Max capacity leased per operation
    channel_capacity_cooldown_secs: float = 300.0 # 5-minute cooldown between capacity increases
    pending_rental_timeout_secs: float = 1800.0
    pending_open_timeout_secs: float = 1800.0
    max_inflight_operation_timeout_secs: float = 1800.0
    min_channel_buffer_usd: float = 15.0        # Buffer floor for receivable capacity
    min_onchain_btc_open_usd: float = 40.0
    min_onchain_btc_open_amt: float = 0.0004
    btc_max_active_channels: int = 4
    min_onchain_usdc_reserve_usd: float = 0.0

    # On-Chain Gas Reserves (Kept on-chain in wallet for gas; never deposited into state channels)
    min_onchain_gas_reserve_usd: Dict[str, float] = field(default_factory=lambda: {
        "Ethereum": 25.0,  # $25 on-chain gas reserve for Ethereum L1
        "Arbitrum": 25.0,  # $25 on-chain gas reserve for Arbitrum L2
        "Bitcoin": 0.0,    # $0 on Bitcoin (off-chain Cashu/Lightning used instead)
    })

    # Cashu Ecash Protocol (Turnkey Community Liquidity Rail)
    cashu_enabled: bool = os.getenv("CASHU_ENABLED", "true").lower() in ("true", "1")
    cashu_mint_url: str = os.getenv("CASHU_MINT_URL", "http://136.113.240.168:3338")
    cashu_auto_sweep: bool = os.getenv("CASHU_AUTO_SWEEP", "true").lower() in ("true", "1")
    cashu_auto_refuel: bool = os.getenv("CASHU_AUTO_REFUEL", "true").lower() in ("true", "1")
    cashu_min_sink_sats: int = int(os.getenv("CASHU_MIN_SINK_SATS", "30000")) # 30,000 sats floor per sweep
    cashu_max_sweep_usd: float = float(os.getenv("CASHU_MAX_SWEEP_USD", "250.0"))
    cashu_max_refuel_usd: float = float(os.getenv("CASHU_MAX_REFUEL_USD", "250.0"))
    cashu_storage_path: str = os.getenv("CASHU_STORAGE_PATH", "data/cashu_wallet.json")
    cashu_cooldown_secs: float = float(os.getenv("CASHU_COOLDOWN_SECS", "0.0"))
    include_cashu_in_portfolio: bool = True     # Include ecash holdings in total portfolio P&L

    # Web Dashboard Server
    dashboard_port: int = int(os.getenv("DASHBOARD_PORT", "8080"))
