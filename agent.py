"""
Hydranet Pure DLMM (Dynamic Liquidity Market Maker) Agent CLI.
Turnkey, non-custodial community market maker with automated Chaumian Ecash
Lightning liquidity management (no sovereign Lightning node required).
"""

import os
import sys
import time
import argparse
import logging
from typing import Optional

from config import BotConfig, MARKETS
from hydra_client import HydraClient
from external_oracle import ExternalOracle
from channel_manager import ChannelManager
from cashu_bridge import CashuBridge
from pnl_tracker import PnLTracker
from market_maker import MarketMaker
from web_server import DashboardServer

# Configure clean logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("Agent")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Hydranet Pure DLMM Market Maker with Chaumian Ecash Lightning Rail."
    )
    # Mode & Markets
    parser.add_argument("--pair", type=str, default=None, help="Single trading pair to focus on (default: all pairs)")
    parser.add_argument("--all-pairs", action="store_true", help="Market make across all configured DEX pairs (default)")
    parser.add_argument("--live", action="store_true", help="Execute real on-chain/off-chain orders on Hydranet DEX")
    parser.add_argument("--dry-run", action="store_true", help="Simulate order generation and lifecycle without placing live orders")
    parser.add_argument("--testnet", action="store_true", help="Use Hydranet Testnet (Bitcoin Signet 0a03cf40 + Arbitrum Sepolia 421614)")
    parser.add_argument("--start-local-mint", action="store_true", help="Start the embedded Bitcoin Signet Cashu Mint server on port 3338")
    parser.add_argument("--mint-url", type=str, default=None, help="Override Cashu Mint URL (e.g. https://testnut.cashu.space or http://127.0.0.1:3338)")
    parser.add_argument("--ticks", type=int, default=None, help="Execute N ticks and exit")
    parser.add_argument("--interval", type=float, default=15.0, help="Tick interval in seconds (default: 15.0)")

    # Web Dashboard
    parser.add_argument("--no-dashboard", action="store_true", help="Disable the embedded web dashboard")
    parser.add_argument("--port", type=int, default=None, help="Dashboard port (default: 8080 or DASHBOARD_PORT)")

    # Cashu Commands
    parser.add_argument("--cashu-status", action="store_true", help="Query and display Cashu Ecash vault status and exit")
    parser.add_argument("--cashu-export", action="store_true", help="Export and display complete cashuA... ecash token string and exit")
    parser.add_argument("--cashu-sweep", type=float, metavar="USD", help="Manually sweep BTC channel profits to Ecash ($ amount)")
    parser.add_argument("--cashu-refuel", type=float, metavar="USD", help="Manually melt Ecash to refuel BTC channel send capacity ($ amount)")

    return parser.parse_args()


def main():
    args = parse_args()

    # Build Configuration
    cfg = BotConfig()
    if args.live:
        cfg.dry_run = False
    elif args.dry_run:
        cfg.dry_run = True

    if args.all_pairs:
        cfg.all_pairs = True
    elif args.pair:
        cfg.active_pair = args.pair
        cfg.all_pairs = False

    if args.testnet or args.start_local_mint:
        cfg.testnet = True
        if not args.mint_url and "minibits.cash" in cfg.cashu_mint_url:
            cfg.cashu_mint_url = "http://127.0.0.1:3338" if args.start_local_mint else "http://136.113.240.168:3338"

    if args.start_local_mint:
        cfg.start_local_signet_mint = True

    if args.mint_url:
        cfg.cashu_mint_url = args.mint_url

    if args.port is not None:
        cfg.dashboard_port = args.port
    cfg.min_order_interval_secs = args.interval

    # Initialize Core Subsystems
    client = HydraClient(base_url=cfg.node_url)
    oracle = ExternalOracle(auto_start=True)
    channel_mgr = ChannelManager(client=client, bot_config=cfg)
    cashu_bridge = CashuBridge(hydra_client=client, config=cfg)
    pnl_tracker = PnLTracker(client=client, bot_config=cfg)

    # 1. Handle Cashu CLI-Only Flags
    if args.cashu_status:
        status = cashu_bridge.get_status()
        vault = status.get("vault", {})
        cfg_status = status.get("config", {})
        bal_sats = vault.get("balance_sats", 0)
        btc_price = oracle.get_price("BTC/USDC") or 65000.0
        bal_usd = (bal_sats / 100_000_000.0) * btc_price
        print("\n" + "=" * 55)
        print("  Hydranet Chaumian Ecash Vault Status")
        print("=" * 55)
        print(f"  Mint URL       : {status.get('mint_url')}")
        print(f"  Vault Balance  : {bal_sats:,} sats (~${bal_usd:.2f} USD)")
        print(f"  Stored Proofs  : {vault.get('proof_count', 0)}")
        print(f"  Auto-Sweep     : {'Enabled' if cfg_status.get('auto_sweep', True) else 'Disabled'}")
        print(f"  Auto-Refuel    : {'Enabled' if cfg_status.get('auto_refuel', True) else 'Disabled'}")
        print("=" * 55 + "\n")
        return

    if args.cashu_export:
        bal_sats = cashu_bridge.get_wallet_balance_sats()
        if bal_sats <= 0:
            print("\n[CASHU] Vault is empty (0 sats). Nothing to export.\n")
            return
        token = cashu_bridge.export_ecash_token()
        print("\n" + "=" * 65)
        print(f"  Cashu Ecash Token Export ({bal_sats:,} sats)")
        print("=" * 65)
        print(f"\n{token}\n")
        print("Copy the token string above and paste it into Minibits, eCash App,")
        print("or any Cashu wallet to withdraw your sats instantly.")
        print("=" * 65 + "\n")
        return

    if args.cashu_sweep is not None:
        btc_price = oracle.get_price("BTC/USDC") or 65000.0
        print(f"\n[CASHU] Executing manual sweep of ${args.cashu_sweep:.2f} USD from BTC channel...")
        res = cashu_bridge.execute_sweep(
            amount_usd=args.cashu_sweep,
            oracle_btc_price=btc_price,
            manual=True,
        )
        print(f"Result: {res}\n")
        return

    if args.cashu_refuel is not None:
        btc_price = oracle.get_price("BTC/USDC") or 65000.0
        print(f"\n[CASHU] Executing manual refuel of ${args.cashu_refuel:.2f} USD to BTC channel...")
        res = cashu_bridge.execute_refuel(
            amount_usd=args.cashu_refuel,
            oracle_btc_price=btc_price,
            manual=True,
        )
        print(f"Result: {res}\n")
        return

    # 2. Initialize Market Maker Engine
    mm = MarketMaker(
        config=cfg,
        hydra_client=client,
        external_oracle=oracle,
        channel_manager=channel_mgr,
        cashu_bridge=cashu_bridge,
        pnl_tracker=pnl_tracker,
    )

    # 3. Launch Dashboard Web Server
    dashboard_server = None
    if not args.no_dashboard:
        dashboard_server = DashboardServer(market_maker=mm, port=cfg.dashboard_port)
        dashboard_server.start()

    # 4. Run Main Loop
    try:
        mm.run(max_ticks=args.ticks, interval_secs=cfg.min_order_interval_secs)
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting...")
    finally:
        mm.stop()
        if dashboard_server:
            dashboard_server.stop()
        oracle.stop()


if __name__ == "__main__":
    main()
