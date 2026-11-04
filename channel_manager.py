"""
Channel Manager Module for Hydranet DEX Market Maker.
Handles state channel capacity audits (Arbitrum Lithium & Bitcoin Lightning),
Liquidity Hub lease / rental fee estimation and execution, and channel lifecycle management.
"""

import re
import time
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple
from hydra_client import HydraClient, HydraRPCError
from config import (
    MARKETS,
    TESTNET_MARKETS,
    HUB_NODE_PUBKEYS,
    HUB_PEER_URLS,
    BotConfig,
    MarketConfig,
    get_network_id,
    get_hub_node_pubkey,
    get_hub_peer_url,
)

logger = logging.getLogger("ChannelManager")

# Known EVM Lithium smart contract spender addresses requiring token allowance
LITHIUM_SPENDERS: Dict[str, str] = {
    "Ethereum": "0xcc4c47cf0175aa8108e78679c2586a0a6abb242a",
    "Arbitrum": "0x895090d931660fbfc5f6bdedc24a87dd3d64202d",
}

TESTNET_LITHIUM_SPENDERS: Dict[str, str] = {
    "Ethereum": "0xcdb8b899e654199815cc52e693b4aea2e7bfc892",
    "Arbitrum": "0x7f0ca68254036d9c5e2483aae8d89785f3d3a603",
}


def extract_descriptive_error(err: Exception) -> str:
    """Extracts the most actionable error line from RPC/node error messages."""
    safe_err = str(err).encode("ascii", "replace").decode("ascii")
    lines = [l.strip() for l in safe_err.splitlines() if l.strip()]
    meaningful = []
    for l in lines:
        if any(l.startswith(prefix) for prefix in ("??at ", "at /", "├╴at /", "╰─▶", "│")):
            continue
        clean = l.lstrip("?").strip()
        if clean and clean not in ("Hydra error", "Failed to execute operations (data: None)"):
            meaningful.append(clean)
    msg = " | ".join(meaningful[-2:]) if meaningful else (lines[-1] if lines else "RPC Error")
    if "asset price not found" in msg or "Failed to quote the hub's liquidity" in msg or "Failed to compute total leased USD" in msg:
        msg += " — [Hydranet Testnet Hub Notice: The Hub's server-side price oracle is currently missing ETH/BTC price feeds on Testnet, so Hub inbound rentals are temporarily unavailable on Testnet.]"
    return msg


@dataclass
class AssetCapacity:
    symbol: str
    asset_id: str
    network_id: str
    free_local: float       # Sendable (capacity to SELL this asset or pay fees)
    free_remote: float      # Receivable (capacity to BUY this asset)
    max_sendable: float
    max_receivable: float
    is_active: bool
    status: str
    pending_local: float = 0.0
    pending_remote: float = 0.0
    onchain_in_flight: bool = False
    last_operation_timestamp: str = ""
    unspendable_local_reserve: float = 0.0
    unspendable_remote_reserve: float = 0.0
    can_send: bool = True
    can_receive: bool = True
    unavailable_local: float = 0.0
    max_withdrawable_local: float = 0.0
    withdrawing_local: float = 0.0

    @property
    def is_updating(self) -> bool:
        st = self.status.upper()
        return (
            any(k in st for k in ("UPDATING", "OPENING", "PENDING", "COOPERATIVELYOPENING"))
            or self.onchain_in_flight
            or self.pending_local > 0
            or self.pending_remote > 0
        )

    @property
    def is_usable(self) -> bool:
        """Returns True if this asset capacity can actively send or receive (either active or actively updating with usable balances)."""
        if self.is_active:
            return True
        # Dynamic splice in-flight: existing confirmed local/remote balances remain usable if not in a pending initial opening state
        if self.is_updating and "PENDING" not in self.status.upper():
            if (self.can_send and self.free_local > 0) or (self.can_receive and self.free_remote > 0):
                return True
        return False

    @property
    def has_reserve_deficit(self) -> bool:
        """Returns True if local balance is below the unspendable reserve requirement while remote capacity exists."""
        return (self.is_active or self.is_usable or self.free_remote > 0 or self.max_receivable > 0) and (self.free_local < self.unspendable_local_reserve)

    @property
    def reserve_deficit(self) -> float:
        """Returns the missing local collateral needed to satisfy the unspendable reserve requirement."""
        if self.has_reserve_deficit:
            return max(0.0, self.unspendable_local_reserve - self.free_local)
        return 0.0

    def is_operation_stale(self, max_age_seconds: Optional[float] = None) -> bool:
        """Returns True if the last on-chain/in-flight operation timestamp is older than max_age_seconds."""
        if not self.last_operation_timestamp:
            return False
        if max_age_seconds is None:
            max_age_seconds = 3600.0 if self.network_id == "f9beb4d9" else 1800.0
        try:
            dt = datetime.fromisoformat(self.last_operation_timestamp)
            now = datetime.now(timezone.utc)
            return (now - dt).total_seconds() > max_age_seconds
        except Exception:
            return False


@dataclass
class ChannelInfo:
    channel_id: str
    network_name: str
    network_id: str
    protocol: str
    counterparty: str
    status: str
    assets: Dict[str, AssetCapacity] = field(default_factory=dict)
    is_rented: bool = False
    is_closable: bool = True
    is_updatable: bool = True


@dataclass
class MarketChannelAudit:
    pair_name: str
    base_symbol: str
    quote_symbol: str
    base_network: str
    quote_network: str
    base_sendable: float
    base_receivable: float
    quote_sendable: float
    quote_receivable: float
    can_place_bids: bool   # Need quote_sendable > quote_min and base_receivable > base_min
    can_place_asks: bool   # Need base_sendable > base_min and quote_receivable > quote_min
    status_summary: str
    recommended_action: Optional[str] = None
    base_free_sendable: float = 0.0
    quote_free_sendable: float = 0.0
    can_place_3_bids: bool = True
    can_place_3_asks: bool = True
    zombie_orders_count: int = 0
    zombie_locked_recv: float = 0.0
    bids_constrained_by: Optional[Dict[str, Any]] = None
    asks_constrained_by: Optional[Dict[str, Any]] = None
    limiting_constraint: Optional[str] = None


class ChannelManager:
    """Manages channel monitoring, rental quotes, and channel provisioning."""

    def __init__(self, client: HydraClient, bot_config: Optional[BotConfig] = None):
        self.client = client
        self.config = bot_config or BotConfig()
        self._pending_rentals: Dict[str, float] = {}
        self._pending_opens: Dict[str, float] = {}
        self._pending_closed: Dict[str, float] = {}

    def _net_id(self, network_name: str) -> str:
        """Resolves networkId for Mainnet (f9beb4d9/42161/1) or Testnet (0a03cf40/421614/11155111)."""
        return get_network_id(network_name, testnet=getattr(self.config, "testnet", False))

    def is_channel_closing_or_closed(self, ch: ChannelInfo) -> bool:
        """
        Returns True if the channel is closing, waiting confirmation, cooperatively closing,
        closed, or marked as recently closed.
        """
        cid = getattr(ch, "channel_id", "")
        if cid and cid in self._pending_closed:
            if time.time() - self._pending_closed[cid] < 1800.0:
                return True
            else:
                self._pending_closed.pop(cid, None)

        ch_st = str(getattr(ch, "status", "")).upper()
        if any(k in ch_st for k in ("CLOSING", "CLOSED", "WAITINGCLOSECONFIRMATION", "COOPERATIVELYCLOSING")):
            return True

        for a in getattr(ch, "assets", {}).values():
            a_st = str(getattr(a, "status", "")).upper()
            if any(k in a_st for k in ("CLOSING", "CLOSED", "WAITINGCLOSECONFIRMATION", "COOPERATIVELYCLOSING")):
                return True

        return False

    def has_pending_rental(self, network_name: str = "Bitcoin", asset_symbol: str = "BTC") -> bool:
        """Returns True if a liquidity rental was initiated and is still in-flight/pending confirmation."""
        rent_key = f"{network_name}:{asset_symbol}"
        pending_timeout = getattr(self.config, "pending_rental_timeout_secs", 1800.0)
        last_req_time = self._pending_rentals.get(rent_key, 0.0)
        if last_req_time > 0 and (time.time() - last_req_time) < pending_timeout:
            return True
        return False

    def has_pending_open(self, network_name: str = "Bitcoin", asset_symbol: str = "BTC") -> bool:
        """Returns True if an on-chain channel open was initiated recently and is pending confirmation."""
        open_key = f"{network_name}:{asset_symbol}"
        pending_timeout = getattr(self.config, "pending_open_timeout_secs", 1800.0)
        last_open_time = self._pending_opens.get(open_key, 0.0)
        if last_open_time > 0 and (time.time() - last_open_time) < pending_timeout:
            return True
        return False

    def has_pending_channel_update(self, network_name: str = "Bitcoin", asset_symbol: str = "BTC") -> Dict[str, Any]:
        """
        Returns whether a channel open, splice update, or liquidity rental
        is currently in-flight or pending update for the specified network/asset.
        """
        now = time.time()

        # 1. Check in-memory pending rental
        rent_key = f"{network_name}:{asset_symbol}"
        pending_rent_timeout = getattr(self.config, "pending_rental_timeout_secs", 1800.0)
        last_rent_time = self._pending_rentals.get(rent_key, 0.0)
        if last_rent_time > 0:
            elapsed = now - last_rent_time
            if elapsed < pending_rent_timeout:
                return {
                    "pending": True,
                    "type": "PENDING_RENTAL",
                    "reason": f"A liquidity rental for {asset_symbol} on {network_name} was initiated {int(elapsed)}s ago and is still pending/confirming ({int(pending_rent_timeout - elapsed)}s remaining).",
                    "elapsed_secs": elapsed,
                }

        # 2. Check in-memory pending channel open
        open_key = f"{network_name}:{asset_symbol}"
        pending_open_timeout = getattr(self.config, "pending_open_timeout_secs", 1800.0)
        last_open_time = self._pending_opens.get(open_key, 0.0)
        if last_open_time > 0:
            elapsed = now - last_open_time
            if elapsed < pending_open_timeout:
                return {
                    "pending": True,
                    "type": "PENDING_OPEN",
                    "reason": f"An on-chain channel open for {asset_symbol} on {network_name} was initiated {int(elapsed)}s ago and is pending confirmation ({int(pending_open_timeout - elapsed)}s remaining).",
                    "elapsed_secs": elapsed,
                }

        # 3. Check channels on the node for updating / opening / pending states
        try:
            proto = "PROTOCOL_BITCOIN" if network_name == "Bitcoin" else "PROTOCOL_EVM"
            nid = self._net_id(network_name)
            channels = self.get_channels_for_network(proto, nid, network_name)
            for ch in channels:
                raw_st = str(ch.status).upper()
                if any(k in raw_st for k in ("UPDATING", "OPENING", "PENDING", "COOPERATIVELYOPENING")):
                    return {
                        "pending": True,
                        "type": "CHANNEL_STATUS_" + str(ch.status),
                        "channel_id": ch.channel_id,
                        "reason": f"Channel {ch.channel_id[:12]}... on {network_name} has status {ch.status}.",
                    }

                for a in ch.assets.values():
                    sym_match = (
                        a.symbol == asset_symbol
                        or asset_symbol == "*"
                        or (asset_symbol == "BTC" and (a.network_id in ("f9beb4d9", "0a03cf40") or a.symbol in ("BTC", "0x000000")))
                    )
                    if sym_match:
                        if a.onchain_in_flight:
                            return {
                                "pending": True,
                                "type": "ONCHAIN_IN_FLIGHT",
                                "channel_id": ch.channel_id,
                                "reason": f"Channel {ch.channel_id[:12]}... on {network_name} ({a.symbol}) has an on-chain operation in flight.",
                            }
                        ast = str(a.status).upper()
                        if any(k in ast for k in ("UPDATING", "OPENING", "PENDING", "COOPERATIVELYOPENING")):
                            return {
                                "pending": True,
                                "type": "ASSET_STATUS_" + str(a.status),
                                "channel_id": ch.channel_id,
                                "reason": f"Channel {ch.channel_id[:12]}... on {network_name} ({a.symbol}) has asset status {a.status}.",
                            }
                        if (a.pending_local > 0 or a.pending_remote > 0) and not a.is_operation_stale():
                            return {
                                "pending": True,
                                "type": "PENDING_BALANCES",
                                "channel_id": ch.channel_id,
                                "reason": f"Channel {ch.channel_id[:12]}... on {network_name} ({a.symbol}) has pending balances (local: {a.pending_local:.8f}, remote: {a.pending_remote:.8f}).",
                            }
        except Exception as e:
            logger.debug(f"Error checking pending channel updates: {e}")

        return {"pending": False}

    def get_fiat_price(self, symbol: str) -> float:
        """Retrieves asset fiat price from hydra-app oracle, falling back to oracle cache."""
        if hasattr(self, "client") and self.client:
            try:
                p = self.client.get_fiat_price(symbol)
                if isinstance(p, (int, float)) and p > 0:
                    return float(p)
                elif p is not None and not type(p).__name__.startswith("MagicMock"):
                    fp = float(p)
                    if fp > 0:
                        return fp
            except Exception:
                pass
            cache_obj = getattr(self.client, "_oracle_price_cache", None)
            if isinstance(cache_obj, dict):
                cached = cache_obj.get(symbol)
                if isinstance(cached, (int, float)) and cached > 0:
                    return float(cached)
        return 1.0 if symbol == "USDC" else (2750.0 if symbol == "ETH" else 86000.0 if symbol == "BTC" else 0.027 if symbol == "HDN" else 1.0)

    def get_channels_for_network(self, protocol: str, network_id: str, network_name: str) -> List[ChannelInfo]:
        """Fetches and parses channels for a given protocol and network."""
        try:
            raw_channels = self.client.get_channels(protocol, network_id)
        except Exception as e:
            if "fast-fail circuit breaker" not in str(e) and not getattr(self.config, "dry_run", False):
                logger.warning(f"Failed to fetch channels for {network_name} ({network_id}): {e}")
            return []

        channels = []
        for ch in raw_channels:
            ch_id = ch.get("id", "")
            counterparty = ch.get("counterparty", "")
            status = ch.get("status", "UNKNOWN")
            asset_channels = ch.get("assetChannels", {})
            
            is_rented = False
            assets = {}
            for aid, data in asset_channels.items():
                if data.get("leaseExpiry") or data.get("liquidityExpiry"):
                    is_rented = True
                bal = data.get("balance", {})
                free_local = float(bal.get("freeLocal", {}).get("value", 0.0))
                free_remote = float(bal.get("freeRemote", {}).get("value", 0.0))
                pending_local = float(bal.get("pendingLocal", {}).get("value", 0.0))
                pending_remote = float(bal.get("pendingRemote", {}).get("value", 0.0))
                max_send = float(bal.get("maxSendable", {}).get("value", free_local))
                max_recv = float(bal.get("maxReceivable", {}).get("value", free_remote))
                unspendable_local = float(bal.get("unspendableLocalReserve", {}).get("value", 0.0))
                unspendable_remote = float(bal.get("unspendableRemoteReserve", {}).get("value", 0.0))
                unavailable_local = float(bal.get("unavailableLocal", {}).get("value", 0.0))
                max_withdrawable_local = float(bal.get("maxWithdrawableLocal", {}).get("value", max_send))
                withdrawing_local = float(bal.get("withdrawingLocal", {}).get("value", 0.0))
                onchain_op = bool(data.get("onchainOperationInFlight", False))
                st = data.get("status", {})
                is_active = "active" in st
                status_str = "ACTIVE" if is_active else list(st.keys())[0] if st else "INACTIVE"
                can_send = bool(data.get("canSend", is_active))
                can_receive = bool(data.get("canReceive", is_active))

                assets[aid] = AssetCapacity(
                    symbol=self._lookup_symbol(network_id, aid),
                    asset_id=aid,
                    network_id=network_id,
                    free_local=free_local,
                    free_remote=free_remote,
                    max_sendable=max_send,
                    max_receivable=max_recv,
                    is_active=is_active,
                    status=status_str,
                    pending_local=pending_local,
                    pending_remote=pending_remote,
                    onchain_in_flight=onchain_op,
                    last_operation_timestamp=data.get("lastOperationTimestamp", ""),
                    unspendable_local_reserve=unspendable_local,
                    unspendable_remote_reserve=unspendable_remote,
                    can_send=can_send,
                    can_receive=can_receive,
                    unavailable_local=unavailable_local,
                    max_withdrawable_local=max_withdrawable_local,
                    withdrawing_local=withdrawing_local,
                )

            channels.append(ChannelInfo(
                channel_id=ch_id,
                network_name=network_name,
                network_id=network_id,
                protocol=protocol,
                counterparty=counterparty,
                status=status,
                assets=assets,
                is_rented=is_rented,
                is_closable=bool(ch.get("isClosable", True)),
                is_updatable=bool(ch.get("isUpdatable", True)),
            ))
        return channels

    def get_active_channel(self, network_name: str) -> Optional[ChannelInfo]:
        """Returns the active payment channel for a given network name, if one exists."""
        active = self.get_active_channels_by_capacity(network_name)
        if active:
            return active[-1]
        proto = "PROTOCOL_BITCOIN" if network_name == "Bitcoin" else "PROTOCOL_EVM"
        nid = self._net_id(network_name)
        channels = self.get_channels_for_network(proto, nid, network_name)
        non_closing = [ch for ch in channels if not self.is_channel_closing_or_closed(ch)]
        return non_closing[0] if non_closing else None

    def get_primary_lithium_channel(
        self,
        network_name: str,
        required_asset_id: Optional[str] = None,
    ) -> Optional[ChannelInfo]:
        """
        Returns the single consolidated primary Lithium channel for an EVM network (Arbitrum or Ethereum).
        Lithium channels are multi-token state channels capable of holding ETH, USDC, and HDN simultaneously.
        Concentrating all liquidity and rentals onto a single well-used channel retains rental capacity
        because the Hub liquidity service avoids force-closing expired rentals on active, well-used channels.
        
        Selection criteria:
        1. Non-closing, non-closed active/updating channel.
        2. Channel where required_asset_id (if provided) is active or present (not force-closed).
        3. Channels carrying multiple traded assets (e.g. USDC, HDN, ETH) prioritized.
        4. Highest total channel capacity (local + remote balance across all assets).
        """
        if network_name not in ("Arbitrum", "Ethereum"):
            return None

        proto = "PROTOCOL_EVM"
        nid = self._net_id(network_name)
        channels = self.get_channels_for_network(proto, nid, network_name)

        active_statuses = ("CHANNEL_STATUS_ACTIVE", "CHANNEL_STATUS_UPDATING", "active", "updating")
        candidates = [
            ch for ch in channels
            if not self.is_channel_closing_or_closed(ch)
            and (ch.status in active_statuses or any(a.is_active or getattr(a, "is_usable", False) for a in ch.assets.values()))
        ]

        if not candidates:
            return None

        def _score_channel(ch: ChannelInfo) -> Tuple[int, int, float, float]:
            has_req = 1
            if required_asset_id:
                has_req = 0
                for aid, a in ch.assets.items():
                    is_match = (
                        aid.lower() == required_asset_id.lower() or (
                            required_asset_id.startswith("0x0000000000000000000000000000000000000000") and
                            aid.startswith("0x0000000000000000000000000000000000000000")
                        )
                    )
                    if is_match and str(a.status).lower() not in ("closed", "closing", "force_closed"):
                        has_req = 1
                        break

            active_assets_count = sum(1 for a in ch.assets.values() if a.is_active or getattr(a, "is_usable", False))

            total_cap_usd = 0.0
            total_remote_usd = 0.0
            for aid, a in ch.assets.items():
                p = self.get_fiat_price(a.symbol)
                tot = (a.free_local + a.free_remote) * p
                rem = a.free_remote * p
                total_cap_usd += tot
                total_remote_usd += rem

            return (has_req, active_assets_count, total_cap_usd, total_remote_usd)

        candidates.sort(key=_score_channel, reverse=True)
        return candidates[0]


    def _lookup_symbol(self, network_id: str, asset_id: str) -> str:
        """Helper to resolve symbol from known Mainnet or Testnet market configs."""
        for m_map in (MARKETS, TESTNET_MARKETS):
            for m in m_map.values():
                if m.base.network_id == network_id and m.base.asset_id.lower() == asset_id.lower():
                    return m.base.symbol
                if m.quote.network_id == network_id and m.quote.asset_id.lower() == asset_id.lower():
                    return m.quote.symbol
        from config import TESTNET_TOKEN_ASSETS
        for (_net_name, sym), aid in TESTNET_TOKEN_ASSETS.items():
            if aid.lower() == asset_id.lower():
                return sym
        if asset_id == "0x0000000000000000000000000000000000000000" or asset_id.replace("0", "").lower() in ("0x", ""):
            return "BTC" if network_id in ("f9beb4d9", "0a03cf40") else "ETH"
        return asset_id[:8]

    def _get_bot_total_equity(self) -> Optional[float]:
        """Queries the GMX Agent bot API for total portfolio equity (including locked GMX collateral).
        Caches the response for 10s (with a 60s stale fallback) to ensure ultra-fast, resilient execution.
        """
        import time
        now = time.time()
        cached_eq = getattr(self, "_cached_bot_equity", None)
        cached_time = getattr(self, "_cached_bot_equity_time", 0.0)
        if cached_eq is not None and (now - cached_time) < 10.0:
            return cached_eq
        try:
            import urllib.request, json
            req = urllib.request.Request("http://127.0.0.1:8090/api/status", headers={"User-Agent": "HydraAgent"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode())
                eq = float(data.get("account", {}).get("total_equity", 0.0))
                if eq > 0:
                    self._cached_bot_equity = eq
                    self._cached_bot_equity_time = now
                    return eq
        except Exception:
            pass
        if cached_eq is not None and (now - cached_time) < 60.0:
            return cached_eq
        return None

    def _calculate_usable_usdc(self, onchain_val: float, usdc_reserve: float) -> float:
        """Calculates usable on-chain USDC while ensuring GMX Agent bot total equity stays >= usdc_reserve (e.g. $1030)."""
        if usdc_reserve <= 0:
            return onchain_val
        total_equity = self._get_bot_total_equity()
        if total_equity is not None and total_equity > 0:
            # Total equity includes locked collateral + wallet cash + unPnL
            # Headroom is what can be safely removed before total equity drops below threshold
            headroom = max(0.0, total_equity - usdc_reserve)
            return min(onchain_val, headroom)
        # Fallback: if bot API not running, enforce directly on on-chain wallet balance
        return max(0.0, onchain_val - usdc_reserve)


    def audit_all_channels(self, target_pairs: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Audits channels across Arbitrum, Bitcoin, and Ethereum, and maps capacities
        against the quoting requirements of target markets.
        """
        active_markets = TESTNET_MARKETS if getattr(self.config, "testnet", False) else MARKETS
        pairs = target_pairs or list(active_markets.keys())

        arb_nid = self._net_id("Arbitrum")
        btc_nid = self._net_id("Bitcoin")
        eth_nid = self._net_id("Ethereum")

        # 1. Fetch channel lists
        arb_channels = self.get_channels_for_network("PROTOCOL_EVM", arb_nid, "Arbitrum")
        btc_channels = self.get_channels_for_network("PROTOCOL_BITCOIN", btc_nid, "Bitcoin")
        eth_channels = self.get_channels_for_network("PROTOCOL_EVM", eth_nid, "Ethereum")

        all_channels = {
            "Arbitrum": arb_channels,
            "Bitcoin": btc_channels,
            "Ethereum": eth_channels,
        }

        # 1b. Check for channel reserve deficits (free local < unspendable local reserve)
        reserve_deficits = []
        for net_name, ch_list in all_channels.items():
            for ch in ch_list:
                for aid, cap in ch.assets.items():
                    if cap.has_reserve_deficit:
                        deficit = cap.reserve_deficit
                        reserve_deficits.append({
                            "channel_id": ch.channel_id,
                            "network_name": net_name,
                            "network_id": ch.network_id,
                            "protocol": ch.protocol,
                            "asset_id": aid,
                            "symbol": cap.symbol,
                            "free_local": cap.free_local,
                            "unspendable_local_reserve": cap.unspendable_local_reserve,
                            "deficit": deficit,
                            "target_topup": round(deficit * 1.20, 6 if cap.symbol in ("BTC", "ETH") else 2),
                        })

        # 1c. Check for zombie orders (pendingCancel > 15m)
        zombie_orders = []
        try:
            raw_own = self.client.get_all_own_orders()
            now_dt = datetime.now(timezone.utc)
            order_dict = raw_own if isinstance(raw_own, dict) else {o.get("id", str(i)): o for i, o in enumerate(raw_own)} if isinstance(raw_own, list) else {}
            for oid, o in order_dict.items():
                if o.get("pendingCancel"):
                    lim = o.get("pairOrder", {}).get("limitOrder", {})
                    created_at_str = lim.get("createdAt") or o.get("createdAt") or o.get("creationTime")
                    age_minutes = 0.0
                    if created_at_str:
                        try:
                            clean_ts = str(created_at_str)
                            if "." in clean_ts and "+" in clean_ts:
                                dot_idx = clean_ts.index(".")
                                plus_idx = clean_ts.index("+")
                                clean_ts = clean_ts[:dot_idx + 7] + clean_ts[plus_idx:]
                            dt = datetime.fromisoformat(clean_ts)
                            age_minutes = (now_dt - dt).total_seconds() / 60.0
                        except Exception:
                            age_minutes = 999.0
                    if age_minutes >= 15.0 or not created_at_str:
                        base_info = lim.get("base", {})
                        quote_info = lim.get("quote", {})
                        rem_base = float(lim.get("remainingAmount", {}).get("base", {}).get("amount", {}).get("value", 0.0))
                        p_val = float(lim.get("price", {}).get("value", 0.0))
                        variant = lim.get("variant", {})
                        side = "BUY" if "buy" in variant else "SELL" if "sell" in variant else "UNKNOWN"
                        zombie_orders.append({
                            "order_id": oid,
                            "client_order_id": o.get("clientOrderId", ""),
                            "side": side,
                            "price": p_val,
                            "remaining_base": rem_base,
                            "base_symbol": self._lookup_symbol(base_info.get("networkId", ""), base_info.get("assetId", "")),
                            "base_network_id": base_info.get("networkId"),
                            "base_asset_id": base_info.get("assetId"),
                            "quote_network_id": quote_info.get("networkId"),
                            "quote_asset_id": quote_info.get("assetId"),
                            "age_minutes": round(age_minutes, 1),
                            "created_at": created_at_str,
                        })
        except Exception as e:
            logger.debug(f"Error checking zombie orders in audit: {e}")

        # 2. Fetch on-chain balances for funding readiness & compute usable balance after gas reserve
        onchain_balances = {}
        usable_onchain_balances = {}
        eth_price = None

        for net_name, (proto, nid) in {
            "Arbitrum": ("PROTOCOL_EVM", arb_nid),
            "Bitcoin": ("PROTOCOL_BITCOIN", btc_nid),
            "Ethereum": ("PROTOCOL_EVM", eth_nid),
        }.items():
            try:
                bals = self.client.get_wallet_balances(proto, nid)
                net_bals = {}
                usable_bals = {}
                reserve_usd = self.config.min_onchain_gas_reserve_usd.get(net_name, 0.0)

                for aid, val_map in bals.items():
                    onchain_val = float(val_map.get("onchain", {}).get("confirmed", {}).get("value", 0.0))
                    if onchain_val > 0:
                        sym = self._lookup_symbol(nid, aid)
                        net_bals[sym] = onchain_val

                        # Apply gas reserve deduction for native ETH on EVM networks
                        usdc_reserve = getattr(self.config, "min_onchain_usdc_reserve_usd", 1030.0)
                        if sym == "ETH" and reserve_usd > 0:
                            if eth_price is None:
                                eth_price = self.get_fiat_price("ETH")
                            min_res_eth = (reserve_usd / eth_price) if eth_price > 0 else 0.0
                            usable_val = max(0.0, onchain_val - min_res_eth)
                            usable_bals[sym] = usable_val
                        elif sym == "USDC" and net_name == "Arbitrum" and usdc_reserve > 0:
                            usable_val = self._calculate_usable_usdc(onchain_val, usdc_reserve)
                            usable_bals[sym] = usable_val
                        else:
                            usable_bals[sym] = onchain_val

                onchain_balances[net_name] = net_bals
                usable_onchain_balances[net_name] = usable_bals
            except Exception:
                onchain_balances[net_name] = {}
                usable_onchain_balances[net_name] = {}

        # 3. Aggregate sendable/receivable capacity by (network_id, asset_id)
        capacity_map: Dict[tuple, Dict[str, float]] = {}
        for net_name, ch_list in all_channels.items():
            for ch in ch_list:
                for aid, cap in ch.assets.items():
                    key = (ch.network_id, aid.lower())
                    if key not in capacity_map:
                        capacity_map[key] = {"sendable": 0.0, "receivable": 0.0}
                    if cap.is_active or getattr(cap, "is_usable", False):
                        capacity_map[key]["sendable"] += cap.max_sendable
                        capacity_map[key]["receivable"] += cap.max_receivable

        # Fetch live orderbook usable balances (free sendable vs total in-use)
        ob_bals: Dict[tuple, float] = {}
        ob_free_bals: Dict[tuple, float] = {}
        try:
            raw_ob = self.client.get_orderbook_balances()
            for item in raw_ob:
                curr = item.get("currency", {})
                aid = curr.get("assetId", "").lower()
                nid = curr.get("networkId", "")
                bal = item.get("balance", {})
                s = float(bal.get("sending", {}).get("value", 0.0))
                in_use = float(bal.get("inUseSending", {}).get("value", 0.0))
                ob_bals[(nid, aid)] = s + in_use
                ob_free_bals[(nid, aid)] = s
        except Exception:
            ob_bals = {}
            ob_free_bals = {}

        # 4. Audit each market
        market_audits: Dict[str, MarketChannelAudit] = {}
        for pair_name in pairs:
            if pair_name not in active_markets:
                continue
            m = active_markets[pair_name]
            base_key = (m.base.network_id, m.base.asset_id.lower())
            quote_key = (m.quote.network_id, m.quote.asset_id.lower())

            base_cap = capacity_map.get(base_key, {"sendable": 0.0, "receivable": 0.0})
            quote_cap = capacity_map.get(quote_key, {"sendable": 0.0, "receivable": 0.0})

            # Prefer live orderbook sending balance if present (accounts for reservations/bonds)
            b_send = ob_bals.get(base_key, base_cap["sendable"])
            b_send_free = ob_free_bals.get(base_key, base_cap["sendable"])
            b_recv = base_cap["receivable"]
            q_send = ob_bals.get(quote_key, quote_cap["sendable"])
            q_send_free = ob_free_bals.get(quote_key, quote_cap["sendable"])
            q_recv = quote_cap["receivable"]

            min_b = m.base.min_amount
            min_q = m.quote.min_amount

            # Ensure send capacity also satisfies Hub minimum placement floor ($0.75 USD) and 90% budget allocation
            min_floor_usd = getattr(self.config, "hub_min_placement_floor_usd", 0.75)
            base_fiat = self.get_fiat_price(m.base.symbol)
            quote_fiat = self.get_fiat_price(m.quote.symbol)

            target_rungs = getattr(self.config, "rungs_per_side", 15)
            # Price-aware sizing:
            # When quoting Bids (buying Base, spending Quote):
            # Quote sendable needed is max(min_q, min_b * price). Base receivable needed is max(min_b, min_q / price).
            # When quoting Asks (selling Base, receiving Quote):
            # Base sendable needed is max(min_b, min_q / price). Quote receivable needed is max(min_q, min_b * price).
            center_price = (base_fiat / quote_fiat) if quote_fiat > 0 else 1.0
            min_b_rung = max(min_b, (min_floor_usd / base_fiat) if base_fiat > 0 else min_b)
            min_q_rung = max(min_q, (min_floor_usd / quote_fiat) if quote_fiat > 0 else min_q)

            req_b_base = max(min_b_rung, (min_q_rung / center_price) if center_price > 0 else 0.0)
            req_q_base = max(min_q_rung, (min_b_rung * center_price) if center_price > 0 else 0.0)

            # Minimum required to place a single rung
            req_b_send_1 = req_b_base
            req_q_send_1 = req_q_base
            req_b_recv_1 = req_b_base
            req_q_recv_1 = req_q_base

            # Requirements for a full ladder on each side
            req_b_send_3 = req_b_send_1 * target_rungs
            req_q_send_3 = req_q_send_1 * target_rungs
            req_b_recv_3 = req_b_recv_1 * target_rungs
            req_q_recv_3 = req_q_recv_1 * target_rungs

            # Check if channel has headroom to place at least 1 order
            can_bid_1 = ((q_send * 0.90) >= req_q_send_1) and (b_recv >= req_b_recv_1)
            can_ask_1 = ((b_send * 0.90) >= req_b_send_1) and (q_recv >= req_q_recv_1)

            # Detailed constraint capture for Bids (Buying base with quote)
            has_bid_q_send = ((q_send * 0.90) >= req_q_send_3)
            has_bid_b_recv = (b_recv >= req_b_recv_3)
            can_bid_3 = has_bid_q_send and has_bid_b_recv

            bids_constrained_by = None
            if not can_bid_3:
                deficit_q = max(0.0, req_q_send_3 - (q_send * 0.90))
                deficit_b = max(0.0, req_b_recv_3 - b_recv)
                deficit_q_usd = round(deficit_q * quote_fiat, 2)
                deficit_b_usd = round(deficit_b * base_fiat, 2)
                status_qualifier = "0 Bids placed" if not can_bid_1 else f"Partial Bids placed"

                if not has_bid_q_send and has_bid_b_recv:
                    bids_constrained_by = {
                        "side": "BIDS",
                        "asset": m.quote.symbol,
                        "network": m.quote.network_name,
                        "type": "SEND",
                        "deficit": round(deficit_q, 6),
                        "deficit_usd": deficit_q_usd,
                        "reason": f"Insufficient {m.quote.network_name} {m.quote.symbol} send capacity: {status_qualifier} (needs {deficit_q:.4f} {m.quote.symbol} [~${deficit_q_usd:.2f}] for {target_rungs} Bids)"
                    }
                elif not has_bid_b_recv and has_bid_q_send:
                    bids_constrained_by = {
                        "side": "BIDS",
                        "asset": m.base.symbol,
                        "network": m.base.network_name,
                        "type": "RECV",
                        "deficit": round(deficit_b, 6),
                        "deficit_usd": deficit_b_usd,
                        "reason": f"Insufficient {m.base.network_name} {m.base.symbol} receive capacity: {status_qualifier} (needs {deficit_b:.6f} {m.base.symbol} [~${deficit_b_usd:.2f}] for {target_rungs} Bids)"
                    }
                else:
                    bids_constrained_by = {
                        "side": "BIDS",
                        "asset": f"{m.quote.symbol}+{m.base.symbol}",
                        "network": "BOTH",
                        "type": "BOTH",
                        "deficit": round(max(deficit_q, deficit_b), 6),
                        "deficit_usd": max(deficit_q_usd, deficit_b_usd),
                        "reason": f"Both {m.quote.symbol} send (~${deficit_q_usd:.2f}) and {m.base.symbol} receive (~${deficit_b_usd:.2f}) capacities constrained ({status_qualifier})"
                    }

            # Detailed constraint capture for Asks (Selling base for quote)
            has_ask_b_send = ((b_send * 0.90) >= req_b_send_3)
            has_ask_q_recv = (q_recv >= req_q_recv_3)
            can_ask_3 = has_ask_b_send and has_ask_q_recv

            asks_constrained_by = None
            if not can_ask_3:
                deficit_b = max(0.0, req_b_send_3 - (b_send * 0.90))
                deficit_q = max(0.0, req_q_recv_3 - q_recv)
                deficit_b_usd = round(deficit_b * base_fiat, 2)
                deficit_q_usd = round(deficit_q * quote_fiat, 2)
                status_qualifier = "0 Asks placed" if not can_ask_1 else f"Partial Asks placed"

                if not has_ask_b_send and has_ask_q_recv:
                    asks_constrained_by = {
                        "side": "ASKS",
                        "asset": m.base.symbol,
                        "network": m.base.network_name,
                        "type": "SEND",
                        "deficit": round(deficit_b, 6),
                        "deficit_usd": deficit_b_usd,
                        "reason": f"Insufficient {m.base.network_name} {m.base.symbol} send capacity: {status_qualifier} (needs {deficit_b:.6f} {m.base.symbol} [~${deficit_b_usd:.2f}] for {target_rungs} Asks)"
                    }
                elif not has_ask_q_recv and has_ask_b_send:
                    asks_constrained_by = {
                        "side": "ASKS",
                        "asset": m.quote.symbol,
                        "network": m.quote.network_name,
                        "type": "RECV",
                        "deficit": round(deficit_q, 6),
                        "deficit_usd": deficit_q_usd,
                        "reason": f"Insufficient {m.quote.network_name} {m.quote.symbol} receive capacity: {status_qualifier} (needs {deficit_q:.4f} {m.quote.symbol} [~${deficit_q_usd:.2f}] for {target_rungs} Asks)"
                    }
                else:
                    asks_constrained_by = {
                        "side": "ASKS",
                        "asset": f"{m.base.symbol}+{m.quote.symbol}",
                        "network": "BOTH",
                        "type": "BOTH",
                        "deficit": round(max(deficit_b, deficit_q), 6),
                        "deficit_usd": max(deficit_b_usd, deficit_q_usd),
                        "reason": f"Both {m.base.symbol} send (~${deficit_b_usd:.2f}) and {m.quote.symbol} receive (~${deficit_q_usd:.2f}) capacities constrained ({status_qualifier})"
                    }

            # Build human-readable limiting constraint descriptor
            constraint_parts = []
            if bids_constrained_by:
                constraint_parts.append(f"Bids: {bids_constrained_by['asset']} {bids_constrained_by['type']}")
            if asks_constrained_by:
                constraint_parts.append(f"Asks: {asks_constrained_by['asset']} {asks_constrained_by['type']}")
            limiting_constraint = " • ".join(constraint_parts) if constraint_parts else None

            can_bid = can_bid_1
            can_ask = can_ask_1

            rec_action = None
            if can_bid_3 and can_ask_3:
                summary = "READY (Two-Way Liquidity Available)"
            elif can_bid and not can_ask:
                summary = f"DEGRADED (One-Sided: Bids Only [{limiting_constraint or 'Asks Suspended'}])"
                if asks_constrained_by:
                    rec_action = f"{asks_constrained_by['reason']}. Quoting one-sided Bids to accumulate {m.base.symbol} on fills."
                elif (b_send * 0.90) < req_b_send_1:
                    deficit_b = req_b_send_3 - (b_send * 0.90)
                    rec_action = f"Seed {m.base.symbol} channel (needs {deficit_b:.4f} {m.base.symbol} for {target_rungs} Asks). Quoting one-sided Bids to accumulate {m.base.symbol} on fills."
                elif q_recv < req_q_recv_1:
                    rec_action = f"Rent {m.quote.symbol} inbound capacity to enable Asks"
                else:
                    rec_action = f"Quoting one-sided maker Bids to organically acquire {m.base.symbol} without taker fees."
            elif not can_bid and can_ask:
                summary = f"DEGRADED (One-Sided: Asks Only [{limiting_constraint or 'Bids Suspended'}])"
                if bids_constrained_by:
                    rec_action = f"{bids_constrained_by['reason']}. Quoting one-sided Asks to accumulate {m.quote.symbol} on fills."
                elif (q_send * 0.90) < req_q_send_1:
                    deficit_q = req_q_send_3 - (q_send * 0.90)
                    rec_action = f"Seed {m.quote.symbol} channel (needs {deficit_q:.4f} {m.quote.symbol} for {target_rungs} Bids). Quoting one-sided Asks to accumulate {m.quote.symbol} on fills."
                elif b_recv < req_b_recv_1:
                    rec_action = f"Rent {m.base.symbol} inbound capacity to enable Bids"
                else:
                    rec_action = f"Quoting one-sided maker Asks to organically acquire {m.quote.symbol} without taker fees."
            elif can_bid_1 and can_ask_1:
                summary = f"DEGRADED (Partial Depth: {limiting_constraint or f'Headroom Below {target_rungs} Rungs'})"
                reasons = []
                if bids_constrained_by:
                    reasons.append(bids_constrained_by["reason"])
                if asks_constrained_by:
                    reasons.append(asks_constrained_by["reason"])
                rec_action = " • ".join(reasons) if reasons else f"Channel capacity allows partial quoting. Increase balances or inbound capacity for full {target_rungs}-rung ladder on both sides."
            else:
                def _get_asset_lifecycle(net_name: str, target_aid: str):
                    net_channels = all_channels.get(net_name, [])
                    if not net_channels:
                        return "NO_CHANNELS"
                    for ch in net_channels:
                        for aid, a in ch.assets.items():
                            is_match = (
                                aid.lower() == target_aid.lower() or
                                (target_aid.startswith("0x0000000000000000000000000000000000000000") and
                                 aid.startswith("0x0000000000000000000000000000000000000000"))
                            )
                            if is_match:
                                timeout_secs = getattr(self.config, "max_inflight_operation_timeout_secs", 600.0)
                                if (a.status in ("cooperativelyOpening", "opening") or a.onchain_in_flight or a.pending_local > 0) and not a.is_operation_stale(timeout_secs):
                                    return "OPENING"
                                if a.status == "recovering":
                                    return "RECOVERING"
                                if not a.is_active:
                                    return f"INACTIVE_{a.status.upper()}"
                                return "ACTIVE"
                    return "NOT_FOUND"

                b_state = _get_asset_lifecycle(m.base.network_name, m.base.asset_id)
                q_state = _get_asset_lifecycle(m.quote.network_name, m.quote.asset_id)

                if b_state == "OPENING" or q_state == "OPENING":
                    op_net = m.base.network_name if b_state == "OPENING" else m.quote.network_name
                    op_sym = m.base.symbol if b_state == "OPENING" else m.quote.symbol
                    summary = f"CHANNEL OPENING ON-CHAIN ({op_net} {op_sym} Awaiting Confirmations)"
                    rec_action = f"State channel on {op_net} for {op_sym} is currently confirming on-chain. Waiting for block confirmations."
                elif b_state == "NO_CHANNELS" or q_state == "NO_CHANNELS":
                    missing_net = m.base.network_name if b_state == "NO_CHANNELS" else m.quote.network_name
                    missing_sym = m.base.symbol if b_state == "NO_CHANNELS" else m.quote.symbol
                    summary = f"NO CAPACITY ({missing_net} Channel Needed)"
                    rec_action = f"Open state channel on {missing_net} for {missing_sym}"
                elif b_state == "RECOVERING" or q_state == "RECOVERING":
                    rec_net = m.base.network_name if b_state == "RECOVERING" else m.quote.network_name
                    rec_sym = m.base.symbol if b_state == "RECOVERING" else m.quote.symbol
                    summary = f"ASSET INACTIVE ({rec_net} {rec_sym} Recovering On-Chain)"
                    rec_action = f"{rec_sym} on {rec_net} is in recovery mode since Sept 10. Quoting unavailable until resolved."
                else:
                    if (b_send * 0.90) < req_b_send_1 and (q_send * 0.90) < req_q_send_1:
                        summary = "LOW CAPACITY (Send Capacity Depleted on Both Sides)"
                        rec_action = f"Deposit on-chain funds to replenish send capacity for {m.base.symbol}/{m.quote.symbol}"
                    else:
                        summary = "NO CAPACITY (Cannot Quote)"
                        rec_action = f"Rent capacity or fund channels for {m.base.symbol}/{m.quote.symbol}"

            pair_zombies = [
                z for z in zombie_orders
                if (z.get("base_network_id") == m.base.network_id and str(z.get("base_asset_id", "")).lower() == m.base.asset_id.lower()) or
                   (z.get("quote_network_id") == m.quote.network_id and str(z.get("quote_asset_id", "")).lower() == m.quote.asset_id.lower())
            ]
            zombie_count = len(pair_zombies)
            zombie_locked = sum(z.get("remaining_base", 0.0) for z in pair_zombies if z.get("side") == "BUY")

            market_audits[pair_name] = MarketChannelAudit(
                pair_name=pair_name,
                base_symbol=m.base.symbol,
                quote_symbol=m.quote.symbol,
                base_network=m.base.network_name,
                quote_network=m.quote.network_name,
                base_sendable=b_send,
                base_receivable=b_recv,
                quote_sendable=q_send,
                quote_receivable=q_recv,
                can_place_bids=can_bid,
                can_place_asks=can_ask,
                status_summary=summary,
                recommended_action=rec_action,
                base_free_sendable=b_send_free,
                quote_free_sendable=q_send_free,
                can_place_3_bids=can_bid_3,
                can_place_3_asks=can_ask_3,
                zombie_orders_count=zombie_count,
                zombie_locked_recv=zombie_locked,
                bids_constrained_by=bids_constrained_by,
                asks_constrained_by=asks_constrained_by,
                limiting_constraint=limiting_constraint,
            )

        # Lithium channels on EVM networks (Arbitrum and Ethereum) are multi-token pooled state channels.
        # Exactly one active channel should handle all tokens (USDC, HDN, ETH) on each EVM network.
        # Concentrating all liquidity and volume on a single well-used channel retains rental capacity
        # because the Hub liquidity service avoids reclaiming expired rental capacity on well-used channels.
        active_statuses = ("CHANNEL_STATUS_ACTIVE", "CHANNEL_STATUS_UPDATING", "active", "updating")
        eth_active = [ch for ch in eth_channels if ch.status in active_statuses and not self.is_channel_closing_or_closed(ch)]
        arb_active = [ch for ch in arb_channels if ch.status in active_statuses and not self.is_channel_closing_or_closed(ch)]

        primary_arb = self.get_primary_lithium_channel("Arbitrum")
        primary_eth = self.get_primary_lithium_channel("Ethereum")

        total_evm_active = len(arb_active) + len(eth_active)
        arb_excessive = len(arb_active) > 1
        eth_excessive = len(eth_active) > 1
        # Each distinct EVM network (Arbitrum L2 and Ethereum L1) requires exactly 1 consolidated multi-asset channel.
        # Flag excessive channels only if an individual network has > 1 active channel.
        has_excessive_lithium = arb_excessive or eth_excessive

        lithium_channels = {
            "Arbitrum": {
                "active_count": len(arb_active),
                "primary_channel_id": primary_arb.channel_id if primary_arb else None,
                "self_opened_count": len(arb_active),
                "rented_count": 0,
                "recommended_max_channels": 1,
                "recommended_max_self_opened": 1,
                "is_excessive": arb_excessive,
            },
            "Ethereum": {
                "active_count": len(eth_active),
                "primary_channel_id": primary_eth.channel_id if primary_eth else None,
                "self_opened_count": len(eth_active),
                "rented_count": 0,
                "recommended_max_channels": 1,
                "recommended_max_self_opened": 1,
                "is_excessive": eth_excessive,
            },
            "total_active_lithium": total_evm_active,
            "total_self_opened": total_evm_active,
            "total_rented": 0,
            "target_channels": 1,
            "has_excessive_channels": has_excessive_lithium,
            "consolidation_goal": "1 unified channel per EVM network (Arbitrum: 1, Ethereum: 1)",
        }

        return {
            "channels": all_channels,
            "onchain_balances": onchain_balances,
            "usable_onchain_balances": usable_onchain_balances,
            "market_audits": market_audits,
            "reserve_deficits": reserve_deficits,
            "zombie_orders": zombie_orders,
            "lithium_channels": lithium_channels,
        }

    def print_audit_report(self, audit_result: Dict[str, Any]):
        """Prints a human-readable ASCII report to the console."""
        print("\n" + "=" * 80)
        print("          HYDRANET STATE CHANNEL & LIQUIDITY CAPACITY AUDIT")
        print("=" * 80)

        # On-Chain Balances
        print("\n[ON-CHAIN WALLET BALANCES (Ready to Fund Channels)]")
        usable_map = audit_result.get("usable_onchain_balances", {})
        for net, bals in audit_result.get("onchain_balances", {}).items():
            reserve_usd = self.config.min_onchain_gas_reserve_usd.get(net, 0.0)
            if bals:
                bal_strs = []
                usdc_res_disp = getattr(self.config, "min_onchain_usdc_reserve_usd", 1030.0)
                for s, v in bals.items():
                    if s == "ETH" and reserve_usd > 0:
                        usable_v = usable_map.get(net, {}).get(s, v)
                        bal_strs.append(f"{v:.6f} {s} (Usable: {usable_v:.6f} {s}, Gas Reserve: ${reserve_usd:.0f})")
                    elif s == "USDC" and net == "Arbitrum" and usdc_res_disp > 0:
                        usable_v = usable_map.get(net, {}).get(s, v)
                        bal_strs.append(f"{v:.2f} {s} (Usable: {usable_v:.2f} {s}, Collateral Reserve: ${usdc_res_disp:.0f})")
                    else:
                        bal_strs.append(f"{v:.6f} {s}")
                print(f"  * {net:<12}: {', '.join(bal_strs)}")
            else:
                res_str = f" (Gas Reserve: ${reserve_usd:.0f})" if reserve_usd > 0 else ""
                print(f"  * {net:<12}: 0.000000 (no on-chain funds){res_str}")

        # Active Channels
        print("\n[ACTIVE PAYMENT CHANNELS]")
        has_channels = False
        for net, ch_list in audit_result.get("channels", {}).items():
            for ch in ch_list:
                has_channels = True
                print(f"  Network: {net} | ID: {ch.channel_id[:16]}... | Status: {ch.status}")
                for aid, asset in ch.assets.items():
                    act_tag = "[OK]" if asset.is_active else f"[{asset.status}]"
                    line = f"    {act_tag} {asset.symbol:<6} -> Sendable (Local): {asset.free_local:>12.4f} | Receivable (Remote): {asset.free_remote:>12.4f}"
                    if asset.pending_local > 0 or asset.onchain_in_flight:
                        line += f" | Pending (Opening): {asset.pending_local:.6f}"
                    print(line)
        if not has_channels:
            print("  (No state channels found)")

        # Channel Reserve Deficits
        deficits = audit_result.get("reserve_deficits", [])
        if deficits:
            print("\n[CHANNEL RESERVE DEFICITS (Autonomous Self-Healing Active)]")
            for d in deficits:
                print(
                    f"  ! Network: {d['network_name']:<10} | ID: {d['channel_id'][:16]}... | "
                    f"Asset: {d['symbol']} | Free Local: {d['free_local']:.6f} < Reserve: {d['unspendable_local_reserve']:.6f} "
                    f"(Deficit: {d['deficit']:.6f}, Top-Up Target: {d['target_topup']:.6f})"
                )

        # Zombie / Stuck Orders
        zombies = audit_result.get("zombie_orders", [])
        if zombies:
            print("\n[ZOMBIE / STUCK ORDERS (pendingCancel > 15m)]")
            for z in zombies:
                print(
                    f"  ! Order ID: {z['order_id'][:16]}... | Age: {z['age_minutes']}m | "
                    f"Side: {z['side']} | Remaining: {z['remaining_base']} {z['base_symbol']} | Price: {z['price']}"
                )

        # Market Quoting Status
        print("\n[MARKET QUOTING READINESS]")
        audits = audit_result.get("market_audits", {})
        for pair_name, a in audits.items():
            bid_tag = "[OK] Bids" if a.can_place_bids else "[X] Bids"
            ask_tag = "[OK] Asks" if a.can_place_asks else "[X] Asks"
            print(f"  {pair_name:<18} | {bid_tag:<10} | {ask_tag:<10} | {a.status_summary}")
            if a.recommended_action:
                print(f"    Action Advice: {a.recommended_action}")
        # Lithium Consolidation Target
        lith = audit_result.get("lithium_channels", {})
        if lith.get("has_excessive_channels"):
            print("\n[LITHIUM CONSOLIDATION TARGET (Goal: 1 Channel for All EVM Assets)]")
            total_active = lith.get("total_active_lithium", 0)
            arb_cnt = lith.get("Arbitrum", {}).get("active_count", 0)
            eth_cnt = lith.get("Ethereum", {}).get("active_count", 0)
            print(
                f"  ! Notice: Currently {total_active} active EVM channels open (Arbitrum: {arb_cnt}, Ethereum: {eth_cnt}).\n"
                f"    Target is 1 unified Lithium channel containing both Arbitrum and Ethereum assets to maximize\n"
                f"    rental retention with the Hub. Consolidate remaining channels over time as appropriate."
            )
        print("=" * 80 + "\n")

    # ==================== LIQUIDITY RENTAL / LEASE API ====================

    def estimate_rental_fee(
        self,
        network_name: str,
        asset_id: str,
        amount: float,
        duration_seconds: int = 86400,
        payment_network_name: str = "Arbitrum",
        payment_asset_id: str = "erc20:0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        channel_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Estimates the fee to lease inbound capacity from the Hydranet Liquidity Hub.
        For EVM networks (Arbitrum/Ethereum), prioritizes depositing into the single primary
        multi-asset Lithium channel using ChannelLiquidityRequestOperation.deposit to retain
        rental capacity by concentrating activity on a well-used channel.
        Falls back to openOrDeposit if no channel exists or if deposit is not accepted.
        """
        network_config = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
            "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": self._net_id("Bitcoin")},
        }.get(network_name)
        if not network_config:
            raise ValueError(f"Unknown network {network_name}")

        payment_network = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
            "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": self._net_id("Bitcoin")},
        }.get(payment_network_name, {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")})

        # Resolve target channel ID for Lithium networks
        target_cid = channel_id
        if not target_cid and network_name in ("Arbitrum", "Ethereum"):
            primary_ch = self.get_primary_lithium_channel(network_name, required_asset_id=asset_id)
            if not primary_ch:
                primary_ch = self.get_primary_lithium_channel(network_name)
            if primary_ch:
                target_cid = primary_ch.channel_id

        # Try deposit operation on target channel if available
        if target_cid:
            operation = {
                "deposit": {
                    "channelId": target_cid,
                    "assetLiquidity": {
                        asset_id: {
                            "serverAmount": {"value": str(amount)},
                            "clientAmount": {"value": "0"},
                        }
                    }
                }
            }
            try:
                res = self.client.estimate_request_channel_liquidity_fee(
                    network=network_config,
                    operation=operation,
                    duration_seconds=duration_seconds,
                    payment_network=payment_network,
                    payment_asset_id=payment_asset_id,
                    fee_payment_type="offchainFeePayment",
                )
                return {
                    "operation_type": "deposit",
                    "channel_id": target_cid,
                    "fee": res.get("fee", {}).get("value", "0.0"),
                    "quote_id": res.get("quoteId"),
                    "raw": res,
                }
            except Exception as e:
                logger.info(
                    f"[ChannelManager] Rental estimate using deposit operation on channel {target_cid[:16]}... "
                    f"returned: {e}. Falling back to openOrDeposit."
                )

        # Fallback to openOrDeposit (standard for Bitcoin, or when no EVM channel exists)
        operation = {
            "openOrDeposit": {
                "assetLiquidity": {
                    asset_id: {
                        "serverAmount": {"value": str(amount)},
                        "clientAmount": {"value": "0"},
                    }
                }
            }
        }
        res = self.client.estimate_request_channel_liquidity_fee(
            network=network_config,
            operation=operation,
            duration_seconds=duration_seconds,
            payment_network=payment_network,
            payment_asset_id=payment_asset_id,
            fee_payment_type="offchainFeePayment",
        )
        return {
            "operation_type": "open_or_deposit",
            "channel_id": None,
            "fee": res.get("fee", {}).get("value", "0.0"),
            "quote_id": res.get("quoteId"),
            "raw": res,
        }

    def rent_liquidity(
        self,
        network_name: str,
        asset_id: str,
        amount: float,
        duration_seconds: int = 86400,
        payment_network_name: str = "Arbitrum",
        payment_asset_id: str = "erc20:0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        channel_id: Optional[str] = None,
        live: bool = False,
    ) -> Dict[str, Any]:
        """
        Requests inbound liquidity from the Hydranet Liquidity Hub.
        For EVM networks (Arbitrum/Ethereum), prioritizes depositing into the single primary
        multi-asset Lithium channel using ChannelLiquidityRequestOperation.deposit to retain
        rental capacity by concentrating activity on a well-used channel.
        If live=False, performs estimation and dry-run check only.
        """
        est = self.estimate_rental_fee(
            network_name=network_name,
            asset_id=asset_id,
            amount=amount,
            duration_seconds=duration_seconds,
            payment_network_name=payment_network_name,
            payment_asset_id=payment_asset_id,
            channel_id=channel_id,
        )

        fee_str = est.get("fee", "0.0")
        target_cid = est.get("channel_id")
        op_type = est.get("operation_type", "open_or_deposit")
        cid_label = f" into channel {target_cid[:16]}..." if target_cid else ""
        logger.info(f"Rental fee estimate for {amount} {asset_id} on {network_name} ({op_type}{cid_label}) for {duration_seconds}s: {fee_str} {payment_asset_id}")

        if not live:
            logger.info(f"[DRY-RUN] Would execute {op_type} liquidity request{cid_label}. (Run with --live to execute)")
            return {
                "dry_run": True,
                "estimated_fee": fee_str,
                "operation_type": op_type,
                "channel_id": target_cid,
            }

        # LIVE EXECUTION
        # Ensure active peer connection with the Hub for target network and payment network
        if not self.ensure_peer_connected(network_name):
            raise ConnectionError(f"Could not establish active peer session with {network_name} Hub peer before renting liquidity.")
        if payment_network_name != network_name and not self.ensure_peer_connected(payment_network_name):
            raise ConnectionError(f"Could not establish active peer session with {payment_network_name} Hub peer for fee payment.")

        network_config = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
            "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": self._net_id("Bitcoin")},
        }[network_name]

        payment_network = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
            "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": self._net_id("Bitcoin")},
        }[payment_network_name]

        if op_type == "deposit" and target_cid:
            operation = {
                "deposit": {
                    "channelId": target_cid,
                    "assetLiquidity": {
                        asset_id: {
                            "serverAmount": {"value": str(amount)},
                            "clientAmount": {"value": "0"},
                        }
                    }
                }
            }
        else:
            operation = {
                "openOrDeposit": {
                    "assetLiquidity": {
                        asset_id: {
                            "serverAmount": {"value": str(amount)},
                            "clientAmount": {"value": "0"},
                        }
                    }
                }
            }

        res = self.client.request_channel_liquidity(
            network=network_config,
            operation=operation,
            duration_seconds=duration_seconds,
            payment_network=payment_network,
            payment_asset_id=payment_asset_id,
            fee_payment_type="offchainFeePayment",
            quote_id=est.get("quote_id"),
        )
        logger.info(f"[LIVE] Liquidity rental requested successfully ({op_type}{cid_label}): {res}")
        return res

    # ==================== NATIVE CHANNEL OPENING ====================

    def estimate_open_channel_fee(
        self,
        network_name: str,
        asset_amounts: Dict[str, str],
        fee_option: str = "medium",
    ) -> Dict[str, Any]:
        """Estimates on-chain fee for opening a channel with the Hub peer."""
        network_config = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
            "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": self._net_id("Bitcoin")},
        }.get(network_name)
        if not network_config:
            raise ValueError(f"Unknown network {network_name}")

        hub_pubkey = get_hub_node_pubkey(network_name, getattr(self.config, "testnet", False))
        return self.client.estimate_open_channel_fee(
            network=network_config,
            node_id=hub_pubkey,
            asset_amounts=asset_amounts,
            fee_option=fee_option,
        )

    def is_peer_connected(self, network_name: str) -> bool:
        """Checks if the local node is currently connected to the Hub peer for this network."""
        net = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
            "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": self._net_id("Bitcoin")},
        }.get(network_name)
        if not net:
            return False
        hub_pubkey = get_hub_node_pubkey(network_name, getattr(self.config, "testnet", False))
        if not hub_pubkey:
            return False
        try:
            return self.client.is_peer_connected(net["protocol"], net["id"], hub_pubkey)
        except Exception:
            return False

    def ensure_peer_connected(self, network_name: str, timeout_secs: float = 8.0) -> bool:
        """
        Verifies active peer session with the Hub. If disconnected, establishes WebSocket
        peer connection and waits for the protocol handshake (e.g. Lightning Init) to complete.
        """
        if self.is_peer_connected(network_name):
            return True

        net = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
            "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": self._net_id("Bitcoin")},
        }.get(network_name)
        peer_url = get_hub_peer_url(network_name, getattr(self.config, "testnet", False))
        if not net or not peer_url:
            return False

        logger.info(f"Connecting to {network_name} Hub peer: {peer_url}")
        try:
            self.client.connect_to_peer(net["protocol"], net["id"], peer_url)
        except Exception as e:
            logger.warning(f"Error calling connect_to_peer on {network_name}: {e}")
            if any(k in str(e) for k in ("Node not initialized", "FAILURE_REASON_NOT_CONFIGURED", "offline", "Failed to communicate")):
                return False

        # Poll for handshake confirmation
        start_t = time.time()
        while time.time() - start_t < timeout_secs:
            time.sleep(0.5)
            if self.is_peer_connected(network_name):
                logger.info(f"Established active peer connection with {network_name} Hub.")
                return True

        logger.warning(f"Timed out waiting for {network_name} peer handshake after {timeout_secs}s.")
        return False

    def get_active_channels_by_capacity(self, network_name: str = "Bitcoin") -> List[ChannelInfo]:
        """
        Returns active payment channels for a network sorted ascending by total capacity (send + receive).
        The first element is the 'smallest' channel with the least total capacity.
        Excludes channels that are closing, waiting confirmation, or closed.
        """
        proto = "PROTOCOL_BITCOIN" if network_name == "Bitcoin" else "PROTOCOL_EVM"
        nid = self._net_id(network_name)
        channels = self.get_channels_for_network(proto, nid, network_name)
        active = [
            ch for ch in channels
            if not self.is_channel_closing_or_closed(ch)
            and (
                ch.status in ("CHANNEL_STATUS_ACTIVE", "ACTIVE")
                or any(a.is_active or getattr(a, "is_usable", False) for a in ch.assets.values())
            )
        ]
        return sorted(active, key=lambda ch: sum(a.free_local + a.free_remote for a in ch.assets.values()))

    def get_largest_channel_capacity(self, network_name: str = "Bitcoin") -> float:
        """
        Returns the total capacity (send + receive) of the largest active or updating channel for a network.
        Returns 0.0 if no channels exist.
        Excludes channels that are closing, waiting confirmation, or closed.
        """
        proto = "PROTOCOL_BITCOIN" if network_name == "Bitcoin" else "PROTOCOL_EVM"
        nid = self._net_id(network_name)
        channels = self.get_channels_for_network(proto, nid, network_name)
        active_or_updating = [
            ch for ch in channels
            if not self.is_channel_closing_or_closed(ch)
            and (
                ch.status in ("CHANNEL_STATUS_ACTIVE", "ACTIVE", "UPDATING", "cooperativelyOpening")
                or any(a.is_active or "UPDATING" in str(a.status).upper() for a in ch.assets.values())
            )
        ]
        if not active_or_updating:
            return 0.0
        caps = [sum(a.free_local + a.free_remote for a in ch.assets.values()) for ch in active_or_updating]
        return max(caps) if caps else 0.0

    def get_smallest_remaining_channel_capacity(self, network_name: str = "Bitcoin") -> float:
        """
        Returns the total capacity (send + receive) of the smallest channel among the channels
        that will remain active after pruning the smallest channel (if at max slots), or the
        smallest active channel if slots are currently available.
        Returns 0.0 if no active channels exist.
        """
        active_sorted = self.get_active_channels_by_capacity(network_name)
        if not active_sorted:
            return 0.0
        max_channels = getattr(self.config, "btc_max_active_channels", 4)
        if len(active_sorted) >= max_channels:
            # Slot limit reached: smallest channel (index 0) will be closed,
            # so the smallest remaining channel is index 1 (or index 0 if only 1 exists).
            target_ch = active_sorted[1] if len(active_sorted) > 1 else active_sorted[0]
        else:
            # Slots available (< max_channels): no channel is closed, so smallest is index 0.
            target_ch = active_sorted[0]
        return sum(a.free_local + a.free_remote for a in target_ch.assets.values())

    def get_btc_channels_summary(self, oracle_btc_price: float = 65000.0) -> Dict[str, Any]:
        """
        Returns a detailed summary of all Bitcoin payment channels:
        active channels, updating/pending channels, closing channels,
        individual channel capacities (free_local, free_remote, total, percentages),
        and slot limit utilization against the Hub's 4-channel maximum.
        """
        raw_channels = self.get_channels_for_network("PROTOCOL_BITCOIN", self._net_id("Bitcoin"), "Bitcoin")

        channels_list = []
        active_count = 0
        updating_count = 0
        closing_count = 0
        total_local = 0.0
        total_remote = 0.0

        for ch in raw_channels:
            ch_local = 0.0
            ch_remote = 0.0
            ch_max_send = 0.0
            ch_max_recv = 0.0
            ch_unspendable_local = 0.0
            ch_unspendable_remote = 0.0
            ch_pending_send = 0.0
            ch_pending_recv = 0.0
            asset_status = ""
            is_active_asset = False
            is_usable_asset = False

            for a in ch.assets.values():
                ch_local += a.free_local
                ch_remote += a.free_remote
                ch_max_send += a.max_sendable
                ch_max_recv += a.max_receivable
                ch_unspendable_local += a.unspendable_local_reserve
                ch_unspendable_remote += a.unspendable_remote_reserve
                ch_pending_send += a.pending_local
                ch_pending_recv += a.pending_remote
                if a.is_active:
                    is_active_asset = True
                if getattr(a, "is_usable", a.is_active):
                    is_usable_asset = True
                if a.status:
                    asset_status = a.status

            raw_status = str(ch.status)
            is_closing = self.is_channel_closing_or_closed(ch)
            is_active = not is_closing and (raw_status in ("CHANNEL_STATUS_ACTIVE", "ACTIVE") or is_active_asset)
            is_updating = not is_closing and (
                "UPDATING" in raw_status.upper()
                or "OPENING" in raw_status.upper()
                or "PENDING" in raw_status.upper()
                or "COOPERATIVELYOPENING" in raw_status.upper()
                or "UPDATING" in asset_status.upper()
                or "OPENING" in asset_status.upper()
                or "PENDING" in asset_status.upper()
                or any(a.onchain_in_flight for a in ch.assets.values())
            )

            if is_closing:
                ui_status = "CLOSING"
                closing_count += 1
            elif is_active:
                ui_status = "ACTIVE"
                active_count += 1
                total_local += ch_local
                total_remote += ch_remote
            elif is_updating:
                ui_status = "UPDATING"
                updating_count += 1
                # If this channel has confirmed usable capacity (e.g. in-flight dynamic splice), credit it to totals
                if is_usable_asset:
                    total_local += ch_local
                    total_remote += ch_remote
            else:
                ui_status = raw_status.replace("CHANNEL_STATUS_", "")

            ch_total = ch_local + ch_remote
            local_pct = (ch_local / ch_total * 100.0) if ch_total > 0 else 50.0
            remote_pct = (ch_remote / ch_total * 100.0) if ch_total > 0 else 50.0

            channels_list.append({
                "channel_id": ch.channel_id,
                "short_id": f"{ch.channel_id[:6]}...{ch.channel_id[-4:]}" if len(ch.channel_id) > 12 else ch.channel_id,
                "status": ui_status,
                "raw_status": raw_status,
                "asset_status": asset_status,
                "free_local": round(ch_local, 8),
                "free_remote": round(ch_remote, 8),
                "pending_local": round(ch_pending_send, 8),
                "pending_remote": round(ch_pending_recv, 8),
                "total_capacity": round(ch_total, 8),
                "total_usd": round(ch_total * oracle_btc_price, 2),
                "local_pct": round(local_pct, 1),
                "remote_pct": round(remote_pct, 1),
                "unspendable_local_reserve": round(ch_unspendable_local, 8),
                "unspendable_remote_reserve": round(ch_unspendable_remote, 8),
                "counterparty": ch.counterparty,
                "is_active": (ui_status == "ACTIVE"),
                "is_usable": is_usable_asset,
                "is_splicing": (ui_status == "UPDATING" and is_usable_asset),
            })

        status_priority = {"ACTIVE": 0, "UPDATING": 1, "INACTIVE": 2, "CLOSING": 3}
        channels_list.sort(key=lambda x: (status_priority.get(x["status"], 9), -x["total_capacity"]))

        total_cap = total_local + total_remote
        return {
            "slots": {
                "active": active_count,
                "max": 4,
                "available": max(0, 4 - active_count),
                "updating": updating_count,
                "closing": closing_count,
                "total": len(channels_list),
            },
            "totals": {
                "free_local": round(total_local, 8),
                "free_remote": round(total_remote, 8),
                "total_capacity": round(total_cap, 8),
                "total_usd": round(total_cap * oracle_btc_price, 2),
                "local_pct": round((total_local / total_cap * 100.0) if total_cap > 0 else 50.0, 1),
                "remote_pct": round((total_remote / total_cap * 100.0) if total_cap > 0 else 50.0, 1),
            },
            "channels": channels_list,
        }

    def close_channel(
        self,
        network_name: str,
        channel_id: str,
        fee_option: str = "medium",
        live: bool = False
    ) -> Dict[str, Any]:
        """Cooperatively closes an active channel with safety validation."""
        net_config = {
            "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": self._net_id("Bitcoin")},
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
        }.get(network_name)
        if not net_config:
            raise ValueError(f"Unknown network {network_name}")

        self._pending_closed[channel_id] = time.time()

        if not live:
            logger.info(f"[DRY-RUN] Would cooperatively close channel {channel_id[:16]}... on {network_name}")
            return {"status": "DRY_RUN_SUCCESS", "channel_id": channel_id, "network": network_name}

        self.ensure_peer_connected(network_name)
        res = self.client.close_channel(net_config, channel_id, fee_option=fee_option)
        logger.info(f"Cooperatively closed channel {channel_id[:16]}... on {network_name}: {res}")
        return {"status": "SUCCESS", "result": res, "channel_id": channel_id, "network": network_name}

    def prepare_btc_channel_slots(
        self,
        action_type: str,
        live: bool = False,
        payment_network_name: Optional[str] = None,
        asset_amounts: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Enforces the Hub's 4 allowable channel architecture for Bitcoin:
        - The architecture maintains 4 active channels.
        - Closing channels do NOT occupy a channel slot (cooperative close immediately vacates a Hub slot).
        - If < 4 active channels (e.g. 0, 1, 2, or 3):
          Open / rent directly into available slot without closing any channels (status: READY, action: PROCEED).
        - If >= 4 active channels:
          When a rental or open channel is needed, cooperatively close the 1 smallest active channel
          by total capacity (send + receive) to immediately vacate a slot.
          Because a cooperative close immediately allows for another channel on the Hub,
          we immediately proceed with the open / rental (status: READY, action: PROCEED, closed_before: [cid]),
          preserving all 4 active channels.
        - If any channel is currently updating / opening on-chain, block new actions until confirmed.
        - Pre-flight checks: peer connectivity, balance sufficiency, and fee estimation must succeed before closing.
        """
        max_channels = getattr(self.config, "btc_max_active_channels", 4)
        raw_channels = self.get_channels_for_network("PROTOCOL_BITCOIN", self._net_id("Bitcoin"), "Bitcoin")

        # 1. Guard: If any Bitcoin channel is currently updating or opening, block new capacity actions
        updating_channels = [
            ch for ch in raw_channels
            if not self.is_channel_closing_or_closed(ch) and (
                any(k in str(ch.status).upper() for k in ("UPDATING", "OPENING", "PENDING", "COOPERATIVELYOPENING"))
                or any(a.is_updating or a.onchain_in_flight for a in ch.assets.values())
            )
        ]
        if updating_channels:
            ch_up = updating_channels[0]
            cid = ch_up.channel_id
            msg = f"Bitcoin channel {cid[:16]}... is currently pending update/confirmation on-chain ({len(updating_channels)} in-flight). Blocking {action_type} until confirmed."
            logger.warning(f"[BTC Channel Manager] {msg}")
            return {
                "status": "BLOCKED",
                "action": "BLOCKED",
                "reason": msg,
                "channels_to_close_after": [],
                "closed_before": [],
            }

        # 2. Pre-flight Guard: Verify peer connectivity before executing any on-chain close
        if live:
            if not self.ensure_peer_connected("Bitcoin"):
                msg = f"Cannot establish active peer connection with Bitcoin Hub peer. Aborting {action_type} without closing any channels."
                logger.warning(f"[BTC Channel Manager] {msg}")
                return {
                    "status": "BLOCKED",
                    "action": "BLOCKED",
                    "reason": msg,
                    "channels_to_close_after": [],
                    "closed_before": [],
                }
            if action_type == "RENT_INBOUND" and payment_network_name and payment_network_name != "Bitcoin":
                if not self.ensure_peer_connected(payment_network_name):
                    msg = f"Cannot establish active peer connection with {payment_network_name} Hub peer for fee payment. Aborting RENT_INBOUND without closing any channels."
                    logger.warning(f"[BTC Channel Manager] {msg}")
                    return {
                        "status": "BLOCKED",
                        "action": "BLOCKED",
                        "reason": msg,
                        "channels_to_close_after": [],
                        "closed_before": [],
                    }
            if action_type == "OPEN_CHANNEL" and asset_amounts:
                btc_aid = self._resolve_asset_id("Bitcoin", "BTC")
                try:
                    bals = self.client.get_wallet_balances("PROTOCOL_BITCOIN", self._net_id("Bitcoin"))
                    conf_btc = float(bals.get(btc_aid, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))
                    req_amt = sum(float(v) for v in asset_amounts.values())
                    if conf_btc < req_amt:
                        msg = f"Insufficient on-chain confirmed BTC ({conf_btc:.6f} < {req_amt:.6f} required). Aborting channel open without closing any channels."
                        logger.warning(f"[BTC Channel Manager] {msg}")
                        return {
                            "status": "BLOCKED",
                            "action": "BLOCKED",
                            "reason": msg,
                            "channels_to_close_after": [],
                            "closed_before": [],
                        }
                except Exception as e:
                    logger.warning(f"[BTC Channel Manager] Pre-flight balance check warning: {e}")

        active_sorted = self.get_active_channels_by_capacity("Bitcoin")
        # Note: Closing channels do NOT count against the Hub slot limit.
        logger.info(f"[BTC Channel Manager] Active Bitcoin channels count: {len(active_sorted)} (limit: {max_channels})")

        if len(active_sorted) < max_channels:
            logger.info(
                f"[BTC Channel Manager] {len(active_sorted)} channels active (< {max_channels} limit). Utilizing available "
                f"slot for {action_type} without closing any channels."
            )
            return {
                "status": "READY",
                "action": "PROCEED",
                "channels_to_close_after": [],
            }

        else:  # len(active_sorted) >= max_channels
            if not active_sorted:
                return {
                    "status": "BLOCKED",
                    "action": "BLOCKED",
                    "reason": f"Bitcoin channel slots occupied ({len(active_sorted)} >= {max_channels}), but no active channels available to prune.",
                    "channels_to_close_after": [],
                    "closed_before": [],
                }
            smallest = active_sorted[0]
            sm_cap = sum(a.free_local + a.free_remote for a in smallest.assets.values())
            cid = smallest.channel_id
            closed_ids = []
            logger.warning(
                f"[BTC Channel Manager] {len(active_sorted)} active channels detected (>= {max_channels} limit). "
                f"Cooperatively closing smallest channel {cid[:16]}... (capacity: {sm_cap:.8f} BTC) to immediately vacate "
                f"a slot for {action_type}."
            )
            try:
                self.close_channel("Bitcoin", cid, fee_option="medium", live=live)
                closed_ids.append(cid)
            except Exception as e:
                logger.error(f"[BTC Channel Manager] Failed to close smallest channel {cid[:16]}...: {e}")
                return {
                    "status": "BLOCKED",
                    "action": "BLOCKED",
                    "reason": f"Failed to close smallest channel {cid[:16]}... to vacate slot for {action_type}: {e}",
                    "channels_to_close_after": [],
                    "closed_before": [],
                }

            return {
                "status": "READY",
                "action": "PROCEED",
                "channels_to_close_after": [],
                "closed_before": closed_ids,
            }

    def open_channel(
        self,
        network_name: str,
        asset_amounts: Dict[str, str],
        fee_option: str = "medium",
        live: bool = False,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Opens a payment channel with the Hub using on-chain funds."""
        # Ensure peer connection is established before estimating or opening
        if not self.ensure_peer_connected(network_name):
            raise ConnectionError(f"Could not establish active peer session with {network_name} Hub peer before opening channel.")

        # Hard guard against opening duplicate Lithium channels on EVM networks
        if not force and network_name in ("Ethereum", "Arbitrum"):
            primary_ch = self.get_primary_lithium_channel(network_name)
            if primary_ch:
                logger.warning(
                    f"[ChannelManager] OPEN_CHANNEL blocked for {network_name}: Active Lithium channel already exists ({primary_ch.channel_id[:16]}...). "
                    f"Lithium supports multi-asset pooling and in-flight deposits. Auto-routing to deposit_channel on primary channel {primary_ch.channel_id[:16]}..."
                )
                return self.deposit_channel(network_name, primary_ch.channel_id, asset_amounts, fee_option=fee_option, live=live)

        # Enforce Bitcoin channel limit and dust open threshold
        if not force and network_name == "Bitcoin":
            max_btc_chs = getattr(self.config, "btc_max_active_channels", 4)
            active_btc = self.get_active_channels_by_capacity("Bitcoin")
            if len(active_btc) >= max_btc_chs:
                raise ValueError(
                    f"Bitcoin active channel limit reached ({len(active_btc)} active >= {max_btc_chs} limit). "
                    f"Must cooperatively close smallest active channel before opening a new channel."
                )

            min_btc_open_usd = getattr(self.config, "min_onchain_btc_open_usd", 40.0)
            min_btc_open_amt = getattr(self.config, "min_onchain_btc_open_amt", 0.0004)
            btc_price = self.get_fiat_price("BTC")
            for aid, amt_str in asset_amounts.items():
                open_btc_amt = float(amt_str)
                if open_btc_amt < min_btc_open_amt or (open_btc_amt * btc_price) < min_btc_open_usd:
                    raise ValueError(
                        f"Dust channel open prevented on Bitcoin: {open_btc_amt:.8f} BTC (~${(open_btc_amt * btc_price):.2f}) "
                        f"is below the minimum required open threshold ({min_btc_open_amt:.8f} BTC / ${min_btc_open_usd:.2f} USD)."
                    )

        # Check on-chain collateral reserve guardrail for USDC on Arbitrum (protects GMX Agent bot)
        usdc_reserve = getattr(self.config, "min_onchain_usdc_reserve_usd", 1030.0)
        if not force and usdc_reserve > 0 and network_name == "Arbitrum":
            proto = "PROTOCOL_EVM"
            nid = self._net_id("Arbitrum")
            bals = self.client.get_wallet_balances(proto, nid)
            for aid, amt_str in asset_amounts.items():
                sym = self._lookup_symbol(nid, aid)
                if sym == "USDC" or "af88d065" in aid.lower():
                    dep_amount = float(amt_str)
                    conf_usdc = float(bals.get(aid, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))
                    usable_usdc = self._calculate_usable_usdc(conf_usdc, usdc_reserve)
                    if dep_amount > usable_usdc:
                        total_eq = self._get_bot_total_equity()
                        eq_info = f"current total equity is ${total_eq:.2f}, " if total_eq else ""
                        raise ValueError(
                            f"Collateral reserve violation on {network_name}: cannot deposit {dep_amount:.2f} USDC. "
                            f"Must retain at least ${usdc_reserve:.2f} for GMX Agent bot ({eq_info}"
                            f"confirmed on-chain: ${conf_usdc:.2f}, usable: ${usable_usdc:.2f})."
                        )

        # Check on-chain gas reserve guardrail for EVM networks
        reserve_usd = self.config.min_onchain_gas_reserve_usd.get(network_name, 0.0)
        min_gas_floor = 0.0002
        if network_name in ("Ethereum", "Arbitrum"):
            eth_price = self.get_fiat_price("ETH")
            min_reserve_eth = (reserve_usd / eth_price) if (reserve_usd > 0 and not force and eth_price > 0) else min_gas_floor
            proto = "PROTOCOL_EVM"
            nid = self._net_id(network_name)
            bals = self.client.get_wallet_balances(proto, nid)
            native_aid = "0x0000000000000000000000000000000000000000"
            confirmed_eth = float(bals.get(native_aid, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))

            for aid, amt_str in asset_amounts.items():
                if aid.lower() == native_aid.lower():
                    dep_amount = float(amt_str)
                    if (confirmed_eth - dep_amount) < min_reserve_eth:
                        usable_eth = max(0.0, confirmed_eth - min_reserve_eth)
                        reserve_label = f"${reserve_usd:.2f} ({min_reserve_eth:.6f} ETH)" if not force else f"{min_gas_floor:.6f} ETH"
                        raise ValueError(
                            f"Gas reserve violation on {network_name}: cannot deposit {dep_amount:.6f} ETH. "
                            f"Must retain at least {reserve_label} for on-chain gas. "
                            f"(Confirmed on-chain: {confirmed_eth:.6f} ETH, usable: {usable_eth:.6f} ETH)."
                        )
                else:
                    if confirmed_eth < min_gas_floor:
                        raise ValueError(
                            f"Insufficient ETH for gas on {network_name}: confirmed ETH balance ({confirmed_eth:.6f} ETH) "
                            f"is below the minimum required for transaction gas ({min_gas_floor:.6f} ETH)."
                        )

        # Check / set token allowance for ERC-20 assets
        spenders = TESTNET_LITHIUM_SPENDERS if getattr(self.config, "testnet", False) else LITHIUM_SPENDERS
        known_spender = spenders.get(network_name)
        for aid, amt_str in asset_amounts.items():
            if aid.startswith("erc20:") and known_spender:
                self.ensure_token_allowance(network_name, aid, known_spender, float(amt_str), live=live)

        try:
            est = self.estimate_open_channel_fee(network_name, asset_amounts, fee_option)
            fee_val = est.get("fee", {}).get("value", "0.0")
            logger.info(f"Open channel on-chain fee estimate on {network_name}: {fee_val}")
        except Exception as e:
            err_str = str(e)
            m = re.search(r"spender=(0x[0-9a-fA-F]+)", err_str)
            if "InsufficientAllowance" in err_str and m:
                spender_addr = m.group(1)
                logger.info(f"Detected spender {spender_addr} requiring allowance on {network_name}.")
                for aid, amt_str in asset_amounts.items():
                    if aid.startswith("erc20:"):
                        self.ensure_token_allowance(network_name, aid, spender_addr, float(amt_str), live=live)
                est = self.estimate_open_channel_fee(network_name, asset_amounts, fee_option)
                fee_val = est.get("fee", {}).get("value", "0.0")
            else:
                raise

        if not live:
            logger.info(f"[DRY-RUN] Would open channel on {network_name} with {asset_amounts}. (Run with --live to execute)")
            return {"dry_run": True, "estimated_fee": fee_val}

        network_config = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
            "Bitcoin": {"protocol": "PROTOCOL_BITCOIN", "id": self._net_id("Bitcoin")},
        }[network_name]
        hub_pubkey = get_hub_node_pubkey(network_name, getattr(self.config, "testnet", False))

        res = self.client.open_channel(
            network=network_config,
            node_id=hub_pubkey,
            asset_amounts=asset_amounts,
            fee_option=fee_option,
        )
        for aid in asset_amounts.keys():
            sym = self._lookup_symbol(network_config["id"], aid)
            self._pending_opens[f"{network_name}:{sym}"] = time.time()
        return res

    def ensure_token_allowance(
        self,
        network_name: str,
        token_id: str,
        spender: str,
        required_amount: float = 0.0,
        live: bool = False
    ) -> bool:
        """
        Ensures that the spender contract has sufficient on-chain allowance for an ERC-20 token.
        If current allowance is insufficient or 0, broadcasts client_setTokenAllowance (unlimited).
        """
        if not token_id.startswith("erc20:"):
            return True

        network_config = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
        }.get(network_name)
        if not network_config:
            return True

        node_id = self.client.get_node_id(network_config["protocol"], network_config["id"])
        if not node_id:
            logger.warning(f"Cannot get node ID for {network_name} to check token allowance.")
            return False

        try:
            allow_res = self.client.get_token_allowance(
                network=network_config,
                owner=node_id,
                spender=spender,
                token_id=token_id
            )
            allow_data = allow_res.get("allowance", {}).get("token", {}).get("amount", {})
            if "unlimited" in allow_data:
                return True
            curr_str = allow_data.get("exact", {}).get("amount", {}).get("value", "0.0")
            curr_val = float(curr_str)
            if curr_val > 0 and curr_val >= required_amount:
                return True
        except Exception as e:
            logger.warning(f"Error checking token allowance for {token_id} on {network_name}: {e}")

        logger.info(f"Token allowance needed for {token_id} on {network_name} (spender: {spender}).")
        if not live:
            logger.info(f"[DRY-RUN] Would broadcast client_setTokenAllowance for {token_id} on {network_name}.")
            return True

        try:
            res = self.client.set_token_allowance(
                network=network_config,
                spender=spender,
                token_id=token_id,
                amount="unlimited",
                fee_option="medium"
            )
            txid = res.get("txid", "")
            logger.info(f"Token allowance transaction broadcast on {network_name}: txid={txid}")
            return True
        except Exception as e:
            logger.error(f"Failed to set token allowance for {token_id} on {network_name}: {e}")
            return False

    def deposit_channel(
        self,
        network_name: str,
        channel_id: str,
        asset_amounts: Dict[str, str],
        fee_option: str = "medium",
        live: bool = False,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Deposits funds into an existing payment channel with safety validation."""
        if network_name == "Bitcoin":
            logger.info("Channel deposits not supported on Bitcoin Lightning. Routing to open_channel to increase send capacity.")
            return self.open_channel(network_name, asset_amounts, fee_option=fee_option, live=live, force=force)

        network_config = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
        }.get(network_name)
        if not network_config:
            raise ValueError(f"Channel deposits not supported on network {network_name}")

        # Check on-chain collateral reserve guardrail for USDC on Arbitrum (protects GMX Agent bot)
        usdc_reserve = getattr(self.config, "min_onchain_usdc_reserve_usd", 1030.0)
        if not force and usdc_reserve > 0 and network_name == "Arbitrum":
            bals = self.client.get_wallet_balances(network_config["protocol"], network_config["id"])
            for aid, amt_str in asset_amounts.items():
                sym = self._lookup_symbol(network_config["id"], aid)
                if sym == "USDC" or "af88d065" in aid.lower():
                    dep_amount = float(amt_str)
                    conf_usdc = float(bals.get(aid, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))
                    usable_usdc = self._calculate_usable_usdc(conf_usdc, usdc_reserve)
                    if dep_amount > usable_usdc:
                        total_eq = self._get_bot_total_equity()
                        eq_info = f"current total equity is ${total_eq:.2f}, " if total_eq else ""
                        raise ValueError(
                            f"Collateral reserve violation on {network_name}: cannot deposit {dep_amount:.2f} USDC. "
                            f"Must retain at least ${usdc_reserve:.2f} for GMX Agent bot ({eq_info}"
                            f"confirmed on-chain: ${conf_usdc:.2f}, usable: ${usable_usdc:.2f})."
                        )

        # Gas reserve check
        reserve_usd = self.config.min_onchain_gas_reserve_usd.get(network_name, 0.0)
        min_gas_floor = 0.0002
        eth_price = self.get_fiat_price("ETH")
        min_reserve_eth = (reserve_usd / eth_price) if (reserve_usd > 0 and not force and eth_price > 0) else min_gas_floor
        bals = self.client.get_wallet_balances(network_config["protocol"], network_config["id"])
        native_aid = "0x0000000000000000000000000000000000000000"
        confirmed_eth = float(bals.get(native_aid, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))

        for aid, amt_str in asset_amounts.items():
            if aid.lower() == native_aid.lower():
                dep_amount = float(amt_str)
                if (confirmed_eth - dep_amount) < min_reserve_eth:
                    usable_eth = max(0.0, confirmed_eth - min_reserve_eth)
                    reserve_label = f"${reserve_usd:.2f} ({min_reserve_eth:.6f} ETH)" if not force else f"{min_gas_floor:.6f} ETH"
                    raise ValueError(
                        f"Gas reserve violation on {network_name}: cannot deposit {dep_amount:.6f} ETH. "
                        f"Must retain at least {reserve_label} for on-chain gas. "
                        f"(Confirmed on-chain: {confirmed_eth:.6f} ETH, usable: {usable_eth:.6f} ETH)."
                    )
            else:
                # Non-ETH asset (USDC, HDN, etc.): ETH is used for transaction gas.
                # Ensure at least minimal ETH is present to cover gas (~0.0002 ETH)
                if confirmed_eth < min_gas_floor:
                    raise ValueError(
                        f"Insufficient ETH for gas on {network_name}: confirmed ETH balance ({confirmed_eth:.6f} ETH) "
                        f"is below the minimum required for transaction gas ({min_gas_floor:.6f} ETH)."
                    )

        # Check / set token allowance for ERC-20 assets
        spenders = TESTNET_LITHIUM_SPENDERS if getattr(self.config, "testnet", False) else LITHIUM_SPENDERS
        known_spender = spenders.get(network_name)
        for aid, amt_str in asset_amounts.items():
            if aid.startswith("erc20:") and known_spender:
                self.ensure_token_allowance(network_name, aid, known_spender, float(amt_str), live=live)

        try:
            est = self.client.estimate_deposit_channel_fee(network_config, channel_id, asset_amounts, fee_option)
            fee_val = est.get("fee", {}).get("value", "0.0")
            logger.info(f"Deposit channel on-chain fee estimate on {network_name}: {fee_val}")
        except Exception as e:
            err_str = str(e)
            m = re.search(r"spender=(0x[0-9a-fA-F]+)", err_str)
            if "InsufficientAllowance" in err_str and m:
                spender_addr = m.group(1)
                logger.info(f"Detected spender {spender_addr} requiring allowance on {network_name}.")
                for aid, amt_str in asset_amounts.items():
                    if aid.startswith("erc20:"):
                        self.ensure_token_allowance(network_name, aid, spender_addr, float(amt_str), live=live)
                est = self.client.estimate_deposit_channel_fee(network_config, channel_id, asset_amounts, fee_option)
                fee_val = est.get("fee", {}).get("value", "0.0")
            else:
                raise

        if not live:
            logger.info(f"[DRY-RUN] Would deposit into channel {channel_id} on {network_name} with {asset_amounts}. (Run with --live to execute)")
            return {"dry_run": True, "estimated_fee": fee_val}

        return self.client.deposit_channel(
            network=network_config,
            channel_id=channel_id,
            asset_amounts=asset_amounts,
            fee_option=fee_option
        )

    def withdraw_from_channel(
        self,
        network_name: str,
        channel_id: str,
        asset_symbol: str,
        amount: float,
        fee_option: str = "medium",
        live: bool = False,
    ) -> Dict[str, Any]:
        """Withdraws funds from an active EVM Lithium state channel back to the on-chain wallet."""
        if network_name == "Bitcoin":
            raise ValueError(
                "Partial channel withdrawals are not supported on Bitcoin Lightning; "
                "use close_channel to sweep local BTC back to on-chain."
            )

        network_config = {
            "Arbitrum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Arbitrum")},
            "Ethereum": {"protocol": "PROTOCOL_EVM", "id": self._net_id("Ethereum")},
        }.get(network_name)
        if not network_config:
            raise ValueError(f"Channel withdrawals not supported on network {network_name}")

        asset_id = self._resolve_asset_id(network_name, asset_symbol)
        if not asset_id:
            raise ValueError(f"Could not resolve asset ID for {asset_symbol} on {network_name}")

        self.ensure_peer_connected(network_name)

        dec = 6 if asset_symbol.upper() in ("BTC", "ETH") else 2
        amt_str = f"{amount:.{dec}f}".rstrip("0").rstrip(".") if "." in f"{amount:.{dec}f}" else str(amount)
        asset_amounts = {asset_id: amt_str}

        est = self.client.estimate_withdraw_channel_fee(
            network=network_config,
            channel_id=channel_id,
            asset_amounts=asset_amounts,
            fee_option=fee_option,
        )
        fee_val = est.get("fee", {}).get("value", "0.0")
        logger.info(f"Withdraw channel on-chain fee estimate on {network_name}: {fee_val}")

        if not live:
            logger.info(f"[DRY-RUN] Would withdraw {amt_str} {asset_symbol} from channel {channel_id} on {network_name}.")
            return {
                "status": "DRY_RUN_SUCCESS",
                "dry_run": True,
                "estimated_fee": fee_val,
                "amount": float(amt_str),
                "asset": asset_symbol.upper(),
                "network": network_name,
                "channel_id": channel_id,
            }

        res = self.client.withdraw_channel(
            network=network_config,
            channel_id=channel_id,
            asset_amounts=asset_amounts,
            fee_option=fee_option,
        )
        return {
            "status": "SUCCESS",
            "result": res,
            "estimated_fee": fee_val,
            "amount": float(amt_str),
            "asset": asset_symbol.upper(),
            "network": network_name,
            "channel_id": channel_id,
        }

    # ==================== AUTOMATED CAPACITY EXECUTION ====================

    def _resolve_asset_id(self, network_name: str, symbol: str) -> Optional[str]:
        """Resolves asset ID hex string for a given network and symbol."""
        sym_u = symbol.upper()
        active_markets = TESTNET_MARKETS if getattr(self.config, "testnet", False) else MARKETS
        for m in active_markets.values():
            if m.base.network_name.lower() == network_name.lower() and m.base.symbol.upper() == sym_u:
                return m.base.asset_id
            if m.quote.network_name.lower() == network_name.lower() and m.quote.symbol.upper() == sym_u:
                return m.quote.asset_id
        if getattr(self.config, "testnet", False):
            from config import TESTNET_TOKEN_ASSETS
            for (net_k, sym_k), aid in TESTNET_TOKEN_ASSETS.items():
                if net_k.lower() == network_name.lower() and sym_k.upper() == sym_u:
                    return aid
        if sym_u == "BTC" and network_name.lower() == "bitcoin":
            return "0x0000000000000000000000000000000000000000000000000000000000000000"
        if sym_u == "ETH" and network_name.lower() in ("arbitrum", "ethereum"):
            return "0x0000000000000000000000000000000000000000"
        return None

    def find_best_rental_payment_asset(self, target_network: Optional[str] = None) -> Tuple[str, str, str]:
        """
        Finds the best off-chain asset to pay for a channel rental fee.
        Returns: (network_name, asset_id, symbol).
        Checks orderbook balances for spendable off-chain capacity.
        Prioritizes the target network when specified (e.g. Bitcoin Lightning leases MUST be paid in BTC,
        as the Hub liquidity service does not support cross-protocol ERC-20 decimal lookups for Bitcoin).
        Falls back to ('Arbitrum', 'erc20:0xaf88d065e77c8cc2239327c5edb3a432268e5831', 'USDC') if nothing found.
        """
        is_testnet = getattr(self.config, "testnet", False)
        if is_testnet:
            all_candidates = [
                ("Bitcoin", "0x0000000000000000000000000000000000000000000000000000000000000000", "BTC", "0a03cf40", 0.00005),
                ("Ethereum", "erc20:0x8cd0da3d001b013336918b8bc4e56d9dda1347e0", "USDC", "11155111", 0.50),
                ("Arbitrum", "erc20:0x1baabb04529d43a73232b713c0fe471f7c7334d5", "USDC", "421614", 0.50),
                ("Ethereum", "0x0000000000000000000000000000000000000000", "ETH", "11155111", 0.0005),
                ("Arbitrum", "0x0000000000000000000000000000000000000000", "ETH", "421614", 0.0005),
                ("Arbitrum", "erc20:0x67e6a7eae40107ff676908b28c3fc632a38f1499", "HDN", "421614", 20.0),
            ]
        else:
            all_candidates = [
                ("Bitcoin", "0x0000000000000000000000000000000000000000000000000000000000000000", "BTC", "f9beb4d9", 0.00005),
                ("Arbitrum", "erc20:0xaf88d065e77c8cc2239327c5edb3a432268e5831", "USDC", "42161", 0.50),
                ("Ethereum", "0x0000000000000000000000000000000000000000", "ETH", "1", 0.0005),
                ("Arbitrum", "0x0000000000000000000000000000000000000000", "ETH", "42161", 0.0005),
                ("Arbitrum", "erc20:0xb0f66bdb39acbb043308eb9dbe78f5bb47ea5430", "HDN", "42161", 20.0),
            ]
        if target_network == "Bitcoin":
            candidates = [c for c in all_candidates if c[0] == "Bitcoin"] + [c for c in all_candidates if c[0] != "Bitcoin"]
        elif target_network == "Ethereum":
            candidates = (
                [c for c in all_candidates if c[0] == "Ethereum"]
                + [c for c in all_candidates if c[0] == "Arbitrum"]
                + [c for c in all_candidates if c[0] == "Bitcoin"]
            )
        else:
            candidates = (
                [c for c in all_candidates if c[0] == "Arbitrum"]
                + [c for c in all_candidates if c[0] == "Ethereum"]
                + [c for c in all_candidates if c[0] == "Bitcoin"]
            )

        try:
            bals = self.client.get_orderbook_balances()
            spendable_map = {}
            for b in bals:
                c = b.get("currency", {})
                nid = c.get("networkId")
                aid = c.get("assetId", "").lower()
                val = float(b.get("balance", {}).get("sending", {}).get("value", 0.0))
                spendable_map[(nid, aid)] = val

            for net, aid, sym, nid, min_req in candidates:
                avail = spendable_map.get((nid, aid.lower()), 0.0)
                if avail >= min_req:
                    return (net, aid, sym)
        except Exception as e:
            logger.warning(f"Error querying spendable balances for rental payment: {e}")

        if is_testnet:
            return ("Ethereum", "erc20:0x8cd0da3d001b013336918b8bc4e56d9dda1347e0", "USDC")
        return ("Arbitrum", "erc20:0xaf88d065e77c8cc2239327c5edb3a432268e5831", "USDC")

    def execute_capacity_action(
        self,
        channel_action: Dict[str, Any],
        live: bool = False
    ) -> Dict[str, Any]:
        """
        Executes or simulates a capacity adjustment advised by the AI supervisor.
        Validates bounds, checks fee estimates against safety thresholds, and triggers execution.
        """
        if not channel_action or not isinstance(channel_action, dict):
            return {"status": "SKIPPED", "reason": "No channel action provided."}

        action_type = channel_action.get("action_type", "NONE").upper()
        if action_type == "NONE":
            return {"status": "SKIPPED", "reason": channel_action.get("reason", "No capacity increase advised.")}

        network_name = channel_action.get("network", "Arbitrum")
        asset_symbol = channel_action.get("asset", "USDC").upper()
        raw_amount = channel_action.get("amount", 25.0)

        try:
            amount = float(raw_amount)
        except (ValueError, TypeError):
            amount = 25.0 if asset_symbol == "USDC" else 100.0

        print(f"\n[CapacityManager] >>> EXECUTING ADVISED CAPACITY ACTION: {action_type} <<<")
        print(f"  Target Network : {network_name}")
        print(f"  Target Asset   : {asset_symbol}")
        print(f"  Target Amount  : {amount}")
        print(f"  Supervisor Note: {channel_action.get('reason', '')}")
        print(f"  Execution Mode : {'[LIVE]' if live else '[DRY-RUN]'}")

        # CRITICAL GUARD: If a rental or channel open/update is pending, do not execute new capacity adjustments
        pending_info = self.has_pending_channel_update(network_name, asset_symbol)
        if pending_info.get("pending"):
            msg = f"Cannot execute {action_type} on {network_name} for {asset_symbol}: {pending_info.get('reason')}"
            print(f"  [CapacityManager] SKIPPED: {msg}")
            return {"status": "SKIPPED", "reason": msg}

        # =================== RENT INBOUND CAPACITY ===================
        if action_type == "RENT_INBOUND":
            asset_id = self._resolve_asset_id(network_name, asset_symbol)
            if not asset_id:
                msg = f"Cannot resolve asset ID for {asset_symbol} on {network_name}"
                print(f"  [!] REJECTED: {msg}")
                return {"status": "FAILED", "error": msg}

            # Guard against duplicate rentals if a rental is already in-flight or pending on the Hub
            rent_key = f"{network_name}:{asset_symbol}"
            now = time.time()
            pending_timeout = getattr(self.config, "pending_rental_timeout_secs", 1800.0)
            last_req_time = self._pending_rentals.get(rent_key, 0.0)
            elapsed_since_req = now - last_req_time
            if elapsed_since_req < pending_timeout:
                msg = (
                    f"A liquidity rental for {asset_symbol} on {network_name} was initiated {int(elapsed_since_req)}s ago "
                    f"and is still pending/confirming with the Hub ({int(pending_timeout - elapsed_since_req)}s lockout remaining). "
                    f"Preventing duplicate Hub request."
                )
                print(f"  [CapacityManager] SKIPPED: {msg}")
                return {"status": "SKIPPED", "reason": msg}

            # Guard against duplicate rentals if a channel for THIS asset is already updating or confirming on-chain
            proto = "PROTOCOL_BITCOIN" if network_name == "Bitcoin" else "PROTOCOL_EVM"
            nid = self._net_id(network_name)
            existing_channels = self.get_channels_for_network(proto, nid, network_name)

            def _asset_matches(aid_a: str, aid_b: str) -> bool:
                if aid_a.lower() == aid_b.lower():
                    return True
                zero_eth = "0x0000000000000000000000000000000000000000"
                if aid_a.startswith(zero_eth) and aid_b.startswith(zero_eth):
                    return True
                return False

            timeout_secs = getattr(self.config, "max_inflight_operation_timeout_secs", 1800.0)
            is_updating = False
            for ch in existing_channels:
                if any(k in str(ch.status).upper() for k in ("UPDATING", "OPENING", "PENDING", "COOPERATIVELYOPENING")):
                    is_updating = True
                    break
                for aid, a in ch.assets.items():
                    if _asset_matches(aid, asset_id):
                        if a.is_updating or a.onchain_in_flight or a.pending_local > 0 or a.pending_remote > 0 or any(k in a.status.upper() for k in ("UPDATING", "OPENING", "PENDING", "COOPERATIVELYOPENING")):
                            if not a.is_operation_stale(timeout_secs):
                                is_updating = True
                                break
                            else:
                                print(f"  [CapacityManager] Note: In-flight operation on channel {ch.channel_id[:16]}... is stale (> {int(timeout_secs)}s). Ignoring to allow fresh capacity adjustment.")

            if is_updating:
                msg = f"A channel on {network_name} for {asset_symbol} is already updating or confirming on-chain. Waiting for block confirmation before requesting new liquidity."
                print(f"  [CapacityManager] SKIPPED: {msg}")
                return {"status": "SKIPPED", "reason": msg}

            asset_price = self.get_fiat_price(asset_symbol)

            # Bitcoin Lightning channels cannot be deposited into; each rental opens a new channel.
            # Ratchet Bitcoin rental size based on the smallest remaining channel capacity * 1.5
            # (or smallest_remaining + 1.5x deficit if greater). This allows Bitcoin rentals to grow
            # organically while reserving OPEN_CHANNEL for the largest channels.
            if network_name == "Bitcoin":
                smallest_remaining_cap = self.get_smallest_remaining_channel_capacity("Bitcoin")
                deficit = float(channel_action.get("deficit", amount))
                if smallest_remaining_cap > 0:
                    ratchet_target = round(max(smallest_remaining_cap * 1.5, smallest_remaining_cap + (1.5 * deficit)), 6)
                    if ratchet_target > amount:
                        logger.info(
                            f"[BTC Channel Manager] Ratchet rental sizing target: {ratchet_target:.6f} BTC "
                            f"(smallest remaining: {smallest_remaining_cap:.6f} BTC, 1.5x baseline: {(smallest_remaining_cap * 1.5):.6f}, deficit: {deficit:.6f})."
                        )
                        amount = ratchet_target

            est_value_usd = amount * asset_price

            # Enforce Hub lease constraints: Minimum $10.00 USD
            min_hub_lease_usd = getattr(self.config, "min_auto_rental_amount_usd", 10.0)
            if est_value_usd < min_hub_lease_usd:
                min_amt = round((min_hub_lease_usd + 0.50) / asset_price, 6 if asset_symbol in ("ETH", "BTC") else 2)
                print(f"  [!] Note: Requested rental ${est_value_usd:.2f} is below Hub minimum ${min_hub_lease_usd:.2f}. Adjusting to {min_amt} {asset_symbol} (~${(min_amt * asset_price):.2f} USD).")
                amount = min_amt
                est_value_usd = amount * asset_price

            # Enforce max_auto_rental_amount_usd ONLY for non-Bitcoin networks (Lithium/EVM).
            # Bitcoin rentals ratchet organically without the arbitrary $250 cap, protected by max_auto_rental_fee_usd.
            if network_name != "Bitcoin" and est_value_usd > self.config.max_auto_rental_amount_usd:
                max_amt = round(self.config.max_auto_rental_amount_usd / asset_price, 6 if asset_symbol in ("ETH", "BTC") else 2)
                print(f"  [!] SAFETY: Requested rental ${est_value_usd:.2f} exceeds cap ${self.config.max_auto_rental_amount_usd:.2f}. Clamping to {max_amt} {asset_symbol}.")
                amount = max_amt

            # Ensure peer connection with the Hub before fee estimation or rental
            if not self.ensure_peer_connected(network_name):
                msg = f"Could not establish active peer session with {network_name} Hub peer."
                print(f"  [!] CONNECTION NOTICE: {msg}")
                return {"status": "FAILED", "error": msg}

            # Dynamically select an off-chain asset with spendable balance to pay lease fee
            pay_net, pay_aid, pay_sym = self.find_best_rental_payment_asset(target_network=network_name)
            print(f"  Fee Payment via: {pay_net} {pay_sym}")

            target_cid = channel_action.get("channel_id")
            try:
                est = self.estimate_rental_fee(
                    network_name=network_name,
                    asset_id=asset_id,
                    amount=amount,
                    duration_seconds=self.config.default_lease_duration_secs,
                    payment_network_name=pay_net,
                    payment_asset_id=pay_aid,
                    channel_id=target_cid,
                )
            except Exception as e:
                desc_err = extract_descriptive_error(e)
                print(f"  [!] Fee estimation note: {desc_err}")
                return {"status": "FAILED", "error": desc_err}

            fee_str = est.get("fee", "0.0")
            try:
                raw_fee = float(fee_str)
            except ValueError:
                raw_fee = 0.0

            pay_price = self.get_fiat_price(pay_sym)
            fee_usd = raw_fee * pay_price

            print(f"  Estimated Service Fee: {fee_str} {pay_sym} (~${fee_usd:.4f} USD)")

            if fee_usd > self.config.max_auto_rental_fee_usd:
                msg = f"Fee ${fee_usd:.2f} exceeds safety maximum ${self.config.max_auto_rental_fee_usd:.2f}"
                print(f"  [!] REJECTED BY SAFETY GUARDRAILS: {msg}")
                return {"status": "REJECTED_FEE_LIMIT", "error": msg, "fee": fee_usd}

            try:
                # Enforce Bitcoin 4-channel limit before renting
                channels_to_close_after = []
                closed_before = []
                if network_name == "Bitcoin":
                    slot_prep = self.prepare_btc_channel_slots("RENT_INBOUND", live=live, payment_network_name=pay_net)
                    if slot_prep.get("status") == "BLOCKED":
                        msg = slot_prep.get("reason", "Bitcoin channel slots full.")
                        print(f"  [CapacityManager] SKIPPED: {msg}")
                        return {"status": "SKIPPED", "reason": msg}
                    channels_to_close_after = slot_prep.get("channels_to_close_after", [])
                    closed_before = slot_prep.get("closed_before", [])

                # If a channel was closed to vacate a slot, pause briefly to allow node and Hub state synchronization
                if live and closed_before:
                    logger.info(f"[BTC Channel Manager] Closed channel {closed_before[0][:16]}... to vacate slot. Pausing 2.0s for Hub state synchronization...")
                    time.sleep(2.0)

                # Mark as pending immediately before network dispatch so timeouts don't cause instant re-spams
                self._pending_rentals[rent_key] = time.time()

                max_attempts = 3 if (closed_before and live) else 1
                res = None
                last_err = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        res = self.rent_liquidity(
                            network_name=network_name,
                            asset_id=asset_id,
                            amount=amount,
                            duration_seconds=self.config.default_lease_duration_secs,
                            payment_network_name=pay_net,
                            payment_asset_id=pay_aid,
                            channel_id=target_cid,
                            live=live,
                        )
                        break
                    except Exception as err:
                        last_err = err
                        if attempt < max_attempts:
                            wait_s = attempt * 2.0
                            logger.warning(
                                f"[BTC Channel Manager] RENT_INBOUND attempt {attempt}/{max_attempts} failed ({err}). "
                                f"Retrying with fresh quote in {wait_s}s..."
                            )
                            time.sleep(wait_s)

                if res is None:
                    if closed_before and network_name == "Bitcoin":
                        logger.warning(
                            f"[BTC Channel Manager] RENT_INBOUND failed after {max_attempts} attempts following channel closure ({last_err}). "
                            f"Falling back to operating with remaining 3 channels. Closed channel funds will settle on-chain."
                        )
                    raise last_err

                status = "SUCCESS" if live else "DRY_RUN_SUCCESS"
                print(f"  [CapacityManager] {status}: Inbound liquidity {est['operation_type']} processed.")

                # If any channels were scheduled to close after, prune them now
                if channels_to_close_after:
                    for ch_to_close in channels_to_close_after:
                        cid = ch_to_close.channel_id
                        cap = sum(a.free_local + a.free_remote for a in ch_to_close.assets.values())
                        print(f"  [BTC Channel Manager] Pruning smallest active channel {cid[:16]}... (capacity: {cap:.8f} BTC) to maintain <= 4 channel limit.")
                        try:
                            self.close_channel("Bitcoin", cid, fee_option="medium", live=live)
                        except Exception as e:
                            logger.error(f"Failed to prune smallest channel {cid[:16]}...: {e}")

                return {"status": status, "result": res, "amount": amount, "fee": fee_usd}
            except Exception as e:
                # Clear pending lockout on failure so subsequent attempts or fallbacks are not blocked for 10 minutes
                self._pending_rentals.pop(rent_key, None)
                desc_err = extract_descriptive_error(e)
                print(f"  [!] Rental execution note: {desc_err}")
                return {"status": "FAILED", "error": desc_err}

        # =================== DEPOSIT INTO EXISTING CHANNEL ===================
        elif action_type == "DEPOSIT_CHANNEL":
            # For Bitcoin (which does not support in-flight channel splice/deposits), route directly to OPEN_CHANNEL
            if network_name == "Bitcoin":
                msg = f"In-flight channel deposits not supported on Bitcoin Lightning. Routing to OPEN_CHANNEL to expand send capacity."
                print(f"  [CapacityManager] {msg}")
                channel_action["action_type"] = "OPEN_CHANNEL"
                return self.execute_capacity_action(channel_action, live=live)

            proto = "PROTOCOL_BITCOIN" if network_name == "Bitcoin" else "PROTOCOL_EVM"
            nid = self._net_id(network_name)
            existing_channels = self.get_channels_for_network(proto, nid, network_name)
            asset_id = self._resolve_asset_id(network_name, asset_symbol)
            if not asset_id:
                msg = f"Cannot resolve asset ID for {asset_symbol} on {network_name}"
                print(f"  [!] REJECTED: {msg}")
                return {"status": "FAILED", "error": msg}

            # Find active channel with the Hub for this asset (targeting specific channel if requested)
            target_ch_id = channel_action.get("channel_id")
            target_ch = None
            if target_ch_id:
                for ch in existing_channels:
                    if ch.channel_id.lower() == target_ch_id.lower():
                        target_ch = ch
                        break

            if not target_ch:
                if network_name in ("Arbitrum", "Ethereum"):
                    target_ch = self.get_primary_lithium_channel(network_name, required_asset_id=asset_id)
                    if not target_ch:
                        target_ch = self.get_primary_lithium_channel(network_name)
                    if target_ch:
                        print(
                            f"  [CapacityManager] Concentrating on primary Lithium channel {target_ch.channel_id[:16]}... "
                            f"on {network_name} for {asset_symbol} deposit."
                        )

            if not target_ch:
                candidates = []
                for ch in existing_channels:
                    for aid, a in ch.assets.items():
                        is_match = (
                            aid.lower() == asset_id.lower() or (
                                asset_id.startswith("0x0000000000000000000000000000000000000000") and
                                aid.startswith("0x0000000000000000000000000000000000000000")
                            )
                        )
                        if is_match and (a.is_active or ch.status in ("CHANNEL_STATUS_ACTIVE", "CHANNEL_STATUS_UPDATING", "active", "updating")):
                            tot_cap = getattr(a, "free_local", 0.0) + getattr(a, "free_remote", 0.0)
                            candidates.append((ch, a, tot_cap))
                            break

                if candidates:
                    # Sort candidates so the consolidated primary channel is chosen:
                    # 1. Total capacity descending (places funds where channel scale is largest)
                    # 2. Remote inbound capacity descending (prioritizes channels that facilitate two-way trades)
                    candidates.sort(key=lambda item: (item[2], getattr(item[1], "free_remote", 0.0)), reverse=True)
                    target_ch = candidates[0][0]
                    chosen_a = candidates[0][1]
                    print(
                        f"  [CapacityManager] Selected primary channel {target_ch.channel_id[:16]}... for {asset_symbol} on {network_name} "
                        f"(total capacity: {candidates[0][2]:.4f}, remote: {getattr(chosen_a, 'free_remote', 0.0):.4f}, local: {getattr(chosen_a, 'free_local', 0.0):.4f})."
                    )

            if not target_ch:
                msg = f"No active channel found on {network_name} for {asset_symbol}. Routing to OPEN_CHANNEL."
                print(f"  [CapacityManager] {msg}")
                channel_action["action_type"] = "OPEN_CHANNEL"
                return self.execute_capacity_action(channel_action, live=live)

            # Guard against duplicate operations if any channel for this asset is already updating or confirming on-chain
            timeout_secs = getattr(self.config, "max_inflight_operation_timeout_secs", 600.0)
            is_in_flight = any(
                (aid.lower() == asset_id.lower() or (
                    asset_id.startswith("0x0000000000000000000000000000000000000000") and
                    aid.startswith("0x0000000000000000000000000000000000000000")
                )) and
                (a.onchain_in_flight or a.pending_local > 0 or a.status in ("cooperativelyOpening", "opening", "updating")) and
                not a.is_operation_stale(timeout_secs)
                for ch in existing_channels
                for aid, a in ch.assets.items()
            )
            if is_in_flight:
                msg = f"A channel on {network_name} for {asset_symbol} is already updating or confirming on-chain. Waiting for block confirmation."
                print(f"  [CapacityManager] SKIPPED: {msg}")
                return {"status": "SKIPPED", "reason": msg}

            # Validate amount against confirmed on-chain balance
            raw_bals = self.client.get_wallet_balances(proto, nid)
            conf_bal = float(raw_bals.get(asset_id, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))
            if conf_bal <= 0:
                msg = f"Zero confirmed on-chain balance for {asset_symbol} on {network_name}."
                print(f"  [CapacityManager] REJECTED: {msg}")
                return {"status": "REJECTED_NO_BALANCE", "error": msg}

            # Collateral reserve check for USDC on Arbitrum (protects GMX Agent bot)
            usdc_reserve = getattr(self.config, "min_onchain_usdc_reserve_usd", 1030.0)
            if asset_symbol == "USDC" and network_name == "Arbitrum" and usdc_reserve > 0:
                usable_usdc = self._calculate_usable_usdc(conf_bal, usdc_reserve)
                if amount > usable_usdc:
                    total_eq = self._get_bot_total_equity()
                    eq_info = f"current total equity is ${total_eq:.2f}, " if total_eq else ""
                    msg = (
                        f"Cannot deposit {amount:.2f} USDC on {network_name}: must retain at least "
                        f"${usdc_reserve:.2f} collateral reserve for GMX Agent bot ({eq_info}"
                        f"confirmed wallet USDC: ${conf_bal:.2f}, usable: ${usable_usdc:.2f})."
                    )
                    print(f"  [CapacityManager] REJECTED BY COLLATERAL RESERVE: {msg}")
                    return {"status": "REJECTED_COLLATERAL_RESERVE", "error": msg}

            # Gas reserve check
            reserve_usd = self.config.min_onchain_gas_reserve_usd.get(network_name, 0.0)
            if reserve_usd > 0 and network_name in ("Ethereum", "Arbitrum"):
                eth_p = self.get_fiat_price("ETH")
                min_res_eth = (reserve_usd / eth_p) if eth_p > 0 else 0.0
                native_aid = "0x0000000000000000000000000000000000000000"
                conf_eth = float(raw_bals.get(native_aid, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))
                if asset_symbol == "ETH":
                    usable = max(0.0, conf_eth - min_res_eth)
                    if amount > usable:
                        msg = (
                            f"Cannot deposit {amount:.6f} ETH on {network_name}: must retain at least "
                            f"${reserve_usd:.2f} ({min_res_eth:.6f} ETH) for on-chain gas. "
                            f"(Confirmed: {conf_eth:.6f} ETH, usable: {usable:.6f} ETH)."
                        )
                        print(f"  [CapacityManager] REJECTED BY GAS RESERVE: {msg}")
                        return {"status": "REJECTED_GAS_RESERVE", "error": msg}
                else:
                    min_gas_floor = 0.0002
                    if conf_eth < min_gas_floor:
                        msg = (
                            f"Cannot deposit {asset_symbol} on {network_name}: confirmed ETH balance ({conf_eth:.6f} ETH) "
                            f"is below the minimum required for transaction gas ({min_gas_floor:.6f} ETH)."
                        )
                        print(f"  [CapacityManager] REJECTED BY GAS: {msg}")
                        return {"status": "REJECTED_GAS", "error": msg}

            deposit_amt = min(amount, conf_bal)
            asset_amounts = {asset_id: str(deposit_amt)}

            try:
                res = self.deposit_channel(
                    network_name=network_name,
                    channel_id=target_ch.channel_id,
                    asset_amounts=asset_amounts,
                    fee_option="medium",
                    live=live,
                )
                status = "SUCCESS" if live else "DRY_RUN_SUCCESS"
                print(f"  [CapacityManager] {status}: Deposit of {deposit_amt} {asset_symbol} into channel {target_ch.channel_id[:10]}... initiated.")
                return {"status": status, "result": res, "amount": deposit_amt, "channel_id": target_ch.channel_id}
            except Exception as e:
                desc_err = extract_descriptive_error(e)
                print(f"  [!] Channel deposit execution note: {desc_err}")
                return {"status": "FAILED", "error": desc_err}

        # =================== OPEN NATIVE CHANNEL ===================
        elif action_type == "OPEN_CHANNEL":
            # Guard against duplicate opens if a channel is already confirming
            proto = "PROTOCOL_BITCOIN" if network_name == "Bitcoin" else "PROTOCOL_EVM"
            nid = self._net_id(network_name)
            existing_channels = self.get_channels_for_network(proto, nid, network_name)

            # Prevent opening redundant Lithium channels if an active one already exists
            if network_name in ("Ethereum", "Arbitrum"):
                primary_ch = self.get_primary_lithium_channel(network_name)
                if primary_ch:
                    print(
                        f"  [CapacityManager] Active Lithium channel {primary_ch.channel_id[:16]}... already exists on {network_name}. "
                        f"Redirecting OPEN_CHANNEL to DEPOSIT_CHANNEL to maintain single-channel concentration."
                    )
                    channel_action["action_type"] = "DEPOSIT_CHANNEL"
                    channel_action["channel_id"] = primary_ch.channel_id
                    return self.execute_capacity_action(channel_action, live=live)

            asset_id = self._resolve_asset_id(network_name, asset_symbol)
            if not asset_id:
                msg = f"Cannot resolve asset ID for {asset_symbol} on {network_name}"
                print(f"  [!] REJECTED: {msg}")
                return {"status": "FAILED", "error": msg}

            timeout_secs = getattr(self.config, "max_inflight_operation_timeout_secs", 600.0)
            is_opening = any(
                (aid.lower() == asset_id.lower() or (asset_id.startswith("0x0000000000000000000000000000000000000000") and aid.startswith("0x0000000000000000000000000000000000000000"))) and
                (a.status in ("cooperativelyOpening", "opening") or a.onchain_in_flight or a.pending_local > 0) and
                not a.is_operation_stale(timeout_secs)
                for ch in existing_channels
                for aid, a in ch.assets.items()
            )
            if is_opening:
                msg = f"State channel on {network_name} for {asset_symbol} is already opening and confirming on-chain. Waiting for block confirmation."
                print(f"  [CapacityManager] SKIPPED: {msg}")
                return {"status": "SKIPPED", "reason": msg}

            # Collateral reserve check for USDC on Arbitrum (protects GMX Agent bot)
            usdc_reserve = getattr(self.config, "min_onchain_usdc_reserve_usd", 1030.0)
            if asset_symbol == "USDC" and network_name == "Arbitrum" and usdc_reserve > 0:
                raw_bals_chk = self.client.get_wallet_balances(proto, nid)
                conf_usdc_chk = float(raw_bals_chk.get(asset_id, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))
                usable_usdc = self._calculate_usable_usdc(conf_usdc_chk, usdc_reserve)
                if amount > usable_usdc:
                    total_eq = self._get_bot_total_equity()
                    eq_info = f"current total equity is ${total_eq:.2f}, " if total_eq else ""
                    msg = (
                        f"Cannot open channel with {amount:.2f} USDC on {network_name}: must retain at least "
                        f"${usdc_reserve:.2f} collateral reserve for GMX Agent bot ({eq_info}"
                        f"confirmed wallet USDC: ${conf_usdc_chk:.2f}, usable: ${usable_usdc:.2f})."
                    )
                    print(f"  [CapacityManager] REJECTED BY COLLATERAL RESERVE: {msg}")
                    return {"status": "REJECTED_COLLATERAL_RESERVE", "error": msg}

            # Check gas reserve threshold before attempting open
            reserve_usd = self.config.min_onchain_gas_reserve_usd.get(network_name, 0.0)
            if reserve_usd > 0 and network_name in ("Ethereum", "Arbitrum"):
                eth_p = self.get_fiat_price("ETH")
                min_res_eth = (reserve_usd / eth_p) if eth_p > 0 else 0.0
                raw_bals = self.client.get_wallet_balances(proto, nid)
                native_aid = "0x0000000000000000000000000000000000000000"
                conf_eth = float(raw_bals.get(native_aid, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))
                if asset_symbol == "ETH":
                    usable = max(0.0, conf_eth - min_res_eth)
                    if amount > usable:
                        msg = (
                            f"Cannot deposit {amount:.6f} ETH on {network_name}: must retain at least "
                            f"${reserve_usd:.2f} ({min_res_eth:.6f} ETH) for on-chain gas. "
                            f"(Confirmed: {conf_eth:.6f} ETH, usable: {usable:.6f} ETH)."
                        )
                        print(f"  [CapacityManager] REJECTED BY GAS RESERVE: {msg}")
                        return {"status": "REJECTED_GAS_RESERVE", "error": msg}
                else:
                    min_gas_floor = 0.0002
                    if conf_eth < min_gas_floor:
                        msg = (
                            f"Cannot deposit {asset_symbol} on {network_name}: confirmed ETH balance ({conf_eth:.6f} ETH) "
                            f"is below the minimum required for transaction gas ({min_gas_floor:.6f} ETH)."
                        )
                        print(f"  [CapacityManager] REJECTED BY GAS: {msg}")
                        return {"status": "REJECTED_GAS", "error": msg}

            # Validate confirmed on-chain balance for all assets
            raw_bals = self.client.get_wallet_balances(proto, nid)
            conf_bal = float(raw_bals.get(asset_id, {}).get("onchain", {}).get("confirmed", {}).get("value", 0.0))
            if conf_bal <= 0:
                msg = f"Zero confirmed on-chain balance for {asset_symbol} on {network_name}."
                print(f"  [CapacityManager] REJECTED: {msg}")
                return {"status": "REJECTED_NO_BALANCE", "error": msg}

            if network_name == "Bitcoin":
                min_btc_usd = getattr(self.config, "min_onchain_btc_open_usd", 40.0)
                min_btc_amt = getattr(self.config, "min_onchain_btc_open_amt", 0.0004)
                btc_price = self.get_fiat_price("BTC")
                conf_val_usd = conf_bal * btc_price
                if conf_bal < min_btc_amt or conf_val_usd < min_btc_usd:
                    msg = (
                        f"Cannot open Bitcoin channel: confirmed balance {conf_bal:.8f} BTC (${conf_val_usd:.2f}) "
                        f"is below minimum required threshold ({min_btc_amt:.8f} BTC / ${min_btc_usd:.2f} USD). "
                        f"Dust channel creation prevented."
                    )
                    print(f"  [CapacityManager] REJECTED BY DUST THRESHOLD: {msg}")
                    return {"status": "REJECTED_DUST_THRESHOLD", "error": msg}

                max_avail = round(max(0.0001, conf_bal - 0.00005), 6)
                largest_cap = self.get_largest_channel_capacity("Bitcoin")
                deficit = float(channel_action.get("deficit", amount))
                ratchet_target = largest_cap + (1.5 * deficit)

                # Open the best channel possible with available confirmed on-chain funds.
                # If on-chain balance can support ratchet scaling (largest_cap + 1.5x deficit), size up to ratchet target.
                # If available funds are less than ratchet target (even if less than largest_cap), open the maximum available (max_avail)
                # to open the best channel possible, rather than artificially restricting the channel to a micro-amount/deficit (e.g. 0.00015 BTC).
                if channel_action.get("ratchet", False) and largest_cap > 0:
                    target_amt = round(ratchet_target, 6)
                    open_amt = min(max_avail, target_amt) if max_avail >= target_amt else max_avail
                    logger.info(
                        f"[BTC Channel Manager] Ratchet sizing target: {ratchet_target:.6f} BTC "
                        f"(largest: {largest_cap:.6f} + 1.5x deficit: {deficit:.6f}). Executing open with best available: {open_amt:.6f} BTC."
                    )
                elif channel_action.get("ratchet", False):
                    open_amt = max_avail
                else:
                    open_amt = min(amount, max_avail)
            else:
                open_amt = min(amount, conf_bal)
            asset_amounts = {asset_id: str(open_amt)}

            try:
                est = self.estimate_open_channel_fee(network_name, asset_amounts, fee_option="medium")
                fee_val = est.get("fee", {}).get("value", "0.0")
                print(f"  Estimated On-Chain Gas Fee: {fee_val}")
            except Exception as e:
                desc_err = extract_descriptive_error(e)
                print(f"  [!] Open channel fee estimation note: {desc_err}")
                return {"status": "FAILED", "error": desc_err}

            try:
                # Enforce Bitcoin 4-channel limit before opening
                channels_to_close_after = []
                closed_before = []
                if network_name == "Bitcoin":
                    slot_prep = self.prepare_btc_channel_slots("OPEN_CHANNEL", live=live, asset_amounts=asset_amounts)
                    if slot_prep.get("status") == "BLOCKED":
                        msg = slot_prep.get("reason", "Bitcoin channel slots full.")
                        print(f"  [CapacityManager] SKIPPED: {msg}")
                        return {"status": "SKIPPED", "reason": msg}
                    channels_to_close_after = slot_prep.get("channels_to_close_after", [])
                    closed_before = slot_prep.get("closed_before", [])

                # If a channel was closed to vacate a slot, pause briefly to allow node and Hub state synchronization
                if live and closed_before:
                    logger.info(f"[BTC Channel Manager] Closed channel {closed_before[0][:16]}... to vacate slot. Pausing 2.0s for Hub state synchronization...")
                    time.sleep(2.0)

                max_attempts = 3 if (closed_before and live) else 1
                res = None
                last_err = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        res = self.open_channel(network_name, asset_amounts, fee_option="medium", live=live)
                        break
                    except Exception as err:
                        last_err = err
                        if attempt < max_attempts:
                            wait_s = attempt * 2.0
                            logger.warning(
                                f"[BTC Channel Manager] OPEN_CHANNEL attempt {attempt}/{max_attempts} failed ({err}). "
                                f"Retrying in {wait_s}s..."
                            )
                            time.sleep(wait_s)

                if res is None:
                    if closed_before and network_name == "Bitcoin":
                        logger.warning(
                            f"[BTC Channel Manager] OPEN_CHANNEL failed after {max_attempts} attempts following channel closure ({last_err}). "
                            f"Falling back to operating with remaining 3 channels."
                        )
                    raise last_err

                status = "SUCCESS" if live else "DRY_RUN_SUCCESS"
                self._pending_opens[f"{network_name}:{asset_symbol}"] = time.time()
                print(f"  [CapacityManager] {status}: Channel opening on {network_name} initiated.")

                # If any channels were scheduled to close after, prune them now
                if channels_to_close_after:
                    for ch_to_close in channels_to_close_after:
                        cid = ch_to_close.channel_id
                        cap = sum(a.free_local + a.free_remote for a in ch_to_close.assets.values())
                        print(f"  [BTC Channel Manager] Pruning smallest active channel {cid[:16]}... (capacity: {cap:.8f} BTC) to maintain <= 4 channel limit.")
                        try:
                            self.close_channel("Bitcoin", cid, fee_option="medium", live=live)
                        except Exception as e:
                            logger.error(f"Failed to prune smallest channel {cid[:16]}...: {e}")

                return {"status": status, "result": res, "amount": open_amt}
            except Exception as e:
                desc_err = extract_descriptive_error(e)
                print(f"  [!] Open channel execution note: {desc_err}")
                return {"status": "FAILED", "error": desc_err}

        else:
            return {"status": "UNKNOWN_ACTION", "error": f"Unknown action type {action_type}"}
