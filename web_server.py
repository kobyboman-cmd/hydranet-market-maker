"""
Embedded Web Server for Hydranet DLMM Market Maker Dashboard.
Provides a zero-dependency, multi-threaded HTTP REST API and serves the
modern glassmorphism frontend on http://localhost:8080 (or configured DASHBOARD_PORT).
"""

import os
import sys
import json
import time
import urllib.parse
import threading
import logging
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from typing import Optional, Dict, Any, List

from config import MARKETS, TESTNET_MARKETS, BotConfig, NETWORK_RAILS, CASHU_MINT_PRESETS
from market_maker import MarketMaker

logger = logging.getLogger("WebServer")


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that suppresses noisy socket abort/reset tracebacks on client disconnect."""
    def handle_error(self, request, client_address):
        exc_type, exc_val, exc_tb = sys.exc_info()
        if exc_type and issubclass(exc_type, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError)):
            return
        super().handle_error(request, client_address)


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """HTTP Request Handler providing REST endpoints and static UI assets."""

    server_version = "HydranetDLMM/2.1"

    def __init__(self, *args, **kwargs):
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
        super().__init__(*args, directory=web_dir, **kwargs)

    @property
    def market_maker(self) -> Optional[MarketMaker]:
        return getattr(self.server, "market_maker", None)

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except Exception as e:
            logger.warning(f"Error parsing request body: {e}")
            return {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/summary":
            self._handle_get_summary()
        elif path == "/api/orders":
            self._handle_get_orders()
        elif path == "/api/channels":
            self._handle_get_channels()
        elif path == "/api/wallet":
            self._handle_get_wallet()
        elif path == "/api/cashu":
            self._handle_get_cashu()
        else:
            # Fallback to serving static frontend files
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/cashu/export":
            self._handle_post_cashu_export()
        elif path == "/api/cashu/initialize":
            self._handle_post_cashu_initialize()
        elif path == "/api/cashu/sweep":
            self._handle_post_cashu_sweep()
        elif path == "/api/cashu/refuel":
            self._handle_post_cashu_refuel()
        elif path == "/api/cashu/toggle" or path == "/api/channel-management/toggle":
            self._handle_post_cashu_toggle()
        elif path == "/api/cashu/test-mint":
            self._handle_post_cashu_test_mint()
        elif path == "/api/simulate":
            self._handle_post_simulate()
        elif path == "/api/curve":
            self._handle_post_curve()
        elif path == "/api/network":
            self._handle_post_network()
        elif path == "/api/wallet/transfer":
            self._handle_post_wallet_transfer()
        elif path == "/api/pairs/toggle":
            self._handle_post_pairs_toggle()
        elif path == "/api/control":
            self._handle_post_control()
        else:
            self._send_json({"error": "Endpoint not found"}, status=404)

    # ==================== GET HANDLERS ====================

    def _handle_get_summary(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"error": "Market maker not initialized"}, status=503)
            return

        cfg = mm.config
        prices = {
            "BTC": mm.oracle.get_price("BTC/USDC") or 82000.0,
            "ETH": mm.oracle.get_price("ETH/USDC") or 2450.0,
            "HDN": mm.oracle.get_price("HDN/USDC") or 0.026,
            "USDC": 1.0,
        }

        # Cashu status
        cashu_sats = 0
        cashu_usd = 0.0
        cashu_metrics = {}
        if mm.cashu_bridge:
            st = mm.cashu_bridge.get_status(oracle_btc_price=prices["BTC"])
            cashu_sats = st.get("vault", {}).get("balance_sats", 0)
            cashu_usd = st.get("vault", {}).get("balance_usd", 0.0)
            cashu_metrics = st.get("metrics", {})

        is_testnet = getattr(cfg, "testnet", False)
        active_markets = TESTNET_MARKETS if is_testnet else MARKETS

        # Ensure enabled markets have initial curve telemetry even before their first background order sync completes
        for p in list(mm.target_pairs):
            audit = mm.market_audits.get(p)
            if not audit or audit.mid_price <= 0 or not getattr(audit, "ideal_bids", None):
                try:
                    mm.generate_orders_for_market(p)
                except Exception:
                    pass

        # Market Audits
        markets_summary = {}
        for p, audit in mm.market_audits.items():
            m_cfg = active_markets.get(p)
            markets_summary[p] = {
                "pair": p,
                "is_peg_pair": bool(getattr(m_cfg, "is_peg_pair", False)),
                "base_symbol": m_cfg.base.symbol if m_cfg else p.split("/")[0],
                "quote_symbol": m_cfg.quote.symbol if m_cfg else p.split("/")[1],
                "base_network": m_cfg.base.network_name if m_cfg else "Arbitrum",
                "quote_network": m_cfg.quote.network_name if m_cfg else "Arbitrum",
                "mid_price": audit.mid_price,
                "reservation_price": audit.reservation_price,
                "center_price_bias_pct": audit.center_price_bias_pct,
                "half_spread_pct": audit.half_spread_pct,
                "bid_spread_multiplier": audit.bid_spread_multiplier,
                "ask_spread_multiplier": audit.ask_spread_multiplier,
                "bid_size_multiplier": audit.bid_size_multiplier,
                "ask_size_multiplier": audit.ask_size_multiplier,
                "curve_concentration": audit.curve_concentration,
                "dynamic_sigma": getattr(audit, "dynamic_sigma", cfg.bell_curve_sigma),
                "curve_profile_reason": getattr(audit, "curve_profile_reason", ""),
                "risk_level": audit.risk_level,
                "maker_rebate_rate": audit.maker_rebate_rate,
                "base_ratio": audit.base_ratio,
                "target_base_ratio": audit.target_base_ratio,
                "inventory_skew_pct": audit.inventory_skew_pct,
                "base_sendable": audit.base_sendable,
                "base_receivable": audit.base_receivable,
                "quote_sendable": audit.quote_sendable,
                "quote_receivable": audit.quote_receivable,
                "base_usd": audit.base_usd,
                "quote_usd": audit.quote_usd,
                "total_usd": audit.total_usd,
                "active_bids_count": audit.active_bids_count,
                "active_asks_count": audit.active_asks_count,
                "active_bids_value_usd": audit.active_bids_value_usd,
                "active_asks_value_usd": audit.active_asks_value_usd,
                "ideal_bids": getattr(audit, "ideal_bids", []),
                "ideal_asks": getattr(audit, "ideal_asks", []),
            }

        weights = mm.compute_bell_curve_weights(cfg.rungs_per_side, cfg.bell_curve_sigma)

        rail_key = "testnet" if is_testnet else "mainnet"
        local_mint_status = (
            mm.cashu_bridge.local_signet_mint.get_status()
            if mm.cashu_bridge and hasattr(mm.cashu_bridge, "local_signet_mint")
            else {"running": False, "url": "http://127.0.0.1:3338"}
        )

        # Query live on-chain balances and comprehensive wallet/channel overview from hydra-app
        wallet_overview = {}
        try:
            wallet_overview = mm.get_wallet_and_channels_overview()
        except Exception as e:
            logger.debug(f"Wallet overview fallback: {e}")

        onchain_balances = {}
        try:
            for net_name, net_data in wallet_overview.get("networks", {}).items():
                onchain_balances[net_name] = {
                    a["symbol"]: float(a.get("onchain_confirmed", 0.0) or 0.0)
                    for a in net_data.get("assets", [])
                }
        except Exception:
            pass

        data = {
            "mode": "DRY-RUN" if cfg.dry_run else "LIVE",
            "testnet": is_testnet,
            "rail": rail_key,
            "networks": NETWORK_RAILS[rail_key],
            "onchain_balances": onchain_balances,
            "wallet_overview": wallet_overview,
            "node_awaiting_whitelist": getattr(mm.client, "awaiting_whitelist", False),
            "node_identity_pubkey": getattr(mm.client, "identity_pubkey_hex", None) or mm.client.get_identity_public_key(),
            "is_running": mm.is_running,
            "is_paused": mm.is_paused,
            "active_pairs": list(mm.target_pairs),
            "all_pairs": list(getattr(mm, "all_known_pairs", ["BTC/USDC", "ETH/USDC", "ETH/BTC", "HDN/USDC"])),
            "prices": prices,
            "passive_income": mm.get_passive_income_summary(),
            "cashu": {
                "initialized": bool(getattr(mm.cashu_bridge, "initialized", False)) if mm.cashu_bridge else False,
                "enabled": getattr(cfg, "cashu_enabled", True) and bool(getattr(mm.cashu_bridge, "initialized", False)),
                "balance_sats": cashu_sats,
                "balance_usd": round(cashu_usd, 2),
                "mint_url": getattr(cfg, "cashu_mint_url", ""),
                "mint_presets": CASHU_MINT_PRESETS,
                "local_signet_mint": local_mint_status,
                "metrics": cashu_metrics,
            },
            "markets": markets_summary,
            "dlmm": {
                "rungs_per_side": cfg.rungs_per_side,
                "bell_curve_sigma": cfg.bell_curve_sigma,
                "base_half_spread_pct": cfg.base_half_spread_pct,
                "target_base_ratio": cfg.target_base_ratio,
                "bell_curve_weights": [round(w, 4) for w in weights],
            },
            "timestamp": time.time(),
        }
        self._send_json(data)

    def _handle_get_orders(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"orders": {}})
            return

        if not mm.is_running:
            with mm._lock:
                for p in mm._active_markets().keys():
                    if not mm.resting_orders.get(p):
                        try:
                            mm.sync_orders_for_market(p)
                        except Exception:
                            pass
        orders_copy = {p: list(ords) for p, ords in list(mm.resting_orders.items())}
        self._send_json({"orders": orders_copy})

    def _handle_get_channels(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"channels": []})
            return

        channels_data = mm.get_channels_snapshot()
        self._send_json({"channels": channels_data})

    def _handle_get_wallet(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"error": "Market maker not initialized"}, status=503)
            return

        self._send_json(mm.get_wallet_and_channels_overview())

    def _handle_get_cashu(self):
        mm = self.market_maker
        if not mm or not mm.cashu_bridge:
            self._send_json({"enabled": False})
            return

        btc_price = mm.oracle.get_price("BTC/USDC") or 82000.0
        bridge = mm.cashu_bridge
        status = bridge.get_status(oracle_btc_price=btc_price)
        vault = status.get("vault", {})
        cfg_status = status.get("config", {})
        resp = {
            "initialized": status.get("initialized", False),
            "enabled": status.get("enabled", True),
            "mint_url": status.get("mint_url", ""),
            "balance_sats": vault.get("balance_sats", 0),
            "balance_usd": vault.get("balance_usd", 0.0),
            "proofs_count": vault.get("proof_count", 0),
            "auto_sweep_enabled": cfg_status.get("auto_sweep", True),
            "auto_refuel_enabled": cfg_status.get("auto_refuel", True),
            "metrics": status.get("metrics", {}),
            "recent_events": status.get("recent_events", []),
            "raw": status,
        }
        self._send_json(resp)

    # ==================== POST HANDLERS ====================

    def _handle_post_cashu_initialize(self):
        mm = self.market_maker
        if not mm or not mm.cashu_bridge:
            self._send_json({"error": "Cashu bridge not available"}, status=400)
            return

        res = mm.cashu_bridge.initialize_vault()
        mm.config.auto_cashu = True
        mm.config.cashu_auto_sweep = True
        mm.config.cashu_auto_refuel = True
        mm.log_channel_action(
            mode="MANUAL",
            network="Bitcoin",
            asset="BTC",
            action_type="INIT_CASHU_VAULT",
            amount=float(res.get("balance_sats", 0)),
            status="COMPLETED",
            message=f"Initialized Cashu Ecash Vault connected to community mint ({res.get('mint_url', '')}).",
            unit="sats",
        )
        self._send_json(res)

    def _handle_post_cashu_export(self):
        mm = self.market_maker
        if not mm or not mm.cashu_bridge:
            self._send_json({"error": "Cashu bridge not active"}, status=400)
            return

        try:
            token = mm.cashu_bridge.export_ecash_token()
            balance = mm.cashu_bridge.get_wallet_balance_sats()
            self._send_json({
                "status": "SUCCESS",
                "token": token,
                "amount_sats": balance,
                "message": f"Successfully exported {balance:,} sats token.",
            })
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_post_cashu_sweep(self):
        mm = self.market_maker
        if not mm or not mm.cashu_bridge:
            self._send_json({"error": "Cashu bridge not active"}, status=400)
            return

        body = self._read_json_body()
        amount_usd = float(body.get("amount_usd", 25.0))
        amount_sats = body.get("amount_sats")
        btc_price = mm.oracle.get_price("BTC/USDC") or 82000.0

        amount_btc = (float(amount_sats) / 100_000_000.0) if amount_sats else None

        res = mm.cashu_bridge.execute_sweep(
            amount_usd=amount_usd,
            oracle_btc_price=btc_price,
            manual=True,
            amount_btc=amount_btc,
        )
        sats = int(res.get("amount_sats", 0))
        if res.get("status") == "COMPLETED":
            mm.apply_cashu_channel_delta("SWEEP", res.get("amount_btc", 0.0))
            for p in mm._active_markets().keys():
                mm.last_order_refresh_time[p] = 0.0
                mm.sync_orders_for_market(p)
            res["status"] = "SUCCESS"
            mm.log_channel_action(
                mode="MANUAL",
                network="Bitcoin",
                asset="BTC",
                action_type="CASHU_MINT",
                amount=float(sats),
                status="COMPLETED",
                message=f"Minted {sats:,} sats (~${res.get('amount_usd', amount_usd):.2f}) from BTC Lightning channel into Cashu Vault.",
                unit="sats",
            )
        else:
            mm.log_channel_action(
                mode="MANUAL",
                network="Bitcoin",
                asset="BTC",
                action_type="CASHU_MINT",
                amount=float(sats),
                status="FAILED",
                message=f"Manual Cashu Mint failed: {res.get('error', 'unknown')}",
                unit="sats",
            )
        self._send_json(res)

    def _handle_post_cashu_refuel(self):
        mm = self.market_maker
        if not mm or not mm.cashu_bridge:
            self._send_json({"error": "Cashu bridge not active"}, status=400)
            return

        body = self._read_json_body()
        amount_usd = float(body.get("amount_usd", 25.0))
        amount_sats = body.get("amount_sats")
        btc_price = mm.oracle.get_price("BTC/USDC") or 82000.0

        amount_btc = (float(amount_sats) / 100_000_000.0) if amount_sats else None

        res = mm.cashu_bridge.execute_refuel(
            amount_usd=amount_usd,
            oracle_btc_price=btc_price,
            manual=True,
            amount_btc=amount_btc,
        )
        sats = int(res.get("amount_sats", 0))
        if res.get("status") == "COMPLETED":
            mm.apply_cashu_channel_delta("REFUEL", res.get("amount_btc", 0.0))
            for p in mm._active_markets().keys():
                mm.last_order_refresh_time[p] = 0.0
                mm.sync_orders_for_market(p)
            res["status"] = "SUCCESS"
            mm.log_channel_action(
                mode="MANUAL",
                network="Bitcoin",
                asset="BTC",
                action_type="CASHU_MELT",
                amount=float(sats),
                status="COMPLETED",
                message=f"Melted {sats:,} sats (~${res.get('amount_usd', amount_usd):.2f}) from Cashu Vault into BTC Lightning channel.",
                unit="sats",
            )
        else:
            mm.log_channel_action(
                mode="MANUAL",
                network="Bitcoin",
                asset="BTC",
                action_type="CASHU_MELT",
                amount=float(sats),
                status="FAILED",
                message=f"Manual Cashu Melt failed: {res.get('error', 'unknown')}",
                unit="sats",
            )
        self._send_json(res)

    def _handle_post_cashu_toggle(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"error": "Market maker not initialized"}, status=400)
            return

        body = self._read_json_body()
        if "auto_sweep" in body:
            mm.config.cashu_auto_sweep = bool(body["auto_sweep"])
            mm.config.auto_cashu = bool(body["auto_sweep"]) and bool(getattr(mm.config, "cashu_auto_refuel", True))
        if "auto_refuel" in body:
            mm.config.cashu_auto_refuel = bool(body["auto_refuel"])
            mm.config.auto_cashu = bool(getattr(mm.config, "cashu_auto_sweep", True)) and bool(body["auto_refuel"])

        auto_cashu = body.get("auto_cashu")
        auto_btc = body.get("auto_btc_channels")
        auto_lithium = body.get("auto_lithium_channels")

        res = mm.set_channel_automation(
            auto_cashu=auto_cashu,
            auto_btc_channels=auto_btc,
            auto_lithium_channels=auto_lithium,
        )

        # If any automation toggle was just enabled, evaluate in background so HTTP response is never blocked
        if any(bool(body.get(k)) for k in ("auto_cashu", "auto_btc_channels", "auto_lithium_channels")):
            threading.Thread(target=mm.evaluate_channel_liquidity, daemon=True).start()

        self._send_json({
            "status": "SUCCESS",
            "cashu_auto_sweep": mm.config.cashu_auto_sweep,
            "cashu_auto_refuel": mm.config.cashu_auto_refuel,
            "automation": res["automation"],
        })

    def _handle_post_cashu_test_mint(self):
        mm = self.market_maker
        if not mm or not mm.cashu_bridge:
            self._send_json({"error": "Cashu bridge not active"}, status=400)
            return

        body = self._read_json_body()
        test_sats = int(body.get("test_sats", 30000))
        res = mm.cashu_bridge.verify_live_mint_hypothesis(test_sats=test_sats)
        self._send_json(res)

    def _handle_post_simulate(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"error": "Market maker not initialized"}, status=503)
            return

        body = self._read_json_body()
        scenario = body.get("scenario", "ping_pong")
        pair = body.get("pair", "BTC/USDC")
        res = mm.simulate_scenario(scenario=scenario, pair=pair)
        self._send_json(res)

    def _handle_post_curve(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"error": "Market maker not initialized"}, status=503)
            return

        body = self._read_json_body()
        if "sigma" in body:
            sigma = max(0.5, min(float(body["sigma"]), 2.5))
            mm.config.bell_curve_sigma = sigma
        if "half_spread_pct" in body:
            sp = max(0.002, min(float(body["half_spread_pct"]), 0.035))
            mm.config.base_half_spread_pct = sp

        with mm._lock:
            for p in list(mm.target_pairs):
                mm.last_order_refresh_time[p] = 0.0
                if mm.config.dry_run:
                    mm.sync_orders_for_market(p)
                else:
                    mm.generate_orders_for_market(p)

        self._send_json({
            "status": "SUCCESS",
            "bell_curve_sigma": mm.config.bell_curve_sigma,
            "base_half_spread_pct": mm.config.base_half_spread_pct,
        })

    def _handle_post_network(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"error": "Market maker not initialized"}, status=503)
            return

        body = self._read_json_body()
        testnet = body.get("testnet") if "testnet" in body else None
        mint_url = body.get("mint_url") if "mint_url" in body else None
        start_local_mint = body.get("start_local_mint") if "start_local_mint" in body else None

        res = mm.set_network_mode(
            testnet=testnet,
            mint_url=mint_url,
            start_local_mint=start_local_mint,
        )
        self._send_json(res)

    def _handle_post_wallet_transfer(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"error": "Market maker not initialized"}, status=503)
            return

        body = self._read_json_body()
        direction = str(body.get("direction", "onchain_to_offchain"))
        network = str(body.get("network", "Ethereum"))
        asset = str(body.get("asset", "ETH"))
        try:
            amount = float(body.get("amount", 0.0) or 0.0)
        except (ValueError, TypeError):
            amount = 0.0
        channel_id = body.get("channel_id")
        action_subtype = body.get("action_subtype")

        res = mm.execute_bridge_transfer(
            direction=direction,
            network_name=network,
            asset_symbol=asset,
            amount=amount,
            channel_id=channel_id,
            action_subtype=action_subtype,
        )
        status_code = 200 if res.get("status") == "SUCCESS" else 400
        self._send_json(res, status=status_code)

    def _handle_post_control(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"error": "Market maker not initialized"}, status=503)
            return

        body = self._read_json_body()
        action = body.get("action", "").lower()

        if action == "pause":
            mm.is_paused = True
            logger.info("Market maker paused by user.")
            self._send_json({"status": "SUCCESS", "is_paused": True})
        elif action == "resume":
            mm.is_paused = False
            logger.info("Market maker resumed by user.")
            self._send_json({"status": "SUCCESS", "is_paused": False})
        elif action == "tick":
            mm.run_tick()
            self._send_json({"status": "SUCCESS", "message": "Tick executed manually."})
        else:
            self._send_json({"error": f"Unknown action '{action}'"}, status=400)

    def _handle_post_pairs_toggle(self):
        mm = self.market_maker
        if not mm:
            self._send_json({"error": "Market maker not initialized"}, status=503)
            return

        body = self._read_json_body()
        pair = str(body.get("pair", "")).strip()
        enabled = bool(body.get("enabled", True))
        res = mm.set_pair_enabled(pair=pair, enabled=enabled)
        status_code = 200 if res.get("status") == "SUCCESS" else 400
        self._send_json(res, status=status_code)


class DashboardServer:
    """Manages the background HTTP web server thread."""

    def __init__(self, market_maker: MarketMaker, port: int = 8080):
        self.market_maker = market_maker
        self.port = port
        self.server: Optional[QuietThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self):
        """Starts the dashboard web server in a daemon thread."""
        try:
            self.server = QuietThreadingHTTPServer(("0.0.0.0", self.port), DashboardRequestHandler)
            self.server.market_maker = self.market_maker
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            print(f"\n[WEB] Pure DLMM Dashboard active at http://localhost:{self.port}/\n")
        except Exception as e:
            logger.error(f"Failed to start dashboard web server on port {self.port}: {e}")

    def stop(self):
        """Shuts down the dashboard server cleanly."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            print("[WEB] Dashboard server stopped cleanly.")
