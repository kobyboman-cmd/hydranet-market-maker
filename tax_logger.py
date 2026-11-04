"""
Tax Trade Logger for Hydranet DEX Market Maker.
Maintains a permanent, append-only tax audit ledger (CSV and JSONL) for IRS Form 8949,
Schedule D, and crypto tax software (CoinTracker, Koinly, TaxBit, CryptoTaxCalculator).

Every executed trade (maker fill, active rebalance, triangular arbitrage) is recorded
with precise disposal (Sent Asset/Amount), acquisition (Received Asset/Amount),
USD spot prices, total trade valuation (cost basis / proceeds), maker fee rebates
(taxable income / fee offset), and transaction IDs.
"""

import os
import csv
import json
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Set, Tuple
from dataclasses import dataclass, asdict, field
from pathlib import Path
from collections import defaultdict, deque

logger = logging.getLogger("TaxLogger")


# Standard market metadata fallback mapping for asset and network resolution
PAIR_METADATA: Dict[str, Dict[str, Any]] = {
    "HDN/USDC": {
        "base_symbol": "HDN",
        "base_network": "Arbitrum",
        "quote_symbol": "USDC",
        "quote_network": "Arbitrum",
    },
    "BTC/USDC": {
        "base_symbol": "BTC",
        "base_network": "Bitcoin",
        "quote_symbol": "USDC",
        "quote_network": "Arbitrum",
    },
    "ETH/USDC": {
        "base_symbol": "ETH",
        "base_network": "Ethereum",
        "quote_symbol": "USDC",
        "quote_network": "Arbitrum",
    },
    "ETH/BTC": {
        "base_symbol": "ETH",
        "base_network": "Ethereum",
        "quote_symbol": "BTC",
        "quote_network": "Bitcoin",
    },
    "ETH(Arb)/ETH(Eth)": {
        "base_symbol": "ETH",
        "base_network": "Arbitrum",
        "quote_symbol": "ETH",
        "quote_network": "Ethereum",
    },
    "USDC(Arb)/USDC(Eth)": {
        "base_symbol": "USDC",
        "base_network": "Arbitrum",
        "quote_symbol": "USDC",
        "quote_network": "Ethereum",
    },
    "ETH_ARB/ETH_ETH": {
        "base_symbol": "ETH",
        "base_network": "Arbitrum",
        "quote_symbol": "ETH",
        "quote_network": "Ethereum",
    },
    "USDC_ARB/USDC_ETH": {
        "base_symbol": "USDC",
        "base_network": "Arbitrum",
        "quote_symbol": "USDC",
        "quote_network": "Ethereum",
    },
}

CSV_COLUMNS: List[str] = [
    "Date (UTC)",
    "Time (UTC)",
    "Timestamp (ISO)",
    "Timestamp (Unix)",
    "Trade ID",
    "Order ID",
    "Platform",
    "Market",
    "Side",
    "Trade Type",
    "Tax Type",
    "Sent Quantity",
    "Sent Asset",
    "Sent Network",
    "Received Quantity",
    "Received Asset",
    "Received Network",
    "Execution Price",
    "Volume USD",
    "Base Asset Price USD",
    "Quote Asset Price USD",
    "Fee Amount",
    "Fee Asset",
    "Fee USD",
    "Maker Rebate Rate",
    "Rebate USD",
    "Net Profit USD",
    "Tax Classification",
    "Notes",
]


@dataclass
class TaxTradeRecord:
    date_utc: str
    time_utc: str
    timestamp_iso: str
    timestamp_unix: float
    trade_id: str
    order_id: str
    platform: str
    market: str
    side: str
    trade_type: str
    tax_type: str
    sent_quantity: float
    sent_asset: str
    sent_network: str
    received_quantity: float
    received_asset: str
    received_network: str
    execution_price: float
    volume_usd: float
    base_asset_price_usd: float
    quote_asset_price_usd: float
    fee_amount: float
    fee_asset: str
    fee_usd: float
    maker_rebate_rate: float
    rebate_usd: float
    net_profit_usd: float
    tax_classification: str
    notes: str

    def to_csv_row(self) -> Dict[str, Any]:
        """Converts record to CSV dictionary matching CSV_COLUMNS."""
        return {
            "Date (UTC)": self.date_utc,
            "Time (UTC)": self.time_utc,
            "Timestamp (ISO)": self.timestamp_iso,
            "Timestamp (Unix)": f"{self.timestamp_unix:.4f}",
            "Trade ID": self.trade_id,
            "Order ID": self.order_id,
            "Platform": self.platform,
            "Market": self.market,
            "Side": self.side,
            "Trade Type": self.trade_type,
            "Tax Type": self.tax_type,
            "Sent Quantity": f"{self.sent_quantity:.8f}".rstrip("0").rstrip(".") if self.sent_quantity else "0",
            "Sent Asset": self.sent_asset,
            "Sent Network": self.sent_network,
            "Received Quantity": f"{self.received_quantity:.8f}".rstrip("0").rstrip(".") if self.received_quantity else "0",
            "Received Asset": self.received_asset,
            "Received Network": self.received_network,
            "Execution Price": f"{self.execution_price:.8f}".rstrip("0").rstrip("."),
            "Volume USD": f"{self.volume_usd:.2f}",
            "Base Asset Price USD": f"{self.base_asset_price_usd:.4f}",
            "Quote Asset Price USD": f"{self.quote_asset_price_usd:.4f}",
            "Fee Amount": f"{self.fee_amount:.8f}".rstrip("0").rstrip(".") if self.fee_amount else "0",
            "Fee Asset": self.fee_asset,
            "Fee USD": f"{self.fee_usd:.4f}",
            "Maker Rebate Rate": f"{self.maker_rebate_rate:.6f}",
            "Rebate USD": f"{self.rebate_usd:.4f}",
            "Net Profit USD": f"{self.net_profit_usd:.4f}",
            "Tax Classification": self.tax_classification,
            "Notes": self.notes,
        }


class TaxLogger:
    """
    Append-only persistent tax ledger manager.
    Guarantees that all historical trades are preserved forever, with no truncation.
    """

    def __init__(
        self,
        csv_path: str = "data/tax_trade_log.csv",
        jsonl_path: str = "data/tax_trade_log.jsonl",
        auto_backfill: bool = True,
    ):
        self.csv_path = Path(csv_path)
        self.jsonl_path = Path(jsonl_path)
        self.logged_trade_ids: Set[str] = set()

        # Cache for high-frequency trading performance queries
        self._cached_trading_perf: Optional[Dict[str, Any]] = None
        self._last_perf_calc_time: float = 0.0
        self._cached_records_len: int = 0

        self._init_storage()
        if auto_backfill:
            self._auto_backfill_historical()

    def _init_storage(self):
        """Initializes data directory and CSV header if not existing."""
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing trade IDs from CSV if file already exists
        if self.csv_path.exists() and self.csv_path.stat().st_size > 0:
            try:
                with open(self.csv_path, "r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        tid = row.get("Trade ID")
                        if tid:
                            self.logged_trade_ids.add(tid)
                logger.info(f"Loaded {len(self.logged_trade_ids)} existing trade records from tax ledger CSV.")
            except Exception as e:
                logger.warning(f"Error reading existing tax log CSV: {e}")
        else:
            # Write new CSV with header
            try:
                with open(self.csv_path, "w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                    writer.writeheader()
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                logger.error(f"Failed to create tax log CSV header: {e}")

    def _resolve_market_assets(self, pair: str) -> Tuple[str, str, str, str]:
        """
        Resolves base_symbol, base_network, quote_symbol, quote_network.
        First checks MARKETS from config if available, then PAIR_METADATA, then heuristics.
        """
        try:
            from config import MARKETS
            if pair in MARKETS:
                m = MARKETS[pair]
                return (
                    m.base.symbol,
                    m.base.network_name,
                    m.quote.symbol,
                    m.quote.network_name,
                )
        except Exception:
            pass

        if pair in PAIR_METADATA:
            meta = PAIR_METADATA[pair]
            return (
                meta["base_symbol"],
                meta["base_network"],
                meta["quote_symbol"],
                meta["quote_network"],
            )

        # Fallback heuristic
        if "/" in pair:
            parts = pair.split("/")
            base_raw = parts[0].strip()
            quote_raw = parts[1].strip()

            base_net = "Arbitrum" if "ARB" in base_raw else ("Ethereum" if "ETH" in base_raw else "Arbitrum")
            quote_net = "Ethereum" if "ETH" in quote_raw else ("Bitcoin" if "BTC" in quote_raw else "Arbitrum")
            base_sym = base_raw.split("_")[0]
            quote_sym = quote_raw.split("_")[0]
            return (base_sym, base_net, quote_sym, quote_net)

        return (pair, "Arbitrum", "USDC", "Arbitrum")

    def build_record(
        self,
        trade_id: str,
        timestamp: float,
        pair: str,
        side: str,
        price: float,
        amount: float,
        volume_usd: float = 0.0,
        maker_rebate_rate: float = 0.0,
        rebate_usd: float = 0.0,
        order_id: Optional[str] = None,
        trade_type: str = "MAKER",
        profit_usd: float = 0.0,
        base_usd_price: float = 0.0,
        quote_usd_price: float = 0.0,
        notes: str = "",
    ) -> TaxTradeRecord:
        """Constructs a fully populated, audit-ready TaxTradeRecord."""
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        date_utc = dt.strftime("%Y-%m-%d")
        time_utc = dt.strftime("%H:%M:%S")
        timestamp_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"

        base_sym, base_net, quote_sym, quote_net = self._resolve_market_assets(pair)

        # Estimate asset prices if not supplied
        if base_usd_price <= 0.0 or quote_usd_price <= 0.0:
            if "USDC" in quote_sym:
                quote_usd_price = 1.0
                base_usd_price = price if base_usd_price <= 0.0 else base_usd_price
            elif "USDC" in base_sym:
                base_usd_price = 1.0
                quote_usd_price = (1.0 / price) if price > 0 else 1.0
            elif quote_sym == "BTC":
                quote_usd_price = 83000.0  # Approx fallback
                base_usd_price = price * quote_usd_price

        # Calculate volume in USD if 0
        if volume_usd <= 0.0:
            if base_usd_price > 0:
                volume_usd = amount * base_usd_price
            elif quote_usd_price > 0:
                volume_usd = (amount * price) * quote_usd_price
            else:
                volume_usd = amount * price

        side_upper = side.upper()
        # Crypto Tax Rules (IRS Form 8949):
        # SELL base: Dispose base asset, Acquire quote asset
        # BUY base: Dispose quote asset, Acquire base asset
        if side_upper == "SELL":
            sent_quantity = amount
            sent_asset = base_sym
            sent_network = base_net
            received_quantity = amount * price
            received_asset = quote_sym
            received_network = quote_net
        else:  # BUY
            sent_quantity = amount * price
            sent_asset = quote_sym
            sent_network = quote_net
            received_quantity = amount
            received_asset = base_sym
            received_network = base_net

        # Rebates vs Taker fees
        # In Hydranet DLMM, maker fills receive maker rebate paid in quote currency
        rebate_rate_abs = abs(maker_rebate_rate)
        if rebate_usd > 0:
            fee_amount = rebate_usd / quote_usd_price if quote_usd_price > 0 else (amount * price * rebate_rate_abs)
            fee_asset = quote_sym
            fee_usd = rebate_usd  # positive for rebate earned
        else:
            fee_amount = 0.0
            fee_asset = quote_sym
            fee_usd = 0.0

        if not notes:
            if trade_type == "ACTIVE_REBALANCE":
                notes = f"Active Inventory Rebalancing Taker Fill ({side_upper} {amount} {base_sym} @ {price})"
            elif trade_type == "TRIANGULAR_ARB":
                notes = f"Triangular Arbitrage Taker Leg ({side_upper} {amount} {base_sym} @ {price})"
            else:
                notes = f"DLMM Maker Ladder Fill ({side_upper} {amount} {base_sym} @ {price})"

        return TaxTradeRecord(
            date_utc=date_utc,
            time_utc=time_utc,
            timestamp_iso=timestamp_iso,
            timestamp_unix=timestamp,
            trade_id=trade_id or f"trd-{int(timestamp * 1000)}",
            order_id=order_id or "",
            platform="Hydranet DEX",
            market=pair,
            side=side_upper,
            trade_type=trade_type,
            tax_type="Trade",
            sent_quantity=sent_quantity,
            sent_asset=sent_asset,
            sent_network=sent_network,
            received_quantity=received_quantity,
            received_asset=received_asset,
            received_network=received_network,
            execution_price=price,
            volume_usd=round(volume_usd, 2),
            base_asset_price_usd=round(base_usd_price, 4),
            quote_asset_price_usd=round(quote_usd_price, 4),
            fee_amount=round(fee_amount, 8),
            fee_asset=fee_asset,
            fee_usd=round(fee_usd, 4),
            maker_rebate_rate=rebate_rate_abs,
            rebate_usd=round(rebate_usd, 4),
            net_profit_usd=round(profit_usd, 4),
            tax_classification="Disposal and Acquisition via DEX State Channel Swap",
            notes=notes,
        )

    def log_trade(
        self,
        trade_id: str,
        timestamp: float,
        pair: str,
        side: str,
        price: float,
        amount: float,
        volume_usd: float = 0.0,
        maker_rebate_rate: float = 0.0,
        rebate_usd: float = 0.0,
        order_id: Optional[str] = None,
        trade_type: str = "MAKER",
        profit_usd: float = 0.0,
        base_usd_price: float = 0.0,
        quote_usd_price: float = 0.0,
        notes: str = "",
    ) -> Optional[TaxTradeRecord]:
        """
        Appends an individual trade to both CSV and JSONL ledgers immediately.
        Deduplicates by trade_id so duplicate calls are safely ignored.
        """
        effective_tid = trade_id or f"trd-{int(timestamp * 1000)}"
        if effective_tid in self.logged_trade_ids:
            return None

        record = self.build_record(
            trade_id=effective_tid,
            timestamp=timestamp,
            pair=pair,
            side=side,
            price=price,
            amount=amount,
            volume_usd=volume_usd,
            maker_rebate_rate=maker_rebate_rate,
            rebate_usd=rebate_usd,
            order_id=order_id,
            trade_type=trade_type,
            profit_usd=profit_usd,
            base_usd_price=base_usd_price,
            quote_usd_price=quote_usd_price,
            notes=notes,
        )

        try:
            # 1. Append to CSV
            with open(self.csv_path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writerow(record.to_csv_row())
                f.flush()
                os.fsync(f.fileno())

            # 2. Append to JSONL
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record)) + "\n")
                f.flush()
                os.fsync(f.fileno())

            self.logged_trade_ids.add(effective_tid)
            self._cached_trading_perf = None
            logger.info(f"[TAX LOG] Appended {record.tax_type} ({record.market} {record.side} {record.sent_quantity:.4f} {record.sent_asset} -> {record.received_quantity:.4f} {record.received_asset}, FMV: ${record.volume_usd:.2f})")
            return record
        except Exception as e:
            logger.error(f"Failed to append to tax log: {e}")
            return None

    def backfill_from_pnl_history(self, pnl_history_path: str = "data/pnl_history.json") -> int:
        """
        Backfills historical trades stored in pnl_history.json into the permanent tax log.
        Skips any trades already present in the ledger.
        """
        path = Path(pnl_history_path)
        if not path.exists():
            return 0

        count = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            trades = data.get("trades", [])
            # Trades are stored newest first in pnl_history; reverse to log chronologically
            for t in reversed(trades):
                tid = t.get("trade_id")
                if tid and tid in self.logged_trade_ids:
                    continue
                ts = float(t.get("timestamp", time.time()))
                pair = t.get("pair", "HDN/USDC")
                side = t.get("side", "BUY")
                price = float(t.get("price", 0.0))
                amount = float(t.get("amount", 0.0))
                vol_usd = float(t.get("volume_usd", 0.0))
                rebate_rate = float(t.get("maker_rebate_rate", 0.0005))
                rebate_usd = float(t.get("rebate_usd", 0.0))
                oid = t.get("order_id")
                ttype = t.get("trade_type", "MAKER")
                profit_usd = float(t.get("profit_usd", 0.0))

                rec = self.log_trade(
                    trade_id=tid,
                    timestamp=ts,
                    pair=pair,
                    side=side,
                    price=price,
                    amount=amount,
                    volume_usd=vol_usd,
                    maker_rebate_rate=rebate_rate,
                    rebate_usd=rebate_usd,
                    order_id=oid,
                    trade_type=ttype,
                    profit_usd=profit_usd,
                    notes=f"Historical Backfill: {ttype} {side} on {pair}",
                )
                if rec:
                    count += 1
            if count > 0:
                logger.info(f"Successfully backfilled {count} historical trades from {pnl_history_path} to tax ledger.")
        except Exception as e:
            logger.warning(f"Error backfilling from {pnl_history_path}: {e}")
        return count

    def backfill_from_active_rebalance(self, rebal_path: str = "data/active_rebalance_history.json") -> int:
        """
        Backfills executed active rebalancing trades from active_rebalance_history.json.
        """
        path = Path(rebal_path)
        if not path.exists():
            return 0

        count = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            executions = data.get("recent_executions", [])
            for e in reversed(executions):
                eid = e.get("id") or e.get("order_id")
                if eid and eid in self.logged_trade_ids:
                    continue
                # Only log real EXECUTED trades (skip SIMULATED)
                if e.get("status") != "EXECUTED":
                    continue
                ts = float(e.get("timestamp", time.time()))
                pair = e.get("pair", "ETH/USDC")
                side = e.get("side", "SELL")
                price = float(e.get("price", 0.0))
                amount = float(e.get("amount", 0.0))
                vol_usd = float(e.get("volume_usd", 0.0))
                profit_usd = float(e.get("net_profit_usd", 0.0))
                oid = e.get("order_id")
                oracle_price = float(e.get("oracle_price", 0.0))

                rec = self.log_trade(
                    trade_id=eid,
                    timestamp=ts,
                    pair=pair,
                    side=side,
                    price=price,
                    amount=amount,
                    volume_usd=vol_usd,
                    maker_rebate_rate=0.0,
                    rebate_usd=0.0,
                    order_id=oid,
                    trade_type="ACTIVE_REBALANCE",
                    profit_usd=profit_usd,
                    base_usd_price=oracle_price,
                    notes=f"Historical Backfill: Active Rebalance Taker {side} on {pair}",
                )
                if rec:
                    count += 1
            if count > 0:
                logger.info(f"Successfully backfilled {count} active rebalance executions from {rebal_path} to tax ledger.")
        except Exception as e:
            logger.warning(f"Error backfilling from {rebal_path}: {e}")
        return count

    def _auto_backfill_historical(self):
        """Runs auto-backfill on initialization."""
        self.backfill_from_pnl_history()
        self.backfill_from_active_rebalance()

    def get_all_records(self) -> List[Dict[str, Any]]:
        """Reads all records from the CSV file."""
        if not self.csv_path.exists():
            return []
        try:
            with open(self.csv_path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                return list(reader)
        except Exception as e:
            logger.error(f"Error reading tax log CSV: {e}")
            return []

    def calculate_trading_performance(self, window_days: Optional[float] = 30.0, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Calculates audit-accurate, real-time trading performance from the permanent tax log.
        If window_days is specified (default 30.0), calculates performance over a rolling window
        matching the 30-day settled volume schedule, completely independent of baseline resets.
        Computes:
          - FIFO round-trip spread capture P&L
          - Total maker fee rebates earned
          - Active inventory rebalancing taker execution costs
          - Net Realized Trading Alpha
          - Per-market performance breakdown (BTC/USDC, ETH/USDC, ETH/BTC, cross-chain pegs, HDN/USDC)
        Caches results for high-frequency dashboard queries (< 0.1ms cache hit).
        """
        now = time.time()
        cached_win = getattr(self, "_cached_window_days", None)
        if (
            not force_refresh
            and self._cached_trading_perf is not None
            and (now - self._last_perf_calc_time < 3.0)
            and len(self.logged_trade_ids) == self._cached_records_len
            and cached_win == window_days
        ):
            return self._cached_trading_perf

        records = self.get_all_records()
        if not records:
            empty_res = {
                "window_days": window_days,
                "total_trades": 0,
                "total_volume_usd": 0.0,
                "realized_spread_pnl_usd": 0.0,
                "maker_rebates_usd": 0.0,
                "rebalance_cost_usd": 0.0,
                "net_realized_alpha_usd": 0.0,
                "by_market": {},
            }
            self._cached_trading_perf = empty_res
            self._last_perf_calc_time = now
            self._cached_records_len = 0
            self._cached_window_days = window_days
            return empty_res

        # If a rolling window is specified, filter records within window
        if window_days is not None and window_days > 0:
            cutoff_ts = now - (window_days * 86400.0)
            records = [r for r in records if float(r.get("Timestamp (Unix)", 0.0) or 0.0) >= cutoff_ts]

        # Sort chronologically
        records.sort(key=lambda t: float(t.get("Timestamp (Unix)", 0.0) or 0.0))

        pair_stats = defaultdict(lambda: {
            "trades": 0,
            "buys": 0,
            "sells": 0,
            "volume_usd": 0.0,
            "spread_pnl_usd": 0.0,
            "rebates_usd": 0.0,
            "rebalance_cost_usd": 0.0,
            "net_profit_usd": 0.0,
            "open_inventory_base": 0.0,
            "unmatched_base_sells": 0.0,
        })

        inventory = defaultdict(deque)

        for t in records:
            market = t.get("Market", "")
            side = t.get("Side", "").upper()
            ttype = t.get("Trade Type", "")
            sent_qty = float(t.get("Sent Quantity", 0.0) or 0.0)
            recv_qty = float(t.get("Received Quantity", 0.0) or 0.0)
            price = float(t.get("Execution Price", 0.0) or 0.0)
            vol_usd = float(t.get("Volume USD", 0.0) or 0.0)
            rebate_usd = float(t.get("Rebate USD", 0.0) or 0.0)
            base_oracle_p = float(t.get("Base Asset Price USD", 0.0) or 0.0)
            quote_p = float(t.get("Quote Asset Price USD", 1.0) or 1.0)
            net_col = float(t.get("Net Profit USD", 0.0) or 0.0)

            s = pair_stats[market]
            s["trades"] += 1
            s["volume_usd"] += vol_usd
            s["rebates_usd"] += rebate_usd

            if ttype == "ACTIVE_REBALANCE":
                s["rebalance_cost_usd"] += net_col

            if market.endswith("/USDC"):
                if side == "BUY":
                    s["buys"] += 1
                    cost = sent_qty / recv_qty if recv_qty > 0 else price
                    inventory[market].append((recv_qty, cost))
                elif side == "SELL":
                    s["sells"] += 1
                    proceeds = recv_qty / sent_qty if sent_qty > 0 else price
                    rem = sent_qty
                    gain = 0.0
                    while rem > 1e-9 and inventory[market]:
                        lot_q, lot_c = inventory[market][0]
                        match_q = min(rem, lot_q)
                        gain += match_q * (proceeds - lot_c)
                        rem -= match_q
                        if lot_q > match_q + 1e-9:
                            inventory[market][0] = (lot_q - match_q, lot_c)
                        else:
                            inventory[market].popleft()
                    if rem > 1e-9:
                        s["unmatched_base_sells"] += rem
                        if base_oracle_p > 0 and proceeds > base_oracle_p:
                            gain += rem * (proceeds - base_oracle_p)
                    s["spread_pnl_usd"] += gain

            elif market == "ETH/BTC":
                quote_btc_p = quote_p if quote_p > 0 else 82000.0
                if side == "BUY":
                    s["buys"] += 1
                    inventory[market].append((recv_qty, price))
                elif side == "SELL":
                    s["sells"] += 1
                    rem = sent_qty
                    gain_btc = 0.0
                    while rem > 1e-9 and inventory[market]:
                        lot_q, lot_c = inventory[market][0]
                        match_q = min(rem, lot_q)
                        gain_btc += match_q * (price - lot_c)
                        rem -= match_q
                        if lot_q > match_q + 1e-9:
                            inventory[market][0] = (lot_q - match_q, lot_c)
                        else:
                            inventory[market].popleft()
                    s["spread_pnl_usd"] += gain_btc * quote_btc_p

            elif market in ("ETH(Arb)/ETH(Eth)", "USDC(Arb)/USDC(Eth)", "ETH_ARB/ETH_ETH", "USDC_ARB/USDC_ETH"):
                if side == "SELL":
                    s["sells"] += 1
                else:
                    s["buys"] += 1
                base_p = base_oracle_p if base_oracle_p > 0 else (2350.0 if "ETH" in market else 1.0)
                profit_tokens = recv_qty - sent_qty
                s["spread_pnl_usd"] += profit_tokens * base_p

            s["net_profit_usd"] = s["spread_pnl_usd"] + s["rebates_usd"] + s["rebalance_cost_usd"]

        for m, s in pair_stats.items():
            s["open_inventory_base"] = sum(q for q, _ in inventory[m])

        tot_spread = sum(s["spread_pnl_usd"] for s in pair_stats.values())
        tot_reb = sum(s["rebates_usd"] for s in pair_stats.values())
        tot_rebal = sum(s["rebalance_cost_usd"] for s in pair_stats.values())
        tot_vol = sum(s["volume_usd"] for s in pair_stats.values())
        tot_net = sum(s["net_profit_usd"] for s in pair_stats.values())

        res = {
            "window_days": window_days,
            "total_trades": len(records),
            "total_volume_usd": round(tot_vol, 2),
            "realized_spread_pnl_usd": round(tot_spread, 2),
            "maker_rebates_usd": round(tot_reb, 4),
            "rebalance_cost_usd": round(tot_rebal, 2),
            "net_realized_alpha_usd": round(tot_net, 2),
            "by_market": {
                k: {sk: round(sv, 4) if isinstance(sv, float) else sv for sk, sv in v.items()}
                for k, v in pair_stats.items()
            },
        }
        self._cached_trading_perf = res
        self._last_perf_calc_time = now
        self._cached_records_len = len(self.logged_trade_ids)
        self._cached_window_days = window_days
        return res

    def get_trading_performance(self, window_days: Optional[float] = 30.0) -> Dict[str, Any]:
        """Returns cached or refreshed trading performance breakdown for the given rolling window (default 30 days)."""
        return self.calculate_trading_performance(window_days=window_days, force_refresh=False)

    def get_summary(self) -> Dict[str, Any]:
        """Calculates aggregate tax figures from all logged records."""
        records = self.get_all_records()
        total_trades = len(records)
        total_volume_usd = sum(float(r.get("Volume USD", 0.0) or 0.0) for r in records)
        total_rebates_usd = sum(float(r.get("Rebate USD", 0.0) or 0.0) for r in records)
        total_profit_usd = sum(float(r.get("Net Profit USD", 0.0) or 0.0) for r in records)

        by_pair: Dict[str, int] = {}
        by_type: Dict[str, int] = {}
        for r in records:
            p = r.get("Market", "Unknown")
            by_pair[p] = by_pair.get(p, 0) + 1
            t = r.get("Trade Type", "MAKER")
            by_type[t] = by_type.get(t, 0) + 1

        oldest = records[0]["Date (UTC)"] + " " + records[0]["Time (UTC)"] if records else "-"
        newest = records[-1]["Date (UTC)"] + " " + records[-1]["Time (UTC)"] if records else "-"

        perf = self.get_trading_performance()

        return {
            "total_trades": total_trades,
            "total_volume_usd": round(total_volume_usd, 2),
            "total_rebates_usd": round(total_rebates_usd, 4),
            "total_profit_usd": round(total_profit_usd, 4),
            "realized_spread_pnl_usd": perf.get("realized_spread_pnl_usd", 0.0),
            "maker_rebates_usd": perf.get("maker_rebates_usd", 0.0),
            "rebalance_cost_usd": perf.get("rebalance_cost_usd", 0.0),
            "net_realized_alpha_usd": perf.get("net_realized_alpha_usd", 0.0),
            "by_market_performance": perf.get("by_market", {}),
            "by_pair": by_pair,
            "by_type": by_type,
            "oldest_trade_utc": oldest,
            "newest_trade_utc": newest,
            "csv_path": str(self.csv_path.resolve()),
            "jsonl_path": str(self.jsonl_path.resolve()),
        }

    def get_csv_content(self) -> str:
        """Returns the full CSV content as string for download/export."""
        if not self.csv_path.exists():
            return ",".join(CSV_COLUMNS) + "\n"
        with open(self.csv_path, "r", encoding="utf-8") as f:
            return f.read()
