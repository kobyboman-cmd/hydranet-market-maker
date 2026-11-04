"""
Hydranet DLMM Profitability & Portfolio Tracker.
Tracks real-time portfolio valuation across Bitcoin, Ethereum, and Arbitrum,
records starting capital baselines, calculates P&L ($ and %), monitors realized maker fee rebates,
spread capture alpha, and maintains trade history and time-series performance data.
"""

import os
import json
import time
import logging
from typing import Dict, Any, Optional, List, Tuple, Callable
from dataclasses import dataclass, field, asdict

from config import MARKETS, MarketConfig, BotConfig
from hydra_client import HydraClient
from tax_logger import TaxLogger

logger = logging.getLogger("PnLTracker")


@dataclass
class TradeRecord:
    trade_id: str
    timestamp: float
    time_str: str
    pair: str
    side: str  # "BUY" or "SELL"
    price: float
    amount: float
    volume_usd: float
    maker_rebate_rate: float
    rebate_usd: float
    order_id: Optional[str] = None
    trade_type: str = "MAKER"  # "MAKER", "ACTIVE_REBALANCE", or "CEX"
    profit_usd: float = 0.0


def normalize_asset_id(aid: str) -> str:
    """Normalizes asset IDs across EVM (20-byte) and Bitcoin (32-byte) hex strings."""
    if not aid:
        return "native"
    aid = aid.lower().strip()
    if aid.startswith("erc20:"):
        aid = aid[6:]
    raw = aid[2:] if aid.startswith("0x") else aid
    if set(raw) == {"0"} or not raw:
        return "native"
    return aid


@dataclass
class AssetHolding:
    symbol: str
    network: str
    asset_id: str
    price_usd: float
    channel_sendable: float
    channel_receivable: float
    channel_in_use: float
    channel_reserve: float = 0.0
    channel_total: float = 0.0
    channel_usd: float = 0.0
    on_chain: float = 0.0
    on_chain_pending: float = 0.0
    on_chain_usd: float = 0.0
    gas_reserve_required: float = 0.0
    usable_on_chain: float = 0.0
    total_amount: float = 0.0
    total_usd: float = 0.0
    total_combined_usd: float = 0.0
    pct_of_portfolio: float = 0.0


@dataclass
class PerformancePoint:
    timestamp: float
    time_label: str
    portfolio_usd: float
    pnl_usd: float
    pnl_pct: float
    cumulative_rebates_usd: float
    captured_profit_usd: float = 0.0


class PnLTracker:
    """Tracks portfolio balances, P&L, maker rebates, and trade performance."""

    def __init__(
        self,
        client: HydraClient,
        storage_path: str = "data/pnl_history.json",
        max_history_points: int = 600,
        bot_config: Optional[Any] = None,
    ):
        self.client = client
        self.storage_path = storage_path
        self.max_history_points = max_history_points
        self.bot_config = bot_config

        self.start_timestamp: float = time.time()
        self.tracking_mode: str = "off_chain_with_onchain_btc"
        self._legacy_offchain_baseline_needs_btc: bool = False
        self.starting_portfolio_usd: Optional[float] = None
        self.starting_asset_prices: Dict[str, float] = {}
        self.starting_asset_amounts: Dict[str, float] = {}

        self.cumulative_rebates_usd: float = 0.0
        self.cumulative_volume_usd: float = 0.0
        self.realized_spread_pnl_usd: float = 0.0
        self.trades: List[TradeRecord] = []
        self.history_points: List[PerformancePoint] = []
        self.last_known_order_ids: Dict[str, set] = {}

        # Getters for captured profit components (triangular arb & active rebalancing)
        self._tri_arb_profit_getter: Optional[Callable[[], float]] = None
        self._active_rebal_profit_getter: Optional[Callable[[], float]] = None
        self._coinbase_saved_getter: Optional[Callable[[], float]] = None

        # Getter for Coinbase Advanced Trade float balances
        self._coinbase_balance_getter: Optional[Callable[[], Dict[str, Dict[str, float]]]] = None

        # Getter for Alby Hub sovereign Lightning channels and NWC balances
        self._alby_balance_getter: Optional[Callable[[], Dict[str, Dict[str, float]]]] = None
        self._alby_summary_getter: Optional[Callable[[], Dict[str, Any]]] = None

        # Fallback oracle prices referencing hydra-app node oracle
        cache_obj = getattr(self.client, "_oracle_price_cache", None)
        self.fallback_prices: Dict[str, float] = dict(cache_obj) if isinstance(cache_obj, dict) else {}

        self.last_snapshot_cache: Optional[Dict[str, Any]] = None
        self.last_snapshot_time: float = 0.0
        self.node_is_reachable: bool = True
        self.last_node_check_time: float = 0.0
        self.fee_tier_info: Dict[str, Any] = {}

        # Tax Logger for permanent, append-only tax audit records
        csv_file = getattr(self.bot_config, "tax_log_csv_path", "data/tax_trade_log.csv") if self.bot_config else "data/tax_trade_log.csv"
        jsonl_file = getattr(self.bot_config, "tax_log_jsonl_path", "data/tax_trade_log.jsonl") if self.bot_config else "data/tax_trade_log.jsonl"
        self.tax_logger = TaxLogger(csv_path=csv_file, jsonl_path=jsonl_file)

        # Load persisted baseline if exists
        self._load_state()

    def set_fee_tier_info(self, info: Dict[str, Any]):
        """Updates cached fee tier standing and volume metrics."""
        self.fee_tier_info = info or {}

    def _ensure_dir(self):
        d = os.path.dirname(self.storage_path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

    def _save_state(self):
        try:
            self._ensure_dir()
            data = {
                "start_timestamp": self.start_timestamp,
                "tracking_mode": self.tracking_mode,
                "starting_portfolio_usd": self.starting_portfolio_usd,
                "starting_asset_prices": self.starting_asset_prices,
                "starting_asset_amounts": self.starting_asset_amounts,
                "cumulative_rebates_usd": self.cumulative_rebates_usd,
                "cumulative_volume_usd": self.cumulative_volume_usd,
                "realized_spread_pnl_usd": self.realized_spread_pnl_usd,
                "trades": [asdict(t) for t in self.trades[:100]],  # Keep newest 100 trades
                "history_points": [asdict(p) for p in self.history_points[-self.max_history_points:]],
            }
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to persist PnL state: {e}")

    def _load_state(self):
        if not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # If previous state was tracking on-chain assets or legacy format (prior to off-chain migration)
            mode = data.get("tracking_mode")
            if mode not in ("off_chain", "off_chain_with_onchain_btc"):
                logger.info("PnL state migration: Legacy on-chain tracking detected. Re-baselining.")
                self.start_timestamp = time.time()
                self.starting_portfolio_usd = None
                self.starting_asset_prices = {}
                self.starting_asset_amounts = {}
                self.history_points = []
                self.cumulative_rebates_usd = data.get("cumulative_rebates_usd", 0.0)
                self.realized_spread_pnl_usd = data.get("realized_spread_pnl_usd", 0.0)
                loaded_trades = data.get("trades", [])
                self.trades = [TradeRecord(**t) for t in loaded_trades]
                self.cumulative_volume_usd = float(data.get("cumulative_volume_usd", sum(t.volume_usd for t in self.trades)))
                self._save_state()
                return

            if mode == "off_chain":
                # Baseline was recorded before on-chain BTC was included in total assets.
                # Flag for smooth baseline adaptation on first snapshot.
                self._legacy_offchain_baseline_needs_btc = True
            else:
                self.tracking_mode = mode

            self.start_timestamp = data.get("start_timestamp", self.start_timestamp)
            self.starting_portfolio_usd = data.get("starting_portfolio_usd")
            self.starting_asset_prices = data.get("starting_asset_prices", {})
            self.starting_asset_amounts = data.get("starting_asset_amounts", {})
            self.cumulative_rebates_usd = data.get("cumulative_rebates_usd", 0.0)
            self.realized_spread_pnl_usd = data.get("realized_spread_pnl_usd", 0.0)

            loaded_trades = data.get("trades", [])
            self.trades = [TradeRecord(**t) for t in loaded_trades]
            self.cumulative_volume_usd = float(data.get("cumulative_volume_usd", sum(t.volume_usd for t in self.trades)))

            loaded_pts = data.get("history_points", [])
            self.history_points = []
            for p in loaded_pts:
                if isinstance(p, dict):
                    if "captured_profit_usd" not in p:
                        p["captured_profit_usd"] = p.get("cumulative_rebates_usd", 0.0)
                    self.history_points.append(PerformancePoint(**p))
            logger.info(f"Loaded existing PnL history. Baseline: ${self.starting_portfolio_usd:.2f}" if self.starting_portfolio_usd else "No previous baseline.")
        except Exception as e:
            logger.warning(f"Could not load previous PnL state: {e}")

    def reset_baseline(self):
        """Clears previous baseline and sets current portfolio value as the new baseline."""
        self.starting_portfolio_usd = None
        self.starting_asset_prices.clear()
        self.starting_asset_amounts.clear()
        self.cumulative_rebates_usd = 0.0
        self.cumulative_volume_usd = 0.0
        self.realized_spread_pnl_usd = 0.0
        self.trades.clear()
        self.history_points.clear()
        self.start_timestamp = time.time()
        self.last_snapshot_cache = None
        self._save_state()
        logger.info("PnL baseline reset to current portfolio state.")

    def set_profit_sources(
        self,
        tri_arb_getter: Optional[Callable[[], float]] = None,
        active_rebal_getter: Optional[Callable[[], float]] = None,
        coinbase_saved_getter: Optional[Callable[[], float]] = None,
    ):
        """Injects getters for external arbitrage, active rebalancing, and Coinbase fee savings."""
        self._tri_arb_profit_getter = tri_arb_getter
        self._active_rebal_profit_getter = active_rebal_getter
        if coinbase_saved_getter:
            self._coinbase_saved_getter = coinbase_saved_getter

    def set_coinbase_bridge(self, coinbase_bridge: Any):
        """Injects CoinbaseBridge instance to query Coinbase balances and savings."""
        if coinbase_bridge and hasattr(coinbase_bridge, "get_coinbase_balances"):
            self._coinbase_balance_getter = lambda: coinbase_bridge.get_coinbase_balances()
            self._coinbase_saved_getter = lambda: getattr(coinbase_bridge, "total_fee_saved_usd", 0.0)

    def set_alby_balance_getter(self, getter: Optional[Callable[[], Dict[str, Dict[str, float]]]]):
        """Injects a getter function that returns cached Alby BTC balances."""
        self._alby_balance_getter = getter

    def set_nwc_bridge(self, nwc_bridge: Any):
        """Injects NWCBridge instance to query Alby sovereign node balances and channel summaries."""
        if nwc_bridge:
            if hasattr(nwc_bridge, "get_alby_balances"):
                self._alby_balance_getter = lambda: nwc_bridge.get_alby_balances()
            elif hasattr(nwc_bridge, "get_balances"):
                self._alby_balance_getter = lambda: nwc_bridge.get_balances()
            if hasattr(nwc_bridge, "get_alby_channels_summary"):
                self._alby_summary_getter = lambda: nwc_bridge.get_alby_channels_summary(self.get_asset_price("BTC"))

    def set_alby_bridge(self, alby_bridge: Any):
        """Alias for set_nwc_bridge."""
        self.set_nwc_bridge(alby_bridge)

    def get_captured_profit_breakdown(self) -> Dict[str, float]:
        """Calculates breakdown of captured profits: maker rebates, triangular arbitrage, and fee savings."""
        rebates = float(self.cumulative_rebates_usd)
        tri_arb = float(self._tri_arb_profit_getter()) if self._tri_arb_profit_getter else 0.0
        active_rebal = float(self._active_rebal_profit_getter()) if self._active_rebal_profit_getter else 0.0
        cb_saved = float(self._coinbase_saved_getter()) if getattr(self, "_coinbase_saved_getter", None) else 0.0
        total = rebates + tri_arb
        return {
            "maker_rebates_usd": round(rebates, 4),
            "tri_arb_profit_usd": round(tri_arb, 4),
            "active_rebalance_profit_usd": round(active_rebal, 4),
            "coinbase_fee_savings_usd": round(cb_saved, 4),
            "total_captured_profit_usd": round(total, 4),
        }

    def get_asset_price(self, symbol: str) -> float:
        """Retrieves asset fiat price from Hydra node or fallback."""
        now = time.time()
        # If node was unreachable recently, don't block on network calls
        if not self.node_is_reachable and (now - self.last_node_check_time < 10.0):
            return self.fallback_prices.get(symbol, 1.0)

        try:
            p = self.client.get_fiat_price(symbol)
            if p and p > 0:
                self.fallback_prices[symbol] = p
                self.node_is_reachable = True
                return p
        except Exception:
            self.node_is_reachable = False
            self.last_node_check_time = now
        return self.fallback_prices.get(symbol, 1.0)

    def get_portfolio_snapshot(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Polls node orderbook channel balances, on-chain balances, and oracle prices
        to construct a complete real-time portfolio snapshot across all assets.
        Uses in-memory cache to guarantee sub-millisecond response times for web dashboards.
        """
        now = time.time()
        if not force_refresh and self.last_snapshot_cache is not None and (now - self.last_snapshot_time < 2.5):
            return self.last_snapshot_cache

        holdings: Dict[str, AssetHolding] = {}

        # 1. Fetch channel balances from orderbook
        chan_balances = []
        node_responsive = True
        try:
            chan_balances = self.client.get_orderbook_balances()
        except Exception as e:
            node_responsive = False
            logger.debug(f"Hydranet node channel balance query failed: {e}")

        # Map channel balances by (network_id, asset_id)
        chan_map: Dict[Tuple[str, str], Dict[str, float]] = {}
        for item in chan_balances:
            curr = item.get("currency", {})
            asset_id = normalize_asset_id(curr.get("assetId", ""))
            net_id = str(curr.get("networkId", ""))
            bal = item.get("balance", {})
            sendable = float(bal.get("sending", {}).get("value", 0.0))
            receivable = float(bal.get("receiving", {}).get("value", 0.0))
            in_use_send = float(bal.get("inUseSending", {}).get("value", 0.0))
            in_use_recv = float(bal.get("inUseReceiving", {}).get("value", 0.0))
            unavail_send = float(bal.get("unavailableSending", {}).get("value", 0.0))
            chan_map[(net_id, asset_id)] = {
                "sendable": sendable,
                "receivable": receivable + in_use_recv,
                "in_use": in_use_send,
                "reserve": unavail_send,
            }

        # 2. Fetch on-chain and off-chain wallet balances for the 3 supported networks
        # Arbitrum (42161), Ethereum (1), Bitcoin (f9beb4d9)
        wallet_map: Dict[Tuple[str, str], Dict[str, float]] = {}
        if node_responsive:
            networks_to_query = [
                ("PROTOCOL_EVM", "42161"),
                ("PROTOCOL_EVM", "1"),
                ("PROTOCOL_BITCOIN", "f9beb4d9"),
            ]
            for proto, nid in networks_to_query:
                try:
                    w_bals = self.client.get_wallet_balances(proto, nid)
                    for raw_aid, binfo in w_bals.items():
                        aid = normalize_asset_id(raw_aid)
                        onchain_conf = float(binfo.get("onchain", {}).get("confirmed", {}).get("value", 0.0))
                        onchain_pend = float(binfo.get("onchain", {}).get("pending", {}).get("value", 0.0))

                        # If Bitcoin reports pending balance, verify whether an actual incoming
                        # transaction exists in the mempool to avoid displaying phantom settlement ghosts.
                        if proto == "PROTOCOL_BITCOIN" and onchain_pend > 0:
                            has_mempool_incoming = False
                            try:
                                if hasattr(self.client, "call"):
                                    tx_res = self.client.call("wallet_getTransactions", {"network": {"protocol": proto, "id": nid}})
                                    for t in tx_res.get("transactions", []):
                                        if t.get("status") == "TX_STATUS_IN_MEMPOOL" and t.get("received"):
                                            has_mempool_incoming = True
                                            break
                                else:
                                    has_mempool_incoming = True
                            except Exception as ex:
                                logger.debug(f"Failed to verify mempool transactions for {proto}/{nid}: {ex}")
                                has_mempool_incoming = False
                            if not has_mempool_incoming:
                                logger.debug(f"Filtering phantom onchain pending BTC: {onchain_pend:.8f} (no incoming mempool tx)")
                                onchain_pend = 0.0

                        offchain_free = float(binfo.get("offchain", {}).get("freeLocal", {}).get("value", 0.0))
                        offchain_unavail = float(binfo.get("offchain", {}).get("unavailableLocal", {}).get("value", 0.0))
                        offchain_reserve = float(binfo.get("offchain", {}).get("unspendableLocalReserve", {}).get("value", 0.0))
                        offchain_pending = float(binfo.get("offchain", {}).get("pendingLocal", {}).get("value", 0.0))
                        offchain_withdrawing = float(binfo.get("offchain", {}).get("withdrawingLocal", {}).get("value", 0.0))
                        offchain_remote = float(binfo.get("offchain", {}).get("freeRemote", {}).get("value", 0.0))
                        wallet_map[(nid, aid)] = {
                            "onchain_confirmed": onchain_conf,
                            "onchain_pending": onchain_pend,
                            "offchain_free": offchain_free,
                            "offchain_unavail": offchain_unavail,
                            "offchain_reserve": offchain_reserve,
                            "offchain_pending": offchain_pending,
                            "offchain_withdrawing": offchain_withdrawing,
                            "offchain_remote": offchain_remote,
                        }
                except Exception as e:
                    logger.debug(f"Failed to fetch wallet balances for {proto}/{nid}: {e}")

        # 3. Define the core assets in the Hydranet ecosystem
        defined_assets = [
            {
                "key": "HDN",
                "symbol": "HDN",
                "price_symbol": "HDN",
                "network": "Arbitrum",
                "net_id": "42161",
                "asset_id": "erc20:0xb0f66bdb39acbb043308eb9dbe78f5bb47ea5430",
                "gas_reserve_usd": 0.0,
            },
            {
                "key": "USDC_ARB",
                "symbol": "USDC",
                "price_symbol": "USDC",
                "network": "Arbitrum",
                "net_id": "42161",
                "asset_id": "erc20:0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                "gas_reserve_usd": 0.0,
            },
            {
                "key": "USDC_ETH",
                "symbol": "USDC",
                "price_symbol": "USDC",
                "network": "Ethereum",
                "net_id": "1",
                "asset_id": "erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                "gas_reserve_usd": 0.0,
            },
            {
                "key": "ETH_ARB",
                "symbol": "ETH",
                "price_symbol": "ETH",
                "network": "Arbitrum",
                "net_id": "42161",
                "asset_id": "native",
                "gas_reserve_usd": 25.0,  # $25 on-chain reserve required
            },
            {
                "key": "ETH_ETH",
                "symbol": "ETH",
                "price_symbol": "ETH",
                "network": "Ethereum",
                "net_id": "1",
                "asset_id": "native",
                "gas_reserve_usd": 25.0,  # $25 on-chain reserve required
            },
            {
                "key": "BTC",
                "symbol": "BTC",
                "price_symbol": "BTC",
                "network": "Bitcoin",
                "net_id": "f9beb4d9",
                "asset_id": "native",
                "gas_reserve_usd": 0.0,
            },
        ]

        total_portfolio_usd = 0.0
        total_channel_usd = 0.0
        total_onchain_usd = 0.0

        for a in defined_assets:
            nid = a["net_id"]
            aid = normalize_asset_id(a["asset_id"])
            price_sym = a.get("price_symbol", a["symbol"])
            price = self.get_asset_price(price_sym)

            ch = chan_map.get((nid, aid), {})
            wb = wallet_map.get((nid, aid), {})

            # Orderbook breakdown
            sendable = ch.get("sendable", wb.get("offchain_free", 0.0))
            in_use = ch.get("in_use", 0.0)
            orderbook_reserve = ch.get("reserve", 0.0)
            receivable = ch.get("receivable", wb.get("offchain_remote", 0.0))

            # Ground-truth wallet totals
            on_chain = wb.get("onchain_confirmed", 0.0)
            on_chain_pending = wb.get("onchain_pending", 0.0)
            offchain_unavail = wb.get("offchain_unavail", 0.0)
            offchain_reserve = wb.get("offchain_reserve", 0.0)
            offchain_pending = wb.get("offchain_pending", 0.0)
            offchain_withdrawing = wb.get("offchain_withdrawing", 0.0)

            # Protocol safety reserve + recovering/in-flight channel funds
            reserve = max(orderbook_reserve, offchain_reserve) + offchain_unavail + offchain_pending + offchain_withdrawing

            if a["key"] == "USDC_ARB":
                usdc_res_req = getattr(self.bot_config, "min_onchain_usdc_reserve_usd", None) if hasattr(self, "bot_config") and self.bot_config else None
                if usdc_res_req is None:
                    try:
                        from config import _get_default_usdc_reserve
                        usdc_res_req = _get_default_usdc_reserve()
                    except Exception:
                        usdc_res_req = 1030.0
                gas_reserve_req = float(usdc_res_req)
                usable_on_chain = max(0.0, on_chain - gas_reserve_req)
            else:
                gas_reserve_req = (a["gas_reserve_usd"] / price) if price > 0 else 0.0
                usable_on_chain = max(0.0, on_chain - gas_reserve_req)

            # Invariant total channel amount (free + resting in orders + reserves)
            channel_total = sendable + in_use + reserve
            channel_usd = channel_total * price
            onchain_amount = on_chain + on_chain_pending
            onchain_usd = onchain_amount * price
            combined_amount = channel_total + onchain_amount
            combined_usd = combined_amount * price

            # Portfolio balance & P&L track off-chain channel assets, NWC/Alby liquidity, and on-chain BTC
            include_onchain_btc = getattr(self.bot_config, "include_onchain_btc_in_portfolio", True) if hasattr(self, "bot_config") and self.bot_config else True
            if a["key"] == "BTC" and include_onchain_btc:
                total_portfolio_usd += channel_usd + onchain_usd
                holdings_total_amount = combined_amount
                holdings_total_usd = round(combined_usd, 2)
            else:
                total_portfolio_usd += channel_usd
                holdings_total_amount = channel_total
                holdings_total_usd = round(channel_usd, 2)

            total_channel_usd += channel_usd
            total_onchain_usd += onchain_usd

            holdings[a["key"]] = AssetHolding(
                symbol=a["symbol"],
                network=a["network"],
                asset_id=aid,
                price_usd=price,
                channel_sendable=sendable,
                channel_receivable=receivable,
                channel_in_use=in_use,
                channel_reserve=reserve,
                channel_total=channel_total,
                channel_usd=round(channel_usd, 2),
                on_chain=on_chain,
                on_chain_pending=on_chain_pending,
                on_chain_usd=round(onchain_usd, 2),
                gas_reserve_required=gas_reserve_req,
                usable_on_chain=usable_on_chain,
                total_amount=holdings_total_amount,
                total_usd=holdings_total_usd,
                total_combined_usd=round(combined_usd, 2),
            )

        # Extract on-chain BTC metrics for easy access and UI display
        btc_holding = holdings.get("BTC")
        onchain_btc_amt = (btc_holding.on_chain + btc_holding.on_chain_pending) if btc_holding else 0.0
        onchain_btc_usd = (onchain_btc_amt * btc_holding.price_usd) if btc_holding else 0.0

        # One-time migration for legacy off-chain baseline without on-chain BTC
        if getattr(self, "_legacy_offchain_baseline_needs_btc", False):
            if self.starting_portfolio_usd is not None and self.starting_portfolio_usd > 0 and onchain_btc_amt > 0:
                base_btc_p = self.starting_asset_prices.get("BTC", btc_holding.price_usd if btc_holding else 86767.0)
                adj_usd = onchain_btc_amt * base_btc_p
                self.starting_portfolio_usd += adj_usd
                if "BTC" in self.starting_asset_amounts:
                    self.starting_asset_amounts["BTC"] += onchain_btc_amt
                logger.info(f"Migrated PnL baseline to include pre-existing on-chain BTC (+{onchain_btc_amt:.6f} BTC / +${adj_usd:.2f}): new baseline ${self.starting_portfolio_usd:.2f}")
            self._legacy_offchain_baseline_needs_btc = False
            self.tracking_mode = "off_chain_with_onchain_btc"
            self._save_state()

        # Include Alby Hub Sovereign Lightning node balances
        total_alby_usd = 0.0
        alby_balances = None
        alby_summary = None
        alby_btc_avail = 0.0
        alby_btc_tot = 0.0
        if getattr(self.bot_config, "include_alby_in_portfolio", True):
            if self._alby_balance_getter:
                try:
                    alby_balances = self._alby_balance_getter()
                except Exception as e:
                    logger.debug(f"Failed to query Alby balances via getter: {e}")

            if getattr(self, "_alby_summary_getter", None):
                try:
                    alby_summary = self._alby_summary_getter()
                except Exception as e:
                    logger.debug(f"Failed to query Alby channel summary: {e}")

        if alby_balances and isinstance(alby_balances, dict):
            a_btc = alby_balances.get("BTC", {})
            alby_btc_avail = float(a_btc.get("available", 0.0) or 0.0)
            alby_btc_tot = float(a_btc.get("total", 0.0) or alby_btc_avail)
            if alby_btc_tot > 0.00000001:
                btc_price = self.get_asset_price("BTC")
                alby_btc_usd = alby_btc_tot * btc_price
                total_alby_usd = alby_btc_usd

                # Extract remote receivable capacity if channels are known
                alby_recv_btc = 0.00209592  # default remote headroom (209,592 sats)
                alby_cap_btc = 0.00705000   # default channel capacity (705,000 sats)
                if alby_summary and isinstance(alby_summary, dict):
                    totals = alby_summary.get("totals", {})
                    if totals.get("remote_balance_btc"):
                        alby_recv_btc = float(totals["remote_balance_btc"])
                    if totals.get("total_capacity_btc"):
                        alby_cap_btc = float(totals["total_capacity_btc"])

                holdings["ALBY_BTC"] = AssetHolding(
                    symbol="BTC",
                    network="Alby",
                    asset_id="alby:btc",
                    price_usd=btc_price,
                    channel_sendable=alby_btc_avail,
                    channel_receivable=alby_recv_btc,
                    channel_in_use=0.0,
                    channel_reserve=0.0,
                    channel_total=alby_cap_btc,
                    channel_usd=round(alby_btc_usd, 2),
                    on_chain=0.0,
                    on_chain_pending=0.0,
                    on_chain_usd=0.0,
                    gas_reserve_required=0.0,
                    usable_on_chain=alby_btc_avail,
                    total_amount=alby_btc_tot,
                    total_usd=round(alby_btc_usd, 2),
                    total_combined_usd=round(alby_btc_usd, 2),
                )

        # Include Alby balances in total portfolio valuation
        total_portfolio_usd += total_alby_usd

        # Include Coinbase Advanced Trade float balances
        total_coinbase_usd = 0.0
        coinbase_balances = None
        if getattr(self.bot_config, "include_coinbase_in_portfolio", True):
            if self._coinbase_balance_getter:
                try:
                    coinbase_balances = self._coinbase_balance_getter()
                except Exception as e:
                    logger.debug(f"Failed to query Coinbase balances via getter: {e}")

            if coinbase_balances and isinstance(coinbase_balances, dict):
                # USDC
                cb_usdc_tot = float(coinbase_balances.get("USDC", {}).get("total", 0.0) or 0.0)
                cb_usdc_avail = float(coinbase_balances.get("USDC", {}).get("available", 0.0) or 0.0)
                if cb_usdc_tot > 0.01:
                    holdings["COINBASE_USDC"] = AssetHolding(
                        symbol="USDC",
                        network="Coinbase",
                        asset_id="coinbase:usdc",
                        price_usd=1.0,
                        channel_sendable=cb_usdc_avail,
                        channel_receivable=0.0,
                        channel_in_use=0.0,
                        channel_reserve=0.0,
                        channel_total=cb_usdc_tot,
                        channel_usd=round(cb_usdc_tot, 2),
                        on_chain=0.0,
                        on_chain_pending=0.0,
                        on_chain_usd=0.0,
                        gas_reserve_required=0.0,
                        usable_on_chain=cb_usdc_avail,
                        total_amount=cb_usdc_tot,
                        total_usd=round(cb_usdc_tot, 2),
                        total_combined_usd=round(cb_usdc_tot, 2),
                    )
                    total_coinbase_usd += cb_usdc_tot

                # BTC
                cb_btc_tot = float(coinbase_balances.get("BTC", {}).get("total", 0.0) or 0.0)
                cb_btc_avail = float(coinbase_balances.get("BTC", {}).get("available", 0.0) or 0.0)
                if cb_btc_tot > 0.00000001:
                    btc_p = self.get_asset_price("BTC")
                    cb_btc_usd = cb_btc_tot * btc_p
                    holdings["COINBASE_BTC"] = AssetHolding(
                        symbol="BTC",
                        network="Coinbase",
                        asset_id="coinbase:btc",
                        price_usd=btc_p,
                        channel_sendable=cb_btc_avail,
                        channel_receivable=0.0,
                        channel_in_use=0.0,
                        channel_reserve=0.0,
                        channel_total=cb_btc_tot,
                        channel_usd=round(cb_btc_usd, 2),
                        on_chain=0.0,
                        on_chain_pending=0.0,
                        on_chain_usd=0.0,
                        gas_reserve_required=0.0,
                        usable_on_chain=cb_btc_avail,
                        total_amount=cb_btc_tot,
                        total_usd=round(cb_btc_usd, 2),
                        total_combined_usd=round(cb_btc_usd, 2),
                    )
                    total_coinbase_usd += cb_btc_usd

                # ETH
                cb_eth_tot = float(coinbase_balances.get("ETH", {}).get("total", 0.0) or 0.0)
                cb_eth_avail = float(coinbase_balances.get("ETH", {}).get("available", 0.0) or 0.0)
                if cb_eth_tot > 0.0001:
                    eth_p = self.get_asset_price("ETH")
                    cb_eth_usd = cb_eth_tot * eth_p
                    holdings["COINBASE_ETH"] = AssetHolding(
                        symbol="ETH",
                        network="Coinbase",
                        asset_id="coinbase:eth",
                        price_usd=eth_p,
                        channel_sendable=cb_eth_avail,
                        channel_receivable=0.0,
                        channel_in_use=0.0,
                        channel_reserve=0.0,
                        channel_total=cb_eth_tot,
                        channel_usd=round(cb_eth_usd, 2),
                        on_chain=0.0,
                        on_chain_pending=0.0,
                        on_chain_usd=0.0,
                        gas_reserve_required=0.0,
                        usable_on_chain=cb_eth_avail,
                        total_amount=cb_eth_tot,
                        total_usd=round(cb_eth_usd, 2),
                        total_combined_usd=round(cb_eth_usd, 2),
                    )
                    total_coinbase_usd += cb_eth_usd

        # Include Coinbase balances in total portfolio valuation
        total_portfolio_usd += total_coinbase_usd

        # Compute % allocation across portfolio
        for k, h in holdings.items():
            if total_portfolio_usd > 0:
                h.pct_of_portfolio = (h.total_usd / total_portfolio_usd) * 100.0

        # Snapshot baseline if first run or if previous baseline was inaccurate (< $200)
        if (self.starting_portfolio_usd is None or self.starting_portfolio_usd < 200.0) and total_portfolio_usd > 200.0:
            self.starting_portfolio_usd = total_portfolio_usd
            for k, h in holdings.items():
                self.starting_asset_prices[k] = h.price_usd
                self.starting_asset_amounts[k] = h.total_amount
            self._save_state()
            logger.info(f"Initialized PnL starting baseline: ${total_portfolio_usd:.2f}")

        # Calculate P&L metrics
        baseline = self.starting_portfolio_usd or total_portfolio_usd
        total_pnl_usd = total_portfolio_usd - baseline
        total_pnl_pct = (total_pnl_usd / baseline * 100.0) if baseline > 0 else 0.0

        # Estimate market beta / inventory drift vs MM alpha across off-chain assets
        inventory_drift_usd = 0.0
        for k, h in holdings.items():
            start_price = self.starting_asset_prices.get(k, h.price_usd)
            start_amt = self.starting_asset_amounts.get(k, h.total_amount)
            inventory_drift_usd += start_amt * (h.price_usd - start_price)

        # Query tax-audited 30-day rolling trading performance from TaxLogger (independent of baseline resets)
        trading_perf = {}
        if hasattr(self, "tax_logger") and self.tax_logger:
            try:
                trading_perf = self.tax_logger.get_trading_performance(window_days=30.0)
            except Exception as e:
                logger.debug(f"Failed to query tax logger trading performance: {e}")

        realized_trading_pnl_usd = float(trading_perf.get("net_realized_alpha_usd", self.cumulative_rebates_usd + self.realized_spread_pnl_usd))
        realized_spread_pnl = float(trading_perf.get("realized_spread_pnl_usd", self.realized_spread_pnl_usd))
        tax_rebates = float(trading_perf.get("maker_rebates_usd", self.cumulative_rebates_usd))
        rebal_cost = float(trading_perf.get("rebalance_cost_usd", 0.0))
        tax_trades_count = int(trading_perf.get("total_trades", len(self.trades)))
        tax_volume_usd = float(trading_perf.get("total_volume_usd", self.cumulative_volume_usd))

        market_maker_alpha_usd = realized_trading_pnl_usd

        # Captured profit calculation (Maker Rebates + Tri Arb)
        captured_breakdown = self.get_captured_profit_breakdown()
        captured_total = captured_breakdown["total_captured_profit_usd"]

        # Record time-series performance point
        now = time.time()
        time_str = time.strftime("%H:%M:%S", time.gmtime(now))
        if not self.history_points or (now - self.history_points[-1].timestamp >= 10.0):
            pt = PerformancePoint(
                timestamp=now,
                time_label=time_str,
                portfolio_usd=round(total_portfolio_usd, 2),
                pnl_usd=round(total_pnl_usd, 4),
                pnl_pct=round(total_pnl_pct, 4),
                cumulative_rebates_usd=round(self.cumulative_rebates_usd, 4),
                captured_profit_usd=round(captured_total, 4),
            )
            self.history_points.append(pt)
            if len(self.history_points) > self.max_history_points:
                self.history_points = self.history_points[-self.max_history_points:]
            self._save_state()

        res = {
            "timestamp": now,
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
            "total_portfolio_usd": round(total_portfolio_usd, 2),
            "total_channel_usd": round(total_channel_usd, 2),
            "total_onchain_usd": round(total_onchain_usd, 2),
            "total_alby_usd": round(total_alby_usd, 2),
            "total_alby_btc": round(alby_btc_avail, 8),
            "total_onchain_btc_usd": round(onchain_btc_usd, 2),
            "onchain_btc_amount": round(onchain_btc_amt, 8),
            "alby_balances": alby_balances or {},
            "starting_portfolio_usd": round(baseline, 2),
            "total_pnl_usd": round(total_pnl_usd, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "realized_trading_pnl_usd": round(realized_trading_pnl_usd, 2),
            "realized_trading_pnl_30d_usd": round(realized_trading_pnl_usd, 2),
            "realized_spread_pnl_usd": round(realized_spread_pnl, 2),
            "realized_spread_pnl_30d_usd": round(realized_spread_pnl, 2),
            "tax_maker_rebates_usd": round(tax_rebates, 4),
            "tax_maker_rebates_30d_usd": round(tax_rebates, 4),
            "rebalance_cost_usd": round(rebal_cost, 2),
            "rebalance_cost_30d_usd": round(rebal_cost, 2),
            "tax_trades_count": tax_trades_count,
            "tax_trades_30d_count": tax_trades_count,
            "tax_volume_usd": round(tax_volume_usd, 2),
            "tax_volume_30d_usd": round(tax_volume_usd, 2),
            "trading_perf_by_market": trading_perf.get("by_market", {}),
            "cumulative_rebates_usd": round(self.cumulative_rebates_usd, 4),
            "captured_profit_usd": round(captured_total, 4),
            "captured_profit": captured_breakdown,
            "market_maker_alpha_usd": round(market_maker_alpha_usd, 4),
            "inventory_drift_usd": round(inventory_drift_usd, 2),
            "usdc_reserve_usd": getattr(self.bot_config, "min_onchain_usdc_reserve_usd", 1830.0) if hasattr(self, "bot_config") and self.bot_config else 1830.0,
            "fee_tier": self.fee_tier_info,
            "volume_30d_usd": round(float(self.fee_tier_info.get("settled_volume_30d_usd", 0.0)), 2) if (self.fee_tier_info and isinstance(self.fee_tier_info, dict)) else round(self.cumulative_volume_usd, 2),
            "holdings": {k: asdict(h) for k, h in holdings.items()},
        }
        self.last_snapshot_cache = res
        self.last_snapshot_time = now
        return res

    def get_settled_volume_30d(self) -> float:
        """Returns the rolling 30-day settled volume from fee tier info, or cumulative volume as fallback."""
        if self.fee_tier_info and isinstance(self.fee_tier_info, dict):
            vol = self.fee_tier_info.get("settled_volume_30d_usd")
            if isinstance(vol, (int, float)):
                return float(vol)
        return self.get_volume_since_reset()

    def record_trade(
        self,
        pair: str,
        side: str,
        price: float,
        amount: float,
        maker_rebate_rate: float = -0.0005,
        order_id: Optional[str] = None,
        trade_type: str = "MAKER",
        profit_usd: float = 0.0,
    ):
        """Records an executed trade fill (maker ladder or active taker rebalance)."""
        now = time.time()
        vol_usd = 0.0

        # Calculate volume in USD
        if "USDC" in pair:
            vol_usd = amount * price if side.upper() == "SELL" else amount * price
        else:
            market = MARKETS.get(pair)
            if market:
                quote_price = self.get_asset_price(market.quote.symbol)
                vol_usd = (amount * price) * quote_price
            else:
                vol_usd = amount * price

        # Rebate rate is negative fee: e.g. -0.0005 -> maker gets +0.05% of volume
        # Active rebalance and CEX trades are taker orders (no maker rebate)
        if trade_type in ("ACTIVE_REBALANCE", "CEX", "TRIANGULAR_ARB"):
            rebate_rate_abs = 0.0
            rebate_usd = 0.0
        else:
            rebate_rate_abs = abs(maker_rebate_rate)
            rebate_usd = vol_usd * rebate_rate_abs

        trade = TradeRecord(
            trade_id=f"trd-{int(now * 1000)}",
            timestamp=now,
            time_str=time.strftime("%H:%M:%S", time.gmtime(now)),
            pair=pair,
            side=side.upper(),
            price=price,
            amount=amount,
            volume_usd=round(vol_usd, 2),
            maker_rebate_rate=rebate_rate_abs,
            rebate_usd=round(rebate_usd, 4),
            order_id=order_id,
            trade_type=trade_type,
            profit_usd=round(profit_usd, 4),
        )

        self.trades.insert(0, trade)
        if len(self.trades) > 200:
            self.trades = self.trades[:200]

        if rebate_usd > 0:
            self.cumulative_rebates_usd += rebate_usd
        self.cumulative_volume_usd += vol_usd
        self._save_state()

        # Log to permanent append-only tax audit ledger
        if hasattr(self, "tax_logger") and self.tax_logger:
            try:
                m = MARKETS.get(pair)
                base_p = self.get_asset_price(m.base.symbol) if m else 0.0
                quote_p = self.get_asset_price(m.quote.symbol) if m else (1.0 if "USDC" in pair else 0.0)
                self.tax_logger.log_trade(
                    trade_id=trade.trade_id,
                    timestamp=trade.timestamp,
                    pair=pair,
                    side=side,
                    price=price,
                    amount=amount,
                    volume_usd=vol_usd,
                    maker_rebate_rate=rebate_rate_abs,
                    rebate_usd=rebate_usd,
                    order_id=order_id,
                    trade_type=trade_type,
                    profit_usd=profit_usd,
                    base_usd_price=base_p,
                    quote_usd_price=quote_p,
                )
            except Exception as e:
                logger.warning(f"Failed to record trade to tax log: {e}")

        logger.info(f"Recorded trade fill ({trade_type}) on {pair}: {side} {amount} @ {price}. Rebate earned: +${rebate_usd:.4f}")
        return trade

    def get_tax_summary(self) -> Dict[str, Any]:
        """Returns aggregate tax summary statistics."""
        if hasattr(self, "tax_logger") and self.tax_logger:
            return self.tax_logger.get_summary()
        return {}

    def get_volume_since_reset(self) -> float:
        """Calculates total accumulated traded volume since the last baseline reset."""
        return round(self.cumulative_volume_usd, 2)

    def get_24h_volume(self) -> float:
        """Alias returning volume since last reset."""
        return self.get_volume_since_reset()

    def get_performance_history(self) -> List[Dict[str, Any]]:
        """Returns time series of PnL data points for charting."""
        return [asdict(p) for p in self.history_points]

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns recent trade records."""
        return [asdict(t) for t in self.trades[:limit]]
