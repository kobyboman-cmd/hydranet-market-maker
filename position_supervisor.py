"""
Deterministic Position Supervisor and Risk Engine for Hydranet DEX DLMM.
Computes inventory skew, reservation price bias, dynamic spread multipliers,
and automated channel capacity management using mathematical formulas
with zero external API calls, zero latency, and zero rate limits.
"""

import logging
from typing import Dict, Any, Optional, Tuple, Union
from config import BotConfig, MARKETS, TESTNET_MARKETS

logger = logging.getLogger("PositionSupervisor")


class SupervisorDecision:
    """Represents a structured market making and risk decision."""

    def __init__(
        self,
        spread_multiplier: float = 1.0,
        center_price_bias_pct: float = 0.0,
        curve_concentration: str = "BALANCED",
        skew_factor: float = 0.0,
        bid_size_multiplier: float = 1.0,
        ask_size_multiplier: float = 1.0,
        risk_level: str = "LOW",
        allow_trading: bool = True,
        channel_advice: str = "Capacity adequate for current quoting.",
        channel_action: Optional[Dict[str, Any]] = None,
        commentary: str = "Operating within normal parameters.",
        base_ratio: float = 0.50,
        quote_ratio: float = 0.50,
        bid_spread_multiplier: float = 1.0,
        ask_spread_multiplier: float = 1.0,
        target_base_ratio: float = 0.50,
        relative_sentiment: Union[int, float] = 0.0,
    ):
        self.spread_multiplier = max(0.8, min(float(spread_multiplier), 3.5))
        self.bid_spread_multiplier = max(0.8, min(float(bid_spread_multiplier), 3.5))
        self.ask_spread_multiplier = max(0.8, min(float(ask_spread_multiplier), 3.5))
        self.target_base_ratio = round(float(target_base_ratio), 4)
        self.relative_sentiment = round(float(relative_sentiment), 2)
        self.center_price_bias_pct = max(-0.03, min(float(center_price_bias_pct), 0.03))
        self.curve_concentration = (
            curve_concentration.upper()
            if curve_concentration.upper() in ("CONCENTRATED", "BALANCED", "WIDE", "STABLESWAP PEG", "PEG-WALL")
            else "BALANCED"
        )
        self.skew_factor = max(-1.0, min(float(skew_factor), 1.0))
        self.bid_size_multiplier = max(0.2, min(float(bid_size_multiplier), 2.0))
        self.ask_size_multiplier = max(0.2, min(float(ask_size_multiplier), 2.0))
        self.risk_level = risk_level.upper()
        self.allow_trading = bool(allow_trading)
        self.channel_advice = channel_advice
        self.channel_action = channel_action or {"action_type": "NONE"}
        self.commentary = commentary
        self.base_ratio = round(float(base_ratio), 4)
        self.quote_ratio = round(float(quote_ratio), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spread_multiplier": self.spread_multiplier,
            "bid_spread_multiplier": self.bid_spread_multiplier,
            "ask_spread_multiplier": self.ask_spread_multiplier,
            "target_base_ratio": self.target_base_ratio,
            "relative_sentiment": self.relative_sentiment,
            "center_price_bias_pct": self.center_price_bias_pct,
            "curve_concentration": self.curve_concentration,
            "skew_factor": self.skew_factor,
            "bid_size_multiplier": self.bid_size_multiplier,
            "ask_size_multiplier": self.ask_size_multiplier,
            "risk_level": self.risk_level,
            "allow_trading": self.allow_trading,
            "channel_advice": self.channel_advice,
            "channel_action": self.channel_action,
            "commentary": self.commentary,
            "base_ratio": self.base_ratio,
            "quote_ratio": self.quote_ratio,
        }

    def __repr__(self) -> str:
        act_type = (
            self.channel_action.get("action_type", "NONE")
            if isinstance(self.channel_action, dict)
            else "NONE"
        )
        return (
            f"<Decision Risk={self.risk_level} Spread={self.spread_multiplier:.2f}x "
            f"CenterBias={self.center_price_bias_pct:+.2%} Curve={self.curve_concentration} "
            f"ChannelAction={act_type} AllowTrading={self.allow_trading}>"
        )


class DeterministicSupervisor:
    """
    High-performance mathematical position supervisor and risk manager.
    Calculates inventory skew, Avellaneda-Stoikov reservation prices,
    dynamic spreads, and channel capacity actions deterministically.
    """

    def __init__(self, config: Optional[BotConfig] = None):
        self.config = config or BotConfig()
        self._fallbacks: Dict[str, float] = {}
        if getattr(self.config, "use_external_oracle", True):
            try:
                from external_oracle import get_external_oracle
                self.external_oracle = get_external_oracle()
            except Exception:
                self.external_oracle = None
        else:
            self.external_oracle = None

    def update_oracle_prices(self, prices: Dict[str, float]):
        """Dynamically updates the supervisor's oracle reference prices from hydra-app or external feed."""
        if prices:
            for k, v in prices.items():
                if v and v > 0:
                    self._fallbacks[k] = float(v)

    def _fetch_oracle_price(self, pair_name: str) -> float:
        """
        Fetches live reference oracle price for a trading pair.
        Prioritizes independent 3rd-party oracle feeds (Coinbase/Kraken/DexScreener)
        over the internal node pricing.
        """
        if "/" in pair_name:
            parts = pair_name.split("/")
            base = parts[0].split("(")[0].split("_")[0].strip()
            quote = parts[1].split("(")[0].split("_")[0].strip()
        else:
            base, quote = pair_name, "USDC"

        if base == quote:
            return 1.0

        if getattr(self, "external_oracle", None):
            fallback_val = self._fallbacks.get(base, 0.0)
            return self.external_oracle.get_reference_cross_rate(base, quote, fallback_rate=fallback_val)

        b_p = self._fallbacks.get(base, 0.0)
        q_p = self._fallbacks.get(quote, 1.0)
        return (b_p / q_p) if (b_p > 0 and q_p > 0) else (b_p if b_p > 0 else 0.0)

    def calculate_sentiment_target_ratio(self, pair_name_or_base: str, quote_symbol: Optional[str] = None) -> Tuple[float, float]:
        """Returns the set-it-and-forget-it target base ratio (default 50/50 equilibrium)."""
        target_ratio = round(float(getattr(self.config, "target_base_ratio", 0.50)), 4)
        return target_ratio, 0.0

    def calculate_shared_inbound_requirement(
        self, network_name: str, asset_symbol: str, total_channel_cap: float = 0.0, fiat_price: float = 0.0
    ) -> float:
        """
        Calculates the aggregate inbound (receivable) rental amount required across ALL active target pairs
        sharing a channel asset on a given network (Bitcoin Lightning or EVM Lithium).

        When inbound capacity is depleted, multiple pairs sharing that channel asset degrade simultaneously:
        - Pairs where the asset is BASE degrade on Bids (cannot receive base).
        - Pairs where the asset is QUOTE degrade on Asks (cannot receive quote).

        This aggregates the structural ladder requirements (min_amount, hub placement floor, and rung order sizes)
        across all competitor pairs that require inbound receiving capacity for this asset, scaled with:
        - 1.5x Gaussian bell-curve distribution multiplier to prevent inner rung clamping.
        - 1 / 0.90 orderbook channel haircut buffer.
        - Cross-pair coexistence multiplier (1 + (K-1)/K) when K >= 2 pairs share the channel.
        - target_auto_rental_amount_usd floor and max_auto_rental_amount_usd ceiling.
        """
        target_rungs = getattr(self.config, "rungs_per_side", 15)
        min_floor_usd = getattr(self.config, "hub_min_placement_floor_usd", 0.75)
        target_pct = getattr(self.config, "target_capacity_increase_pct", 0.10)
        min_hub_usd = getattr(self.config, "min_auto_rental_amount_usd", 10.0)
        target_hub_usd = getattr(self.config, "target_auto_rental_amount_usd", 25.0)
        max_hub_usd = getattr(self.config, "max_auto_rental_amount_usd", 250.0)

        active_markets = TESTNET_MARKETS if getattr(self.config, "testnet", False) else MARKETS
        total_inbound_req = 0.0
        competitor_count = 0
        active_pairs = getattr(self.config, "active_pairs_all", None) or list(active_markets.keys())

        # Determine fiat price of the asset being rented
        asset_fiat = fiat_price if fiat_price > 0 else float(self._fallbacks.get(asset_symbol, 1.0))

        for pair_name in active_pairs:
            if pair_name not in active_markets:
                continue
            m = active_markets[pair_name]

            b_fiat = float(self._fallbacks.get(m.base.symbol, 1.0))
            q_fiat = float(self._fallbacks.get(m.quote.symbol, 1.0))
            if m.base.symbol == asset_symbol and asset_fiat > 0:
                b_fiat = asset_fiat
            if m.quote.symbol == asset_symbol and asset_fiat > 0:
                q_fiat = asset_fiat

            min_rung_b = max(m.base.min_amount, (min_floor_usd / b_fiat) if b_fiat > 0 else m.base.min_amount)
            quote_mid = (b_fiat / q_fiat) if q_fiat > 0 else 1.0
            min_rung_q = max(
                m.quote.min_amount,
                (min_floor_usd / q_fiat) if q_fiat > 0 else m.quote.min_amount,
                min_rung_b * quote_mid,
            )

            # Check if this pair needs this asset as Base (for Bids)
            if m.base.network_name == network_name and m.base.symbol == asset_symbol:
                q_min_as_b = (m.quote.min_amount / quote_mid) if quote_mid > 0 else 0.0
                eff_min_b = max(min_rung_b, q_min_as_b)
                total_inbound_req += eff_min_b * target_rungs
                competitor_count += 1

            # Check if this pair needs this asset as Quote (for Asks)
            elif m.quote.network_name == network_name and m.quote.symbol == asset_symbol:
                total_inbound_req += min_rung_q * target_rungs
                competitor_count += 1

        # Scale for Gaussian bell-curve distribution weights (1.5x) and 90% channel safety haircut (/ 0.90)
        healthy_inbound = (total_inbound_req * 1.5) / 0.90

        # When K >= 2 pairs share the channel asset, scale by cross-pair fair-share reservation headroom
        # so competitor reservations do not starve each other
        if competitor_count > 1:
            healthy_inbound *= (1.0 + (competitor_count - 1.0) / competitor_count)

        cap_target = total_channel_cap * target_pct if total_channel_cap > 0 else 0.0
        hub_floor = (min_hub_usd + 0.50) / asset_fiat if asset_fiat > 0 else 0.0
        target_floor = target_hub_usd / asset_fiat if asset_fiat > 0 else 0.0
        max_cap = float("inf") if network_name == "Bitcoin" else (max_hub_usd / asset_fiat if asset_fiat > 0 else float("inf"))
        precision = 6 if asset_symbol in ("BTC", "ETH") else 2

        recommended = max(cap_target, healthy_inbound, hub_floor, target_floor)
        return round(min(recommended, max_cap), precision)

    def calculate_network_send_requirement(
        self,
        network_name: str,
        asset_symbol: str,
        fiat_price: float = 0.0,
        total_channel_cap: float = 0.0
    ) -> float:
        """
        Calculates the aggregate sendable balance required across ALL active target pairs
        sharing a channel asset on a given network (e.g. BTC on Bitcoin).

        When send capacity is depleted:
        - Pairs where the asset is BASE degrade on Asks (cannot send base to sell).
        - Pairs where the asset is QUOTE degrade on Bids (cannot send quote to buy).

        Includes the 1.5x Gaussian bell-curve multiplier, 0.90 budget allocation factor,
        and cross-pair coexistence multiplier to support full 15-rung ladders simultaneously.
        """
        target_rungs = getattr(self.config, "rungs_per_side", 15)
        min_floor_usd = getattr(self.config, "hub_min_placement_floor_usd", 0.75)
        target_pct = getattr(self.config, "target_capacity_increase_pct", 0.10)
        min_hub_usd = getattr(self.config, "min_auto_rental_amount_usd", 10.0)
        target_hub_usd = getattr(self.config, "target_auto_rental_amount_usd", 25.0)
        max_hub_usd = getattr(self.config, "max_auto_rental_amount_usd", 250.0)

        active_markets = TESTNET_MARKETS if getattr(self.config, "testnet", False) else MARKETS
        total_send_req = 0.0
        competitor_count = 0
        active_pairs = getattr(self.config, "active_pairs_all", None) or list(active_markets.keys())

        asset_fiat = fiat_price if fiat_price > 0 else float(self._fallbacks.get(asset_symbol, 1.0))

        for pair_name in active_pairs:
            if pair_name not in active_markets:
                continue
            m = active_markets[pair_name]

            b_fiat = float(self._fallbacks.get(m.base.symbol, 1.0))
            q_fiat = float(self._fallbacks.get(m.quote.symbol, 1.0))
            if m.base.symbol == asset_symbol and asset_fiat > 0:
                b_fiat = asset_fiat
            if m.quote.symbol == asset_symbol and asset_fiat > 0:
                q_fiat = asset_fiat

            min_rung_b = max(m.base.min_amount, (min_floor_usd / b_fiat) if b_fiat > 0 else m.base.min_amount)
            quote_mid = (b_fiat / q_fiat) if q_fiat > 0 else 1.0
            min_rung_q = max(
                m.quote.min_amount,
                (min_floor_usd / q_fiat) if q_fiat > 0 else m.quote.min_amount,
                min_rung_b * quote_mid,
            )

            # Check if this pair needs this asset as Base (for Asks)
            if m.base.network_name == network_name and m.base.symbol == asset_symbol:
                total_send_req += min_rung_b * target_rungs
                competitor_count += 1

            # Check if this pair needs this asset as Quote (for Bids)
            elif m.quote.network_name == network_name and m.quote.symbol == asset_symbol:
                q_min_as_b = (m.quote.min_amount / quote_mid) if quote_mid > 0 else 0.0
                eff_min = max(min_rung_q, min_rung_b * quote_mid, q_min_as_b * quote_mid)
                total_send_req += eff_min * target_rungs
                competitor_count += 1

        healthy_send = (total_send_req * 1.5) / 0.90
        if competitor_count > 1:
            healthy_send *= (1.0 + (competitor_count - 1.0) / competitor_count)

        cap_target = total_channel_cap * target_pct if total_channel_cap > 0 else 0.0
        hub_floor = (min_hub_usd + 0.50) / asset_fiat if asset_fiat > 0 else 0.0
        target_floor = target_hub_usd / asset_fiat if asset_fiat > 0 else 0.0
        max_cap = float("inf") if network_name == "Bitcoin" else (max_hub_usd / asset_fiat if asset_fiat > 0 else float("inf"))
        precision = 6 if asset_symbol in ("BTC", "ETH") else 2

        recommended = max(cap_target, healthy_send, hub_floor, target_floor)
        return round(min(recommended, max_cap), precision)

    def calculate_network_inbound_requirement(
        self, network_name: str, asset_symbol: str, total_channel_cap: float = 0.0, fiat_price: float = 0.0
    ) -> float:
        """Alias for calculate_shared_inbound_requirement."""
        return self.calculate_shared_inbound_requirement(
            network_name=network_name,
            asset_symbol=asset_symbol,
            total_channel_cap=total_channel_cap,
            fiat_price=fiat_price,
        )

    def get_healthy_ladder_requirements(
        self,
        network_name: str,
        asset_symbol: str,
        fiat_price: float = 0.0
    ) -> Dict[str, float]:
        """Returns both send and receive required amounts across all active target pairs for an asset."""
        req_send = self.calculate_network_send_requirement(network_name, asset_symbol, fiat_price=fiat_price)
        req_recv = self.calculate_shared_inbound_requirement(network_name, asset_symbol, fiat_price=fiat_price)
        return {
            "required_send": req_send,
            "required_recv": req_recv,
        }

    def evaluate_pair(
        self,
        pair: str,
        free_base: float,
        free_quote: float,
        oracle_price: float,
        free_base_recv: float = 0.0,
        free_quote_recv: float = 0.0,
        cashu_btc_available: float = 0.0,
        onchain_btc_available: float = 0.0,
    ) -> SupervisorDecision:
        """
        Lightweight deterministic evaluation helper for a single pair and its free balances.
        Includes Cashu Ecash Vault BTC (and optional on-chain BTC) so sweeping BTC into Cashu
        does not trick the skew engine into thinking BTC inventory was lost.
        """
        active_markets = TESTNET_MARKETS if getattr(self.config, "testnet", False) else MARKETS
        cfg = active_markets.get(pair)
        base_sym = cfg.base_asset if cfg else pair.split("/")[0].split("(")[0].split("_")[0]
        quote_sym = cfg.quote_asset if cfg else pair.split("/")[1].split("(")[0].split("_")[0]
        base_net = cfg.base_network if cfg else "Arbitrum"
        quote_net = cfg.quote_network if cfg else "Arbitrum"

        if base_sym == quote_sym:
            oracle_price = 1.0
            fb_p = self._fallbacks.get(base_sym, 0.0)
            if fb_p <= 0 and getattr(self, "external_oracle", None):
                fb_p = self.external_oracle.get_fiat_price(base_sym)
            asset_usd = 1.0 if base_sym == "USDC" else (fb_p if fb_p > 0 else 2450.0)
            base_fiat = asset_usd
            quote_fiat = asset_usd
        else:
            base_fiat = oracle_price if quote_sym == "USDC" else self._fallbacks.get(base_sym, 1.0)
            quote_fiat = 1.0 if quote_sym == "USDC" else self._fallbacks.get(quote_sym, 1.0)

        inv = {
            "base_symbol": base_sym,
            "quote_symbol": quote_sym,
            "base_network": base_net,
            "quote_network": quote_net,
            "base_sendable": free_base,
            "quote_sendable": free_quote,
            "base_receivable": free_base_recv or free_base,
            "quote_receivable": free_quote_recv or free_quote,
            "base_fiat": base_fiat,
            "quote_fiat": quote_fiat,
            "cashu_btc_available": cashu_btc_available,
            "onchain_btc_available": onchain_btc_available,
            "channel_status": "CHANNEL_STATUS_ACTIVE",
        }

        return self.evaluate_position_and_market(
            pair_name=pair,
            oracle_price=oracle_price,
            orderbook={"orders": {}},
            recent_trades=[],
            inventory=inv,
            own_orders={},
        )

    def evaluate_position_and_market(
        self,
        pair_name: str,
        oracle_price: float,
        orderbook: Dict[str, Any],
        recent_trades: list,
        inventory: Dict[str, Any],
        own_orders: Dict[str, Any]
    ) -> SupervisorDecision:
        """
        Evaluates current inventory and channel capacities deterministically.
        Runs in < 1ms with zero external network dependencies.
        """
        base_sym = inventory.get("base_symbol", "ETH")
        quote_sym = inventory.get("quote_symbol", "USDC")
        base_net = inventory.get("base_network", "Arbitrum")
        quote_net = inventory.get("quote_network", "Arbitrum")

        base_sendable = float(inventory.get("base_sendable", 0.0))
        quote_sendable = float(inventory.get("quote_sendable", 0.0))
        base_receivable = float(inventory.get("base_receivable", 0.0))
        quote_receivable = float(inventory.get("quote_receivable", 0.0))

        # Determine asset fiat prices in USD
        base_fiat = inventory.get("base_fiat")
        if base_fiat is None or base_fiat <= 0:
            base_fiat = self._fallbacks.get(base_sym, 1.0)

        quote_fiat = inventory.get("quote_fiat")
        if quote_fiat is None or quote_fiat <= 0:
            quote_fiat = self._fallbacks.get(quote_sym, 1.0)

        # Factor Cashu Ecash Vault BTC, on-chain BTC, and Alby BTC into inventory evaluation
        # to prevent blind inventory leak when BTC is swept to Ecash
        onchain_btc = (
            float(inventory.get("onchain_btc_available", 0.0))
            if getattr(self.config, "include_onchain_in_inventory_skew", True)
            else 0.0
        )
        alby_btc = (
            float(inventory.get("alby_btc_available", 0.0))
            if getattr(self.config, "include_alby_in_inventory_skew", True)
            else 0.0
        )
        cashu_btc = (
            float(inventory.get("cashu_btc_available", 0.0))
            if getattr(self.config, "include_cashu_in_inventory_skew", True)
            else 0.0
        )

        total_extra_btc = onchain_btc + alby_btc + cashu_btc

        port_base_send = float(inventory.get("portfolio_base_sendable", base_sendable))
        eff_base_inv = port_base_send
        if base_sym == "BTC":
            eff_base_inv += total_extra_btc

        port_quote_send = float(inventory.get("portfolio_quote_sendable", quote_sendable))
        eff_quote_inv = port_quote_send
        if quote_sym == "BTC":
            eff_quote_inv += total_extra_btc

        base_val_usd = eff_base_inv * base_fiat
        quote_val_usd = eff_quote_inv * quote_fiat
        total_usd = base_val_usd + quote_val_usd

        # 1. Inventory Ratio & Sentiment-Driven Target Ratio
        target_ratio, rel_sentiment = self.calculate_sentiment_target_ratio(base_sym, quote_sym)

        if total_usd > 0:
            base_ratio = base_val_usd / total_usd
        else:
            base_ratio = target_ratio

        delta = base_ratio - target_ratio  # Positive = overweight base, Negative = underweight base

        # 2. Reservation Price Bias (Avellaneda-Stoikov skew / Stableswap 1:1 Peg Refill Skew)
        is_peg_pair = (base_sym == quote_sym)
        if is_peg_pair:
            # For 1:1 cross-chain peg pairs, shift center price up to ±peg_max_skew_shift_pct (±4 bps)
            # so overweight inventory quotes Rung #0 right at 1.00000 par for zero-slippage cross-chain rebalancing.
            max_bias = getattr(self.config, "peg_max_skew_shift_pct", 0.0004)
            center_bias = max(-max_bias, min(-delta * 0.0016, max_bias))
        else:
            # Shift center price to incentivize rebalancing, strictly capped to 50% of the half-spread
            # so that Bid Rung #1 is guaranteed to remain comfortably below fair mid (never crossing the book).
            max_bias = getattr(self.config, "base_half_spread_pct", 0.0080) * 0.50
            center_bias = max(-max_bias, min(-delta * 0.05, max_bias))

        # 3. Asymmetric Order Sizing Multipliers
        skew_factor = max(-1.0, min(delta * 3.0, 1.0))
        bid_mult = max(0.3, min(1.0 - skew_factor * 0.5, 2.0))
        ask_mult = max(0.3, min(1.0 + skew_factor * 0.5, 2.0))

        # 4. Decoupled Asymmetric Half-Spread Multipliers
        # Rebalancing side stays tight (1.0x) to encourage taker fills.
        # Depleted side widens (up to 2.5x) to demand a risk premium for scarce inventory.
        if delta < 0:
            # Underweight base -> need to BUY base. Keep bids tight at market, widen asks.
            bid_spread_mult = 1.0
            ask_spread_mult = max(1.0, min(1.0 + 3.0 * abs(delta), 2.5))
        elif delta > 0:
            # Overweight base -> need to SELL base. Keep asks tight at market, widen bids.
            ask_spread_mult = 1.0
            bid_spread_mult = max(1.0, min(1.0 + 3.0 * abs(delta), 2.5))
        else:
            bid_spread_mult = 1.0
            ask_spread_mult = 1.0

        spread_mult = max(bid_spread_mult, ask_spread_mult)

        # 5. Bell Curve Concentration
        if abs(delta) < 0.10:
            concentration = "CONCENTRATED"
        elif abs(delta) < 0.25:
            concentration = "BALANCED"
        else:
            concentration = "WIDE"

        # 6. Risk Level
        if abs(delta) < 0.15:
            risk_level = "LOW"
        elif abs(delta) < 0.35:
            risk_level = "MEDIUM"
        else:
            risk_level = "HIGH"

        # 7. Automated Channel Capacity & Usable Balance Evaluation
        buffer_usd = getattr(self.config, "min_channel_buffer_usd", 15.0)
        target_rental_usd = getattr(self.config, "target_auto_rental_amount_usd", 25.0)
        min_floor_usd = getattr(self.config, "hub_min_placement_floor_usd", 0.75)

        base_send_usd = base_sendable * base_fiat
        quote_send_usd = quote_sendable * quote_fiat
        base_recv_usd = base_receivable * base_fiat
        quote_recv_usd = quote_receivable * quote_fiat

        # Required minimum send thresholds for placing maker orders
        m_cfg = MARKETS.get(pair_name)
        target_rungs = getattr(self.config, "rungs_per_side", 15)

        b_min_amt = inventory.get("base_min_amount", 0.0)
        b_default_amt = inventory.get("base_default_order_size", getattr(m_cfg, "default_order_size_base", b_min_amt) if m_cfg else b_min_amt)
        b_single_rung_amt = max(b_default_amt, b_min_amt)
        b_rung_cost_usd = max(min_floor_usd, (b_single_rung_amt * base_fiat) if base_fiat > 0 else min_floor_usd)

        q_min_amt = inventory.get("quote_min_amount", 0.0)
        q_default_amt = inventory.get("quote_default_order_size", (getattr(m_cfg, "default_order_size_base", 0.0) * oracle_price) if m_cfg and oracle_price > 0 else q_min_amt)
        q_single_rung_amt = max(q_default_amt, q_min_amt)
        q_rung_cost_usd = max(min_floor_usd, (q_single_rung_amt * quote_fiat) if quote_fiat > 0 else min_floor_usd)

        # Single-rung minimum sendable threshold (can place at least 1 rung)
        req_b_send_usd = max(min_floor_usd, (b_min_amt * base_fiat) if base_fiat > 0 else min_floor_usd)
        req_q_send_usd = max(min_floor_usd, (q_min_amt * quote_fiat) if quote_fiat > 0 else min_floor_usd)

        # Full target ladder sendable requirement (can place target_rungs, default 15)
        req_3_b_send_usd = max(buffer_usd, b_rung_cost_usd * target_rungs)
        req_3_q_send_usd = max(buffer_usd, q_rung_cost_usd * target_rungs)

        # Prefer effective sendable (actual usable sendable for this pair after accounting for orders)
        eff_b_send = inventory.get("effective_base_sendable", base_sendable)
        eff_q_send = inventory.get("effective_quote_sendable", quote_sendable)
        eff_b_send_usd = eff_b_send * base_fiat
        eff_q_send_usd = eff_q_send * quote_fiat

        # Exact count of rungs supported by effective send capacity (< target_rungs indicates send capacity deficiency)
        supported_b_rungs = int((eff_b_send_usd * 0.90) / b_rung_cost_usd) if b_rung_cost_usd > 0 else 0
        supported_q_rungs = int((eff_q_send_usd * 0.90) / q_rung_cost_usd) if q_rung_cost_usd > 0 else 0

        can_send_base_1 = ((eff_b_send_usd * 0.90) >= req_b_send_usd)
        can_send_quote_1 = ((eff_q_send_usd * 0.90) >= req_q_send_usd)

        # Deficient if send capacity cannot fund the full target ladder (< 15 rungs)
        can_send_base_3 = (supported_b_rungs >= target_rungs) and ((eff_b_send_usd * 0.90) >= req_3_b_send_usd)
        can_send_quote_3 = (supported_q_rungs >= target_rungs) and ((eff_q_send_usd * 0.90) >= req_3_q_send_usd)

        # On-chain balances for funding readiness
        onchain_bals = inventory.get("onchain_balances", {})
        usable_onchain_bals = inventory.get("usable_onchain_balances", {})

        avail_q = float(usable_onchain_bals.get(quote_net, {}).get(quote_sym, onchain_bals.get(quote_net, {}).get(quote_sym, 0.0)))
        avail_q_usd = avail_q * quote_fiat

        avail_b = float(usable_onchain_bals.get(base_net, {}).get(base_sym, onchain_bals.get(base_net, {}).get(base_sym, 0.0)))
        avail_b_usd = avail_b * base_fiat

        base_has_no_channel = (base_receivable == 0.0 and base_sendable == 0.0)
        quote_has_no_channel = (quote_receivable == 0.0 and quote_sendable == 0.0)

        # 7.0 Channel Reserve Deficit Self-Healing Check
        reserve_deficits = inventory.get("reserve_deficits", [])
        pair_deficits = [
            d for d in reserve_deficits
            if (d.get("symbol") == base_sym and d.get("network_name") == base_net) or
               (d.get("symbol") == quote_sym and d.get("network_name") == quote_net)
        ]
        if pair_deficits:
            for d in pair_deficits:
                d_sym = d.get("symbol", "")
                d_net = d.get("network_name", "")
                d_deficit = float(d.get("deficit", 0.0))
                d_target = float(d.get("target_topup", d_deficit * 1.20))
                d_ch_id = d.get("channel_id", "")

                # Skip self-healing deposit if a channel or rental update is already pending
                if (d_sym == base_sym and inventory.get("base_pending_update")) or (d_sym == quote_sym and inventory.get("quote_pending_update")):
                    continue

                avail_heal = float(usable_onchain_bals.get(d_net, {}).get(d_sym, onchain_bals.get(d_net, {}).get(d_sym, 0.0)))
                if d_sym == "ETH":
                    avail_heal = float(usable_onchain_bals.get(d_net, {}).get("ETH", 0.0))

                if d_deficit > 0 and avail_heal >= d_deficit:
                    dep_heal_amt = round(min(avail_heal, d_target), 6 if d_sym in ("BTC", "ETH") else 2)
                    ch_action = {
                        "action_type": "DEPOSIT_CHANNEL",
                        "network": d_net,
                        "asset": d_sym,
                        "channel_id": d_ch_id,
                        "amount": dep_heal_amt,
                        "reason": (
                            f"Automated Self-Healing: Channel {d_ch_id[:16]}... on {d_net} has reserve deficit of "
                            f"{d_deficit:.6f} {d_sym} (local {d.get('free_local', 0.0):.6f} < reserve {d.get('unspendable_local_reserve', 0.0):.6f}). "
                            f"Depositing {dep_heal_amt:.6f} {d_sym} to satisfy unspendable local reserve and activate orderbook routing."
                        ),
                    }
                    ch_advice = (
                        f"RESERVE DEFICIT DETECTED: Channel {d_ch_id[:16]}... requires {d_deficit:.6f} {d_sym} local reserve. "
                        f"Found {avail_heal:.6f} usable on-chain: advising automated DEPOSIT_CHANNEL ({dep_heal_amt:.6f} {d_sym})."
                    )
                    return SupervisorDecision(
                        spread_multiplier=spread_mult,
                        bid_spread_multiplier=bid_spread_mult,
                        ask_spread_multiplier=ask_spread_mult,
                        target_base_ratio=target_ratio,
                        relative_sentiment=rel_sentiment,
                        center_price_bias_pct=center_bias,
                        curve_concentration=concentration,
                        skew_factor=skew_factor,
                        bid_size_multiplier=bid_mult,
                        ask_size_multiplier=ask_mult,
                        risk_level="ELEVATED",
                        allow_trading=True,
                        channel_advice=ch_advice,
                        channel_action=ch_action,
                        commentary=f"{base_ratio:.1%} {base_sym} / {(1.0-base_ratio):.1%} {quote_sym} (Healing Channel Reserve)",
                        base_ratio=base_ratio,
                        quote_ratio=1.0 - base_ratio,
                    )

        base_needs_cap = (base_recv_usd < buffer_usd) and base_fiat > 0
        quote_needs_cap = (quote_recv_usd < buffer_usd) and quote_fiat > 0

        target_side = None
        if base_needs_cap and quote_needs_cap:
            # If one side has literally zero capacity (no channel), prioritize establishing it first
            if quote_has_no_channel and avail_q_usd >= 1.0:
                target_side = "quote"
            elif base_has_no_channel and avail_b_usd >= 1.0:
                target_side = "base"
            elif quote_has_no_channel:
                target_side = "quote"
            elif base_has_no_channel:
                target_side = "base"
            elif quote_recv_usd < base_recv_usd:
                target_side = "quote"
            else:
                target_side = "base"
        elif base_needs_cap:
            target_side = "base"
        elif quote_needs_cap:
            target_side = "quote"

        target_pct = getattr(self.config, "target_capacity_increase_pct", 0.10)
        min_hub_usd = getattr(self.config, "min_auto_rental_amount_usd", 10.0)
        fallback_baseline_usd = getattr(self.config, "target_auto_rental_amount_usd", 25.0)

        if target_side == "base":
            if base_has_no_channel and avail_b_usd >= 1.0:
                total_b_cap = base_sendable + base_receivable
                cap_target = (total_b_cap * target_pct) if total_b_cap > 0 else (fallback_baseline_usd / base_fiat)
                health_def = max(cap_target, min_hub_usd / base_fiat)
                if base_net == "Bitcoin":
                    btc_largest = float(inventory.get("btc_largest_channel_cap", 0.0))
                    max_avail = round(max(0.0001, avail_b - 0.00005), 6)
                    ratchet_target = (btc_largest + (1.5 * health_def)) if btc_largest > 0 else max_avail
                    target_amt = round(ratchet_target, 6)
                    dep_amt = min(max_avail, target_amt) if max_avail >= target_amt else max_avail
                    reason_str = (
                        f"No active state channel on {base_net} for {base_sym}. "
                        f"Advising OPEN_CHANNEL (best available: {dep_amt:.6f} {base_sym}"
                        + (f", ratchet target: {target_amt:.6f}" if btc_largest > 0 else "")
                        + ") to establish channel and send capacity."
                    )
                    advice_str = f"No active state channel on {base_net} for {base_sym}. Found {avail_b:.4f} {base_sym} on-chain: advise OPEN_CHANNEL ({dep_amt:.6f} {base_sym}) to establish channel."
                    ch_action = {
                        "action_type": "OPEN_CHANNEL",
                        "network": base_net,
                        "asset": base_sym,
                        "amount": dep_amt,
                        "deficit": health_def,
                        "largest_cap": btc_largest,
                        "ratchet": (btc_largest > 0),
                        "reason": reason_str,
                    }
                    ch_advice = advice_str
                elif base_net in ("Arbitrum", "Ethereum") and inventory.get("has_active_lithium_channel_base"):
                    target_amt = round(health_def, 6 if base_sym in ("BTC", "ETH") else 2)
                    dep_amt = min(avail_b, max(target_amt, avail_b))
                    ch_action = {
                        "action_type": "DEPOSIT_CHANNEL",
                        "network": base_net,
                        "asset": base_sym,
                        "amount": dep_amt,
                        "reason": f"Active primary Lithium channel detected on {base_net}. Found {avail_b:.4f} {base_sym} on-chain. Advising DEPOSIT_CHANNEL ({dep_amt} {base_sym}) to concentrate liquidity into single primary channel.",
                    }
                    ch_advice = f"Active primary Lithium channel exists on {base_net}. Found {avail_b:.4f} {base_sym} on-chain: advise DEPOSIT_CHANNEL ({dep_amt} {base_sym}) to fund send capacity."
                else:
                    target_amt = round(health_def, 6 if base_sym in ("BTC", "ETH") else 2)
                    dep_amt = min(avail_b, max(target_amt, avail_b))
                    ch_action = {
                        "action_type": "OPEN_CHANNEL",
                        "network": base_net,
                        "asset": base_sym,
                        "amount": dep_amt,
                        "reason": f"No active state channel on {base_net} for {base_sym}. Detected {avail_b:.4f} {base_sym} on-chain. Advising OPEN_CHANNEL to establish channel and send capacity.",
                    }
                    ch_advice = f"No active state channel on {base_net} for {base_sym}. Found {avail_b:.4f} {base_sym} on-chain: advise OPEN_CHANNEL ({dep_amt} {base_sym}) to establish channel."
            else:
                total_base_cap = base_sendable + base_receivable
                rent_amt = self.calculate_shared_inbound_requirement(
                    network_name=base_net,
                    asset_symbol=base_sym,
                    total_channel_cap=total_base_cap,
                    fiat_price=base_fiat,
                )
                is_btc = (base_net == "Bitcoin")
                btc_smallest_remaining = float(inventory.get("btc_smallest_remaining_channel_cap", inventory.get("btc_largest_channel_cap", 0.0))) if is_btc else 0.0
                deficit = rent_amt
                if is_btc and btc_smallest_remaining > 0:
                    rent_amt = round(max(btc_smallest_remaining * 1.5, btc_smallest_remaining + (1.5 * deficit)), 6)

                ch_action = {
                    "action_type": "RENT_INBOUND",
                    "network": base_net,
                    "asset": base_sym,
                    "amount": rent_amt,
                    "deficit": deficit,
                    "smallest_remaining_cap": btc_smallest_remaining,
                    "ratchet": is_btc,
                    "reason": (
                        f"{base_sym} inbound receiving capacity (${base_recv_usd:.2f}) is below "
                        f"the ${buffer_usd:.0f} USD buffer threshold. Renting {rent_amt} {base_sym} "
                        + (f"(ratchet target based on smallest remaining channel {btc_smallest_remaining:.6f} BTC [1.5x: {(btc_smallest_remaining * 1.5):.6f}, deficit: {deficit:.6f}]) " if is_btc and btc_smallest_remaining > 0 else "")
                        + f"to support {target_rungs}-rung ladders across all shared {base_net} {base_sym} pairs."
                    ),
                }
                if base_receivable == 0.0 and base_sendable == 0.0:
                    ch_advice = f"No active state channel on {base_net} for {base_sym}. Advise renting inbound capacity ({rent_amt} {base_sym}) to enable trading."
                else:
                    ratchet_note = f" (ratchet target: {rent_amt:.6f} {base_sym})" if is_btc and btc_smallest_remaining > 0 else ""
                    ch_advice = (
                        f"DEGRADED: {base_net} {base_sym} inbound capacity is low (${base_recv_usd:.2f} < ${buffer_usd:.0f}). "
                        f"Advise renting {rent_amt} {base_sym}{ratchet_note} to maintain {target_rungs}-rung receiving headroom across active pairs."
                    )
        elif target_side == "quote":
            if quote_has_no_channel and avail_q_usd >= 1.0:
                total_q_cap = quote_sendable + quote_receivable
                cap_target = (total_q_cap * target_pct) if total_q_cap > 0 else (fallback_baseline_usd / quote_fiat)
                health_def = max(cap_target, min_hub_usd / quote_fiat)
                if quote_net == "Bitcoin":
                    btc_largest = float(inventory.get("btc_largest_channel_cap", 0.0))
                    max_avail = round(max(0.0001, avail_q - 0.00005), 6)
                    ratchet_target = (btc_largest + (1.5 * health_def)) if btc_largest > 0 else max_avail
                    target_amt = round(ratchet_target, 6)
                    dep_amt = min(max_avail, target_amt) if max_avail >= target_amt else max_avail
                    reason_str = (
                        f"No active state channel on {quote_net} for {quote_sym}. "
                        f"Advising OPEN_CHANNEL (best available: {dep_amt:.6f} {quote_sym}"
                        + (f", ratchet target: {target_amt:.6f}" if btc_largest > 0 else "")
                        + ") to establish channel and send capacity."
                    )
                    advice_str = f"No active state channel on {quote_net} for {quote_sym}. Found {avail_q:.4f} {quote_sym} on-chain: advise OPEN_CHANNEL ({dep_amt:.6f} {quote_sym}) to establish channel."
                    ch_action = {
                        "action_type": "OPEN_CHANNEL",
                        "network": quote_net,
                        "asset": quote_sym,
                        "amount": dep_amt,
                        "deficit": health_def,
                        "largest_cap": btc_largest,
                        "ratchet": (btc_largest > 0),
                        "reason": reason_str,
                    }
                    ch_advice = advice_str
                elif quote_net in ("Arbitrum", "Ethereum") and inventory.get("has_active_lithium_channel_quote"):
                    target_amt = round(health_def, 6 if quote_sym in ("BTC", "ETH") else 2)
                    dep_amt = min(avail_q, max(target_amt, avail_q))
                    ch_action = {
                        "action_type": "DEPOSIT_CHANNEL",
                        "network": quote_net,
                        "asset": quote_sym,
                        "amount": dep_amt,
                        "reason": f"Active primary Lithium channel detected on {quote_net}. Found {avail_q:.4f} {quote_sym} on-chain. Advising DEPOSIT_CHANNEL ({dep_amt} {quote_sym}) to concentrate liquidity into single primary channel.",
                    }
                    ch_advice = f"Active primary Lithium channel exists on {quote_net}. Found {avail_q:.4f} {quote_sym} on-chain: advise DEPOSIT_CHANNEL ({dep_amt} {quote_sym}) to fund send capacity."
                else:
                    target_amt = round(health_def, 6 if quote_sym in ("BTC", "ETH") else 2)
                    dep_amt = min(avail_q, max(target_amt, avail_q))
                    ch_action = {
                        "action_type": "OPEN_CHANNEL",
                        "network": quote_net,
                        "asset": quote_sym,
                        "amount": dep_amt,
                        "reason": f"No active state channel on {quote_net} for {quote_sym}. Detected {avail_q:.4f} {quote_sym} on-chain. Advising OPEN_CHANNEL to establish channel and send capacity.",
                    }
                    ch_advice = f"No active state channel on {quote_net} for {quote_sym}. Found {avail_q:.4f} {quote_sym} on-chain: advise OPEN_CHANNEL ({dep_amt} {quote_sym}) to establish channel."
            else:
                total_quote_cap = quote_sendable + quote_receivable
                rent_amt = self.calculate_shared_inbound_requirement(
                    network_name=quote_net,
                    asset_symbol=quote_sym,
                    total_channel_cap=total_quote_cap,
                    fiat_price=quote_fiat,
                )
                is_btc = (quote_net == "Bitcoin")
                btc_smallest_remaining = float(inventory.get("btc_smallest_remaining_channel_cap", inventory.get("btc_largest_channel_cap", 0.0))) if is_btc else 0.0
                deficit = rent_amt
                if is_btc and btc_smallest_remaining > 0:
                    rent_amt = round(max(btc_smallest_remaining * 1.5, btc_smallest_remaining + (1.5 * deficit)), 6)

                ch_action = {
                    "action_type": "RENT_INBOUND",
                    "network": quote_net,
                    "asset": quote_sym,
                    "amount": rent_amt,
                    "deficit": deficit,
                    "smallest_remaining_cap": btc_smallest_remaining,
                    "ratchet": is_btc,
                    "reason": (
                        f"{quote_sym} inbound receiving capacity (${quote_recv_usd:.2f}) is below "
                        f"the ${buffer_usd:.0f} USD buffer threshold. Renting {rent_amt} {quote_sym} "
                        + (f"(ratchet target based on smallest remaining channel {btc_smallest_remaining:.6f} BTC [1.5x: {(btc_smallest_remaining * 1.5):.6f}, deficit: {deficit:.6f}]) " if is_btc and btc_smallest_remaining > 0 else "")
                        + f"to support {target_rungs}-rung ladders across all shared {quote_net} {quote_sym} pairs."
                    ),
                }
                if quote_receivable == 0.0 and quote_sendable == 0.0:
                    ch_advice = f"No active state channel on {quote_net} for {quote_sym}. Advise renting inbound capacity ({rent_amt} {quote_sym}) to enable trading."
                else:
                    ratchet_note = f" (ratchet target: {rent_amt:.6f} {quote_sym})" if is_btc and btc_smallest_remaining > 0 else ""
                    ch_advice = (
                        f"DEGRADED: {quote_net} {quote_sym} inbound capacity is low (${quote_recv_usd:.2f} < ${buffer_usd:.0f}). "
                        f"Advise renting {rent_amt} {quote_sym}{ratchet_note} to maintain {target_rungs}-rung receiving headroom across active pairs."
                    )
        elif not can_send_base_1 and not can_send_quote_1:
            # Both sides lack sendable capacity to place orders


            if avail_q_usd >= 1.0 or avail_b_usd >= 1.0:
                fund_quote = (avail_q_usd >= 1.0) and (avail_b_usd < 1.0 or quote_send_usd <= base_send_usd)
                if fund_quote:
                    has_ch = not (quote_receivable == 0.0 and quote_sendable == 0.0)
                    act_type = "OPEN_CHANNEL" if quote_net == "Bitcoin" else ("DEPOSIT_CHANNEL" if (has_ch or (quote_net in ("Arbitrum", "Ethereum") and inventory.get("has_active_lithium_channel_quote"))) else "OPEN_CHANNEL")
                    total_q_cap = quote_sendable + quote_receivable
                    cap_target = (total_q_cap * target_pct) if total_q_cap > 0 else (fallback_baseline_usd / quote_fiat)
                    health_def = max(cap_target, min_hub_usd / quote_fiat)
                    if quote_net == "Bitcoin":
                        btc_largest = float(inventory.get("btc_largest_channel_cap", 0.0))
                        max_avail = round(max(0.0001, avail_q - 0.00005), 6)
                        ratchet_target = (btc_largest + (1.5 * health_def)) if btc_largest > 0 else max_avail
                        target_amt = round(ratchet_target, 6)
                        dep_amt = min(max_avail, target_amt) if max_avail >= target_amt else max_avail
                        ch_action = {
                            "action_type": act_type,
                            "network": quote_net,
                            "asset": quote_sym,
                            "amount": dep_amt,
                            "deficit": health_def,
                            "largest_cap": btc_largest,
                            "ratchet": (btc_largest > 0),
                            "reason": (
                                f"{quote_net} {quote_sym} send capacity is depleted (${quote_send_usd:.2f}). "
                                f"Advising OPEN_CHANNEL (best available: {dep_amt:.6f} {quote_sym}"
                                + (f", ratchet target: {target_amt:.6f}" if btc_largest > 0 else "")
                                + ") to restore send capacity and grow scale."
                            ),
                        }
                        ch_advice = f"DEGRADED: {quote_net} {quote_sym} send capacity is depleted. Found {avail_q:.4f} {quote_sym} on-chain. Advising {act_type} ({dep_amt:.6f} {quote_sym}) to grow channel capacity."
                    else:
                        target_amt = round(health_def, 6 if quote_sym in ("BTC", "ETH") else 2)
                        dep_amt = min(avail_q, target_amt)
                        ch_action = {
                            "action_type": act_type,
                            "network": quote_net,
                            "asset": quote_sym,
                            "amount": dep_amt,
                            "reason": f"{quote_net} {quote_sym} send capacity is depleted (${quote_send_usd:.2f}). Detected {avail_q:.4f} {quote_sym} on-chain. Advising {act_type} to restore send capacity.",
                        }
                        ch_advice = f"DEGRADED: {quote_net} {quote_sym} send capacity is depleted. Found {avail_q:.2f} {quote_sym} on-chain. Advising {act_type} ({dep_amt:.2f} {quote_sym}) to replenish channel."
                else:
                    has_ch = not (base_receivable == 0.0 and base_sendable == 0.0)
                    act_type = "OPEN_CHANNEL" if base_net == "Bitcoin" else ("DEPOSIT_CHANNEL" if (has_ch or (base_net in ("Arbitrum", "Ethereum") and inventory.get("has_active_lithium_channel_base"))) else "OPEN_CHANNEL")
                    total_b_cap = base_sendable + base_receivable
                    cap_target = (total_b_cap * target_pct) if total_b_cap > 0 else (fallback_baseline_usd / base_fiat)
                    health_def = max(cap_target, min_hub_usd / base_fiat)
                    if base_net == "Bitcoin":
                        btc_largest = float(inventory.get("btc_largest_channel_cap", 0.0))
                        max_avail = round(max(0.0001, avail_b - 0.00005), 6)
                        ratchet_target = (btc_largest + (1.5 * health_def)) if btc_largest > 0 else max_avail
                        target_amt = round(ratchet_target, 6)
                        dep_amt = min(max_avail, target_amt) if max_avail >= target_amt else max_avail
                        ch_action = {
                            "action_type": act_type,
                            "network": base_net,
                            "asset": base_sym,
                            "amount": dep_amt,
                            "deficit": health_def,
                            "largest_cap": btc_largest,
                            "ratchet": (btc_largest > 0),
                            "reason": (
                                f"{base_net} {base_sym} send capacity is depleted (${base_send_usd:.2f}). "
                                f"Advising OPEN_CHANNEL (best available: {dep_amt:.6f} {base_sym}"
                                + (f", ratchet target: {target_amt:.6f}" if btc_largest > 0 else "")
                                + ") to restore send capacity and grow scale."
                            ),
                        }
                        scale_str = f"ratchet: {dep_amt:.6f}" if max_avail >= target_amt else f"best available: {dep_amt:.6f}"
                        ch_advice = f"DEGRADED: {base_net} {base_sym} send capacity is depleted. Found {avail_b:.4f} {base_sym} on-chain. Advising {act_type} ({scale_str} {base_sym}) to grow channel capacity."
                    else:
                        target_amt = round(health_def, 6 if base_sym in ("BTC", "ETH") else 2)
                        dep_amt = min(avail_b, target_amt)
                        ch_action = {
                            "action_type": act_type,
                            "network": base_net,
                            "asset": base_sym,
                            "amount": dep_amt,
                            "reason": f"{base_net} {base_sym} send capacity is depleted (${base_send_usd:.2f}). Detected {avail_b:.4f} {base_sym} on-chain. Advising {act_type} to restore send capacity.",
                        }
                        ch_advice = f"DEGRADED: {base_net} {base_sym} send capacity is depleted. Found {avail_b:.2f} {base_sym} on-chain. Advising {act_type} ({dep_amt:.2f} {base_sym}) to replenish channel."
            else:
                ch_action = {"action_type": "NONE", "reason": "Send capacity depleted on both sides."}
                ch_advice = (
                    f"HALTED: Send capacity depleted on both sides ({base_sym}: ${base_send_usd:.2f}, {quote_sym}: ${quote_send_usd:.2f} < ${min_floor_usd:.2f} min). "
                    f"Quoting suspended until channels are funded."
                )
        elif not can_send_quote_1 or not can_send_quote_3:
            # Cannot place full 3 bids, can place asks -> One-sided or degraded
            onchain_bals = inventory.get("onchain_balances", {})
            usable_onchain_bals = inventory.get("usable_onchain_balances", {})
            avail_q = float(usable_onchain_bals.get(quote_net, {}).get(quote_sym, onchain_bals.get(quote_net, {}).get(quote_sym, 0.0)))
            avail_q_usd = avail_q * quote_fiat

            deficit_q_usd = max(0.0, req_3_q_send_usd - (eff_q_send_usd * 0.90))
            deficit_q_amt = deficit_q_usd / quote_fiat if quote_fiat > 0 else 0.0

            # Actively steer quoting to attract fills on the active side to accumulate quote
            risk_level = "ELEVATED"
            spread_mult = 0.90
            if delta >= 0:
                center_bias = min(center_bias, -max_bias * 0.8)

            eff_b_recv = inventory.get("effective_base_receivable", base_receivable)
            eff_b_recv_usd = eff_b_recv * base_fiat
            is_base_recv_zero = eff_b_recv_usd < req_b_send_usd
            can_fund_q = (avail_q >= getattr(self.config, "min_onchain_btc_open_amt", 0.0004) and avail_q_usd >= getattr(self.config, "min_onchain_btc_open_usd", 40.0)) if quote_net == "Bitcoin" else (avail_q_usd >= 1.0)
            if can_fund_q and deficit_q_usd >= 0.50 and not is_base_recv_zero:
                has_ch = not (quote_receivable == 0.0 and quote_sendable == 0.0)
                act_type = "OPEN_CHANNEL" if quote_net == "Bitcoin" else ("DEPOSIT_CHANNEL" if (has_ch or (quote_net in ("Arbitrum", "Ethereum") and inventory.get("has_active_lithium_channel_quote"))) else "OPEN_CHANNEL")
                total_q_cap = quote_sendable + quote_receivable
                cap_target = (total_q_cap * target_pct) if total_q_cap > 0 else (fallback_baseline_usd / quote_fiat)
                health_def = max(deficit_q_amt, cap_target, min_hub_usd / quote_fiat)
                if quote_net == "Bitcoin":
                    btc_largest = float(inventory.get("btc_largest_channel_cap", 0.0))
                    max_avail = round(max(0.0001, avail_q - 0.00005), 6)
                    ratchet_target = (btc_largest + (1.5 * health_def)) if btc_largest > 0 else max_avail
                    target_amt = round(ratchet_target, 6)
                    dep_amt = min(max_avail, target_amt) if max_avail >= target_amt else max_avail
                    scale_str = f"ratchet: {dep_amt:.6f}" if max_avail >= target_amt else f"best available: {dep_amt:.6f}"
                    reason_label = "Bids suspended" if not can_send_quote_1 else f"{quote_sym} send capacity deficient ({supported_q_rungs}/{target_rungs} rungs)"
                    ch_action = {
                        "action_type": act_type,
                        "network": quote_net,
                        "asset": quote_sym,
                        "amount": dep_amt,
                        "deficit": health_def,
                        "largest_cap": btc_largest,
                        "ratchet": (btc_largest > 0),
                        "reason": (
                            f"DEGRADED: {reason_label} (needs ${deficit_q_usd:.2f} {quote_sym}). "
                            f"Advising OPEN_CHANNEL (best available: {dep_amt:.6f} {quote_sym}"
                            + (f", ratchet target: {target_amt:.6f}" if btc_largest > 0 else "")
                            + f") to restore {target_rungs} Bids and grow scale."
                        ),
                    }
                    advice_label = "DEGRADED (One-Sided: Bids Suspended)" if not can_send_quote_1 else f"DEGRADED ({quote_sym} Send Capacity Deficient: {supported_q_rungs}/{target_rungs} rungs)"
                    ch_advice = f"{advice_label}: Needs ${deficit_q_usd:.2f} {quote_sym}. Found on-chain {quote_sym}: advising {act_type} ({scale_str} {quote_sym}) to restore {target_rungs} Bids."
                else:
                    target_amt = round(health_def, 6 if quote_sym in ("BTC", "ETH") else 2)
                    dep_amt = min(avail_q, target_amt)
                    ch_action = {
                        "action_type": act_type,
                        "network": quote_net,
                        "asset": quote_sym,
                        "amount": dep_amt,
                        "reason": f"DEGRADED: Bids suspended (needs ${deficit_q_usd:.2f} {quote_sym}). Detected {avail_q:.4f} {quote_sym} on-chain. Advising {act_type} to restore {target_rungs} Bids.",
                    }
                    ch_advice = f"DEGRADED (One-Sided: Bids Suspended): Needs ${deficit_q_usd:.2f} {quote_sym}. Found on-chain {quote_sym}: advising {act_type} ({dep_amt:.4f} {quote_sym}) to restore {target_rungs} Bids."
            else:
                # Quote cannot be restored from on-chain. Check if base side is also degraded and has on-chain funds
                avail_b = float(usable_onchain_bals.get(base_net, {}).get(base_sym, onchain_bals.get(base_net, {}).get(base_sym, 0.0)))
                avail_b_usd = avail_b * base_fiat
                deficit_b_usd = max(0.0, req_3_b_send_usd - (eff_b_send_usd * 0.90))
                deficit_b_amt = deficit_b_usd / base_fiat if base_fiat > 0 else 0.0

                can_fund_b = (avail_b >= getattr(self.config, "min_onchain_btc_open_amt", 0.0004) and avail_b_usd >= getattr(self.config, "min_onchain_btc_open_usd", 40.0)) if base_net == "Bitcoin" else (avail_b_usd >= 1.0)
                if (not can_send_base_1 or not can_send_base_3) and can_fund_b and deficit_b_usd >= 0.50 and not is_base_recv_zero:
                    has_ch = not (base_receivable == 0.0 and base_sendable == 0.0)
                    act_type = "OPEN_CHANNEL" if base_net == "Bitcoin" else ("DEPOSIT_CHANNEL" if (has_ch or (base_net in ("Arbitrum", "Ethereum") and inventory.get("has_active_lithium_channel_base"))) else "OPEN_CHANNEL")
                    total_b_cap = base_sendable + base_receivable
                    cap_target = (total_b_cap * target_pct) if total_b_cap > 0 else (fallback_baseline_usd / base_fiat)
                    health_def = max(deficit_b_amt, cap_target, min_hub_usd / base_fiat)
                    if base_net == "Bitcoin":
                        btc_largest = float(inventory.get("btc_largest_channel_cap", 0.0))
                        max_avail = round(max(0.0001, avail_b - 0.00005), 6)
                        ratchet_target = (btc_largest + (1.5 * health_def)) if btc_largest > 0 else max_avail
                        target_amt = round(ratchet_target, 6)
                        dep_amt = min(max_avail, target_amt) if max_avail >= target_amt else max_avail
                        scale_str = f"ratchet: {dep_amt:.6f}" if max_avail >= target_amt else f"best available: {dep_amt:.6f}"
                        reason_label = "Asks suspended" if not can_send_base_1 else f"{base_sym} send capacity deficient ({supported_b_rungs}/{target_rungs} rungs)"
                        ch_action = {
                            "action_type": act_type,
                            "network": base_net,
                            "asset": base_sym,
                            "amount": dep_amt,
                            "deficit": health_def,
                            "largest_cap": btc_largest,
                            "ratchet": (btc_largest > 0),
                            "reason": (
                                f"DEGRADED: Bids suspended & {reason_label} (needs ${deficit_b_usd:.2f} {base_sym}). "
                                f"Advising OPEN_CHANNEL (best available: {dep_amt:.6f} {base_sym}"
                                + (f", ratchet target: {target_amt:.6f}" if btc_largest > 0 else "")
                                + f") to restore {target_rungs} Asks and grow scale."
                            ),
                        }
                        advice_label = "DEGRADED (One-Sided: Asks Suspended)" if not can_send_base_1 else f"DEGRADED ({base_sym} Send Capacity Deficient: {supported_b_rungs}/{target_rungs} rungs)"
                        ch_advice = f"{advice_label}: Bids suspended. Found on-chain {base_sym}: advising {act_type} ({scale_str} {base_sym}) to restore {target_rungs} Asks."
                    else:
                        target_amt = round(health_def, 6 if base_sym in ("BTC", "ETH") else 2)
                        dep_amt = min(avail_b, target_amt)
                        ch_action = {
                            "action_type": act_type,
                            "network": base_net,
                            "asset": base_sym,
                            "amount": dep_amt,
                            "reason": f"DEGRADED: Bids suspended & Asks degraded (needs ${deficit_b_usd:.2f} {base_sym}). Detected {avail_b:.4f} {base_sym} on-chain. Advising {act_type} to restore {target_rungs} Asks.",
                        }
                        ch_advice = f"DEGRADED (One-Sided: Asks Degraded): Bids suspended. Found on-chain {base_sym}: advising {act_type} ({dep_amt:.4f} {base_sym}) to restore {target_rungs} Asks."
                else:
                    ch_action = {"action_type": "NONE", "reason": "One-sided organic rebalancing."}
                    deficit_q_str = f" (needs ${deficit_q_usd:.2f} {quote_sym} for {target_rungs} Bids)" if deficit_q_usd >= 0.50 else ""
                    ch_advice = (
                        f"DEGRADED (One-Sided Quoting): Bids suspended{deficit_q_str}. "
                        f"Quoting aggressive Asks (spread 0.90x, reservation bias) to attract fills and accumulate {quote_sym}."
                    )
        elif not can_send_base_1 or not can_send_base_3:
            # Cannot place full 3 asks, can place bids -> One-sided or degraded
            onchain_bals = inventory.get("onchain_balances", {})
            usable_onchain_bals = inventory.get("usable_onchain_balances", {})
            avail_b = float(usable_onchain_bals.get(base_net, {}).get(base_sym, onchain_bals.get(base_net, {}).get(base_sym, 0.0)))
            avail_b_usd = avail_b * base_fiat

            deficit_b_usd = max(0.0, req_3_b_send_usd - (eff_b_send_usd * 0.90))
            deficit_b_amt = deficit_b_usd / base_fiat if base_fiat > 0 else 0.0

            # Actively steer quoting to attract fills on the active side to accumulate base
            risk_level = "ELEVATED"
            spread_mult = 0.90
            if delta <= 0:
                center_bias = max(center_bias, +max_bias * 0.8)

            eff_q_recv = inventory.get("effective_quote_receivable", quote_receivable)
            eff_q_recv_usd = eff_q_recv * quote_fiat
            is_quote_recv_zero = eff_q_recv_usd < req_q_send_usd
            can_fund_b = (avail_b >= getattr(self.config, "min_onchain_btc_open_amt", 0.0004) and avail_b_usd >= getattr(self.config, "min_onchain_btc_open_usd", 40.0)) if base_net == "Bitcoin" else (avail_b_usd >= 1.0)
            if can_fund_b and deficit_b_usd >= 0.50 and not is_quote_recv_zero:
                has_ch = not (base_receivable == 0.0 and base_sendable == 0.0)
                act_type = "OPEN_CHANNEL" if base_net == "Bitcoin" else ("DEPOSIT_CHANNEL" if (has_ch or (base_net in ("Arbitrum", "Ethereum") and inventory.get("has_active_lithium_channel_base"))) else "OPEN_CHANNEL")
                total_b_cap = base_sendable + base_receivable
                cap_target = (total_b_cap * target_pct) if total_b_cap > 0 else (fallback_baseline_usd / base_fiat)
                health_def = max(deficit_b_amt, cap_target, min_hub_usd / base_fiat)
                if base_net == "Bitcoin":
                    btc_largest = float(inventory.get("btc_largest_channel_cap", 0.0))
                    max_avail = round(max(0.0001, avail_b - 0.00005), 6)
                    ratchet_target = (btc_largest + (1.5 * health_def)) if btc_largest > 0 else max_avail
                    target_amt = round(ratchet_target, 6)
                    dep_amt = min(max_avail, target_amt) if max_avail >= target_amt else max_avail
                    scale_str = f"ratchet: {dep_amt:.6f}" if max_avail >= target_amt else f"best available: {dep_amt:.6f}"
                    reason_label = "Asks suspended" if not can_send_base_1 else f"{base_sym} send capacity deficient ({supported_b_rungs}/{target_rungs} rungs)"
                    ch_action = {
                        "action_type": act_type,
                        "network": base_net,
                        "asset": base_sym,
                        "amount": dep_amt,
                        "deficit": health_def,
                        "largest_cap": btc_largest,
                        "ratchet": (btc_largest > 0),
                        "reason": (
                            f"DEGRADED: {reason_label} (needs ${deficit_b_usd:.2f} {base_sym}). "
                            f"Advising OPEN_CHANNEL (best available: {dep_amt:.6f} {base_sym}"
                            + (f", ratchet target: {target_amt:.6f}" if btc_largest > 0 else "")
                            + f") to restore {target_rungs} Asks and grow scale."
                        ),
                    }
                    advice_label = "DEGRADED (One-Sided: Asks Suspended)" if not can_send_base_1 else f"DEGRADED ({base_sym} Send Capacity Deficient: {supported_b_rungs}/{target_rungs} rungs)"
                    ch_advice = f"{advice_label}: Needs ${deficit_b_usd:.2f} {base_sym}. Found on-chain {base_sym}: advising {act_type} ({scale_str} {base_sym}) to restore {target_rungs} Asks."
                else:
                    target_amt = round(health_def, 6 if base_sym in ("BTC", "ETH") else 2)
                    dep_amt = min(avail_b, target_amt)
                    ch_action = {
                        "action_type": act_type,
                        "network": base_net,
                        "asset": base_sym,
                        "amount": dep_amt,
                        "reason": f"DEGRADED: Asks suspended (needs ${deficit_b_usd:.2f} {base_sym}). Detected {avail_b:.4f} {base_sym} on-chain. Advising {act_type} to restore {target_rungs} Asks.",
                    }
                    ch_advice = f"DEGRADED (One-Sided: Asks Suspended): Needs ${deficit_b_usd:.2f} {base_sym}. Found on-chain {base_sym}: advising {act_type} ({dep_amt:.4f} {base_sym}) to restore {target_rungs} Asks."
            else:
                # Base cannot be restored from on-chain. Check if quote side is also degraded and has on-chain funds
                avail_q = float(usable_onchain_bals.get(quote_net, {}).get(quote_sym, onchain_bals.get(quote_net, {}).get(quote_sym, 0.0)))
                avail_q_usd = avail_q * quote_fiat
                deficit_q_usd = max(0.0, req_3_q_send_usd - (eff_q_send_usd * 0.90))
                deficit_q_amt = deficit_q_usd / quote_fiat if quote_fiat > 0 else 0.0

                can_fund_q = (avail_q >= getattr(self.config, "min_onchain_btc_open_amt", 0.0004) and avail_q_usd >= getattr(self.config, "min_onchain_btc_open_usd", 40.0)) if quote_net == "Bitcoin" else (avail_q_usd >= 1.0)
                if (not can_send_quote_1 or not can_send_quote_3) and can_fund_q and deficit_q_usd >= 0.50 and not is_quote_recv_zero:
                    has_ch = not (quote_receivable == 0.0 and quote_sendable == 0.0)
                    act_type = "OPEN_CHANNEL" if quote_net == "Bitcoin" else ("DEPOSIT_CHANNEL" if (has_ch or (quote_net in ("Arbitrum", "Ethereum") and inventory.get("has_active_lithium_channel_quote"))) else "OPEN_CHANNEL")
                    total_q_cap = quote_sendable + quote_receivable
                    cap_target = (total_q_cap * target_pct) if total_q_cap > 0 else (fallback_baseline_usd / quote_fiat)
                    health_def = max(deficit_q_amt, cap_target, min_hub_usd / quote_fiat)
                    if quote_net == "Bitcoin":
                        btc_largest = float(inventory.get("btc_largest_channel_cap", 0.0))
                        max_avail = round(max(0.0001, avail_q - 0.00005), 6)
                        ratchet_target = (btc_largest + (1.5 * health_def)) if btc_largest > 0 else max_avail
                        target_amt = round(ratchet_target, 6)
                        dep_amt = min(max_avail, target_amt) if max_avail >= target_amt else max_avail
                        scale_str = f"ratchet: {dep_amt:.6f}" if max_avail >= target_amt else f"best available: {dep_amt:.6f}"
                        reason_label = "Bids suspended" if not can_send_quote_1 else f"{quote_sym} send capacity deficient ({supported_q_rungs}/{target_rungs} rungs)"
                        ch_action = {
                            "action_type": act_type,
                            "network": quote_net,
                            "asset": quote_sym,
                            "amount": dep_amt,
                            "deficit": health_def,
                            "largest_cap": btc_largest,
                            "ratchet": (btc_largest > 0),
                            "reason": (
                                f"DEGRADED: Asks suspended & {reason_label} (needs ${deficit_q_usd:.2f} {quote_sym}). "
                                f"Advising OPEN_CHANNEL (best available: {dep_amt:.6f} {quote_sym}"
                                + (f", ratchet target: {target_amt:.6f}" if btc_largest > 0 else "")
                                + f") to restore {target_rungs} Bids and grow scale."
                            ),
                        }
                        advice_label = "DEGRADED (One-Sided: Bids Suspended)" if not can_send_quote_1 else f"DEGRADED ({quote_sym} Send Capacity Deficient: {supported_q_rungs}/{target_rungs} rungs)"
                        ch_advice = f"{advice_label}: Asks suspended. Found on-chain {quote_sym}: advising {act_type} ({scale_str} {quote_sym}) to restore {target_rungs} Bids."
                    else:
                        target_amt = round(health_def, 6 if quote_sym in ("BTC", "ETH") else 2)
                        dep_amt = min(avail_q, target_amt)
                        ch_action = {
                            "action_type": act_type,
                            "network": quote_net,
                            "asset": quote_sym,
                            "amount": dep_amt,
                            "reason": f"DEGRADED: Asks suspended & Bids degraded (needs ${deficit_q_usd:.2f} {quote_sym}). Detected {avail_q:.4f} {quote_sym} on-chain. Advising {act_type} to restore {target_rungs} Bids.",
                        }
                        ch_advice = f"DEGRADED (One-Sided: Bids Degraded): Asks suspended. Found on-chain {quote_sym}: advising {act_type} ({dep_amt:.4f} {quote_sym}) to restore {target_rungs} Bids."
                else:
                    ch_action = {"action_type": "NONE", "reason": "One-sided organic rebalancing."}
                    deficit_b_str = f" (needs ${deficit_b_usd:.2f} {base_sym} for {target_rungs} Asks)" if deficit_b_usd >= 0.50 else ""
                    ch_advice = (
                        f"DEGRADED (One-Sided Quoting): Asks suspended{deficit_b_str}. "
                        f"Quoting aggressive Bids (spread 0.90x, reservation bias) to attract fills and accumulate {base_sym}."
                    )
        else:
            ch_action = {"action_type": "NONE", "reason": "Capacities sufficient."}
            ch_advice = (
                f"State channel capacities are operational with two-way liquidity ({target_rungs} bid/ask rungs supported, inbound buffer above ${buffer_usd:.0f} USD on both sides)."
            )


        # Collateral reserve protection check for Arbitrum USDC (protects GMX Agent bot)
        if ch_action and ch_action.get("action_type") in ("DEPOSIT_CHANNEL", "OPEN_CHANNEL"):
            if ch_action.get("asset") == "USDC" and ch_action.get("network") == "Arbitrum":
                usable_onchain_bals = inventory.get("usable_onchain_balances", {})
                usable_usdc = float(usable_onchain_bals.get("Arbitrum", {}).get("USDC", 0.0))
                req_increment = getattr(self.config, "target_auto_rental_amount_usd", 25.0)
                usdc_res_val = getattr(self.config, "min_onchain_usdc_reserve_usd", 1030.0)
                if usable_usdc < req_increment or ch_action.get("amount", 0.0) > usable_usdc:
                    ch_advice = (
                        f"PROTECTED: Arbitrum USDC usable headroom (${usable_usdc:.2f}) is below "
                        f"the ${req_increment:.2f} increment needed to protect GMX Agent bot's collateral needs (${usdc_res_val:.0f})."
                    )
                    ch_action = None

        # Pending channel/rental update protection check
        if ch_action and ch_action.get("action_type") in ("RENT_INBOUND", "OPEN_CHANNEL", "DEPOSIT_CHANNEL"):
            act_net = ch_action.get("network", "")
            act_asset = ch_action.get("asset", "")
            is_pending = False
            if act_asset == base_sym and (act_net == base_net or not act_net):
                is_pending = bool(inventory.get("base_pending_update"))
            elif act_asset == quote_sym and (act_net == quote_net or not act_net):
                is_pending = bool(inventory.get("quote_pending_update"))

            if is_pending:
                ch_advice = (
                    f"PENDING UPDATE: {act_net} {act_asset} channel or inbound rental is currently pending on-chain confirmation. "
                    f"Suppressing new {ch_action.get('action_type')} capacity action."
                )
                ch_action = None

        extra_notes = []
        if onchain_btc > 0.000001 and "BTC" in (base_sym, quote_sym):
            extra_notes.append(f"{onchain_btc:.4f} BTC On-Chain")
        if alby_btc > 0.000001 and "BTC" in (base_sym, quote_sym):
            extra_notes.append(f"{alby_btc:.4f} BTC Alby")
        extra_str = f" (incl. {', '.join(extra_notes)})" if extra_notes else ""
        commentary = f"{base_ratio:.1%} {base_sym} / {(1.0-base_ratio):.1%} {quote_sym}{extra_str}"

        return SupervisorDecision(
            spread_multiplier=spread_mult,
            bid_spread_multiplier=bid_spread_mult,
            ask_spread_multiplier=ask_spread_mult,
            target_base_ratio=target_ratio,
            relative_sentiment=rel_sentiment,
            center_price_bias_pct=center_bias,
            curve_concentration=concentration,
            skew_factor=skew_factor,
            bid_size_multiplier=bid_mult,
            ask_size_multiplier=ask_mult,
            risk_level=risk_level,
            allow_trading=True,
            channel_advice=ch_advice,
            channel_action=ch_action,
            commentary=commentary,
            base_ratio=base_ratio,
            quote_ratio=1.0 - base_ratio,
        )

    def calculate_rebalance_deficit(
        self, inventory: Dict[str, Any], oracle_price: float
    ) -> Tuple[str, float, float, float]:
        """
        Calculates directional trade need, base unit deficit, USD deficit, and current skew delta
        to bring channel inventory back to the sentiment-adjusted target ratio.

        Returns:
            (action_needed, deficit_base_units, deficit_usd, delta)
            where action_needed is "SELL_BASE" (overweight base), "BUY_BASE" (underweight base), or "BALANCED"
        """
        base_sendable = float(inventory.get("base_sendable", inventory.get("effective_base_sendable", 0.0)))
        quote_sendable = float(inventory.get("quote_sendable", inventory.get("effective_quote_sendable", 0.0)))

        base_fiat = inventory.get("base_fiat")
        if base_fiat is None or base_fiat <= 0:
            base_fiat = self._fallbacks.get(inventory.get("base_symbol", ""), 1.0)

        quote_fiat = inventory.get("quote_fiat")
        if quote_fiat is None or quote_fiat <= 0:
            quote_fiat = self._fallbacks.get(inventory.get("quote_symbol", ""), 1.0)

        base_sym = inventory.get("base_symbol", "")
        quote_sym = inventory.get("quote_symbol", "")

        onchain_btc = (
            float(inventory.get("onchain_btc_available", 0.0))
            if getattr(self.config, "include_onchain_in_inventory_skew", True)
            else 0.0
        )
        alby_btc = (
            float(inventory.get("alby_btc_available", 0.0))
            if getattr(self.config, "include_alby_in_inventory_skew", True)
            else 0.0
        )

        total_extra_btc = onchain_btc + alby_btc

        port_base_send = float(inventory.get("portfolio_base_sendable", base_sendable))
        eff_base_inv = port_base_send
        if base_sym == "BTC":
            eff_base_inv += total_extra_btc

        port_quote_send = float(inventory.get("portfolio_quote_sendable", quote_sendable))
        eff_quote_inv = port_quote_send
        if quote_sym == "BTC":
            eff_quote_inv += total_extra_btc

        base_val_usd = eff_base_inv * base_fiat
        quote_val_usd = eff_quote_inv * quote_fiat
        total_usd = base_val_usd + quote_val_usd
        target_ratio, rel_sentiment = self.calculate_sentiment_target_ratio(base_sym, quote_sym)

        base_ratio = base_val_usd / total_usd
        delta = base_ratio - target_ratio

        target_base_usd = total_usd * target_ratio
        deficit_usd = abs(base_val_usd - target_base_usd)
        deficit_base_units = deficit_usd / base_fiat if base_fiat > 0 else (deficit_usd / oracle_price)

        min_skew = getattr(self.config, "active_rebalance_min_skew_pct", 0.0)
        if abs(delta) <= min_skew or deficit_base_units <= 0:
            return ("BALANCED", deficit_base_units, deficit_usd, delta)

        if delta > 0:
            return ("SELL_BASE", deficit_base_units, deficit_usd, delta)
        else:
            return ("BUY_BASE", deficit_base_units, deficit_usd, delta)

    calculate_active_rebalance_need = calculate_rebalance_deficit


# Alias for backward compatibility
PositionSupervisor = DeterministicSupervisor

