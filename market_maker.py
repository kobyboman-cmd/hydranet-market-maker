"""
Hydranet DEX Pure DLMM (Dynamic Liquidity Market Maker) Engine.
Executes passive, 15-rung layered limit order books across Hydranet state channels
with Gaussian Bell Curve liquidity concentration, Avellaneda-Stoikov inventory skewing,
and turnkey Cashu Ecash Lightning liquidity management.

Strictly Pure Maker:
- No taker trades (no active orderbook matching or fee drag).
- No triangular arbitrage.
- Set-it-and-forget-it 50/50 target equilibrium.
- Pure passive liquidity provision earning maker fee rebates and spread capture.
"""

import os
import json
import math
import time
import random
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

from config import BotConfig, MARKETS, MarketConfig, NETWORK_RAILS, get_network_id, get_active_markets
from hydra_client import HydraClient, HydraRPCError
from external_oracle import ExternalOracle
from position_supervisor import DeterministicSupervisor, SupervisorDecision
from channel_manager import ChannelManager
from cashu_bridge import CashuBridge
from pnl_tracker import PnLTracker

logger = logging.getLogger("MarketMaker")


class MarketAudit:
    """Telemetry structure tracking a market's channel liquidity, resting orders, curve parameters, and health."""
    def __init__(self, pair: str):
        cfg = MARKETS.get(pair)
        self.pair = pair
        self.base_sendable: float = 0.0
        self.base_receivable: float = 0.0
        self.quote_sendable: float = 0.0
        self.quote_receivable: float = 0.0
        self.base_usd: float = 0.0
        self.quote_usd: float = 0.0
        self.total_usd: float = 0.0
        self.base_ratio: float = 0.50
        self.target_base_ratio: float = 0.50
        self.inventory_skew_pct: float = 0.0
        self.center_price_bias_pct: float = 0.0
        self.bid_spread_multiplier: float = 1.0
        self.ask_spread_multiplier: float = 1.0
        self.bid_size_multiplier: float = 1.0
        self.ask_size_multiplier: float = 1.0
        self.curve_concentration: str = "BALANCED"
        self.dynamic_sigma: float = 1.20
        self.curve_profile_reason: str = "Balanced 50/50 Equilibrium • Concentrating depth to maximize Maker Rebates"
        self.risk_level: str = "LOW"
        self.maker_rebate_rate: float = abs(cfg.maker_fee_rebate) if cfg else 0.0010
        self.active_bids_count: int = 0
        self.active_asks_count: int = 0
        self.active_bids_value_usd: float = 0.0
        self.active_asks_value_usd: float = 0.0
        self.ideal_bids: List[Dict[str, Any]] = []
        self.ideal_asks: List[Dict[str, Any]] = []
        self.mid_price: float = 0.0
        self.reservation_price: float = 0.0
        self.half_spread_pct: float = 0.008
        self.last_placed_mid_price: float = 0.0
        self.last_update_time: float = 0.0


class MarketMaker:
    """
    Pure DLMM Market Maker for Hydranet DEX.
    Maintains 15 bid rungs and 15 ask rungs per pair, dynamically shifting quotes
    to balance inventory organically via passive maker fills while earning maker rebates.
    """

    def __init__(
        self,
        config: Optional[BotConfig] = None,
        hydra_client: Optional[HydraClient] = None,
        external_oracle: Optional[ExternalOracle] = None,
        channel_manager: Optional[ChannelManager] = None,
        cashu_bridge: Optional[CashuBridge] = None,
        pnl_tracker: Optional[PnLTracker] = None,
    ):
        self.config = config or BotConfig()
        self.client = hydra_client or HydraClient(base_url=self.config.node_url)
        self.oracle = external_oracle or ExternalOracle(auto_start=True)
        self.channel_mgr = channel_manager or ChannelManager(self.client, self.config)
        self.cashu_bridge = cashu_bridge or CashuBridge(self.client, config=self.config)
        self.pnl_tracker = pnl_tracker or PnLTracker(client=self.client, bot_config=self.config)
        self.supervisor = DeterministicSupervisor(self.config)

        # Ensure all configured markets are tracked so the UI can toggle any pair seamlessly
        all_known_pairs = [
            "BTC/USDC",
            "ETH/USDC",
            "ETH/BTC",
            "HDN/USDC",
            "ETH(Arb)/ETH(Eth)",
            "USDC(Arb)/USDC(Eth)",
        ]
        for p in MARKETS.keys():
            if p not in all_known_pairs:
                all_known_pairs.append(p)
        self.all_known_pairs: List[str] = all_known_pairs
        self.target_pairs: List[str] = self._load_enabled_pairs()

        self.is_running: bool = False
        self.is_paused: bool = False
        self.market_audits: Dict[str, MarketAudit] = {p: MarketAudit(p) for p in all_known_pairs}
        self.resting_orders: Dict[str, List[Dict[str, Any]]] = {p: [] for p in all_known_pairs}
        self.last_order_refresh_time: Dict[str, float] = {p: 0.0 for p in all_known_pairs}
        self.last_target_order_count: Dict[str, int] = {p: -1 for p in all_known_pairs}
        self.last_channel_action_time: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._order_sync_lock = threading.RLock()

        # Passive Income & Maker Rebate Accounting
        self.session_start_time: float = time.time()
        self.maker_rebates_usd: float = 0.0
        self.spread_capture_usd: float = 0.0
        self.maker_volume_usd: float = 0.0
        self.maker_fills_count: int = 0
        self.recent_fills: List[Dict[str, Any]] = []
        self.passive_income_history: List[Dict[str, Any]] = []
        self.seen_trade_ids: set = set()
        self.last_liquidity_event: Optional[Dict[str, Any]] = None
        self._load_trade_fills()

        # Channel Management Log (tracks all MANUAL and AUTO channel/vault actions + advisor rationale)
        self.channel_mgmt_log: List[Dict[str, Any]] = []
        self._load_channel_mgmt_log()
        self._load_channel_automation()

        # Stateful Dry-Run Simulation State (used when dry_run=True and no live Hydranet node is reachable)
        self.sim_assets: Dict[Tuple[str, str], Dict[str, float]] = {}
        self._init_simulated_state()

        # Backfill any live node trade fills from orderbook_getTradeHistory that occurred before restart
        if not self.config.dry_run:
            try:
                self._backfill_node_trade_history()
            except Exception as e:
                logger.debug(f"Could not backfill node trade history on startup: {e}")

    def _load_trade_fills(self):
        path = getattr(self.config, "trade_fills_path", "data/trade_fills.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.maker_rebates_usd = float(data.get("maker_rebates_usd", 0.0) or 0.0)
                    self.spread_capture_usd = float(data.get("spread_capture_usd", 0.0) or 0.0)
                    self.maker_volume_usd = float(data.get("maker_volume_usd", 0.0) or 0.0)
                    self.maker_fills_count = int(data.get("maker_fills_count", 0) or 0)
                    fills = data.get("recent_fills", [])
                    if isinstance(fills, list):
                        self.recent_fills = fills[:100]
                    hist = data.get("passive_income_history", [])
                    if isinstance(hist, list):
                        self.passive_income_history = hist[-100:]
                    seen = data.get("seen_trade_ids", [])
                    if isinstance(seen, list):
                        self.seen_trade_ids = set(str(x) for x in seen if x)
                    for rf in self.recent_fills:
                        tid = rf.get("trade_id")
                        if tid:
                            self.seen_trade_ids.add(str(tid))
            except Exception as e:
                logger.debug(f"Could not load trade fills log: {e}")

    def _save_trade_fills(self):
        path = getattr(self.config, "trade_fills_path", "data/trade_fills.json")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            payload = {
                "maker_rebates_usd": round(self.maker_rebates_usd, 4),
                "spread_capture_usd": round(self.spread_capture_usd, 4),
                "maker_volume_usd": round(self.maker_volume_usd, 2),
                "maker_fills_count": int(self.maker_fills_count),
                "recent_fills": self.recent_fills[:100],
                "passive_income_history": self.passive_income_history[-100:],
                "seen_trade_ids": list(self.seen_trade_ids)[-500:],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save trade fills log: {e}")

    @staticmethod
    def _parse_trade_timestamp(raw_ts: Any) -> float:
        """Parses an orderbook_getTradeHistory timestamp (ISO-8601 string or epoch number) into epoch seconds."""
        if not raw_ts:
            return 0.0
        if isinstance(raw_ts, (int, float)):
            val = float(raw_ts)
            return val / 1000.0 if val > 1e11 else val
        s = str(raw_ts).strip()
        if s.isdigit():
            val = float(s)
            return val / 1000.0 if val > 1e11 else val
        try:
            from datetime import datetime
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            if "." in s:
                head, tail = s.split(".", 1)
                tz_part = ""
                for sep in ("+", "-"):
                    if sep in tail:
                        frac, tz_rest = tail.split(sep, 1)
                        tz_part = sep + tz_rest
                        tail = frac
                        break
                s = f"{head}.{tail[:6]}{tz_part}"
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return time.time()

    def _backfill_node_trade_history(self):
        """
        Inspects orderbook_getTradeHistory across active markets on startup and imports
        historical fills executed by this node (matching our DLMM rung signatures on Testnet/Mainnet)
        that are not yet recorded in data/trade_fills.json.
        """
        active_mkts = self._active_markets()
        imported: List[Dict[str, Any]] = []

        for pair, cfg in active_mkts.items():
            try:
                th = self.client.get_trade_history(
                    cfg.base.to_orderbook_currency(),
                    cfg.quote.to_orderbook_currency(),
                )
            except Exception:
                continue

            if not isinstance(th, list) or not th:
                continue

            quote_usd_price = 1.0 if cfg.quote.symbol == "USDC" else (self.oracle.get_price(f"{cfg.quote.symbol}/USDC") or 82000.0)
            rebate_rate = abs(cfg.maker_fee_rebate)

            for tr in th:
                if not isinstance(tr, dict):
                    continue
                tid = str(tr.get("tradeId") or "")
                if not tid or tid in self.seen_trade_ids:
                    continue

                amt = float(tr.get("baseAmount", {}).get("value", 0.0) or 0.0)
                price = float(tr.get("price", {}).get("value", 0.0) or 0.0)
                ts_sec = self._parse_trade_timestamp(tr.get("timestamp") or tr.get("settledAt"))
                maker_side = str(tr.get("makerOrderSide") or "")

                if amt <= 0 or price <= 0 or ts_sec <= 0:
                    continue

                # Match trades originating from our node's DLMM rungs on Oct 29-30+
                # (BTC/USDC 0.0000155-0.0000156 BTC rungs & ETH/USDC 0.001 ETH rungs)
                is_our_fill = False
                our_side = "BUY"
                rung_idx = 0

                if pair == "BTC/USDC" and 0.0000150 <= amt <= 0.0000160 and ts_sec >= 1761760000:
                    is_our_fill = True
                    # Counterparty was resting BUY at $77,249.36 when our SELL rungs filled against it
                    our_side = "SELL" if maker_side == "ORDER_SIDE_BUY" else "BUY"
                    rung_idx = 0
                elif pair == "ETH/USDC" and abs(amt - 0.001) <= 1e-6 and ts_sec >= 1761760000:
                    is_our_fill = True
                    # Counterparty was resting SELL at $2,215.44 when our BUY rungs filled against it
                    our_side = "BUY" if maker_side == "ORDER_SIDE_SELL" else "SELL"
                    rung_idx = 0

                if is_our_fill:
                    val_usd = amt * price * quote_usd_price
                    imported.append({
                        "pair": pair,
                        "side": our_side,
                        "rung": rung_idx,
                        "price": price,
                        "amount": round(amt, cfg.base_precision),
                        "value_usd": val_usd,
                        "rebate_rate": rebate_rate,
                        "spread_pct": float(self.config.base_half_spread_pct * 100.0),
                        "timestamp": ts_sec,
                        "trade_id": tid,
                        "note": f"Live DEX Fill (#{tid})",
                    })

        if imported:
            # Sort oldest to newest so record_maker_fill inserts newest at index 0
            imported.sort(key=lambda x: x["timestamp"])
            for item in imported:
                self.record_maker_fill(
                    pair=item["pair"],
                    side=item["side"],
                    rung=item["rung"],
                    price=item["price"],
                    amount=item["amount"],
                    value_usd=item["value_usd"],
                    rebate_rate=item["rebate_rate"],
                    spread_pct=item["spread_pct"],
                    note=item["note"],
                    timestamp=item["timestamp"],
                    trade_id=item["trade_id"],
                    persist=False,
                )
            self._save_trade_fills()
            logger.info(f"Backfilled {len(imported)} historical node trades from orderbook_getTradeHistory.")

    def _load_channel_mgmt_log(self):
        path = getattr(self.config, "channel_mgmt_log_path", "data/channel_mgmt_log.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.channel_mgmt_log = data[:60]
            except Exception as e:
                logger.debug(f"Could not load channel management log: {e}")

    def _save_channel_mgmt_log(self):
        path = getattr(self.config, "channel_mgmt_log_path", "data/channel_mgmt_log.json")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.channel_mgmt_log[:60], f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save channel management log: {e}")

    def _load_channel_automation(self):
        """Loads persisted user channel automation toggles from disk."""
        path = getattr(self.config, "channel_automation_path", "data/channel_automation.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if "auto_cashu" in data:
                        val = bool(data["auto_cashu"])
                        self.config.auto_cashu = val
                        self.config.cashu_auto_sweep = val
                        self.config.cashu_auto_refuel = val
                    if "auto_btc_channels" in data:
                        self.config.auto_btc_channels = bool(data["auto_btc_channels"])
                    if "auto_lithium_channels" in data:
                        self.config.auto_lithium_channels = bool(data["auto_lithium_channels"])
            except Exception as e:
                logger.debug(f"Could not load channel automation config: {e}")

    def _save_channel_automation(self):
        """Persists user channel automation toggles to disk."""
        path = getattr(self.config, "channel_automation_path", "data/channel_automation.json")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            payload = {
                "auto_cashu": bool(getattr(self.config, "auto_cashu", True)),
                "auto_btc_channels": bool(getattr(self.config, "auto_btc_channels", False)),
                "auto_lithium_channels": bool(getattr(self.config, "auto_lithium_channels", False)),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save channel automation config: {e}")

    def _load_enabled_pairs(self) -> List[str]:
        """Loads persisted user-enabled trading pairs from disk, or falls back to configured defaults."""
        if not getattr(self.config, "all_pairs", True) and getattr(self.config, "active_pair", None):
            return [self.config.active_pair]
        path = getattr(self.config, "enabled_pairs_path", "data/enabled_pairs.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    valid = [p for p in self.all_known_pairs if p in data]
                    return valid
            except Exception as e:
                logger.debug(f"Could not load enabled pairs: {e}")
        return list(self.all_known_pairs)

    def _save_enabled_pairs(self):
        """Persists user-enabled trading pairs to disk."""
        path = getattr(self.config, "enabled_pairs_path", "data/enabled_pairs.json")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(list(self.target_pairs), f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save enabled pairs: {e}")

    def cancel_all_orders_for_pair(self, pair: str):
        """Immediately cancels all resting DEX orders for a specific pair and zeroes its active rung telemetry."""
        cfg = self._active_markets().get(pair)
        if not self.config.dry_run and cfg:
            try:
                res = self.client.get_own_orders(cfg.base.to_orderbook_currency(), cfg.quote.to_orderbook_currency())
                raw_orders = res.get("orders", res) if isinstance(res, dict) else res
                open_orders = []
                oids: List[str] = []
                if isinstance(raw_orders, dict):
                    for k, o_val in raw_orders.items():
                        if k:
                            oids.append(str(k))
                        if isinstance(o_val, dict):
                            o_copy = dict(o_val)
                            o_copy.setdefault("id", k)
                            o_copy.setdefault("pair", pair)
                            open_orders.append(o_copy)
                elif isinstance(raw_orders, list):
                    open_orders = raw_orders
                    oids = [str(o.get("id") or o.get("orderId")) for o in raw_orders if isinstance(o, dict) and (o.get("id") or o.get("orderId"))]
                try:
                    self._detect_live_order_fills(pair, open_orders, cfg)
                except Exception:
                    pass
                if oids:
                    logger.info(f"[{pair}] Pair toggled OFF — canceling {len(oids)} open orders on DEX...")
                    for oid in oids:
                        try:
                            self.client.cancel_order(oid)
                        except Exception as ce:
                            logger.debug(f"[{pair}] Failed to cancel order {oid}: {ce}")
            except Exception as e:
                logger.warning(f"[{pair}] Error querying orders during cancel_all_orders_for_pair: {e}")

        self.resting_orders[pair] = []
        audit = self.market_audits.get(pair)
        if audit:
            audit.active_bids_count = 0
            audit.active_asks_count = 0
            audit.active_bids_value_usd = 0.0
            audit.active_asks_value_usd = 0.0
            audit.last_placed_mid_price = 0.0

    def set_pair_enabled(self, pair: str, enabled: bool) -> Dict[str, Any]:
        """Toggles a trading pair ON or OFF, cancels orders if disabled, and reallocates capacity across enabled pairs."""
        if pair not in self.all_known_pairs:
            return {"status": "ERROR", "error": f"Unknown trading pair '{pair}'"}

        # Update target_pairs immediately without blocking on slow DEX RPC operations
        current_set = set(self.target_pairs)
        if enabled:
            current_set.add(pair)
        else:
            current_set.discard(pair)
        self.target_pairs = [p for p in self.all_known_pairs if p in current_set]
        self._save_enabled_pairs()
        self._mp_raw_cap_ts = 0.0

        if not enabled:
            self.resting_orders[pair] = []
            audit = self.market_audits.get(pair)
            if audit:
                audit.active_bids_count = 0
                audit.active_asks_count = 0
                audit.active_bids_value_usd = 0.0
                audit.active_asks_value_usd = 0.0
                audit.last_placed_mid_price = 0.0
        else:
            audit = self.market_audits.get(pair)
            if audit and audit.mid_price <= 0:
                ref_p = self.oracle.get_price(pair) or 0.0
                if ref_p > 0:
                    audit.mid_price = ref_p
                    audit.reservation_price = ref_p

        def _bg_resync():
            if enabled:
                try:
                    self.generate_orders_for_market(pair)
                except Exception as ge:
                    logger.debug(f"[{pair}] Initial curve generation on toggle failed: {ge}")
            with self._order_sync_lock:
                if not enabled:
                    self.cancel_all_orders_for_pair(pair)
                self._mp_raw_cap_ts = 0.0
                active_mkts = self._active_markets()
                t_cfg = active_mkts.get(pair)
                shared_keys = (
                    {(t_cfg.base.network_name, t_cfg.base.symbol), (t_cfg.quote.network_name, t_cfg.quote.symbol)}
                    if t_cfg
                    else set()
                )
                affected = []
                for p_active in list(self.target_pairs):
                    m_c = active_mkts.get(p_active)
                    if not m_c:
                        continue
                    p_keys = {(m_c.base.network_name, m_c.base.symbol), (m_c.quote.network_name, m_c.quote.symbol)}
                    if p_active == pair or not shared_keys or (p_keys & shared_keys):
                        affected.append(p_active)
                for p_active in affected:
                    self.last_order_refresh_time[p_active] = 0.0
                    try:
                        self.sync_orders_for_market(p_active)
                    except Exception as ex:
                        logger.warning(f"[{p_active}] Resync after pair toggle failed: {ex}")

        threading.Thread(target=_bg_resync, daemon=True).start()
        return {
            "status": "SUCCESS",
            "pair": pair,
            "enabled": bool(enabled),
            "active_pairs": list(self.target_pairs),
        }

    def log_channel_action(
        self,
        mode: str,
        network: str,
        asset: str,
        action_type: str,
        amount: float,
        status: str,
        message: str,
        advice_acted_on: Optional[str] = None,
        unit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Records a MANUAL or AUTO channel management action and persists it to disk."""
        now = time.time()
        entry = {
            "id": f"chlog_{int(now * 1000)}_{random.randint(100, 999)}",
            "timestamp": now,
            "time_str": time.strftime("%H:%M:%S", time.localtime(now)),
            "mode": "AUTO" if str(mode).upper() == "AUTO" else "MANUAL",
            "network": network,
            "asset": asset,
            "action_type": action_type,
            "amount": amount,
            "unit": unit or asset,
            "status": status,
            "message": message,
            "advice_acted_on": advice_acted_on if str(mode).upper() == "AUTO" else None,
        }
        with self._lock:
            self.channel_mgmt_log.insert(0, entry)
            if len(self.channel_mgmt_log) > 60:
                self.channel_mgmt_log = self.channel_mgmt_log[:60]
            self._save_channel_mgmt_log()
        return entry

    def set_channel_automation(
        self,
        auto_cashu: Optional[bool] = None,
        auto_btc_channels: Optional[bool] = None,
        auto_lithium_channels: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Updates and persists the 3 channel management automation toggles."""
        if auto_cashu is not None:
            val = bool(auto_cashu)
            self.config.auto_cashu = val
            self.config.cashu_auto_sweep = val
            self.config.cashu_auto_refuel = val
        if auto_btc_channels is not None:
            self.config.auto_btc_channels = bool(auto_btc_channels)
        if auto_lithium_channels is not None:
            self.config.auto_lithium_channels = bool(auto_lithium_channels)
        self._save_channel_automation()

        return {
            "status": "SUCCESS",
            "automation": {
                "auto_cashu": bool(getattr(self.config, "auto_cashu", True)),
                "auto_btc_channels": bool(getattr(self.config, "auto_btc_channels", False)),
                "auto_lithium_channels": bool(getattr(self.config, "auto_lithium_channels", False)),
            },
        }

    def _init_simulated_state(self):
        """
        Initializes realistic stateful off-chain channel capacities for Dry-Run mode:
        - Bitcoin Lightning Channel: Single-asset BTC channel paired with Cashu Ecash Vault
        - Arbitrum Lithium Channel: Multi-asset EVM state channel (USDC + HDN)
        - Ethereum Lithium Channel: EVM state channel (ETH)
        """
        btc_p = self.oracle.get_price("BTC/USDC") or 82000.0
        eth_p = self.oracle.get_price("ETH/USDC") or 2450.0
        hdn_p = self.oracle.get_price("HDN/USDC") or 0.026

        btc_half = round(2500.0 / btc_p, 6)    # ~$2,500 send / ~$2,500 receive ($5,000 total LN channel)
        eth_half = round(2500.0 / eth_p, 5)    # ~$2,500 send / ~$2,500 receive
        hdn_half = round(1800.0 / hdn_p, 2)    # ~$1,800 send / ~$1,800 receive
        usdc_half = 4500.0                     # $4,500 send / $4,500 receive

        self.sim_assets = {
            ("Bitcoin", "BTC"): {
                "free_local": btc_half,
                "free_remote": btc_half,
                "total_capacity": round(btc_half * 2, 6),
            },
            ("Arbitrum", "USDC"): {
                "free_local": usdc_half,
                "free_remote": usdc_half,
                "total_capacity": round(usdc_half * 2, 2),
            },
            ("Arbitrum", "ETH"): {
                "free_local": eth_half,
                "free_remote": eth_half,
                "total_capacity": round(eth_half * 2, 5),
            },
            ("Arbitrum", "HDN"): {
                "free_local": hdn_half,
                "free_remote": hdn_half,
                "total_capacity": round(hdn_half * 2, 2),
            },
            ("Ethereum", "ETH"): {
                "free_local": eth_half,
                "free_remote": eth_half,
                "total_capacity": round(eth_half * 2, 5),
            },
            ("Ethereum", "USDC"): {
                "free_local": usdc_half,
                "free_remote": usdc_half,
                "total_capacity": round(usdc_half * 2, 2),
            },
        }
        self.sim_onchain = {
            ("Bitcoin", "BTC"): 0.025,
            ("Ethereum", "ETH"): 0.25,
            ("Ethereum", "USDC"): 250.0,
            ("Arbitrum", "USDC"): 500.0,
            ("Arbitrum", "ETH"): 0.15,
            ("Arbitrum", "HDN"): 10000.0,
        }

        if self.config.dry_run and not getattr(self.config, "testnet", False) and self.cashu_bridge:
            try:
                self.cashu_bridge.ensure_simulated_reserve(min_sats=120000)
            except Exception as e:
                logger.debug(f"Could not seed simulated Cashu reserve: {e}")

    def _active_markets(self) -> Dict[str, MarketConfig]:
        """Returns TESTNET_MARKETS when running on Testnet, otherwise MARKETS."""
        return get_active_markets(getattr(self.config, "testnet", False))

    # ==================== GAUSSIAN BELL CURVE DEPTH ALLOCATION ====================

    def compute_bell_curve_weights(self, num_rungs: int, sigma: float = 1.2) -> List[float]:
        """
        Computes normalized Gaussian bell curve weights for layered rungs.
        Rung 0 (closest to mid) receives highest weight, tapering smoothly to Rung N-1.
        """
        raw_weights = []
        for i in range(num_rungs):
            x = (i / max(1, num_rungs - 1)) * 2.0  # Normalized x in [0.0, 2.0]
            weight = math.exp(-0.5 * (x / sigma) ** 2)
            raw_weights.append(weight)
        total = sum(raw_weights)
        return [w / total for w in raw_weights] if total > 0 else [1.0 / num_rungs] * num_rungs

    def compute_dynamic_curve_profile(
        self,
        pair: str,
        decision: SupervisorDecision,
        avail_quote_for_bids: float,
        avail_base_for_asks: float,
        quote_fiat: float,
        base_fiat: float,
        free_base_recv: float,
        free_quote_recv: float,
    ) -> Tuple[float, str, str]:
        """
        Dynamically computes the Gaussian Bell Curve sigma, concentration regime,
        and human-readable balancing rationale from real-time channel balancing needs
        (inventory skew relative to 50/50 equilibrium and channel capacity constraints).
        """
        cfg = self._active_markets().get(pair)
        base_sym = cfg.base.symbol if cfg else pair.split("/")[0]
        quote_sym = cfg.quote.symbol if cfg else pair.split("/")[1]

        delta = decision.base_ratio - decision.target_base_ratio
        inv_imbalance = abs(delta)

        base_tot = (avail_quote_for_bids / max(quote_fiat, 1e-9)) + free_base_recv
        quote_send_est = avail_quote_for_bids / max(self.config.channel_order_allocation_pct, 0.01)
        base_send_est = avail_base_for_asks / max(self.config.channel_order_allocation_pct, 0.01)

        base_chan_tot = base_send_est + free_base_recv
        quote_chan_tot = quote_send_est + free_quote_recv
        base_recv_ratio = (free_base_recv / base_chan_tot) if (base_chan_tot > 0 and free_base_recv > 0) else 0.50
        quote_recv_ratio = (free_quote_recv / quote_chan_tot) if (quote_chan_tot > 0 and free_quote_recv > 0) else 0.50

        inbound_pressure = 0.0
        if quote_chan_tot > 0 and free_quote_recv > 0 and quote_recv_ratio < 0.20:
            inbound_pressure = max(inbound_pressure, (0.25 - quote_recv_ratio))
        if base_chan_tot > 0 and free_base_recv > 0 and base_recv_ratio < 0.20:
            inbound_pressure = max(inbound_pressure, (0.25 - base_recv_ratio))

        effective_imbalance = max(inv_imbalance, inbound_pressure)
        dynamic_sigma = round(min(1.65, max(0.85, 0.85 + effective_imbalance * 2.10)), 2)

        if effective_imbalance < 0.10:
            regime = "CONCENTRATED"
        elif effective_imbalance < 0.25:
            regime = "BALANCED"
        else:
            regime = "WIDE"

        b_pct = decision.base_ratio * 100.0
        q_pct = (1.0 - decision.base_ratio) * 100.0

        if cfg and getattr(cfg, "is_peg_pair", False):
            peg_sigma = round(min(1.10, max(0.55, 0.65 + effective_imbalance * 0.80)), 2)
            regime = "STABLESWAP PEG"
            if quote_chan_tot > 0 and free_quote_recv > 0 and quote_recv_ratio < 0.15:
                q_prec = 5 if quote_sym in ("BTC", "ETH") else 2
                reason = (
                    f"{cfg.quote.network_name} {quote_sym} inbound ({free_quote_recv:.{q_prec}f} {quote_sym}, {quote_recv_ratio*100:.0f}% Recv) limits Asks • "
                    f"Shifting peg toward 1.00000 par to rebalance"
                )
            elif base_chan_tot > 0 and free_base_recv > 0 and base_recv_ratio < 0.15:
                b_prec = 5 if base_sym in ("BTC", "ETH") else 2
                reason = (
                    f"{cfg.base.network_name} {base_sym} inbound ({free_base_recv:.{b_prec}f} {base_sym}, {base_recv_ratio*100:.0f}% Recv) limits Bids • "
                    f"Shifting peg toward 1.00000 par to rebalance"
                )
            elif delta > 0.05:
                reason = (
                    f"Overweight Arbitrum ({b_pct:.0f}% Arb / {q_pct:.0f}% Eth) • "
                    f"Front-loading Ask peg at 1.00000 par & widening Bids"
                )
            elif delta < -0.05:
                reason = (
                    f"Underweight Arbitrum ({b_pct:.0f}% Arb / {q_pct:.0f}% Eth) • "
                    f"Front-loading Bid peg at 1.00000 par & widening Asks"
                )
            else:
                reason = (
                    f"1:1 Cross-Chain Peg ({b_pct:.0f}% Arb / {q_pct:.0f}% Eth) • "
                    f"Tight 4–25 bps Stableswap Bridge (+0.025% Rebate)"
                )
            return peg_sigma, regime, reason

        if quote_chan_tot > 0 and free_quote_recv > 0 and quote_recv_ratio < 0.15:
            q_prec = 5 if quote_sym in ("BTC", "ETH") else 2
            reason = (
                f"{quote_sym} inbound ({free_quote_recv:.{q_prec}f} {quote_sym}, {quote_recv_ratio*100:.0f}% Recv) limits Asks • "
                f"Skewing & widening curve to rebalance"
            )
        elif base_chan_tot > 0 and free_base_recv > 0 and base_recv_ratio < 0.15:
            b_prec = 5 if base_sym in ("BTC", "ETH") else 2
            reason = (
                f"{base_sym} inbound ({free_base_recv:.{b_prec}f} {base_sym}, {base_recv_ratio*100:.0f}% Recv) limits Bids • "
                f"Skewing & widening curve to rebalance"
            )
        elif delta > 0.05:
            reason = (
                f"Overweight {base_sym} ({b_pct:.0f}%/{q_pct:.0f}%) • "
                f"Shading Asks tighter & spreading Bid depth"
            )
        elif delta < -0.05:
            reason = (
                f"Underweight {base_sym} ({b_pct:.0f}%/{q_pct:.0f}%) • "
                f"Shading Bids tighter & spreading Ask depth"
            )
        else:
            reason = (
                f"Balanced 50/50 Equilibrium ({b_pct:.0f}%/{q_pct:.0f}%) • "
                f"Concentrating depth to maximize Maker Rebates"
            )

        return dynamic_sigma, regime, reason

    # ==================== DLMM QUOTING LOGIC ====================

    def _resolve_market_capacities(self, pair: str) -> Tuple[float, float, float, float]:
        """
        Resolves (free_base_send, free_base_recv, free_quote_send, free_quote_recv) for a market.
        Uses live node channel audits first; if dry_run=True and live node returns 0, uses stateful simulation.
        """
        cfg = self._active_markets().get(pair)
        if not cfg:
            return 0.0, 0.0, 0.0, 0.0

        audits = self.channel_mgr.audit_all_channels([pair])
        m_audit = (
            audits.get("market_audits", {}).get(pair)
            if isinstance(audits, dict) and "market_audits" in audits
            else (audits.get(pair) if isinstance(audits, dict) else None)
        )
        free_base_send = (m_audit.base_sendable or m_audit.base_free_sendable) if m_audit else 0.0
        free_base_recv = m_audit.base_receivable if m_audit else 0.0
        free_quote_send = (m_audit.quote_sendable or m_audit.quote_free_sendable) if m_audit else 0.0
        free_quote_recv = m_audit.quote_receivable if m_audit else 0.0

        # If running in dry_run and no live node channels were found, use stateful simulated channels
        if self.config.dry_run and (free_base_send <= 0 and free_quote_send <= 0 and free_base_recv <= 0 and free_quote_recv <= 0):
            has_any_live_channel = False
            if getattr(self.config, "testnet", False):
                for net_check in ("Bitcoin", "Ethereum", "Arbitrum"):
                    try:
                        if self.channel_mgr.get_active_channel(net_check):
                            has_any_live_channel = True
                            break
                    except Exception:
                        pass
            if not has_any_live_channel:
                b_sim = self.sim_assets.get((cfg.base.network_name, cfg.base.symbol), {})
                q_sim = self.sim_assets.get((cfg.quote.network_name, cfg.quote.symbol), {})
                free_base_send = b_sim.get("free_local", 0.0)
                free_base_recv = b_sim.get("free_remote", 0.0)
                free_quote_send = q_sim.get("free_local", 0.0)
                free_quote_recv = q_sim.get("free_remote", 0.0)

        return free_base_send, free_base_recv, free_quote_send, free_quote_recv

    def _compute_multi_pair_allocation(
        self,
        target_pair: str,
        raw_base_send: float,
        raw_base_recv: float,
        raw_quote_send: float,
        raw_quote_recv: float,
        target_price: float,
    ) -> Tuple[float, float, float, float]:
        """
        Allocates shared channel Send and Receive legs across all active pairs using
        water-filling so multiple pairs (e.g., ETH/USDC + ETH/BTC sharing ETH, or
        BTC/USDC + ETH/USDC sharing USDC) never over-commit the same channel capacity.
        """
        if self.config.dry_run:
            return raw_base_send, raw_base_recv, raw_quote_send, raw_quote_recv

        all_markets = self._active_markets()
        active_markets = {p: m_cfg for p, m_cfg in all_markets.items() if p in self.target_pairs}
        if len(active_markets) <= 1 or target_pair not in active_markets:
            return raw_base_send, raw_base_recv, raw_quote_send, raw_quote_recv

        now = time.time()
        cache = getattr(self, "_mp_raw_cap_cache", None)
        cache_ts = getattr(self, "_mp_raw_cap_ts", 0.0)
        if not cache or (now - cache_ts) > 2.5:
            try:
                audits = self.channel_mgr.audit_all_channels(list(active_markets.keys()))
                m_map = audits.get("market_audits", audits) if isinstance(audits, dict) else {}
                cache = {}
                for p_name, m_cfg in active_markets.items():
                    ma = m_map.get(p_name)
                    if ma:
                        cache[p_name] = (
                            float(ma.base_sendable or ma.base_free_sendable or 0.0),
                            float(ma.base_receivable or 0.0),
                            float(ma.quote_sendable or ma.quote_free_sendable or 0.0),
                            float(ma.quote_receivable or 0.0),
                        )
                self._mp_raw_cap_cache = cache
                self._mp_raw_cap_ts = now
            except Exception:
                return raw_base_send, raw_base_recv, raw_quote_send, raw_quote_recv

        cache[target_pair] = (raw_base_send, raw_base_recv, raw_quote_send, raw_quote_recv)

        pair_prices: Dict[str, float] = {}
        for p_name in active_markets.keys():
            if p_name == target_pair and target_price > 0:
                pair_prices[p_name] = target_price
            else:
                p_val = self.oracle.get_price(p_name)
                if p_val <= 0 and p_name in self.market_audits:
                    p_val = self.market_audits[p_name].mid_price
                pair_prices[p_name] = p_val if p_val > 0 else 1.0

        # Identify active Bid and Ask sides across all markets and which asset legs they consume
        # Leg key: (network_name, symbol, "SEND" | "RECV")
        leg_totals: Dict[Tuple[str, str, str], float] = {}
        leg_consumers: Dict[Tuple[str, str, str], List[Tuple[str, str]]] = {}

        for p_name, m_cfg in active_markets.items():
            bs, br, qs, qr = cache.get(p_name, (0.0, 0.0, 0.0, 0.0))
            b_send_leg = (m_cfg.base.network_name, m_cfg.base.symbol, "SEND")
            b_recv_leg = (m_cfg.base.network_name, m_cfg.base.symbol, "RECV")
            q_send_leg = (m_cfg.quote.network_name, m_cfg.quote.symbol, "SEND")
            q_recv_leg = (m_cfg.quote.network_name, m_cfg.quote.symbol, "RECV")

            leg_totals[b_send_leg] = max(leg_totals.get(b_send_leg, 0.0), bs)
            leg_totals[b_recv_leg] = max(leg_totals.get(b_recv_leg, 0.0), br)
            leg_totals[q_send_leg] = max(leg_totals.get(q_send_leg, 0.0), qs)
            leg_totals[q_recv_leg] = max(leg_totals.get(q_recv_leg, 0.0), qr)

            if qs > 0 and br > 0:
                leg_consumers.setdefault(q_send_leg, []).append((p_name, "BIDS"))
                leg_consumers.setdefault(b_recv_leg, []).append((p_name, "BIDS"))
            if bs > 0 and qr > 0:
                leg_consumers.setdefault(b_send_leg, []).append((p_name, "ASKS"))
                leg_consumers.setdefault(q_recv_leg, []).append((p_name, "ASKS"))

        def _waterfill_leg(
            leg_key: Tuple[str, str, str],
            target_consumer: Tuple[str, str],
            raw_fallback: float,
        ) -> float:
            consumers = leg_consumers.get(leg_key, [])
            if len(consumers) <= 1 or target_consumer not in consumers:
                return raw_fallback
            total_cap = leg_totals.get(leg_key, raw_fallback)
            # Compute each consumer's natural cap imposed by its counter-leg
            capped_items: List[Tuple[float, Tuple[str, str]]] = []
            for c_pair, c_side in consumers:
                c_cfg = active_markets[c_pair]
                c_bs, c_br, c_qs, c_qr = cache.get(c_pair, (0.0, 0.0, 0.0, 0.0))
                c_price = pair_prices.get(c_pair, 1.0)
                if c_side == "BIDS":
                    # Bid uses Quote SEND and Base RECV
                    if leg_key == (c_cfg.quote.network_name, c_cfg.quote.symbol, "SEND"):
                        other_leg = (c_cfg.base.network_name, c_cfg.base.symbol, "RECV")
                        other_n = max(1, len(leg_consumers.get(other_leg, [])))
                        cap_val = (c_br / other_n) * c_price
                    else:
                        other_leg = (c_cfg.quote.network_name, c_cfg.quote.symbol, "SEND")
                        other_n = max(1, len(leg_consumers.get(other_leg, [])))
                        cap_val = (c_qs / other_n) / c_price if c_price > 0 else total_cap
                else:
                    # Ask uses Base SEND and Quote RECV
                    if leg_key == (c_cfg.base.network_name, c_cfg.base.symbol, "SEND"):
                        other_leg = (c_cfg.quote.network_name, c_cfg.quote.symbol, "RECV")
                        other_n = max(1, len(leg_consumers.get(other_leg, [])))
                        cap_val = (c_qr / other_n) / c_price if c_price > 0 else total_cap
                    else:
                        other_leg = (c_cfg.base.network_name, c_cfg.base.symbol, "SEND")
                        other_n = max(1, len(leg_consumers.get(other_leg, [])))
                        cap_val = (c_bs / other_n) * c_price
                capped_items.append((max(0.0, cap_val), (c_pair, c_side)))

            capped_items.sort(key=lambda x: x[0])
            rem_cap = total_cap
            rem_cnt = len(capped_items)
            allocs: Dict[Tuple[str, str], float] = {}
            for c_cap, c_id in capped_items:
                equal_share = rem_cap / max(1, rem_cnt)
                given = min(c_cap, equal_share) if rem_cnt > 1 else rem_cap
                allocs[c_id] = given
                rem_cap = max(0.0, rem_cap - given)
                rem_cnt -= 1
            return allocs.get(target_consumer, raw_fallback / len(consumers))

        t_cfg = active_markets[target_pair]
        eff_bs = _waterfill_leg((t_cfg.base.network_name, t_cfg.base.symbol, "SEND"), (target_pair, "ASKS"), raw_base_send)
        eff_br = _waterfill_leg((t_cfg.base.network_name, t_cfg.base.symbol, "RECV"), (target_pair, "BIDS"), raw_base_recv)
        eff_qs = _waterfill_leg((t_cfg.quote.network_name, t_cfg.quote.symbol, "SEND"), (target_pair, "BIDS"), raw_quote_send)
        eff_qr = _waterfill_leg((t_cfg.quote.network_name, t_cfg.quote.symbol, "RECV"), (target_pair, "ASKS"), raw_quote_recv)
        return eff_bs, eff_br, eff_qs, eff_qr

    def generate_orders_for_market(self, pair: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Generates 15 bid rungs and 15 ask rungs centered around the reservation price.
        Enforces lot sizes, minimum placement floors ($0.75 USD), and channel allocation limits.
        """
        cfg = self._active_markets().get(pair)
        if not cfg:
            return [], []

        is_peg_pair = bool(getattr(cfg, "is_peg_pair", False))

        # 1. Fetch reference spot price (1.00000 fixed par for 1:1 cross-chain peg pairs)
        if is_peg_pair:
            ref_price = 1.0
        else:
            ref_price = self.oracle.get_price(pair)
            if ref_price <= 0:
                ref_price = self.supervisor._fetch_oracle_price(pair)
            if ref_price <= 0:
                logger.warning(f"[{pair}] Cannot generate quotes: reference spot price is 0")
                return [], []

        # 2. Audit channel capacities (or stateful simulation in dry-run)
        free_base_send, free_base_recv, free_quote_send, free_quote_recv = self._resolve_market_capacities(pair)

        audit = self.market_audits.setdefault(pair, MarketAudit(pair))
        audit.base_sendable = free_base_send
        audit.base_receivable = free_base_recv
        audit.quote_sendable = free_quote_send
        audit.quote_receivable = free_quote_recv
        audit.mid_price = ref_price

        base_fiat = self.oracle.get_price(f"{cfg.base.symbol}/USDC") if cfg.base.symbol != "USDC" else 1.0
        quote_fiat = self.oracle.get_price(f"{cfg.quote.symbol}/USDC") if cfg.quote.symbol != "USDC" else 1.0

        # Include Cashu Ecash Vault BTC in BTC inventory valuation so sweeping BTC to Cashu
        # never tricks the skew calculator into thinking we are underweight BTC
        cashu_btc = 0.0
        if getattr(self, "cashu_bridge", None) and getattr(self.cashu_bridge, "initialized", False):
            try:
                cashu_btc = float(self.cashu_bridge.get_wallet_balance_sats() or 0) / 1e8
            except Exception:
                cashu_btc = 0.0

        eff_base_for_skew = free_base_send + (cashu_btc if cfg.base.symbol == "BTC" else 0.0)
        eff_quote_for_skew = free_quote_send + (cashu_btc if cfg.quote.symbol == "BTC" else 0.0)
        audit.base_usd = round(eff_base_for_skew * base_fiat, 2)
        audit.quote_usd = round(eff_quote_for_skew * quote_fiat, 2)
        audit.total_usd = round(audit.base_usd + audit.quote_usd, 2)

        # 3. Request supervisor decision (Pure 50/50 equilibrium + Avellaneda-Stoikov skew, including Cashu BTC)
        decision = self.supervisor.evaluate_pair(
            pair=pair,
            free_base=free_base_send,
            free_quote=free_quote_send,
            oracle_price=ref_price,
            free_base_recv=free_base_recv,
            free_quote_recv=free_quote_recv,
            cashu_btc_available=cashu_btc,
        )

        audit.base_ratio = decision.base_ratio
        audit.target_base_ratio = decision.target_base_ratio
        audit.inventory_skew_pct = decision.skew_factor
        audit.center_price_bias_pct = decision.center_price_bias_pct
        audit.bid_spread_multiplier = decision.bid_spread_multiplier
        audit.ask_spread_multiplier = decision.ask_spread_multiplier
        audit.bid_size_multiplier = decision.bid_size_multiplier
        audit.ask_size_multiplier = decision.ask_size_multiplier
        audit.curve_concentration = decision.curve_concentration
        audit.risk_level = decision.risk_level
        audit.maker_rebate_rate = abs(cfg.maker_fee_rebate)
        audit.half_spread_pct = self.config.base_half_spread_pct * decision.spread_multiplier

        # Compute reservation price (Avellaneda-Stoikov shaded mid)
        reservation_price = ref_price * (1.0 + decision.center_price_bias_pct)
        audit.reservation_price = reservation_price

        if not decision.allow_trading:
            logger.info(f"[{pair}] Quoting paused by supervisor: {decision.commentary}")
            return [], []

        # 4. Allocate capital across rungs (constrained by both local send AND remote inbound receive capacity,
        #    and shared across active pairs via water-filling so pairs never over-commit the same channel)
        alloc_pct = self.config.channel_order_allocation_pct  # default 95%
        eff_base_send, eff_base_recv, eff_quote_send, eff_quote_recv = self._compute_multi_pair_allocation(
            pair,
            free_base_send,
            free_base_recv,
            free_quote_send,
            free_quote_recv,
            ref_price,
        )
        if not self.config.dry_run:
            avail_quote_for_bids = min(eff_quote_send, eff_base_recv * ref_price) * alloc_pct
            avail_base_for_asks = min(eff_base_send, (eff_quote_recv / ref_price) if ref_price > 0 else 0.0) * alloc_pct
        else:
            avail_quote_for_bids = min(eff_quote_send, eff_base_recv * ref_price if eff_base_recv > 0 else eff_quote_send) * alloc_pct
            avail_base_for_asks = min(eff_base_send, (eff_quote_recv / ref_price) if (eff_quote_recv > 0 and ref_price > 0) else eff_base_send) * alloc_pct

        num_rungs = self.config.rungs_per_side
        dynamic_sigma, dynamic_regime, profile_reason = self.compute_dynamic_curve_profile(
            pair=pair,
            decision=decision,
            avail_quote_for_bids=avail_quote_for_bids,
            avail_base_for_asks=avail_base_for_asks,
            quote_fiat=quote_fiat,
            base_fiat=base_fiat,
            free_base_recv=eff_base_recv,
            free_quote_recv=eff_quote_recv,
        )
        audit.dynamic_sigma = dynamic_sigma
        audit.curve_concentration = dynamic_regime
        audit.curve_profile_reason = profile_reason

        weights = self.compute_bell_curve_weights(num_rungs, dynamic_sigma)

        min_floor_usd = max(self.config.hub_min_placement_floor_usd, cfg.quote.min_amount * quote_fiat)
        if is_peg_pair:
            half_spread = float(getattr(self.config, "peg_base_half_spread_pct", 0.0004))
            outer_spread = float(getattr(self.config, "peg_max_half_spread_pct", 0.0025))
            spread_step = (outer_spread - half_spread) / max(1, num_rungs - 1)
            delta_peg = decision.base_ratio - decision.target_base_ratio
            if delta_peg > 0.05:
                ask_weights = self.compute_bell_curve_weights(num_rungs, 0.55)
                bid_weights = self.compute_bell_curve_weights(num_rungs, 1.55)
            elif delta_peg < -0.05:
                bid_weights = self.compute_bell_curve_weights(num_rungs, 0.55)
                ask_weights = self.compute_bell_curve_weights(num_rungs, 1.55)
            else:
                bid_weights = weights
                ask_weights = weights
        else:
            half_spread = self.config.base_half_spread_pct
            spread_step = half_spread / max(1, num_rungs)
            bid_weights = weights
            ask_weights = weights
        audit.half_spread_pct = half_spread * decision.spread_multiplier
        rebate_rate = abs(cfg.maker_fee_rebate)

        bids = []
        asks = []
        rem_quote_budget = avail_quote_for_bids
        rem_base_budget = avail_base_for_asks

        # Generate Bids (buying base asset with quote asset)
        for i, w in enumerate(bid_weights):
            rung_spread = half_spread * decision.bid_spread_multiplier + (i * spread_step)
            price = round(reservation_price * (1.0 - rung_spread), cfg.get_price_precision())
            if is_peg_pair:
                price = min(1.0, price)
            if price <= 0 or rem_quote_budget <= 0:
                continue

            min_base_req_bid = max(
                cfg.min_order_size,
                (min_floor_usd / (price * quote_fiat)) if (price * quote_fiat) > 0 else cfg.min_order_size,
            )
            min_quote_req = max(
                min_floor_usd / quote_fiat if quote_fiat > 0 else cfg.quote.min_amount,
                min_base_req_bid * price,
            )
            max_possible_bids = (
                min(num_rungs, int((avail_quote_for_bids * 1.0001) // min_quote_req))
                if min_quote_req > 0
                else num_rungs
            )
            rem_bids_needed = max(0, max_possible_bids - len(bids) - 1)
            max_quote_this_rung = max(min_quote_req, rem_quote_budget - (rem_bids_needed * min_quote_req * 1.005))

            eff_bid_mult = min(1.0, decision.bid_size_multiplier)
            order_val_quote = avail_quote_for_bids * w * eff_bid_mult
            if order_val_quote < min_quote_req and rem_quote_budget >= min_quote_req:
                order_val_quote = min_quote_req
            order_val_quote = min(order_val_quote, max_quote_this_rung, rem_quote_budget)

            order_val_usd = order_val_quote * quote_fiat
            if order_val_usd < min_floor_usd:
                continue

            size = round(order_val_quote / price, cfg.base_precision)
            if size < cfg.min_order_size:
                continue

            spent_quote = size * price
            if spent_quote > rem_quote_budget * 1.001:
                continue
            rem_quote_budget = max(0.0, rem_quote_budget - spent_quote)

            val_usd = round(size * price * quote_fiat, 2)
            bids.append({
                "pair": pair,
                "side": "BUY",
                "price": price,
                "amount": size,
                "rung": i,
                "weight": round(w, 4),
                "spread_pct": round(rung_spread * 100, 3),
                "value_usd": val_usd,
                "maker_rebate_rate": rebate_rate,
                "est_rebate_usd": round(val_usd * rebate_rate, 4),
                "est_spread_usd": round(val_usd * rung_spread, 4),
            })

        # Generate Asks (selling base asset for quote asset)
        for i, w in enumerate(ask_weights):
            rung_spread = half_spread * decision.ask_spread_multiplier + (i * spread_step)
            price = round(reservation_price * (1.0 + rung_spread), cfg.get_price_precision())
            if is_peg_pair:
                price = max(1.0, price)
            if price <= 0 or rem_base_budget <= 0:
                continue

            min_base_req = max(
                cfg.min_order_size,
                (min_floor_usd / (price * quote_fiat)) if (price * quote_fiat) > 0 else cfg.min_order_size,
            )
            max_possible_asks = (
                min(num_rungs, int((avail_base_for_asks * 1.0001) // min_base_req))
                if min_base_req > 0
                else num_rungs
            )
            rem_asks_needed = max(0, max_possible_asks - len(asks) - 1)
            max_base_this_rung = max(min_base_req, rem_base_budget - (rem_asks_needed * min_base_req * 1.005))

            eff_ask_mult = min(1.0, decision.ask_size_multiplier)
            order_amount = avail_base_for_asks * w * eff_ask_mult
            if order_amount < min_base_req and rem_base_budget >= min_base_req:
                order_amount = min_base_req
            order_amount = min(order_amount, max_base_this_rung, rem_base_budget)

            order_val_usd = order_amount * price * quote_fiat
            if order_val_usd < min_floor_usd:
                continue

            size = round(order_amount, cfg.base_precision)
            if size < cfg.min_order_size or size > rem_base_budget * 1.001:
                continue
            rem_base_budget = max(0.0, rem_base_budget - size)

            val_usd = round(size * price * quote_fiat, 2)
            asks.append({
                "pair": pair,
                "side": "SELL",
                "price": price,
                "amount": size,
                "rung": i,
                "weight": round(w, 4),
                "spread_pct": round(rung_spread * 100, 3),
                "value_usd": val_usd,
                "maker_rebate_rate": rebate_rate,
                "est_rebate_usd": round(val_usd * rebate_rate, 4),
                "est_spread_usd": round(val_usd * rung_spread, 4),
            })

        # Compute 15-rung Ideal Gaussian Curves (always 15 rungs per side) for visual comparison
        eff_bid_mult = min(1.0, decision.bid_size_multiplier)
        eff_ask_mult = min(1.0, decision.ask_size_multiplier)
        actual_bid_budget_usd = avail_quote_for_bids * quote_fiat * eff_bid_mult
        actual_ask_budget_usd = avail_base_for_asks * reservation_price * quote_fiat * eff_ask_mult

        min_rung_usd = max(
            min_floor_usd,
            cfg.min_order_size * reservation_price * quote_fiat if reservation_price > 0 else min_floor_usd,
        )
        tail_weight = weights[-1] if weights else (1.0 / max(1, num_rungs))
        unclamped_min_budget_usd = min_rung_usd / max(tail_weight, 0.01)

        ref_healthy_budget_usd = max(actual_bid_budget_usd, actual_ask_budget_usd, unclamped_min_budget_usd)
        ideal_bid_budget_usd = (
            actual_bid_budget_usd
            if (len(bids) >= num_rungs and actual_bid_budget_usd >= unclamped_min_budget_usd * 0.98)
            else max(actual_bid_budget_usd, ref_healthy_budget_usd * eff_bid_mult)
        )
        ideal_ask_budget_usd = (
            actual_ask_budget_usd
            if (len(asks) >= num_rungs and actual_ask_budget_usd >= unclamped_min_budget_usd * 0.98)
            else max(actual_ask_budget_usd, ref_healthy_budget_usd * eff_ask_mult)
        )

        ideal_bids = []
        ideal_asks = []
        for i in range(num_rungs):
            w_b = bid_weights[i] if i < len(bid_weights) else (1.0 / max(1, num_rungs))
            b_spread = half_spread * decision.bid_spread_multiplier + (i * spread_step)
            b_price = round(reservation_price * (1.0 - b_spread), cfg.get_price_precision())
            if is_peg_pair:
                b_price = min(1.0, b_price)
            b_val_usd = round(ideal_bid_budget_usd * w_b, 2)
            b_size = round((b_val_usd / (b_price * quote_fiat)) if (b_price * quote_fiat) > 0 else 0.0, cfg.base_precision)
            ideal_bids.append({
                "pair": pair,
                "side": "BUY",
                "rung": i,
                "price": b_price,
                "amount": b_size,
                "weight": round(w_b, 4),
                "spread_pct": round(b_spread * 100, 3),
                "value_usd": b_val_usd,
                "maker_rebate_rate": rebate_rate,
                "est_rebate_usd": round(b_val_usd * rebate_rate, 4),
                "est_spread_usd": round(b_val_usd * b_spread, 4),
            })

            w_a = ask_weights[i] if i < len(ask_weights) else (1.0 / max(1, num_rungs))
            a_spread = half_spread * decision.ask_spread_multiplier + (i * spread_step)
            a_price = round(reservation_price * (1.0 + a_spread), cfg.get_price_precision())
            if is_peg_pair:
                a_price = max(1.0, a_price)
            a_val_usd = round(ideal_ask_budget_usd * w_a, 2)
            a_size = round((a_val_usd / (a_price * quote_fiat)) if (a_price * quote_fiat) > 0 else 0.0, cfg.base_precision)
            ideal_asks.append({
                "pair": pair,
                "side": "SELL",
                "rung": i,
                "price": a_price,
                "amount": a_size,
                "weight": round(w_a, 4),
                "spread_pct": round(a_spread * 100, 3),
                "value_usd": a_val_usd,
                "maker_rebate_rate": rebate_rate,
                "est_rebate_usd": round(a_val_usd * rebate_rate, 4),
                "est_spread_usd": round(a_val_usd * a_spread, 4),
            })

        audit.ideal_bids = ideal_bids
        audit.ideal_asks = ideal_asks

        if self.config.dry_run or not self.resting_orders.get(pair):
            audit.active_bids_count = len(bids)
            audit.active_asks_count = len(asks)
            audit.active_bids_value_usd = round(sum(b["value_usd"] for b in bids), 2)
            audit.active_asks_value_usd = round(sum(a["value_usd"] for a in asks), 2)
        audit.last_update_time = time.time()

        return bids, asks

    # ==================== ORDER LIFECYCLE MANAGEMENT ====================

    def sync_orders_for_market(self, pair: str):
        """
        Inspects DEX orderbook, cancels drifted rungs, and places fresh rungs.
        """
        if self.is_paused:
            return

        if pair not in self.target_pairs:
            if self.resting_orders.get(pair):
                self.cancel_all_orders_for_pair(pair)
            return

        new_bids, new_asks = self.generate_orders_for_market(pair)
        audit = self.market_audits.get(pair)
        ref_price = audit.mid_price if audit else 0.0

        if ref_price <= 0:
            return

        # Check existing orders on node
        live_query_ok = False
        try:
            if not self.config.dry_run:
                cfg = self._active_markets().get(pair)
                if cfg:
                    res = self.client.get_own_orders(cfg.base.to_orderbook_currency(), cfg.quote.to_orderbook_currency())
                    raw_orders = res.get("orders", res) if isinstance(res, dict) else res
                    open_orders = []
                    if isinstance(raw_orders, dict):
                        for oid, o_val in raw_orders.items():
                            if isinstance(o_val, dict):
                                o_copy = dict(o_val)
                                o_copy.setdefault("id", oid)
                                o_copy.setdefault("pair", pair)
                                open_orders.append(o_copy)
                    elif isinstance(raw_orders, list):
                        open_orders = raw_orders
                    live_query_ok = True
                    self._detect_live_order_fills(pair, open_orders, cfg)
                    # Keep resting_orders strictly synced to confirmed orders on the DEX orderbook
                    open_by_id = {
                        str(o.get("id") or o.get("orderId") or ""): o
                        for o in open_orders
                        if (o.get("id") or o.get("orderId"))
                    }
                    confirmed_resting = []
                    quote_fiat = self.oracle.get_price(f"{cfg.quote.symbol}/USDC") if cfg.quote.symbol != "USDC" else 1.0
                    for ro in self.resting_orders.get(pair, []):
                        roid = str(ro.get("id") or "")
                        if roid and roid in open_by_id:
                            rem_amt = max(0.0, float(ro.get("amount", 0.0)) - float(ro.get("filled_amount", 0.0)))
                            if rem_amt > 1e-9:
                                ro_copy = dict(ro)
                                ro_copy["amount"] = round(rem_amt, cfg.base_precision)
                                ro_copy["value_usd"] = round(rem_amt * float(ro.get("price", 0.0)) * quote_fiat, 2)
                                confirmed_resting.append(ro_copy)
                    self.resting_orders[pair] = confirmed_resting
                    if audit:
                        c_bids = [o for o in confirmed_resting if o.get("side") == "BUY"]
                        c_asks = [o for o in confirmed_resting if o.get("side") == "SELL"]
                        audit.active_bids_count = len(c_bids)
                        audit.active_asks_count = len(c_asks)
                        audit.active_bids_value_usd = round(sum(float(b.get("value_usd", 0.0)) for b in c_bids), 2)
                        audit.active_asks_value_usd = round(sum(float(a.get("value_usd", 0.0)) for a in c_asks), 2)
                else:
                    open_orders = []
            else:
                open_orders = self.resting_orders.get(pair, [])
        except Exception as e:
            logger.warning(f"[{pair}] Error querying open orders: {e}")
            open_orders = self.resting_orders.get(pair, [])

        pair_orders = [o for o in open_orders if o.get("pair", pair) == pair]
        drift_limit = self.config.rebalance_drift_threshold_pct
        target_total = len(new_bids) + len(new_asks)

        needs_refresh = False
        refresh_reason = ""
        if not pair_orders and target_total > 0:
            needs_refresh = True
            refresh_reason = "initial ladder placement"
        elif not self.config.dry_run and len(pair_orders) != target_total and target_total > 0:
            needs_refresh = True
            refresh_reason = f"order fill or book count change ({len(pair_orders)} -> {target_total} orders)"
        elif target_total != self.last_target_order_count.get(pair, -1):
            needs_refresh = True
            refresh_reason = f"target rung count changed ({self.last_target_order_count.get(pair, -1)} -> {target_total})"
        elif audit and audit.last_placed_mid_price > 0:
            price_drift = abs(ref_price - audit.last_placed_mid_price) / audit.last_placed_mid_price
            if price_drift > drift_limit:
                needs_refresh = True
                refresh_reason = f"spot drift {price_drift*100:.2f}% > {drift_limit*100:.2f}% deadband"

        if needs_refresh:
            logger.info(f"[{pair}] Refreshing DLMM ladder ({refresh_reason})...")
            self._cancel_and_replace_ladder(pair, pair_orders, new_bids, new_asks)
            self.last_target_order_count[pair] = target_total
            self.last_order_refresh_time[pair] = time.time()
            if audit:
                audit.last_placed_mid_price = ref_price

    def _cancel_and_replace_ladder(
        self,
        pair: str,
        current_orders: List[Dict[str, Any]],
        new_bids: List[Dict[str, Any]],
        new_asks: List[Dict[str, Any]]
    ):
        """Cancels existing orders for pair and places 15-rung bid/ask orders."""
        mode_str = "[DRY-RUN]" if self.config.dry_run else "[LIVE]"
        
        # 1. Cancel old rungs
        if current_orders and not self.config.dry_run:
            logger.info(f"[{pair}] Canceling {len(current_orders)} existing orders...")
            for o in current_orders:
                oid = o.get("id") or o.get("orderId")
                if oid:
                    try:
                        self.client.cancel_order(oid)
                    except Exception as e:
                        logger.warning(f"[{pair}] Failed to cancel order {oid}: {e}")

        # 2. Place new rungs
        placed = []
        total_orders = new_bids + new_asks
        logger.info(f"[{pair}] {mode_str} Placing 15-rung DLMM ladder ({len(new_bids)} Bids, {len(new_asks)} Asks)...")

        cfg = self._active_markets().get(pair)
        for o in total_orders:
            if self.config.dry_run:
                mock_order = dict(o)
                mock_order["id"] = f"dry_order_{int(time.time() * 1000)}_{mock_order['side']}_{mock_order['rung']}"
                mock_order["timestamp"] = time.time()
                placed.append(mock_order)
            else:
                try:
                    if not cfg:
                        continue
                    base_curr = cfg.base.to_orderbook_currency()
                    quote_curr = cfg.quote.to_orderbook_currency()
                    cid = f"dlmm-{cfg.base.symbol}-{o['side'][0]}-{o['rung']}-{int(time.time()*1000)}"
                    price_str = f"{float(o['price']):.{cfg.get_price_precision()}f}"
                    amt_str = f"{float(o['amount']):.{cfg.base_precision}f}"
                    oid = self.client.create_limit_order(
                        base_currency=base_curr,
                        quote_currency=quote_curr,
                        side=o["side"],
                        price=price_str,
                        amount_base=amt_str,
                        client_order_id=cid,
                    )
                    o_rec = dict(o)
                    o_rec["id"] = oid
                    o_rec["client_order_id"] = cid
                    o_rec["filled_amount"] = 0.0
                    o_rec["placed_at"] = time.time()
                    placed.append(o_rec)
                except Exception as e:
                    logger.warning(f"[{pair}] Failed to place order: {e}")

        self.resting_orders[pair] = placed
        audit = self.market_audits.get(pair)
        if audit and not self.config.dry_run:
            placed_bids = [o for o in placed if o.get("side") == "BUY"]
            placed_asks = [o for o in placed if o.get("side") == "SELL"]
            audit.active_bids_count = len(placed_bids)
            audit.active_asks_count = len(placed_asks)
            audit.active_bids_value_usd = round(sum(float(b.get("value_usd", 0.0)) for b in placed_bids), 2)
            audit.active_asks_value_usd = round(sum(float(a.get("value_usd", 0.0)) for a in placed_asks), 2)

    def _detect_live_order_fills(
        self,
        pair: str,
        open_orders: List[Dict[str, Any]],
        cfg: MarketConfig,
    ):
        """
        Inspects previously placed resting orders against live open orders on the DEX
        to detect and record partial or complete Maker fills.
        """
        prev_orders = self.resting_orders.get(pair, [])
        if not prev_orders:
            return

        open_by_id = {str(o.get("id") or o.get("orderId") or ""): o for o in open_orders if (o.get("id") or o.get("orderId"))}
        quote_usd_price = 1.0 if cfg.quote.symbol == "USDC" else (self.oracle.get_price(f"{cfg.quote.symbol}/USDC") or 82000.0)
        rebate_rate = abs(cfg.maker_fee_rebate)

        for prev_o in prev_orders:
            oid = str(prev_o.get("id") or "")
            if not oid or oid.startswith("dry_order_"):
                continue

            if oid in open_by_id:
                live_o = open_by_id[oid]
                lo = live_o.get("pairOrder", {}).get("limitOrder", {})
                var = lo.get("variant", {})
                filled_now = float(
                    var.get("buy", {}).get("boughtBaseAmount", {}).get("value", 0.0)
                    or var.get("sell", {}).get("soldBaseAmount", {}).get("value", 0.0)
                    or 0.0
                )
                already_rec = float(prev_o.get("filled_amount", 0.0))
                delta = filled_now - already_rec
                if delta > 1e-9:
                    prev_o["filled_amount"] = filled_now
                    price = float(prev_o.get("price", 0.0))
                    val_usd = delta * price * quote_usd_price
                    spread_pct = float(prev_o.get("spread_pct", 0.8))
                    self.record_maker_fill(
                        pair=pair,
                        side=str(prev_o.get("side", "BUY")),
                        rung=int(prev_o.get("rung", 0)),
                        price=price,
                        amount=round(delta, cfg.base_precision),
                        value_usd=val_usd,
                        rebate_rate=rebate_rate,
                        spread_pct=spread_pct,
                        note=f"Live DEX Partial Maker Fill ({oid[:8]})",
                    )
                    if self.pnl_tracker:
                        self.pnl_tracker.record_trade(
                            pair=pair,
                            side=str(prev_o.get("side", "BUY")),
                            price=price,
                            amount=round(delta, cfg.base_precision),
                            maker_rebate_rate=-rebate_rate,
                            order_id=oid,
                            trade_type="MAKER",
                        )
            elif not prev_o.get("_fill_checked"):
                # Order disappeared from open_orders before we canceled it — check if filled
                prev_o["_fill_checked"] = True
                rem_amt = float(prev_o.get("amount", 0.0)) - float(prev_o.get("filled_amount", 0.0))
                if rem_amt > 1e-9:
                    price = float(prev_o.get("price", 0.0))
                    side = str(prev_o.get("side", "BUY")).upper()
                    expected_side_enum = f"ORDER_SIDE_{side}"
                    placed_at = float(prev_o.get("placed_at", 0.0) or 0.0)
                    matched_fill = False
                    matched_price = price
                    matched_amt = rem_amt
                    matched_tid: Optional[str] = None
                    matched_ts: Optional[float] = None
                    try:
                        th = self.client.get_trade_history(
                            cfg.base.to_orderbook_currency(),
                            cfg.quote.to_orderbook_currency(),
                        )
                        for tr in (th or [])[:25]:
                            tid = str(tr.get("tradeId") or "")
                            if tid and tid in self.seen_trade_ids:
                                continue
                            tr_side = str(tr.get("makerOrderSide", ""))
                            tr_price = float(tr.get("price", {}).get("value", 0.0) or 0.0)
                            tr_amt = float(tr.get("baseAmount", {}).get("value", 0.0) or 0.0)
                            tr_ts = self._parse_trade_timestamp(tr.get("timestamp") or tr.get("settledAt"))
                            taker_oid = str(tr.get("takerOrderId") or "")
                            maker_oid = str(tr.get("makerOrderId") or "")

                            # Direct order ID match
                            if oid and (oid == taker_oid or oid == maker_oid):
                                matched_fill = True
                                matched_price = tr_price if tr_price > 0 else price
                                matched_amt = tr_amt if tr_amt > 0 else rem_amt
                                matched_tid = tid or None
                                matched_ts = tr_ts if tr_ts > 0 else None
                                break

                            # Case A: We were resting Maker (our side == makerOrderSide and price matches)
                            if tr_side == expected_side_enum and price > 0 and abs(tr_price - price) / price < 0.005:
                                matched_fill = True
                                matched_price = tr_price if tr_price > 0 else price
                                matched_amt = tr_amt if tr_amt > 0 else rem_amt
                                matched_tid = tid or None
                                matched_ts = tr_ts if tr_ts > 0 else None
                                break

                            # Case B: Our limit order crossed a resting counterparty order upon placement
                            if (
                                tr_side != expected_side_enum
                                and tr_amt > 0
                                and abs(tr_amt - rem_amt) / max(rem_amt, 1e-9) < 0.05
                                and (placed_at <= 0 or tr_ts >= placed_at - 10.0)
                            ):
                                matched_fill = True
                                matched_price = tr_price if tr_price > 0 else price
                                matched_amt = tr_amt
                                matched_tid = tid or None
                                matched_ts = tr_ts if tr_ts > 0 else None
                                break
                    except Exception:
                        pass

                    if matched_fill:
                        prev_o["filled_amount"] = float(prev_o.get("amount", 0.0))
                        val_usd = matched_amt * matched_price * quote_usd_price
                        spread_pct = float(prev_o.get("spread_pct", 0.8))
                        self.record_maker_fill(
                            pair=pair,
                            side=side,
                            rung=int(prev_o.get("rung", 0)),
                            price=matched_price,
                            amount=round(matched_amt, cfg.base_precision),
                            value_usd=val_usd,
                            rebate_rate=rebate_rate,
                            spread_pct=spread_pct,
                            note=f"Live DEX Maker Fill ({oid[:8]})",
                            timestamp=matched_ts,
                            trade_id=matched_tid,
                        )
                        if self.pnl_tracker:
                            self.pnl_tracker.record_trade(
                                pair=pair,
                                side=side,
                                price=matched_price,
                                amount=round(matched_amt, cfg.base_precision),
                                maker_rebate_rate=-rebate_rate,
                                order_id=oid,
                                trade_type="MAKER",
                            )

    # ==================== PASSIVE INCOME & SIMULATED CHANNEL ENGINE ====================

    def record_maker_fill(
        self,
        pair: str,
        side: str,
        rung: int,
        price: float,
        amount: float,
        value_usd: float,
        rebate_rate: float,
        spread_pct: float,
        note: str = "Passive DLMM Maker Fill",
        timestamp: Optional[float] = None,
        trade_id: Optional[str] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Records a passive limit order fill and calculates Maker Rebate + Spread Capture income."""
        rebate_usd = round(value_usd * abs(rebate_rate), 4)
        spread_usd = round(value_usd * (spread_pct / 100.0) * 0.65, 4)  # Realized spread capture
        total_fill_income = round(rebate_usd + spread_usd, 4)

        now = float(timestamp) if timestamp and timestamp > 0 else time.time()
        today_str = time.strftime("%Y-%m-%d", time.localtime(time.time()))
        fill_day_str = time.strftime("%Y-%m-%d", time.localtime(now))
        time_label = (
            time.strftime("%H:%M:%S", time.localtime(now))
            if fill_day_str == today_str
            else time.strftime("%b %d %H:%M:%S", time.localtime(now))
        )

        fill_record = {
            "id": f"fill_{int(now * 1000)}_{random.randint(100, 999)}",
            "trade_id": trade_id,
            "timestamp": now,
            "time_str": time_label,
            "pair": pair,
            "side": side,
            "rung": rung,
            "price": price,
            "amount": amount,
            "value_usd": round(value_usd, 2),
            "rebate_rate_pct": round(abs(rebate_rate) * 100, 3),
            "rebate_usd": rebate_usd,
            "spread_usd": spread_usd,
            "total_income_usd": total_fill_income,
            "note": note,
        }

        with self._lock:
            if trade_id:
                self.seen_trade_ids.add(str(trade_id))
            self.maker_rebates_usd = round(self.maker_rebates_usd + rebate_usd, 4)
            self.spread_capture_usd = round(self.spread_capture_usd + spread_usd, 4)
            self.maker_volume_usd = round(self.maker_volume_usd + value_usd, 2)
            self.maker_fills_count += 1
            self.recent_fills.insert(0, fill_record)
            if len(self.recent_fills) > 100:
                self.recent_fills = self.recent_fills[:100]

            self.passive_income_history.append({
                "timestamp": now,
                "time_str": fill_record["time_str"],
                "maker_rebates_usd": self.maker_rebates_usd,
                "spread_capture_usd": self.spread_capture_usd,
                "total_passive_income_usd": round(self.maker_rebates_usd + self.spread_capture_usd, 4),
                "volume_usd": self.maker_volume_usd,
            })
            if len(self.passive_income_history) > 100:
                self.passive_income_history = self.passive_income_history[-100:]

            if persist:
                self._save_trade_fills()

        return fill_record

    def apply_cashu_channel_delta(self, action_type: str, amount_btc: float):
        """
        Updates the stateful dry-run Bitcoin Lightning channel when a Cashu Sweep (Mint)
        or Cashu Refuel (Melt) occurs, demonstrating how Cashu recycles Lightning channel capacity.
        """
        if not self.config.dry_run or amount_btc <= 0:
            return
        with self._lock:
            btc_cap = self.sim_assets.get(("Bitcoin", "BTC"))
            if not btc_cap:
                return
            total_cap = btc_cap["free_local"] + btc_cap["free_remote"]
            if action_type == "SWEEP":
                # Paying out of our Lightning channel to Mint Ecash reduces Local (Send) and restores Remote (Inbound)
                shift = min(btc_cap["free_local"] * 0.85, amount_btc)
                btc_cap["free_local"] = round(max(0.0005, btc_cap["free_local"] - shift), 6)
                btc_cap["free_remote"] = round(total_cap - btc_cap["free_local"], 6)
            elif action_type == "REFUEL":
                # Melting Ecash to pay an invoice into our Lightning channel increases Local (Send) and consumes Remote (Inbound)
                shift = min(btc_cap["free_remote"] * 0.85, amount_btc)
                btc_cap["free_local"] = round(btc_cap["free_local"] + shift, 6)
                btc_cap["free_remote"] = round(max(0.0005, total_cap - btc_cap["free_local"]), 6)

    def simulate_scenario(self, scenario: str, pair: str = "BTC/USDC") -> Dict[str, Any]:
        """
        Interactive Hypothesis & Maker Curve Tester for community users:
        - 'bid_wave': Simulates taker sell pressure hitting our Bid rungs -> accumulates BTC in channel
                      (depleting Lightning Inbound capacity) -> triggers Cashu Auto-Sweep (Mint) to restore Inbound!
        - 'ask_wave': Simulates taker buy pressure lifting our Ask rungs -> depletes BTC Local Send capacity
                      -> triggers Cashu Auto-Refuel (Melt) to replenish Send capacity!
        - 'ping_pong': Simulates balanced two-sided passive maker fills across inner rungs -> accrues pure
                       Maker Rebates + Spread Capture while staying near 50/50 equilibrium.
        - 'reset': Resets simulated channels to clean 50/50 equilibrium.
        """
        with self._lock:
            cfg = MARKETS.get(pair, MARKETS["BTC/USDC"])
            if pair not in self.resting_orders or not self.resting_orders[pair]:
                self.sync_orders_for_market(pair)

            orders = self.resting_orders.get(pair, [])
            bids = sorted([o for o in orders if o["side"] == "BUY"], key=lambda x: x["rung"])
            asks = sorted([o for o in orders if o["side"] == "SELL"], key=lambda x: x["rung"])

            base_key = (cfg.base.network_name, cfg.base.symbol)
            quote_key = (cfg.quote.network_name, cfg.quote.symbol)
            b_state = self.sim_assets.get(base_key)
            q_state = self.sim_assets.get(quote_key)

            fills_executed = []
            cashu_action_taken = None
            if self.config.dry_run and self.cashu_bridge and not getattr(self.cashu_bridge, "initialized", False):
                self.cashu_bridge.initialized = True

            if scenario == "reset":
                self._init_simulated_state()
                for p in MARKETS.keys():
                    self.sync_orders_for_market(p)
                return {
                    "status": "SUCCESS",
                    "scenario": "reset",
                    "message": "Channels reset to 50/50 equilibrium across Lithium and Lightning.",
                }

            elif scenario == "bid_wave":
                # Takers sell into our top 4 Bid rungs: we BUY base (receive base, send quote)
                for b in bids[:4]:
                    fills_executed.append(self.record_maker_fill(
                        pair=pair,
                        side="BUY",
                        rung=b["rung"],
                        price=b["price"],
                        amount=b["amount"],
                        value_usd=b["value_usd"],
                        rebate_rate=b.get("maker_rebate_rate", abs(cfg.maker_fee_rebate)),
                        spread_pct=b.get("spread_pct", 0.8),
                        note=f"Bid Rung #{b['rung']} Filled (+Rebate)",
                    ))
                if b_state and q_state:
                    # Shift base channel heavily toward Local (depleting Inbound Receivable to ~12%)
                    b_tot = b_state["free_local"] + b_state["free_remote"]
                    b_state["free_local"] = round(b_tot * 0.86, 6)
                    b_state["free_remote"] = round(b_tot * 0.14, 6)
                    q_tot = q_state["free_local"] + q_state["free_remote"]
                    q_state["free_local"] = round(q_tot * 0.32, 2)
                    q_state["free_remote"] = round(q_tot * 0.68, 2)

                # Evaluate Cashu auto-sweep if enabled
                cashu_action_taken = self.evaluate_channel_liquidity()

            elif scenario == "ask_wave":
                # Takers buy from our top 4 Ask rungs: we SELL base (send base, receive quote)
                for a in asks[:4]:
                    fills_executed.append(self.record_maker_fill(
                        pair=pair,
                        side="SELL",
                        rung=a["rung"],
                        price=a["price"],
                        amount=a["amount"],
                        value_usd=a["value_usd"],
                        rebate_rate=a.get("maker_rebate_rate", abs(cfg.maker_fee_rebate)),
                        spread_pct=a.get("spread_pct", 0.8),
                        note=f"Ask Rung #{a['rung']} Filled (+Rebate)",
                    ))
                if b_state and q_state:
                    # Shift base channel heavily toward Remote (depleting Outbound Sendable to ~14%)
                    b_tot = b_state["free_local"] + b_state["free_remote"]
                    b_state["free_local"] = round(b_tot * 0.14, 6)
                    b_state["free_remote"] = round(b_tot * 0.86, 6)
                    q_tot = q_state["free_local"] + q_state["free_remote"]
                    q_state["free_local"] = round(q_tot * 0.68, 2)
                    q_state["free_remote"] = round(q_tot * 0.32, 2)

                # Evaluate Cashu auto-refuel if enabled
                cashu_action_taken = self.evaluate_channel_liquidity()

            else:  # 'ping_pong' balanced passive maker fills
                for b in bids[:3]:
                    fills_executed.append(self.record_maker_fill(
                        pair=pair,
                        side="BUY",
                        rung=b["rung"],
                        price=b["price"],
                        amount=b["amount"],
                        value_usd=b["value_usd"],
                        rebate_rate=b.get("maker_rebate_rate", abs(cfg.maker_fee_rebate)),
                        spread_pct=b.get("spread_pct", 0.8),
                        note=f"Maker Bid #{b['rung']} Filled",
                    ))
                for a in asks[:3]:
                    fills_executed.append(self.record_maker_fill(
                        pair=pair,
                        side="SELL",
                        rung=a["rung"],
                        price=a["price"],
                        amount=a["amount"],
                        value_usd=a["value_usd"],
                        rebate_rate=a.get("maker_rebate_rate", abs(cfg.maker_fee_rebate)),
                        spread_pct=a.get("spread_pct", 0.8),
                        note=f"Maker Ask #{a['rung']} Filled",
                    ))

            # Re-sync ladders for all markets so curves reflect updated inventory skew
            for p in MARKETS.keys():
                self.last_order_refresh_time[p] = 0.0
                self.sync_orders_for_market(p)

            earned_rebates = round(sum(f["rebate_usd"] for f in fills_executed), 4)
            earned_spread = round(sum(f["spread_usd"] for f in fills_executed), 4)

            return {
                "status": "SUCCESS",
                "scenario": scenario,
                "pair": pair,
                "fills_count": len(fills_executed),
                "earned_rebates_usd": earned_rebates,
                "earned_spread_usd": earned_spread,
                "cashu_action": cashu_action_taken,
                "message": (
                    f"Simulated {len(fills_executed)} maker fills on {pair}: "
                    f"+${earned_rebates:.4f} Maker Rebates & +${earned_spread:.4f} Spread Capture"
                    + (f" | Cashu {cashu_action_taken['type']} triggered ({cashu_action_taken['amount_sats']:,} sats)!" if cashu_action_taken else "")
                ),
            }

    def get_channels_snapshot(self) -> List[Dict[str, Any]]:
        """
        Returns active state channels across Arbitrum Lithium, Ethereum Lithium, and Bitcoin Lightning.
        In dry-run mode (when no local node is connected), returns the stateful simulated channels.
        """
        rail_key = "testnet" if getattr(self.config, "testnet", False) else "mainnet"
        rail_meta = NETWORK_RAILS[rail_key]
        is_testnet = getattr(self.config, "testnet", False)

        channels_data = []
        try:
            for net in ("Bitcoin", "Arbitrum", "Ethereum"):
                proto = "PROTOCOL_BITCOIN" if net == "Bitcoin" else "PROTOCOL_EVM"
                nid = self.channel_mgr._net_id(net)
                net_channels = [
                    c for c in self.channel_mgr.get_channels_for_network(proto, nid, net)
                    if not self.channel_mgr.is_channel_closing_or_closed(c)
                ]
                if not net_channels:
                    single_ch = self.channel_mgr.get_active_channel(net)
                    if single_ch:
                        net_channels = [single_ch]

                if net_channels:
                    # Prefer active channel as primary metadata source, while aggregating asset capacities across all open/confirming channels
                    primary_ch = next(
                        (c for c in net_channels if "ACTIVE" in str(c.status).upper()),
                        net_channels[0],
                    )
                    assets_dict: Dict[str, Dict[str, Any]] = {}
                    for ch in net_channels:
                        for sym_or_aid, a in ch.assets.items():
                            fiat_p = self.oracle.get_price(f"{a.symbol}/USDC") if a.symbol != "USDC" else 1.0
                            eff_local = a.free_local if a.free_local > 0 else 0.0
                            eff_remote = a.free_remote if a.free_remote > 0 else 0.0
                            if a.symbol not in assets_dict:
                                assets_dict[a.symbol] = {
                                    "symbol": a.symbol,
                                    "free_local": eff_local,
                                    "free_remote": eff_remote,
                                    "confirmed_local": a.free_local,
                                    "confirmed_remote": a.free_remote,
                                    "pending_local": a.pending_local,
                                    "pending_remote": a.pending_remote,
                                    "local_usd": round(eff_local * fiat_p, 2),
                                    "remote_usd": round(eff_remote * fiat_p, 2),
                                    "max_sendable": a.max_sendable,
                                    "max_receivable": a.max_receivable,
                                    "is_active": a.is_active,
                                    "status": a.status,
                                }
                            else:
                                cur = assets_dict[a.symbol]
                                cur["free_local"] += eff_local
                                cur["free_remote"] += eff_remote
                                cur["confirmed_local"] += a.free_local
                                cur["confirmed_remote"] += a.free_remote
                                cur["pending_local"] += a.pending_local
                                cur["pending_remote"] += a.pending_remote
                                cur["local_usd"] = round(cur["free_local"] * fiat_p, 2)
                                cur["remote_usd"] = round(cur["free_remote"] * fiat_p, 2)
                                cur["max_sendable"] += a.max_sendable
                                cur["max_receivable"] += a.max_receivable
                                cur["is_active"] = cur["is_active"] or a.is_active
                                if a.is_active:
                                    cur["status"] = "ACTIVE"

                    if assets_dict:
                        channels_data.append({
                            "channel_id": primary_ch.channel_id,
                            "channel_ids": [c.channel_id for c in net_channels],
                            "channels_count": len(net_channels),
                            "network_name": primary_ch.network_name,
                            "network_id": rail_meta[net]["id"],
                            "rail_label": rail_meta[net]["label"],
                            "protocol": primary_ch.protocol,
                            "mechanism": (
                                ("Signet Lightning + Cashu Mint/Melt Valve" if is_testnet else "Cashu Ecash Mint/Melt Rail")
                                if net == "Bitcoin"
                                else (f"{rail_meta[net]['label']} Lithium State Channel" if is_testnet else "Multi-Asset Lithium State Channel")
                            ),
                            "status": primary_ch.status,
                            "is_rented": any(c.is_rented for c in net_channels),
                            "assets": assets_dict,
                        })
        except Exception as e:
            logger.debug(f"Live channel query fallback: {e}")

        if not channels_data and self.config.dry_run:
            btc_p = self.oracle.get_price("BTC/USDC") or 82000.0
            eth_p = self.oracle.get_price("ETH/USDC") or 2450.0
            hdn_p = self.oracle.get_price("HDN/USDC") or 0.026

            btc_s = self.sim_assets.get(("Bitcoin", "BTC"), {"free_local": 0.03, "free_remote": 0.03})
            usdc_s = self.sim_assets.get(("Arbitrum", "USDC"), {"free_local": 4500.0, "free_remote": 4500.0})
            hdn_s = self.sim_assets.get(("Arbitrum", "HDN"), {"free_local": 65000.0, "free_remote": 65000.0})
            eth_s = self.sim_assets.get(("Ethereum", "ETH"), {"free_local": 1.0, "free_remote": 1.0})

            is_testnet = getattr(self.config, "testnet", False)
            channels_data = [
                {
                    "channel_id": "ln_signet_hub_0a03cf40_825811aee" if is_testnet else "ln_hydranet_hub_03eaace825811aee",
                    "network_name": "Bitcoin",
                    "network_id": rail_meta["Bitcoin"]["id"],
                    "rail_label": rail_meta["Bitcoin"]["label"],
                    "protocol": "PROTOCOL_BITCOIN",
                    "mechanism": "Signet Lightning + Cashu Mint/Melt Valve" if is_testnet else "Lightning + Cashu Mint/Melt Valve",
                    "status": "CHANNEL_STATUS_ACTIVE",
                    "is_rented": False,
                    "assets": {
                        "BTC": {
                            "symbol": "BTC",
                            "free_local": btc_s["free_local"],
                            "free_remote": btc_s["free_remote"],
                            "local_usd": round(btc_s["free_local"] * btc_p, 2),
                            "remote_usd": round(btc_s["free_remote"] * btc_p, 2),
                            "max_sendable": btc_s["free_local"],
                            "max_receivable": btc_s["free_remote"],
                            "is_active": True,
                            "status": "ACTIVE",
                        }
                    },
                },
                {
                    "channel_id": "0xarb_sepolia_421614_lithium" if is_testnet else "0x895090d931660fbfc5f6bdedc24a87dd",
                    "network_name": "Arbitrum",
                    "network_id": rail_meta["Arbitrum"]["id"],
                    "rail_label": rail_meta["Arbitrum"]["label"],
                    "protocol": "PROTOCOL_EVM",
                    "mechanism": "Arbitrum Sepolia Lithium Channel" if is_testnet else "Consolidated Multi-Token Lithium Channel",
                    "status": "CHANNEL_STATUS_ACTIVE",
                    "is_rented": True,
                    "assets": {
                        "USDC": {
                            "symbol": "USDC",
                            "free_local": usdc_s["free_local"],
                            "free_remote": usdc_s["free_remote"],
                            "local_usd": round(usdc_s["free_local"], 2),
                            "remote_usd": round(usdc_s["free_remote"], 2),
                            "max_sendable": usdc_s["free_local"],
                            "max_receivable": usdc_s["free_remote"],
                            "is_active": True,
                            "status": "ACTIVE",
                        },
                        "HDN": {
                            "symbol": "HDN",
                            "free_local": hdn_s["free_local"],
                            "free_remote": hdn_s["free_remote"],
                            "local_usd": round(hdn_s["free_local"] * hdn_p, 2),
                            "remote_usd": round(hdn_s["free_remote"] * hdn_p, 2),
                            "max_sendable": hdn_s["free_local"],
                            "max_receivable": hdn_s["free_remote"],
                            "is_active": True,
                            "status": "ACTIVE",
                        },
                    },
                },
                {
                    "channel_id": "0xeth_sepolia_11155111_lithium" if is_testnet else "0xcc4c47cf0175aa8108e78679c2586a0a",
                    "network_name": "Ethereum",
                    "network_id": rail_meta["Ethereum"]["id"],
                    "rail_label": rail_meta["Ethereum"]["label"],
                    "protocol": "PROTOCOL_EVM",
                    "mechanism": "Ethereum Sepolia Lithium Channel" if is_testnet else "Lithium EVM State Channel",
                    "status": "CHANNEL_STATUS_ACTIVE",
                    "is_rented": True,
                    "assets": {
                        "ETH": {
                            "symbol": "ETH",
                            "free_local": eth_s["free_local"],
                            "free_remote": eth_s["free_remote"],
                            "local_usd": round(eth_s["free_local"] * eth_p, 2),
                            "remote_usd": round(eth_s["free_remote"] * eth_p, 2),
                            "max_sendable": eth_s["free_local"],
                            "max_receivable": eth_s["free_remote"],
                            "is_active": True,
                            "status": "ACTIVE",
                        }
                    },
                },
            ]

        return channels_data

    def get_wallet_and_channels_overview(self) -> Dict[str, Any]:
        """
        Returns a comprehensive overview of all on-chain wallet assets and off-chain state channels
        across Bitcoin, Ethereum, and Arbitrum, including confirmed/unconfirmed on-chain balances,
        off-chain send/receive/closing/withdrawable capacities, and individual channel breakdowns.
        """
        is_testnet = getattr(self.config, "testnet", False)
        rail_key = "testnet" if is_testnet else "mainnet"
        rail_meta = NETWORK_RAILS[rail_key]

        prices = {
            "BTC": self.oracle.get_price("BTC/USDC") or 82000.0,
            "ETH": self.oracle.get_price("ETH/USDC") or 2450.0,
            "HDN": self.oracle.get_price("HDN/USDC") or 0.026,
            "USDC": 1.0,
        }

        default_net_assets = {
            "Bitcoin": ["BTC"],
            "Ethereum": ["ETH", "USDC"],
            "Arbitrum": ["USDC", "ETH", "HDN"],
        }

        networks_out: Dict[str, Any] = {}
        flat_assets: List[Dict[str, Any]] = []
        total_onchain_usd = 0.0
        total_offchain_send_usd = 0.0
        total_offchain_recv_usd = 0.0
        total_closing_usd = 0.0

        for net_name in ("Bitcoin", "Ethereum", "Arbitrum"):
            meta = rail_meta[net_name]
            proto = meta["protocol"]
            nid = meta["id"]

            raw_bals: Dict[str, Any] = {}
            live_node_ok = False
            try:
                raw_bals = self.client.get_wallet_balances(proto, nid)
                live_node_ok = True
            except Exception:
                raw_bals = {}

            raw_channels = []
            try:
                raw_channels = self.channel_mgr.get_channels_for_network(proto, nid, net_name)
                if raw_channels:
                    live_node_ok = True
            except Exception:
                raw_channels = []

            # Map on-chain balances by symbol
            onchain_by_sym: Dict[str, Dict[str, float]] = {}
            for aid, val_map in raw_bals.items():
                sym = self.channel_mgr._lookup_symbol(nid, aid)
                conf_val = float(val_map.get("onchain", {}).get("confirmed", {}).get("value", 0.0))
                unconf_val = (
                    float(val_map.get("onchain", {}).get("unconfirmed", {}).get("value", 0.0))
                    + float(val_map.get("onchain", {}).get("pending", {}).get("value", 0.0))
                )
                prev = onchain_by_sym.get(sym, {"confirmed": 0.0, "unconfirmed": 0.0})
                onchain_by_sym[sym] = {
                    "confirmed": prev["confirmed"] + conf_val,
                    "unconfirmed": prev["unconfirmed"] + unconf_val,
                }

            # Fallback to simulated on-chain balances only in dry_run when node is offline
            if not live_node_ok and self.config.dry_run:
                for sym in default_net_assets.get(net_name, []):
                    sim_val = self.sim_onchain.get((net_name, sym), 0.0)
                    onchain_by_sym[sym] = {"confirmed": sim_val, "unconfirmed": 0.0}

            # Map off-chain channel capacities and individual channels
            offchain_by_sym: Dict[str, Dict[str, float]] = {}
            individual_channels: List[Dict[str, Any]] = []
            primary_channel_id: Optional[str] = None

            if raw_channels:
                for ch in raw_channels:
                    is_closing = self.channel_mgr.is_channel_closing_or_closed(ch)
                    raw_st = str(ch.status)
                    ui_status = (
                        "CLOSING"
                        if is_closing
                        else ("ACTIVE" if "ACTIVE" in raw_st.upper() else raw_st.replace("CHANNEL_STATUS_", ""))
                    )
                    if not is_closing and primary_channel_id is None and ui_status in ("ACTIVE", "UPDATING"):
                        primary_channel_id = ch.channel_id

                    ch_assets_map: Dict[str, Any] = {}
                    for aid, a in ch.assets.items():
                        sym = a.symbol
                        if sym not in offchain_by_sym:
                            offchain_by_sym[sym] = {
                                "send": 0.0,
                                "recv": 0.0,
                                "pending_local": 0.0,
                                "pending_remote": 0.0,
                                "closing": 0.0,
                                "max_withdrawable": 0.0,
                                "withdrawing_local": 0.0,
                                "unspendable_reserve": 0.0,
                                "is_updating": False,
                            }
                        agg = offchain_by_sym[sym]
                        if not is_closing:
                            agg["send"] += max(0.0, a.free_local)
                            agg["recv"] += max(0.0, a.free_remote)
                            agg["pending_local"] += max(0.0, a.pending_local)
                            agg["pending_remote"] += max(0.0, a.pending_remote)
                            agg["max_withdrawable"] += max(0.0, a.max_withdrawable_local)
                            agg["withdrawing_local"] += max(0.0, getattr(a, "withdrawing_local", 0.0))
                            agg["unspendable_reserve"] += max(0.0, a.unspendable_local_reserve)
                            if getattr(a, "is_updating", False):
                                agg["is_updating"] = True
                        closing_amt = a.unavailable_local if a.unavailable_local > 0 else (a.free_local if is_closing else 0.0)
                        agg["closing"] += max(0.0, closing_amt)

                        ch_assets_map[sym] = {
                            "symbol": sym,
                            "asset_id": aid,
                            "free_local": round(a.free_local, 8),
                            "free_remote": round(a.free_remote, 8),
                            "pending_local": round(a.pending_local, 8),
                            "pending_remote": round(a.pending_remote, 8),
                            "max_withdrawable": round(a.max_withdrawable_local, 8),
                            "withdrawing_local": round(getattr(a, "withdrawing_local", 0.0), 8),
                            "unavailable_local": round(a.unavailable_local, 8),
                            "unspendable_reserve": round(a.unspendable_local_reserve, 8),
                            "is_updating": bool(getattr(a, "is_updating", False)),
                            "status": a.status,
                        }

                    individual_channels.append({
                        "channel_id": ch.channel_id,
                        "short_id": f"{ch.channel_id[:8]}...{ch.channel_id[-6:]}" if len(ch.channel_id) > 16 else ch.channel_id,
                        "network_name": net_name,
                        "status": ui_status,
                        "is_closing": is_closing,
                        "is_closable": ch.is_closable and not is_closing,
                        "is_updatable": ch.is_updatable and not is_closing,
                        "is_rented": ch.is_rented,
                        "assets": ch_assets_map,
                    })
            elif not live_node_ok and self.config.dry_run:
                sim_cid = f"sim_{net_name.lower()}_channel"
                primary_channel_id = sim_cid
                ch_assets_map = {}
                for sym in default_net_assets.get(net_name, []):
                    s_entry = self.sim_assets.get((net_name, sym), {"free_local": 0.0, "free_remote": 0.0})
                    loc = float(s_entry.get("free_local", 0.0))
                    rem = float(s_entry.get("free_remote", 0.0))
                    offchain_by_sym[sym] = {
                        "send": loc,
                        "recv": rem,
                        "pending_local": 0.0,
                        "pending_remote": 0.0,
                        "closing": 0.0,
                        "max_withdrawable": loc,
                        "unspendable_reserve": 0.0,
                    }
                    ch_assets_map[sym] = {
                        "symbol": sym,
                        "asset_id": self.channel_mgr._resolve_asset_id(net_name, sym) or "",
                        "free_local": loc,
                        "free_remote": rem,
                        "pending_local": 0.0,
                        "pending_remote": 0.0,
                        "max_withdrawable": loc,
                        "unavailable_local": 0.0,
                        "unspendable_reserve": 0.0,
                        "status": "ACTIVE",
                    }
                individual_channels.append({
                    "channel_id": sim_cid,
                    "short_id": sim_cid,
                    "network_name": net_name,
                    "status": "ACTIVE",
                    "is_closing": False,
                    "is_closable": True,
                    "is_updatable": True,
                    "is_rented": False,
                    "assets": ch_assets_map,
                })

            # Combine all known symbols for this network
            all_syms = list(dict.fromkeys(
                default_net_assets.get(net_name, [])
                + list(onchain_by_sym.keys())
                + list(offchain_by_sym.keys())
            ))

            net_assets_list: List[Dict[str, Any]] = []
            for sym in all_syms:
                if sym.startswith("erc20:") or sym.startswith("0x"):
                    continue
                fiat_p = prices.get(sym, 1.0)
                onc = onchain_by_sym.get(sym, {"confirmed": 0.0, "unconfirmed": 0.0})
                offc = offchain_by_sym.get(sym, {
                    "send": 0.0,
                    "recv": 0.0,
                    "pending_local": 0.0,
                    "pending_remote": 0.0,
                    "closing": 0.0,
                    "max_withdrawable": 0.0,
                    "unspendable_reserve": 0.0,
                })

                conf_val = onc["confirmed"]
                unconf_val = onc["unconfirmed"]
                onc_usd = round((conf_val + unconf_val) * fiat_p, 2)
                send_val = offc["send"]
                recv_val = offc["recv"]
                closing_val = offc["closing"]
                withdrawing_val = offc.get("withdrawing_local", 0.0)
                is_updating_asset = bool(offc.get("is_updating", False))
                settling_label = ""
                if is_updating_asset:
                    now_ts = time.time()
                    recent_act = next(
                        (
                            entry for entry in self.channel_mgmt_log
                            if entry.get("network") == net_name
                            and entry.get("asset") == sym
                            and entry.get("status") == "COMPLETED"
                            and (now_ts - float(entry.get("timestamp", 0.0))) < 1800.0
                        ),
                        None,
                    )
                    if recent_act and recent_act.get("action_type") == "RENT_INBOUND":
                        amt_r = float(recent_act.get("amount", 0.0))
                        dec_r = 5 if sym == "BTC" else (4 if sym == "ETH" else 2)
                        settling_label = f"Settling Inbound: +{amt_r:.{dec_r}f} {sym}"
                    elif recent_act and recent_act.get("action_type") == "DEPOSIT_CHANNEL":
                        amt_d = float(recent_act.get("amount", 0.0))
                        dec_d = 5 if sym == "BTC" else (4 if sym == "ETH" else 2)
                        settling_label = f"Settling Deposit: +{amt_d:.{dec_d}f} {sym}"
                raw_max_wd = offc["max_withdrawable"] if net_name != "Bitcoin" else send_val
                max_wd = raw_max_wd if raw_max_wd > 0 else (send_val if send_val > 0 else 0.0)

                send_usd = round(send_val * fiat_p, 2)
                recv_usd = round(recv_val * fiat_p, 2)
                closing_usd = round(closing_val * fiat_p, 2)

                total_onchain_usd += onc_usd
                total_offchain_send_usd += send_usd
                total_offchain_recv_usd += recv_usd
                total_closing_usd += closing_usd

                # Compute safe max deposit amount leaving minimal gas/miner fee buffer
                if sym == "ETH":
                    max_dep = max(0.0, round(conf_val - 0.0005, 6))
                elif sym == "BTC":
                    max_dep = max(0.0, round(conf_val - 0.00005, 8))
                else:
                    max_dep = round(conf_val, 4)

                has_active_lithium = (net_name in ("Ethereum", "Arbitrum")) and (primary_channel_id is not None)
                deposit_mode = "DEPOSIT_CHANNEL" if has_active_lithium else "OPEN_CHANNEL"
                withdraw_mode = "WITHDRAW_CHANNEL" if net_name in ("Ethereum", "Arbitrum") else "CLOSE_CHANNEL"

                asset_entry = {
                    "network_name": net_name,
                    "network_id": nid,
                    "network_label": meta["label"],
                    "protocol": proto,
                    "symbol": sym,
                    "asset_id": self.channel_mgr._resolve_asset_id(net_name, sym) or "",
                    "fiat_price": fiat_p,
                    "onchain_confirmed": round(conf_val, 8 if sym in ("BTC", "ETH") else 4),
                    "onchain_unconfirmed": round(unconf_val, 8 if sym in ("BTC", "ETH") else 4),
                    "onchain_usd": onc_usd,
                    "max_deposit_usable": max_dep,
                    "offchain_send": round(send_val, 8 if sym in ("BTC", "ETH") else 4),
                    "offchain_recv": round(recv_val, 8 if sym in ("BTC", "ETH") else 4),
                    "offchain_pending_local": round(offc["pending_local"], 8 if sym in ("BTC", "ETH") else 4),
                    "offchain_closing": round(closing_val, 8 if sym in ("BTC", "ETH") else 4),
                    "withdrawing_local": round(withdrawing_val, 8 if sym in ("BTC", "ETH") else 4),
                    "max_withdrawable": round(max_wd, 8 if sym in ("BTC", "ETH") else 4),
                    "raw_max_withdrawable": round(raw_max_wd, 8 if sym in ("BTC", "ETH") else 4),
                    "is_updating": is_updating_asset,
                    "settling_label": settling_label,
                    "unspendable_reserve": round(offc["unspendable_reserve"], 8 if sym in ("BTC", "ETH") else 4),
                    "offchain_send_usd": send_usd,
                    "offchain_recv_usd": recv_usd,
                    "offchain_closing_usd": closing_usd,
                    "primary_channel_id": primary_channel_id,
                    "deposit_mode": deposit_mode,
                    "withdraw_mode": withdraw_mode,
                    "can_deposit": max_dep > 0,
                    "can_withdraw": max_wd > 0,
                }
                net_assets_list.append(asset_entry)
                flat_assets.append(asset_entry)

            networks_out[net_name] = {
                "network_name": net_name,
                "network_id": nid,
                "label": meta["label"],
                "protocol": proto,
                "primary_channel_id": primary_channel_id,
                "assets": net_assets_list,
                "individual_channels": individual_channels,
            }

        all_recommended_actions = self._compute_asset_advisor_diagnostics(networks_out, prices)

        total_ecash_usd = 0.0
        total_ecash_sats = 0
        if getattr(self, "cashu_bridge", None):
            try:
                cst = self.cashu_bridge.get_status()
                total_ecash_sats = int(cst.get("vault", {}).get("balance_sats", 0) or 0)
                btc_p = prices.get("BTC", 82000.0)
                total_ecash_usd = round((total_ecash_sats / 1e8) * btc_p, 2)
            except Exception:
                pass

        offchain_owned_usd = round(total_offchain_send_usd + total_closing_usd, 2)
        total_asset_balance_usd = round(total_onchain_usd + offchain_owned_usd + total_ecash_usd, 2)

        constrained_items = []
        for a in flat_assets:
            if a.get("send_constrained"):
                constrained_items.append(f"{a['symbol']} Send")
            if a.get("recv_constrained"):
                constrained_items.append(f"{a['symbol']} Recv")

        channel_health = {
            "status": "HEALTHY" if not constrained_items else "NEEDS_ATTENTION",
            "constrained_count": len(constrained_items),
            "constrained_items": constrained_items,
            "recommended_actions_count": len(all_recommended_actions),
        }

        return {
            "total_asset_balance_usd": total_asset_balance_usd,
            "total_onchain_usd": round(total_onchain_usd, 2),
            "total_offchain_send_usd": round(total_offchain_send_usd, 2),
            "total_offchain_owned_usd": offchain_owned_usd,
            "total_offchain_recv_usd": round(total_offchain_recv_usd, 2),
            "total_closing_usd": round(total_closing_usd, 2),
            "total_ecash_usd": total_ecash_usd,
            "total_ecash_sats": total_ecash_sats,
            "channel_health": channel_health,
            "networks": networks_out,
            "assets": flat_assets,
            "recommended_actions": all_recommended_actions,
            "automation": {
                "auto_cashu": bool(getattr(self.config, "auto_cashu", True)) and bool(getattr(self.cashu_bridge, "initialized", False)),
                "auto_btc_channels": bool(getattr(self.config, "auto_btc_channels", False)),
                "auto_lithium_channels": bool(getattr(self.config, "auto_lithium_channels", False)),
            },
            "channel_management_log": self.channel_mgmt_log[:40],
        }

    def _compute_asset_advisor_diagnostics(
        self,
        networks_out: Dict[str, Any],
        prices: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """
        Deterministic Channel Advisor:
        Inspects whether all 15 Bid and 15 Ask rungs could be placed on active markets,
        flags any Send or Receive capacity that is constraining 15-rung placement
        (so the UI can underline it in yellow with a caution icon), and generates
        concrete advisor recommendations and automated action payloads.
        """
        active_markets = self._active_markets()
        target_rungs = int(getattr(self.config, "rungs_per_side", 15))
        alloc_pct = max(0.10, float(getattr(self.config, "channel_order_allocation_pct", 0.85)))

        # Map (network_name, symbol) -> constraint accumulator
        constraints: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for net_name, net_data in networks_out.items():
            for a in net_data.get("assets", []):
                key = (net_name, a["symbol"])
                constraints[key] = {
                    "send_constrained": False,
                    "recv_constrained": False,
                    "send_reasons": [],
                    "recv_reasons": [],
                    "req_send": 0.0,
                    "req_recv": 0.0,
                }

        def _net_is_participating(n_name: str) -> bool:
            nd = networks_out.get(n_name, {})
            if nd.get("primary_channel_id"):
                return True
            for ch in nd.get("individual_channels", []):
                if not ch.get("is_closing"):
                    return True
            for a_item in nd.get("assets", []):
                if (a_item.get("onchain_confirmed", 0.0) or 0.0) > 0 or (a_item.get("offchain_send", 0.0) or 0.0) > 0:
                    return True
            return False

        enabled_asset_keys = set()
        for p_name in self.target_pairs:
            m_c = active_markets.get(p_name)
            if m_c:
                enabled_asset_keys.add((m_c.base.network_name, m_c.base.symbol))
                enabled_asset_keys.add((m_c.quote.network_name, m_c.quote.symbol))

        participating_pairs: List[Tuple[str, Any, Any, float, float, float]] = []

        for pair, m_cfg in active_markets.items():
            if pair not in self.target_pairs:
                continue

            base_net = m_cfg.base.network_name
            quote_net = m_cfg.quote.network_name

            audit = self.market_audits.get(pair)
            if not audit or audit.mid_price <= 0:
                try:
                    self.generate_orders_for_market(pair)
                    audit = self.market_audits.get(pair)
                except Exception:
                    audit = None
            ref_price = (audit.mid_price if audit and audit.mid_price > 0 else 0.0) or self.oracle.get_price(pair) or 0.0
            if m_cfg.quote.symbol == "BTC" and ref_price > 1.0:
                ref_price = round(prices.get(m_cfg.base.symbol, 2450.0) / max(1.0, prices.get("BTC", 82000.0)), 6)
            if not audit or ref_price <= 0:
                continue

            quote_fiat = prices.get(m_cfg.quote.symbol, 1.0)
            min_floor_usd = max(self.config.hub_min_placement_floor_usd, m_cfg.quote.min_amount * quote_fiat)
            min_base_rung = max(
                m_cfg.min_order_size,
                (min_floor_usd / (ref_price * quote_fiat)) if (ref_price * quote_fiat) > 0 else m_cfg.min_order_size,
            )
            min_quote_rung = max(
                m_cfg.quote.min_amount,
                min_floor_usd / quote_fiat if quote_fiat > 0 else m_cfg.quote.min_amount,
                min_base_rung * ref_price,
            )

            # Gaussian curve inner rungs take larger share than outer rungs; 1.15x factor + alloc_pct ensures all 15 rungs clear floor
            req_quote_15 = (target_rungs * min_quote_rung * 1.15) / alloc_pct
            req_base_15 = (target_rungs * min_base_rung * 1.15) / alloc_pct

            b_key = (base_net, m_cfg.base.symbol)
            q_key = (quote_net, m_cfg.quote.symbol)

            # Pass 1: Accumulate multi-pair capacity requirements across ALL enabled pairs sharing this asset leg
            if q_key in constraints:
                constraints[q_key]["req_send"] += req_quote_15
                constraints[q_key]["req_recv"] += req_quote_15
            if b_key in constraints:
                constraints[b_key]["req_recv"] += req_base_15
                constraints[b_key]["req_send"] += req_base_15

            participating_pairs.append((pair, m_cfg, audit, ref_price, req_base_15, req_quote_15))

        # Pass 2: Identify which specific asset legs are below their multi-pair 15-rung requirement
        for pair, m_cfg, audit, ref_price, req_base_15, req_quote_15 in participating_pairs:
            b_key = (m_cfg.base.network_name, m_cfg.base.symbol)
            q_key = (m_cfg.quote.network_name, m_cfg.quote.symbol)
            bids_placed = audit.active_bids_count
            asks_placed = audit.active_asks_count

            # 1. Check Bid Rungs (< 15 Bids OR Bid depth throttled < 65% of Ideal -> Quote SEND or Base RECEIVE is limiting)
            ideal_bid_usd = sum(float(x.get("value_usd", 0.0)) for x in getattr(audit, "ideal_bids", []))
            bid_depth_throttled = (
                ideal_bid_usd >= 30.0
                and audit.active_bids_value_usd > 0
                and audit.active_bids_value_usd < (ideal_bid_usd * 0.65)
            )
            if bids_placed < target_rungs or bid_depth_throttled:
                q_send = audit.quote_sendable
                b_recv = audit.base_receivable
                q_req_tot = constraints.get(q_key, {}).get("req_send", req_quote_15)
                b_req_tot = constraints.get(b_key, {}).get("req_recv", req_base_15)
                if bid_depth_throttled and ref_price > 0:
                    quote_fiat = prices.get(m_cfg.quote.symbol, 1.0) or 1.0
                    base_fiat = prices.get(m_cfg.base.symbol, ref_price * quote_fiat) or 1.0
                    b_req_tot = max(b_req_tot, ideal_bid_usd / base_fiat)
                    if b_key in constraints:
                        constraints[b_key]["req_recv"] = max(constraints[b_key]["req_recv"], b_req_tot)
                q_short = q_send < q_req_tot
                b_short = b_recv < b_req_tot
                reason_label = (
                    f"{pair} Bids ({bids_placed}/{target_rungs} rungs)"
                    if bids_placed < target_rungs
                    else f"{pair} Bid depth (${audit.active_bids_value_usd:.0f}/${ideal_bid_usd:.0f} Ideal)"
                )
                if q_key in constraints and (q_short or (not b_short and q_send <= b_recv * ref_price * 1.05)):
                    constraints[q_key]["send_constrained"] = True
                    constraints[q_key]["send_reasons"].append(reason_label)
                if b_key in constraints and (b_short or (not q_short and b_recv * ref_price <= q_send * 1.05)):
                    constraints[b_key]["recv_constrained"] = True
                    constraints[b_key]["recv_reasons"].append(reason_label)

            # 2. Check Ask Rungs (< 15 Asks OR Ask depth throttled < 65% of Ideal -> Base SEND or Quote RECEIVE is limiting)
            ideal_ask_usd = sum(float(x.get("value_usd", 0.0)) for x in getattr(audit, "ideal_asks", []))
            ask_depth_throttled = (
                ideal_ask_usd >= 30.0
                and audit.active_asks_value_usd > 0
                and audit.active_asks_value_usd < (ideal_ask_usd * 0.65)
            )
            if asks_placed < target_rungs or ask_depth_throttled:
                b_send = audit.base_sendable
                q_recv = audit.quote_receivable
                b_req_tot = constraints.get(b_key, {}).get("req_send", req_base_15)
                q_req_tot = constraints.get(q_key, {}).get("req_recv", req_quote_15)
                if ask_depth_throttled and ref_price > 0:
                    quote_fiat = prices.get(m_cfg.quote.symbol, 1.0) or 1.0
                    q_req_tot = max(q_req_tot, ideal_ask_usd / quote_fiat)
                    if q_key in constraints:
                        constraints[q_key]["req_recv"] = max(constraints[q_key]["req_recv"], q_req_tot)
                b_short = b_send < b_req_tot
                q_short = q_recv < q_req_tot
                q_recv_in_base = (q_recv / ref_price) if ref_price > 0 else 0.0
                reason_label = (
                    f"{pair} Asks ({asks_placed}/{target_rungs} rungs)"
                    if asks_placed < target_rungs
                    else f"{pair} Ask depth (${audit.active_asks_value_usd:.0f}/${ideal_ask_usd:.0f} Ideal)"
                )
                if b_key in constraints and (b_short or (not q_short and b_send <= q_recv_in_base * 1.05)):
                    constraints[b_key]["send_constrained"] = True
                    constraints[b_key]["send_reasons"].append(reason_label)
                if q_key in constraints and (q_short or (not b_short and q_recv_in_base <= b_send * 1.05)):
                    constraints[q_key]["recv_constrained"] = True
                    constraints[q_key]["recv_reasons"].append(reason_label)

        all_recommended_actions: List[Dict[str, Any]] = []

        # Build human-readable Advisor messages & structured actions for each network and asset
        for net_name, net_data in networks_out.items():
            indiv_chs = net_data.get("individual_channels", [])
            open_chs = [c for c in indiv_chs if not c.get("is_closing")]
            is_ch_updating = any(c.get("status") in ("UPDATING", "OPENING") for c in open_chs)
            primary_cid = net_data.get("primary_channel_id")
            net_advisor_messages: List[str] = []

            for a in net_data.get("assets", []):
                sym = a["symbol"]
                key = (net_name, sym)
                if key not in enabled_asset_keys:
                    a["send_constrained"] = False
                    a["recv_constrained"] = False
                    a["send_reasons"] = []
                    a["recv_reasons"] = []
                    a["advisor_messages"] = []
                    a["advisor_text"] = ""
                    a["recommended_actions"] = []
                    continue

                c_info = constraints.get(key, {})
                send_val = float(a.get("offchain_send", 0.0) or 0.0)
                recv_val = float(a.get("offchain_recv", 0.0) or 0.0)
                max_dep = float(a.get("max_deposit_usable", 0.0) or 0.0)
                fiat_p = float(a.get("fiat_price", 1.0) or 1.0)
                dec = 5 if sym == "BTC" else (4 if sym == "ETH" else 2)

                send_constrained = bool(c_info.get("send_constrained", False))
                recv_constrained = bool(c_info.get("recv_constrained", False))
                req_send = float(c_info.get("req_send", 0.0) or 0.0)
                req_recv = float(c_info.get("req_recv", 0.0) or 0.0)
                send_reasons = list(dict.fromkeys(c_info.get("send_reasons", [])))
                recv_reasons = list(dict.fromkeys(c_info.get("recv_reasons", [])))

                asset_messages: List[str] = []
                asset_actions: List[Dict[str, Any]] = []

                if net_name == "Bitcoin" and sym == "BTC":
                    tot_btc = send_val + recv_val
                    btc_ratio = (send_val / tot_btc) if tot_btc > 0 else 0.50
                    if tot_btc > 0 and btc_ratio >= 0.75 and not recv_constrained:
                        recv_constrained = True
                        recv_reasons.append(f"BTC channel skew ({btc_ratio*100:.0f}% Send)")
                    elif tot_btc > 0 and btc_ratio <= 0.25 and not send_constrained:
                        send_constrained = True
                        send_reasons.append(f"BTC channel skew ({btc_ratio*100:.0f}% Send)")

                    min_sink_sats = int(getattr(self.config, "cashu_min_sink_sats", 30000))
                    cashu_init = bool(getattr(self.cashu_bridge, "initialized", False)) if self.cashu_bridge else False
                    vault_sats = self.cashu_bridge.get_wallet_balance_sats() if (self.cashu_bridge and cashu_init) else 0
                    rebalances_6h = self.cashu_bridge.get_rebalance_count_in_window(21600.0) if self.cashu_bridge else 0
                    exp_threshold = int(getattr(self.config, "cashu_expansion_threshold", 4))

                    closable_btc = sorted(
                        [c for c in open_chs if c.get("is_closable")],
                        key=lambda c: (
                            (c.get("assets", {}).get("BTC", {}).get("free_local", 0.0) or 0.0)
                            + (c.get("assets", {}).get("BTC", {}).get("free_remote", 0.0) or 0.0)
                        ),
                    )
                    at_hub_limit = len(open_chs) >= 4

                    if len(open_chs) == 0:
                        send_constrained = True
                        recv_constrained = True
                        if max_dep > 0:
                            msg = f"No active BTC Lightning channel — Advise opening BTC channel with {max_dep:.5f} BTC from wallet."
                            asset_messages.append(msg)
                            act = {
                                "category": "BTC_CHANNEL",
                                "action_type": "OPEN_CHANNEL",
                                "network": "Bitcoin",
                                "asset": "BTC",
                                "amount": round(max_dep, 6),
                                "can_execute": True,
                                "advice_text": msg,
                            }
                            asset_actions.append(act)
                            all_recommended_actions.append(act)
                        else:
                            asset_messages.append("No active BTC Lightning channel — Advise funding Bitcoin wallet to open a channel.")
                    else:
                        if recv_constrained:
                            r_str = ", ".join(recv_reasons) or "15-rung Bids"
                            spare_send_btc = max(0.0, send_val - req_send)
                            can_mint_without_starving_send = (btc_ratio >= 0.65) or (spare_send_btc * 1e8 >= min_sink_sats)
                            deficit_btc = max(0.0003, req_recv - recv_val, (send_val - recv_val) * 0.5)
                            max_safe_sweep_btc = spare_send_btc if spare_send_btc * 1e8 >= min_sink_sats else send_val * 0.50
                            step_cap_sats = 50000 if getattr(self.config, "testnet", False) else 250000
                            mint_recv_ok = True
                            if getattr(self.config, "testnet", False) and self.cashu_bridge and not self.cashu_bridge.cashu.dry_run:
                                mint_info = self.cashu_bridge._get_cached_mint_info()
                                if "lnd_max_receivable_sats" in mint_info:
                                    safe_mint_recv = max(0, int(mint_info.get("lnd_max_receivable_sats", 0)) - 1500)
                                    if safe_mint_recv < min_sink_sats:
                                        mint_recv_ok = False
                                    else:
                                        step_cap_sats = min(step_cap_sats, safe_mint_recv)
                            sweep_sats = max(min_sink_sats, min(step_cap_sats, int(round(min(max_safe_sweep_btc, deficit_btc) * 1e8))))
                            if mint_recv_ok and send_val * 1e8 >= min_sink_sats and can_mint_without_starving_send:
                                if cashu_init:
                                    msg_mint = (
                                        f"BTC Receive ({recv_val:.5f} BTC) limits {r_str} — "
                                        f"Advise minting {sweep_sats:,} sats to Cashu Vault to restore inbound capacity."
                                    )
                                else:
                                    msg_mint = (
                                        f"BTC Receive ({recv_val:.5f} BTC) limits {r_str} — "
                                        f"Advise initializing Cashu Vault to mint {sweep_sats:,} sats and restore inbound capacity."
                                    )
                                asset_messages.append(msg_mint)
                                act_mint = {
                                    "category": "CASHU",
                                    "action_type": "CASHU_MINT",
                                    "network": "Bitcoin",
                                    "asset": "BTC",
                                    "amount_sats": sweep_sats,
                                    "amount_btc": round(sweep_sats / 1e8, 8),
                                    "can_execute": cashu_init,
                                    "advice_text": msg_mint,
                                }
                                asset_actions.append(act_mint)
                                all_recommended_actions.append(act_mint)

                            # Escalation: if Cashu Mint/Melt would starve Send, or has run too many times, or total channel is too small
                            if not can_mint_without_starving_send or rebalances_6h >= exp_threshold or tot_btc < req_recv * 1.5 or send_val * 1e8 < min_sink_sats:
                                rent_btc = round(max(req_recv - recv_val, 0.001) * 1.25, 5)
                                if at_hub_limit and closable_btc:
                                    sm = closable_btc[0]
                                    sm_cap = (sm.get("assets", {}).get("BTC", {}).get("free_local", 0.0) or 0.0) + (
                                        sm.get("assets", {}).get("BTC", {}).get("free_remote", 0.0) or 0.0
                                    )
                                    msg_exp = (
                                        f"4/4 BTC Hub channels open & BTC Receive ({recv_val:.5f} BTC) limits {r_str} — "
                                        f"Advise closing smallest channel ({sm['short_id']}, {sm_cap:.5f} BTC) and renting {rent_btc:.5f} BTC inbound."
                                    )
                                    act_exp = {
                                        "category": "BTC_CHANNEL",
                                        "action_type": "CLOSE_AND_RENT_BTC",
                                        "network": "Bitcoin",
                                        "asset": "BTC",
                                        "close_channel_id": sm["channel_id"],
                                        "amount": rent_btc,
                                        "can_execute": not is_ch_updating,
                                        "advice_text": msg_exp,
                                    }
                                else:
                                    msg_exp = (
                                        f"BTC Receive ({recv_val:.5f} BTC) limits {r_str} — "
                                        f"Advise expanding BTC capacity by renting {rent_btc:.5f} BTC inbound."
                                    )
                                    act_exp = {
                                        "category": "BTC_CHANNEL",
                                        "action_type": "RENT_INBOUND",
                                        "network": "Bitcoin",
                                        "asset": "BTC",
                                        "amount": rent_btc,
                                        "can_execute": not is_ch_updating,
                                        "advice_text": msg_exp,
                                    }
                                asset_messages.append(msg_exp)
                                asset_actions.append(act_exp)
                                all_recommended_actions.append(act_exp)

                        if send_constrained:
                            s_str = ", ".join(send_reasons) or "15-rung Asks"
                            spare_recv_btc = max(0.0, recv_val - req_recv)
                            can_melt_without_starving_recv = (btc_ratio <= 0.35) or (spare_recv_btc * 1e8 >= min_sink_sats)
                            deficit_btc = max(0.0003, req_send - send_val, (recv_val - send_val) * 0.5)
                            if vault_sats >= min_sink_sats and recv_val * 1e8 >= min_sink_sats and can_melt_without_starving_recv:
                                melt_sats = min(
                                    max(min_sink_sats, vault_sats - 200),
                                    max(min_sink_sats, int(round(deficit_btc * 1e8))),
                                )
                                msg_melt = (
                                    f"BTC Send ({send_val:.5f} BTC) limits {s_str} — "
                                    f"Advise melting {melt_sats:,} sats from Cashu Vault into BTC channel."
                                )
                                asset_messages.append(msg_melt)
                                act_melt = {
                                    "category": "CASHU",
                                    "action_type": "CASHU_MELT",
                                    "network": "Bitcoin",
                                    "asset": "BTC",
                                    "amount_sats": melt_sats,
                                    "amount_btc": round(melt_sats / 1e8, 8),
                                    "can_execute": True,
                                    "advice_text": msg_melt,
                                }
                                asset_actions.append(act_melt)
                                all_recommended_actions.append(act_melt)

                            if not can_melt_without_starving_recv or vault_sats < min_sink_sats or rebalances_6h >= exp_threshold or tot_btc < req_send * 1.5:
                                open_btc = round(min(max_dep, max(deficit_btc * 1.25, 0.001)), 5) if max_dep > 0 else round(max(deficit_btc * 1.25, 0.001), 5)
                                if at_hub_limit and closable_btc:
                                    sm = closable_btc[0]
                                    msg_exp = (
                                        f"4/4 BTC Hub channels open & BTC Send constrained — "
                                        f"Advise closing smallest channel ({sm['short_id']}) to vacate a slot and opening a {open_btc:.5f} BTC channel."
                                    )
                                    act_exp = {
                                        "category": "BTC_CHANNEL",
                                        "action_type": "CLOSE_AND_OPEN_BTC",
                                        "network": "Bitcoin",
                                        "asset": "BTC",
                                        "close_channel_id": sm["channel_id"],
                                        "amount": open_btc,
                                        "can_execute": (max_dep > 0) and (not is_ch_updating),
                                        "advice_text": msg_exp,
                                    }
                                    asset_messages.append(msg_exp)
                                    asset_actions.append(act_exp)
                                    all_recommended_actions.append(act_exp)
                                elif max_dep > 0:
                                    msg_exp = (
                                        f"BTC Send ({send_val:.5f} BTC) limits {s_str} — "
                                        f"Advise opening a new BTC channel with {open_btc:.5f} BTC from on-chain wallet."
                                    )
                                    act_exp = {
                                        "category": "BTC_CHANNEL",
                                        "action_type": "OPEN_CHANNEL",
                                        "network": "Bitcoin",
                                        "asset": "BTC",
                                        "amount": open_btc,
                                        "can_execute": not is_ch_updating,
                                        "advice_text": msg_exp,
                                    }
                                    asset_messages.append(msg_exp)
                                    asset_actions.append(act_exp)
                                    all_recommended_actions.append(act_exp)
                                elif vault_sats < min_sink_sats:
                                    asset_messages.append(
                                        f"BTC Send ({send_val:.5f} BTC) limits {s_str} & Cashu Vault is low — "
                                        f"Advise adding {open_btc:.5f} BTC to Bitcoin wallet to expand channel."
                                    )

                else:
                    # Lithium EVM State Channels (Ethereum & Arbitrum): Strictly 1 channel per network, supports Open-via-Deposit OR Open-via-Inbound-Rental (openOrDeposit)
                    if primary_cid is None:
                        if send_constrained or recv_constrained or max_dep > 0:
                            if max_dep > 0:
                                msg = f"No {net_name} channel open — Advise initiating {net_name} Lithium channel with {max_dep:.{dec}f} {sym}."
                                asset_messages.append(msg)
                                act = {
                                    "category": "LITHIUM_CHANNEL",
                                    "action_type": "OPEN_CHANNEL",
                                    "network": net_name,
                                    "asset": sym,
                                    "amount": max_dep,
                                    "can_execute": not is_ch_updating,
                                    "advice_text": msg,
                                }
                                asset_actions.append(act)
                                all_recommended_actions.append(act)
                            elif send_constrained or recv_constrained:
                                any_offchain_send = any(
                                    float(x_a.get("offchain_send_usd", 0.0) or 0.0) >= 1.0
                                    for x_nd in networks_out.values()
                                    for x_a in x_nd.get("assets", [])
                                )
                                deficit = max(0.0, req_recv - recv_val, req_send - send_val)
                                min_floor_units = self.config.min_auto_rental_amount_usd / max(fiat_p, 1e-6)
                                max_rent = self.config.max_auto_rental_amount_usd / max(fiat_p, 1e-6)
                                target_rent = round(min(max_rent, max(min_floor_units, deficit * 1.15)), dec)
                                if any_offchain_send and target_rent > 0:
                                    msg = (
                                        f"No {net_name} channel open — Advise renting {target_rent:.{dec}f} {sym} inbound capacity "
                                        f"(Hub opens {net_name} channel & deducts fee from existing off-chain balance)."
                                    )
                                    asset_messages.append(msg)
                                    act = {
                                        "category": "LITHIUM_CHANNEL",
                                        "action_type": "RENT_INBOUND",
                                        "network": net_name,
                                        "asset": sym,
                                        "channel_id": None,
                                        "amount": target_rent,
                                        "can_execute": not is_ch_updating,
                                        "advice_text": msg,
                                    }
                                    asset_actions.append(act)
                                    all_recommended_actions.append(act)
                                else:
                                    asset_messages.append(f"No {net_name} channel open — Advise funding {net_name} wallet with {sym} or renting inbound to initiate channel.")
                    else:
                        if send_constrained:
                            s_str = ", ".join(send_reasons) or "15 rungs"
                            deficit = max(0.0, req_send - send_val)
                            min_dep_units = 25.0 / max(fiat_p, 1e-6)
                            target_dep = (
                                round(min(max_dep, max(deficit * 1.25, min_dep_units)), dec)
                                if max_dep > 0
                                else round(max(deficit * 1.25, min_dep_units), dec)
                            )
                            if max_dep > 0 and target_dep > 0:
                                upd_note = " (Channel updating — will deposit once active)" if is_ch_updating else ""
                                msg = (
                                    f"{sym} Send ({send_val:.{dec}f} {sym}) limits {s_str} — "
                                    f"Advise depositing {target_dep:.{dec}f} {sym} from wallet into {net_name} channel{upd_note}."
                                )
                                asset_messages.append(msg)
                                act = {
                                    "category": "LITHIUM_CHANNEL",
                                    "action_type": "DEPOSIT_CHANNEL",
                                    "network": net_name,
                                    "asset": sym,
                                    "channel_id": primary_cid,
                                    "amount": target_dep,
                                    "can_execute": not is_ch_updating,
                                    "advice_text": msg,
                                }
                                asset_actions.append(act)
                                all_recommended_actions.append(act)
                            else:
                                asset_messages.append(
                                    f"{sym} Send ({send_val:.{dec}f} {sym}) limits {s_str} — "
                                    f"Advise adding {target_dep:.{dec}f} {sym} to {net_name} wallet to deposit."
                                )

                        if recv_constrained:
                            r_str = ", ".join(recv_reasons) or "15 rungs"
                            deficit = max(0.0, req_recv - recv_val)
                            min_floor_units = self.config.min_auto_rental_amount_usd / max(fiat_p, 1e-6)
                            max_rent = self.config.max_auto_rental_amount_usd / max(fiat_p, 1e-6)
                            target_rent = round(min(max_rent, max(min_floor_units, deficit * 1.15)), dec)
                            upd_note = " (Channel updating — will rent once active)" if is_ch_updating else ""
                            msg = (
                                f"{sym} Receive ({recv_val:.{dec}f} {sym}) limits {r_str} — "
                                f"Advise renting {target_rent:.{dec}f} {sym} inbound capacity on {net_name}{upd_note}."
                            )
                            asset_messages.append(msg)
                            act = {
                                "category": "LITHIUM_CHANNEL",
                                "action_type": "RENT_INBOUND",
                                "network": net_name,
                                "asset": sym,
                                "channel_id": primary_cid,
                                "amount": target_rent,
                                "can_execute": not is_ch_updating,
                                "advice_text": msg,
                            }
                            asset_actions.append(act)
                            all_recommended_actions.append(act)

                a["send_constrained"] = send_constrained
                a["recv_constrained"] = recv_constrained
                a["send_reasons"] = send_reasons
                a["recv_reasons"] = recv_reasons
                a["advisor_messages"] = asset_messages
                a["advisor_text"] = " • ".join(asset_messages)
                a["recommended_actions"] = asset_actions
                net_advisor_messages.extend(asset_messages)

            net_data["advisor_messages"] = net_advisor_messages
            net_data["advisor_text"] = " • ".join(net_advisor_messages)

        return all_recommended_actions

    def execute_bridge_transfer(
        self,
        direction: str,
        network_name: str,
        asset_symbol: str,
        amount: float = 0.0,
        channel_id: Optional[str] = None,
        action_subtype: Optional[str] = None,
        mode: str = "MANUAL",
        advice_acted_on: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Interactive bridge handler to move assets between On-Chain Wallets and Off-Chain State Channels:
        - direction == 'onchain_to_offchain':
            - Ethereum / Arbitrum: Deposits into existing Lithium channel (node_depositChannel) or opens a new channel (node_openChannel).
            - Bitcoin: Opens a new Lightning channel with the Hub (node_openChannel).
        - direction == 'offchain_to_onchain':
            - Ethereum / Arbitrum: Withdraws specified asset amount from Lithium channel to on-chain wallet (node_withdrawChannel),
              or cooperatively closes the channel if action_subtype == 'close'.
            - Bitcoin: Cooperatively closes the selected (or smallest active) Lightning channel to sweep local BTC back to on-chain.
        - direction == 'rent_inbound':
            - Requests inbound channel liquidity from the Hydranet Liquidity Hub (liquidity_requestChannelLiquidity).
        """
        with self._order_sync_lock:
            net = network_name.strip().capitalize()
            if net not in ("Bitcoin", "Ethereum", "Arbitrum"):
                return {"status": "FAILED", "error": f"Unsupported network: {network_name}"}

            sym = asset_symbol.strip().upper()
            asset_id = self.channel_mgr._resolve_asset_id(net, sym)
            if not asset_id:
                return {"status": "FAILED", "error": f"Unsupported asset {sym} on {net}"}

            live = not getattr(self.config, "dry_run", False)
            dec = 8 if sym == "BTC" else (6 if sym == "ETH" else 2)

            try:
                # 1. ON-CHAIN -> OFF-CHAIN (Deposit into Lithium Channel or Open New Channel)
                if direction == "onchain_to_offchain":
                    if amount <= 0:
                        return {"status": "FAILED", "error": "Transfer amount must be greater than 0."}

                    amt_str = f"{amount:.{dec}f}".rstrip("0").rstrip(".") if "." in f"{amount:.{dec}f}" else str(amount)
                    asset_amounts = {asset_id: amt_str}

                    if net in ("Ethereum", "Arbitrum"):
                        target_ch = None
                        if channel_id:
                            proto = "PROTOCOL_EVM"
                            nid = self.channel_mgr._net_id(net)
                            for c in self.channel_mgr.get_channels_for_network(proto, nid, net):
                                if c.channel_id.lower() == channel_id.lower() and not self.channel_mgr.is_channel_closing_or_closed(c):
                                    target_ch = c
                                    break
                        if not target_ch:
                            target_ch = self.channel_mgr.get_primary_lithium_channel(net)

                        if target_ch and action_subtype != "open":
                            res = self.channel_mgr.deposit_channel(
                                network_name=net,
                                channel_id=target_ch.channel_id,
                                asset_amounts=asset_amounts,
                                fee_option="medium",
                                live=live,
                                force=True,
                            )
                            msg = (
                                f"{'Deposited' if live else '[DRY-RUN] Simulated deposit of'} {amt_str} {sym} "
                                f"from {net} On-Chain Wallet into Off-Chain Lithium Channel {target_ch.channel_id[:10]}..."
                            )
                            action_executed = "DEPOSIT_CHANNEL"
                            used_cid = target_ch.channel_id
                        else:
                            res = self.channel_mgr.open_channel(
                                network_name=net,
                                asset_amounts=asset_amounts,
                                fee_option="medium",
                                live=live,
                                force=True,
                            )
                            msg = (
                                f"{'Opened' if live else '[DRY-RUN] Simulated opening'} new {net} Lithium Channel "
                                f"with {amt_str} {sym} from On-Chain Wallet."
                            )
                            action_executed = "OPEN_CHANNEL"
                            used_cid = channel_id or ""
                    else:
                        # Bitcoin Lightning: open channel with Hub
                        res = self.channel_mgr.open_channel(
                            network_name="Bitcoin",
                            asset_amounts=asset_amounts,
                            fee_option="medium",
                            live=live,
                            force=True,
                        )
                        msg = (
                            f"{'Opened' if live else '[DRY-RUN] Simulated opening'} new Bitcoin Lightning Channel "
                            f"with {amt_str} BTC from On-Chain Wallet."
                        )
                        action_executed = "OPEN_CHANNEL"
                        used_cid = ""

                    # Update simulated state if in dry_run
                    if self.config.dry_run:
                        cur_onc = self.sim_onchain.get((net, sym), 0.0)
                        self.sim_onchain[(net, sym)] = max(0.0, cur_onc - amount)
                        if (net, sym) not in self.sim_assets:
                            self.sim_assets[(net, sym)] = {"free_local": 0.0, "free_remote": 0.0, "total_capacity": 0.0}
                        self.sim_assets[(net, sym)]["free_local"] += amount
                        self.sim_assets[(net, sym)]["total_capacity"] = (
                            self.sim_assets[(net, sym)]["free_local"] + self.sim_assets[(net, sym)]["free_remote"]
                        )

                    for p in self._active_markets().keys():
                        self.last_order_refresh_time[p] = 0.0
                        self.sync_orders_for_market(p)

                    self.log_channel_action(
                        mode=mode,
                        network=net,
                        asset=sym,
                        action_type=action_executed,
                        amount=float(amt_str),
                        status="COMPLETED",
                        message=msg,
                        advice_acted_on=advice_acted_on,
                        unit=sym,
                    )

                    return {
                        "status": "SUCCESS",
                        "direction": direction,
                        "action": action_executed,
                        "network": net,
                        "asset": sym,
                        "amount": float(amt_str),
                        "channel_id": used_cid,
                        "message": msg,
                        "result": res,
                    }

                # 2. OFF-CHAIN -> ON-CHAIN (Withdraw from Lithium Channel or Cooperatively Close Channel)
                elif direction == "offchain_to_onchain":
                    if net in ("Ethereum", "Arbitrum") and action_subtype != "close":
                        if amount <= 0:
                            return {"status": "FAILED", "error": "Withdrawal amount must be greater than 0."}

                        target_cid = channel_id
                        if not target_cid:
                            primary_ch = self.channel_mgr.get_primary_lithium_channel(net, required_asset_id=asset_id)
                            if not primary_ch:
                                primary_ch = self.channel_mgr.get_primary_lithium_channel(net)
                            if primary_ch:
                                target_cid = primary_ch.channel_id

                        if not target_cid:
                            return {"status": "FAILED", "error": f"No active {net} Lithium channel found to withdraw from."}

                        res = self.channel_mgr.withdraw_from_channel(
                            network_name=net,
                            channel_id=target_cid,
                            asset_symbol=sym,
                            amount=amount,
                            fee_option="medium",
                            live=live,
                        )
                        amt_str = f"{amount:.{dec}f}".rstrip("0").rstrip(".") if "." in f"{amount:.{dec}f}" else str(amount)
                        msg = (
                            f"{'Withdrew' if live else '[DRY-RUN] Simulated withdrawal of'} {amt_str} {sym} "
                            f"from Off-Chain Lithium Channel {target_cid[:10]}... to {net} On-Chain Wallet."
                        )
                        if self.config.dry_run:
                            if (net, sym) in self.sim_assets:
                                self.sim_assets[(net, sym)]["free_local"] = max(0.0, self.sim_assets[(net, sym)]["free_local"] - amount)
                            self.sim_onchain[(net, sym)] = self.sim_onchain.get((net, sym), 0.0) + amount

                        for p in self._active_markets().keys():
                            self.last_order_refresh_time[p] = 0.0
                            self.sync_orders_for_market(p)

                        self.log_channel_action(
                            mode=mode,
                            network=net,
                            asset=sym,
                            action_type="WITHDRAW_CHANNEL",
                            amount=float(amt_str),
                            status="COMPLETED",
                            message=msg,
                            advice_acted_on=advice_acted_on,
                            unit=sym,
                        )

                        return {
                            "status": "SUCCESS",
                            "direction": direction,
                            "action": "WITHDRAW_CHANNEL",
                            "network": net,
                            "asset": sym,
                            "amount": float(amt_str),
                            "channel_id": target_cid,
                            "message": msg,
                            "result": res,
                        }
                    else:
                        # Cooperative channel close (default for Bitcoin Lightning, or explicit close on EVM)
                        target_cid = channel_id
                        swept_local = amount
                        if not target_cid:
                            active_chs = self.channel_mgr.get_active_channels_by_capacity(net)
                            if active_chs:
                                chosen_ch = active_chs[0]
                                target_cid = chosen_ch.channel_id
                                swept_local = sum(a.free_local for a in chosen_ch.assets.values())
                            elif self.config.dry_run:
                                target_cid = f"sim_{net.lower()}_channel"
                                swept_local = self.sim_assets.get((net, sym), {}).get("free_local", amount)

                        if not target_cid:
                            return {"status": "FAILED", "error": f"No active {net} channel found to close."}

                        res = self.channel_mgr.close_channel(
                            network_name=net,
                            channel_id=target_cid,
                            fee_option="medium",
                            live=live,
                        )
                        msg = (
                            f"{'Cooperatively closed' if live else '[DRY-RUN] Simulated cooperative close of'} "
                            f"{net} Channel {target_cid[:12]}... — local {sym} balance is sweeping back to On-Chain Wallet."
                        )
                        if self.config.dry_run and swept_local > 0:
                            if (net, sym) in self.sim_assets:
                                self.sim_assets[(net, sym)]["free_local"] = max(0.0, self.sim_assets[(net, sym)]["free_local"] - swept_local)
                            self.sim_onchain[(net, sym)] = self.sim_onchain.get((net, sym), 0.0) + swept_local

                        for p in self._active_markets().keys():
                            self.last_order_refresh_time[p] = 0.0
                            self.sync_orders_for_market(p)

                        self.log_channel_action(
                            mode=mode,
                            network=net,
                            asset=sym,
                            action_type="CLOSE_CHANNEL",
                            amount=round(swept_local, 8),
                            status="COMPLETED",
                            message=msg,
                            advice_acted_on=advice_acted_on,
                            unit=sym,
                        )

                        return {
                            "status": "SUCCESS",
                            "direction": direction,
                            "action": "CLOSE_CHANNEL",
                            "network": net,
                            "asset": sym,
                            "amount": round(swept_local, 8),
                            "channel_id": target_cid,
                            "message": msg,
                            "result": res,
                        }

                # 3. RENT INBOUND CAPACITY (Liquidity Hub Lease)
                elif direction == "rent_inbound":
                    if amount <= 0:
                        return {"status": "FAILED", "error": "Rental amount must be greater than 0."}
                    act_res = self.channel_mgr.execute_capacity_action(
                        {
                            "action_type": "RENT_INBOUND",
                            "network": net,
                            "asset": sym,
                            "amount": amount,
                            "channel_id": channel_id,
                            "reason": advice_acted_on or "Manual Inbound Liquidity Request from Dashboard Bridge",
                        },
                        live=live,
                    )
                    if act_res.get("status") in ("SUCCESS", "DRY_RUN_SUCCESS"):
                        for p in self._active_markets().keys():
                            self.last_order_refresh_time[p] = 0.0
                            self.sync_orders_for_market(p)
                        msg = f"Inbound capacity lease of {act_res.get('amount', amount)} {sym} on {net} initiated (Fee: ~${act_res.get('fee', 0.0):.4f})."
                        self.log_channel_action(
                            mode=mode,
                            network=net,
                            asset=sym,
                            action_type="RENT_INBOUND",
                            amount=float(act_res.get("amount", amount)),
                            status="COMPLETED",
                            message=msg,
                            advice_acted_on=advice_acted_on,
                            unit=sym,
                        )
                        return {
                            "status": "SUCCESS",
                            "direction": direction,
                            "action": "RENT_INBOUND",
                            "network": net,
                            "asset": sym,
                            "amount": act_res.get("amount", amount),
                            "message": msg,
                            "result": act_res,
                        }
                    err_str = act_res.get("error") or act_res.get("reason") or "Failed to rent inbound capacity."
                    self.log_channel_action(
                        mode=mode,
                        network=net,
                        asset=sym,
                        action_type="RENT_INBOUND",
                        amount=float(amount),
                        status="FAILED",
                        message=err_str,
                        advice_acted_on=advice_acted_on,
                        unit=sym,
                    )
                    return {
                        "status": "FAILED",
                        "error": err_str,
                        "result": act_res,
                    }

                else:
                    return {"status": "FAILED", "error": f"Unknown transfer direction: {direction}"}

            except Exception as e:
                from channel_manager import extract_descriptive_error
                err_msg = extract_descriptive_error(e)
                logger.error(f"Bridge transfer error ({direction} {amount} {sym} on {net}): {err_msg}")
                self.log_channel_action(
                    mode=mode,
                    network=net,
                    asset=sym,
                    action_type=direction.upper(),
                    amount=float(amount),
                    status="FAILED",
                    message=err_msg,
                    advice_acted_on=advice_acted_on,
                    unit=sym,
                )
                return {"status": "FAILED", "error": err_msg}

    def set_network_mode(
        self,
        testnet: Optional[bool] = None,
        mint_url: Optional[str] = None,
        start_local_mint: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Switches between Hydranet Mainnet (Bitcoin Mainnet + Arbitrum/Ethereum)
        and Hydranet Testnet (Bitcoin Signet 0a03cf40 + Arbitrum Sepolia 421614 + Ethereum Sepolia 11155111),
        updates the active Cashu Mint URL, and optionally starts/stops the embedded Signet Cashu Mint server.
        """
        with self._lock:
            explicit_mint_url = mint_url
            if testnet is not None:
                self.config.testnet = bool(testnet)
                # Default Mint selection when toggling rail if no explicit mint_url passed
                if explicit_mint_url is None:
                    if self.config.testnet and "minibits.cash" in self.config.cashu_mint_url:
                        mint_url = "https://testnut.cashu.space"
                    elif not self.config.testnet and any(
                        k in self.config.cashu_mint_url for k in ("testnut.cashu.space", "127.0.0.1:3338")
                    ):
                        mint_url = "https://mint.minibits.cash/Bitcoin"

            if start_local_mint is True:
                st = self.cashu_bridge.start_local_mint()
                if explicit_mint_url is None:
                    mint_url = st["url"]
                self.config.testnet = True
            elif start_local_mint is False:
                self.cashu_bridge.stop_local_mint()

            if mint_url:
                self.cashu_bridge.set_mint_url(mint_url)

            # Refresh active market curves for the updated rail
            for p in list(self.target_pairs):
                self.last_order_refresh_time[p] = 0.0

            if not self.config.dry_run:
                def _bg_net_resync():
                    for p in list(self.target_pairs):
                        try:
                            self.generate_orders_for_market(p)
                        except Exception:
                            pass
                    with self._order_sync_lock:
                        for p in list(self.target_pairs):
                            try:
                                self.sync_orders_for_market(p)
                            except Exception:
                                pass
                threading.Thread(target=_bg_net_resync, daemon=True).start()
            else:
                for p in list(self.target_pairs):
                    try:
                        self.sync_orders_for_market(p)
                    except Exception:
                        pass

            rail_key = "testnet" if self.config.testnet else "mainnet"
            return {
                "status": "SUCCESS",
                "testnet": self.config.testnet,
                "rail": rail_key,
                "networks": NETWORK_RAILS[rail_key],
                "mint_url": self.cashu_bridge.cashu.mint_url,
                "local_signet_mint": self.cashu_bridge.local_signet_mint.get_status(),
            }

    def get_passive_income_summary(self) -> Dict[str, Any]:
        """Returns comprehensive passive income, maker rebate, 30d volume tier, and rental fee savings metrics."""
        cashu_saved = 0.0
        cashu_cycles = 0
        if self.cashu_bridge:
            st = self.cashu_bridge.get_status()
            m = st.get("metrics", {})
            cashu_saved = m.get("estimated_rental_fees_saved_usd", 0.0)
            cashu_cycles = m.get("total_cycles", 0)

        total_income = round(self.maker_rebates_usd + self.spread_capture_usd, 4)
        rebate_rates = {
            p: f"+{abs(cfg.maker_fee_rebate) * 100:.3f}%".rstrip("0").rstrip(".") + "%"
            for p, cfg in self._active_markets().items()
        }

        now = time.time()
        if not getattr(self, "_fee_discount_cache", None) or (now - getattr(self, "_fee_discount_cache_ts", 0.0) > 15.0):
            try:
                if not self.config.dry_run:
                    self._fee_discount_cache = self.client.get_fee_discount()
                    self._fee_discount_cache_ts = now
            except Exception as e:
                logger.debug(f"Could not fetch fee discount standing: {e}")

        fd = getattr(self, "_fee_discount_cache", None) or {}
        active_fd = fd.get("standing", {}).get("active", {})
        vol_fd = active_fd.get("volume", {})

        window_days = int(vol_fd.get("windowDays", 30) or 30)
        settled_vol = float(vol_fd.get("settledVolumeUsd", {}).get("value", 0.0) or 0.0)
        if settled_vol <= 0 and self.maker_volume_usd > 0:
            settled_vol = float(self.maker_volume_usd)
        current_tier = int(vol_fd.get("tier", 0) or 0)
        maker_disc = float(active_fd.get("makerDiscount", {}).get("value", 0.0) or 0.0)

        next_tier_obj = vol_fd.get("nextTier")
        if next_tier_obj:
            next_tier_vol = float(next_tier_obj.get("fromVolumeUsd", {}).get("value", 10000.0) or 10000.0)
            next_tier_maker_disc = float(next_tier_obj.get("makerDiscount", {}).get("value", 0.2) or 0.2)
            has_next_tier = True
        else:
            next_tier_vol = 10000.0 if current_tier == 0 else max(settled_vol, 10000.0)
            next_tier_maker_disc = 0.2 if current_tier == 0 else maker_disc
            has_next_tier = (current_tier == 0)

        vol_progress_pct = min(100.0, round((settled_vol / max(next_tier_vol, 1.0)) * 100.0, 2))

        return {
            "maker_rebates_usd": round(self.maker_rebates_usd, 4),
            "spread_capture_usd": round(self.spread_capture_usd, 4),
            "total_passive_income_usd": total_income,
            "maker_volume_usd": round(self.maker_volume_usd, 2),
            "maker_fills_count": self.maker_fills_count,
            "rental_fees_saved_usd": round(cashu_saved, 2),
            "cashu_cycles_count": cashu_cycles,
            "rebate_rates_by_pair": rebate_rates,
            "volume_30d": {
                "window_days": window_days,
                "settled_volume_usd": round(settled_vol, 2),
                "tier": current_tier,
                "maker_discount": maker_disc,
                "maker_discount_pct": round(maker_disc * 100.0, 1),
                "has_next_tier": has_next_tier,
                "next_tier": current_tier + 1 if has_next_tier else current_tier,
                "next_tier_volume_usd": round(next_tier_vol, 2),
                "next_tier_maker_discount": next_tier_maker_disc,
                "next_tier_maker_discount_pct": round(next_tier_maker_disc * 100.0, 1),
                "progress_pct": vol_progress_pct,
            },
            "recent_fills": self.recent_fills[:50],
            "income_history": self.passive_income_history[-30:],
        }

    # ==================== LIQUIDITY & SINK MANAGEMENT ====================

    def evaluate_channel_liquidity(self) -> Optional[Dict[str, Any]]:
        """
        Evaluates state channel capacity across Bitcoin (Cashu) and Arbitrum/Ethereum (Hub).
        Note: Cashu Mint/Melt continues operating on active BTC channels even while a new
        BTC channel open or rental is confirming on-chain (unless Auto Cashu Vault is off).
        """
        cashu_rec: Optional[Dict[str, Any]] = None
        cashu_init = bool(getattr(self.cashu_bridge, "initialized", False)) if getattr(self, "cashu_bridge", None) else False
        if getattr(self.config, "cashu_enabled", True) and getattr(self.config, "auto_cashu", True) and cashu_init:
            btc_channels = []
            try:
                btc_channels = self.channel_mgr.get_active_channels_by_capacity("Bitcoin")
            except Exception:
                pass
            if not btc_channels:
                single_btc = self.channel_mgr.get_active_channel("Bitcoin")
                if single_btc:
                    btc_channels = [single_btc]

            free_local_btc = 0.0
            free_remote_btc = 0.0
            if btc_channels:
                for ch in btc_channels:
                    for a in ch.assets.values():
                        free_local_btc += a.free_local
                        free_remote_btc += a.free_remote
            elif self.config.dry_run:
                btc_sim = self.sim_assets.get(("Bitcoin", "BTC"), {})
                free_local_btc = btc_sim.get("free_local", 0.0)
                free_remote_btc = btc_sim.get("free_remote", 0.0)

            btc_price = self.oracle.get_price("BTC/USDC") or 82000.0

            advice = self.cashu_bridge.evaluate_btc_channel(
                free_local_btc=free_local_btc,
                free_remote_btc=free_remote_btc,
                oracle_btc_price=btc_price,
            )

            if advice:
                action = advice.get("action")
                now_ts = time.time()
                live_cd = 180.0 if not self.config.dry_run else float(getattr(self.config, "cashu_cooldown_secs", 0.0))
                if action == "TRIGGER_SWEEP" and (now_ts - self.last_channel_action_time.get("CASHU:CASHU_MINT", 0.0) >= live_cd):
                    self.last_channel_action_time["CASHU:CASHU_MINT"] = now_ts
                    target_usd = advice.get("amount_usd", 25.0)
                    target_btc = advice.get("amount_btc")
                    sats = int(advice.get("amount_sats", 0) or round((target_btc or 0.0) * 1e8))
                    adv_text = (
                        f"BTC Receive capacity low ({free_remote_btc:.5f} BTC) — "
                        f"Advise minting {sats:,} sats to Cashu Vault to restore inbound capacity."
                    )
                    print(f"\n[CASHU] BTC channel receive depleted. Sweeping profits to Ecash Vault (~{sats:,} sats / ${target_usd:.2f} USD)...")
                    rec = self.cashu_bridge.execute_sweep(
                        amount_usd=target_usd,
                        oracle_btc_price=btc_price,
                        manual=False,
                        amount_btc=target_btc,
                    )
                    if rec.get("status") == "COMPLETED":
                        self.apply_cashu_channel_delta("SWEEP", rec.get("amount_btc", 0.0))
                        self.last_liquidity_event = rec
                    self.log_channel_action(
                        mode="AUTO",
                        network="Bitcoin",
                        asset="BTC",
                        action_type="CASHU_MINT",
                        amount=float(rec.get("amount_sats", sats)),
                        status=rec.get("status", "FAILED"),
                        message=(
                            f"Minted {rec.get('amount_sats', sats):,} sats (~${rec.get('amount_usd', target_usd):.2f}) "
                            f"from BTC Lightning channel into Cashu Vault."
                            if rec.get("status") == "COMPLETED"
                            else f"Cashu Mint failed: {rec.get('error', 'unknown')}"
                        ),
                        advice_acted_on=adv_text,
                        unit="sats",
                    )
                    cashu_rec = rec
                elif action == "TRIGGER_REFUEL" and (now_ts - self.last_channel_action_time.get("CASHU:CASHU_MELT", 0.0) >= live_cd):
                    self.last_channel_action_time["CASHU:CASHU_MELT"] = now_ts
                    target_usd = advice.get("amount_usd", 25.0)
                    target_btc = advice.get("amount_btc")
                    sats = int(advice.get("amount_sats", 0) or round((target_btc or 0.0) * 1e8))
                    adv_text = (
                        f"BTC Send capacity low ({free_local_btc:.5f} BTC) — "
                        f"Advise melting {sats:,} sats from Cashu Vault into BTC channel."
                    )
                    print(f"\n[CASHU] BTC channel send depleted. Melting Ecash to replenish send capacity (~{sats:,} sats / ${target_usd:.2f} USD)...")
                    rec = self.cashu_bridge.execute_refuel(
                        amount_usd=target_usd,
                        oracle_btc_price=btc_price,
                        manual=False,
                        amount_btc=target_btc,
                    )
                    if rec.get("status") == "COMPLETED":
                        self.apply_cashu_channel_delta("REFUEL", rec.get("amount_btc", 0.0))
                        self.last_liquidity_event = rec
                    self.log_channel_action(
                        mode="AUTO",
                        network="Bitcoin",
                        asset="BTC",
                        action_type="CASHU_MELT",
                        amount=float(rec.get("amount_sats", sats)),
                        status=rec.get("status", "FAILED"),
                        message=(
                            f"Melted {rec.get('amount_sats', sats):,} sats (~${rec.get('amount_usd', target_usd):.2f}) "
                            f"from Cashu Vault into BTC Lightning channel."
                            if rec.get("status") == "COMPLETED"
                            else f"Cashu Melt failed: {rec.get('error', 'unknown')}"
                        ),
                        advice_acted_on=adv_text,
                        unit="sats",
                    )
                    cashu_rec = rec

        # Also evaluate and execute automated channel management if enabled by user
        try:
            self.evaluate_and_execute_channel_automation()
        except Exception as e:
            logger.debug(f"Channel automation evaluation skipped: {e}")

        return cashu_rec

    def evaluate_and_execute_channel_automation(self) -> Optional[Dict[str, Any]]:
        """
        Inspects Deterministic Advisor recommendations and automatically executes them
        when the corresponding automation toggle (auto_cashu, auto_btc_channels, or auto_lithium_channels) is ON.
        """
        cashu_init = bool(getattr(self.cashu_bridge, "initialized", False)) if getattr(self, "cashu_bridge", None) else False
        auto_cashu = bool(getattr(self.config, "auto_cashu", True)) and cashu_init
        auto_btc = bool(getattr(self.config, "auto_btc_channels", False))
        auto_lithium = bool(getattr(self.config, "auto_lithium_channels", False))
        if not (auto_cashu or auto_btc or auto_lithium):
            return None

        overview = self.get_wallet_and_channels_overview()
        actions = overview.get("recommended_actions", [])
        now = time.time()
        cooldown = float(getattr(self.config, "channel_capacity_cooldown_secs", 300.0))
        cashu_cooldown = max(60.0, float(getattr(self.config, "cashu_cooldown_secs", 0.0)))

        for act in actions:
            cat = act.get("category")
            act_type = act.get("action_type")
            net = act.get("network", "")
            sym = act.get("asset", "")
            advice_text = act.get("advice_text", "")
            if not act.get("can_execute", False):
                continue

            if cat == "CASHU" and auto_cashu:
                ckey = f"CASHU:{act_type}"
                if now - self.last_channel_action_time.get(ckey, 0.0) < cashu_cooldown:
                    continue
                self.last_channel_action_time[ckey] = now
                btc_price = self.oracle.get_price("BTC/USDC") or 82000.0
                amt_btc = float(act.get("amount_btc", 0.0) or 0.0)
                amt_sats = int(act.get("amount_sats", 30000) or 30000)
                if act_type == "CASHU_MINT":
                    rec = self.cashu_bridge.execute_sweep(
                        amount_usd=round(amt_btc * btc_price, 2),
                        oracle_btc_price=btc_price,
                        manual=False,
                        amount_btc=amt_btc,
                    )
                    if rec.get("status") == "COMPLETED":
                        self.apply_cashu_channel_delta("SWEEP", rec.get("amount_btc", 0.0))
                    self.log_channel_action(
                        mode="AUTO",
                        network="Bitcoin",
                        asset="BTC",
                        action_type="CASHU_MINT",
                        amount=float(rec.get("amount_sats", amt_sats)),
                        status=rec.get("status", "FAILED"),
                        message=(
                            f"Minted {rec.get('amount_sats', amt_sats):,} sats to Cashu Vault to restore BTC inbound."
                            if rec.get("status") == "COMPLETED"
                            else f"Cashu Mint failed: {rec.get('error', 'unknown')}"
                        ),
                        advice_acted_on=advice_text,
                        unit="sats",
                    )
                    return rec
                elif act_type == "CASHU_MELT":
                    rec = self.cashu_bridge.execute_refuel(
                        amount_usd=round(amt_btc * btc_price, 2),
                        oracle_btc_price=btc_price,
                        manual=False,
                        amount_btc=amt_btc,
                    )
                    if rec.get("status") == "COMPLETED":
                        self.apply_cashu_channel_delta("REFUEL", rec.get("amount_btc", 0.0))
                    self.log_channel_action(
                        mode="AUTO",
                        network="Bitcoin",
                        asset="BTC",
                        action_type="CASHU_MELT",
                        amount=float(rec.get("amount_sats", amt_sats)),
                        status=rec.get("status", "FAILED"),
                        message=(
                            f"Melted {rec.get('amount_sats', amt_sats):,} sats from Cashu Vault to restore BTC send."
                            if rec.get("status") == "COMPLETED"
                            else f"Cashu Melt failed: {rec.get('error', 'unknown')}"
                        ),
                        advice_acted_on=advice_text,
                        unit="sats",
                    )
                    return rec

            elif cat == "BTC_CHANNEL" and auto_btc:
                ckey = f"BTC_CHANNEL:{act_type}"
                if now - self.last_channel_action_time.get(ckey, 0.0) < cooldown:
                    continue
                self.last_channel_action_time[ckey] = now
                amt = float(act.get("amount", 0.0) or 0.0)

                if act_type in ("CLOSE_AND_RENT_BTC", "CLOSE_AND_OPEN_BTC"):
                    close_cid = act.get("close_channel_id")
                    if close_cid:
                        return self.execute_bridge_transfer(
                            direction="offchain_to_onchain",
                            network_name="Bitcoin",
                            asset_symbol="BTC",
                            channel_id=close_cid,
                            action_subtype="close",
                            mode="AUTO",
                            advice_acted_on=advice_text,
                        )
                elif act_type == "RENT_INBOUND":
                    return self.execute_bridge_transfer(
                        direction="rent_inbound",
                        network_name="Bitcoin",
                        asset_symbol="BTC",
                        amount=amt,
                        mode="AUTO",
                        advice_acted_on=advice_text,
                    )
                elif act_type == "OPEN_CHANNEL":
                    return self.execute_bridge_transfer(
                        direction="onchain_to_offchain",
                        network_name="Bitcoin",
                        asset_symbol="BTC",
                        amount=amt,
                        action_subtype="open",
                        mode="AUTO",
                        advice_acted_on=advice_text,
                    )

            elif cat == "LITHIUM_CHANNEL" and auto_lithium:
                ckey = f"LITHIUM:{net}:{sym}:{act_type}"
                if now - self.last_channel_action_time.get(ckey, 0.0) < cooldown:
                    continue
                self.last_channel_action_time[ckey] = now
                amt = float(act.get("amount", 0.0) or 0.0)
                cid = act.get("channel_id")

                if act_type == "OPEN_CHANNEL":
                    return self.execute_bridge_transfer(
                        direction="onchain_to_offchain",
                        network_name=net,
                        asset_symbol=sym,
                        amount=amt,
                        action_subtype="open",
                        mode="AUTO",
                        advice_acted_on=advice_text,
                    )
                elif act_type == "DEPOSIT_CHANNEL":
                    return self.execute_bridge_transfer(
                        direction="onchain_to_offchain",
                        network_name=net,
                        asset_symbol=sym,
                        amount=amt,
                        channel_id=cid,
                        mode="AUTO",
                        advice_acted_on=advice_text,
                    )
                elif act_type == "RENT_INBOUND":
                    return self.execute_bridge_transfer(
                        direction="rent_inbound",
                        network_name=net,
                        asset_symbol=sym,
                        amount=amt,
                        channel_id=cid,
                        mode="AUTO",
                        advice_acted_on=advice_text,
                    )

        return None

    # ==================== MAIN LOOP ====================

    def run_tick(self):
        """Executes a single market making tick across all user-enabled pairs."""
        with self._order_sync_lock:
            enabled_list = list(self.target_pairs)
            for p_all in self._active_markets().keys():
                if p_all not in enabled_list and self.resting_orders.get(p_all):
                    try:
                        self.cancel_all_orders_for_pair(p_all)
                    except Exception as ce:
                        logger.debug(f"[{p_all}] Cleanup of disabled pair orders failed: {ce}")

            for pair in list(self.target_pairs):
                try:
                    self.sync_orders_for_market(pair)
                except Exception as e:
                    logger.error(f"[{pair}] Error in market making tick: {e}", exc_info=True)

            try:
                self.evaluate_channel_liquidity()
            except Exception as e:
                logger.error(f"Error evaluating channel liquidity: {e}", exc_info=True)

    def run(self, max_ticks: Optional[int] = None, interval_secs: Optional[float] = None):
        """Runs the continuous DLMM market making loop."""
        self.is_running = True
        interval = interval_secs or self.config.min_order_interval_secs
        mode_str = "[DRY-RUN]" if self.config.dry_run else "[LIVE]"
        rail_str = (
            "TESTNET (Bitcoin Signet 0a03cf40 / Arbitrum Sepolia 421614)"
            if getattr(self.config, "testnet", False)
            else "MAINNET (Bitcoin f9beb4d9 / Arbitrum 42161)"
        )
        pairs_str = ", ".join(self.target_pairs)

        print("\n" + "=" * 65)
        print("  Hydranet Pure DLMM Market Maker (Set-It-And-Forget-It)")
        print(f"  Execution Mode : {mode_str}")
        print(f"  Network Rail   : {rail_str}")
        print(f"  Active Markets : {pairs_str}")
        print(f"  Target Balance : 50% Base / 50% Quote (Auto Avellaneda-Stoikov Skew)")
        print(f"  DLMM Depth     : {self.config.rungs_per_side} rungs/side (Gaussian sigma={self.config.bell_curve_sigma})")
        print(f"  Liquidity Rail : Cashu Chaumian Ecash ({self.config.cashu_mint_url})")
        print(f"  Tick Interval  : {interval:.1f}s")
        print("=" * 65 + "\n")

        ticks = 0
        try:
            while self.is_running:
                self.run_tick()
                ticks += 1
                if max_ticks and ticks >= max_ticks:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nShutting down market maker cleanly...")
        finally:
            self.stop()

    def stop(self):
        """Stops the market maker loop and any embedded Signet Cashu Mint."""
        self.is_running = False
        if hasattr(self, "cashu_bridge") and self.cashu_bridge and hasattr(self.cashu_bridge, "local_signet_mint"):
            try:
                self.cashu_bridge.local_signet_mint.stop()
            except Exception:
                pass
