"""
Unit tests for Hydranet Pure DLMM Dashboard Web Server.
Tests REST API endpoints using standard urllib and in-memory mock MarketMaker.
"""

import os
import json
import time
import tempfile
import urllib.request
import unittest
from unittest.mock import MagicMock

from config import BotConfig
from market_maker import MarketMaker
from cashu_bridge import CashuBridge
from hydra_client import HydraClient
from web_server import DashboardServer


class TestWebServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp_dir = tempfile.TemporaryDirectory()
        cls.config = BotConfig(
            dry_run=True,
            active_pair="BTC/USDC",
            dashboard_port=8999,
            cashu_storage_path=os.path.join(cls._tmp_dir.name, "test_wallet.json"),
        )
        client = HydraClient(base_url=cls.config.node_url)
        bridge = CashuBridge(
            client,
            config=cls.config,
            wallet_path=os.path.join(cls._tmp_dir.name, "test_wallet.json"),
            history_path=os.path.join(cls._tmp_dir.name, "test_history.json"),
        )
        cls.mm = MarketMaker(config=cls.config, hydra_client=client, cashu_bridge=bridge)
        cls.mm.oracle.get_price = MagicMock(return_value=65000.0)

        # Mock resting orders
        cls.mm.resting_orders["BTC/USDC"] = [
            {"pair": "BTC/USDC", "side": "BUY", "price": 64500.0, "amount": 0.005, "rung": 0, "value_usd": 322.5, "est_rebate_usd": 0.3225},
            {"pair": "BTC/USDC", "side": "SELL", "price": 65500.0, "amount": 0.005, "rung": 0, "value_usd": 327.5, "est_rebate_usd": 0.3275},
        ]

        cls.server = DashboardServer(market_maker=cls.mm, port=8999)
        cls.server.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._tmp_dir.cleanup()

    def _get(self, path: str):
        url = f"http://127.0.0.1:8999{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data), resp.status

    def _post(self, path: str, payload: dict):
        url = f"http://127.0.0.1:8999{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data), resp.status

    def test_get_summary(self):
        data, status = self._get("/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "DRY-RUN")
        self.assertIn("prices", data)
        self.assertIn("BTC", data["prices"])
        self.assertIn("cashu", data)
        self.assertIn("passive_income", data)
        self.assertIn("bell_curve_weights", data["dlmm"])

    def test_get_orders(self):
        data, status = self._get("/api/orders")
        self.assertEqual(status, 200)
        self.assertIn("orders", data)
        self.assertIn("BTC/USDC", data["orders"])
        self.assertGreaterEqual(len(data["orders"]["BTC/USDC"]), 2)

    def test_get_channels(self):
        data, status = self._get("/api/channels")
        self.assertEqual(status, 200)
        self.assertIn("channels", data)
        self.assertGreaterEqual(len(data["channels"]), 1)

    def test_get_cashu_and_verify_mint(self):
        data, status = self._get("/api/cashu")
        self.assertEqual(status, 200)
        self.assertIn("balance_sats", data)
        self.assertIn("mint_url", data)

        verify_res, v_status = self._post("/api/cashu/test-mint", {"test_sats": 30000})
        self.assertEqual(v_status, 200)
        self.assertTrue(verify_res["hypothesis_verified"])
        self.assertTrue(verify_res["bdhke_verified"])

    def test_simulate_and_curve_flow(self):
        # 1. Update Gaussian curve sigma
        res, status = self._post("/api/curve", {"sigma": 0.85})
        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["bell_curve_sigma"], 0.85)

        # 2. Simulate maker fills
        sim_res, sim_status = self._post("/api/simulate", {"scenario": "ping_pong", "pair": "BTC/USDC"})
        self.assertEqual(sim_status, 200)
        self.assertEqual(sim_res["status"], "SUCCESS")
        self.assertGreater(sim_res["earned_rebates_usd"], 0.0)

    def test_control_pause_resume(self):
        # Pause
        res, status = self._post("/api/control", {"action": "pause"})
        self.assertEqual(status, 200)
        self.assertTrue(res["is_paused"])
        self.assertTrue(self.mm.is_paused)

        # Resume
        res, status = self._post("/api/control", {"action": "resume"})
        self.assertEqual(status, 200)
        self.assertFalse(res["is_paused"])
        self.assertFalse(self.mm.is_paused)

    def test_network_rail_and_local_signet_mint_switch(self):
        # Switch to Hydranet Testnet (Bitcoin Signet 0a03cf40 / Arbitrum Sepolia 421614) + Start Local Signet Mint
        res, status = self._post("/api/network", {"testnet": True, "start_local_mint": True})
        self.assertEqual(status, 200)
        self.assertTrue(res["testnet"])
        self.assertEqual(res["networks"]["Bitcoin"]["id"], "0a03cf40")
        self.assertEqual(res["networks"]["Arbitrum"]["id"], "421614")
        self.assertTrue(res["local_signet_mint"]["running"])

        # Verify full live Mint + Melt round-trip against the running local Signet Cashu Mint
        v_res, v_status = self._post("/api/cashu/test-mint", {"test_sats": 30000})
        self.assertEqual(v_status, 200)
        self.assertTrue(v_res["hypothesis_verified"])
        self.assertTrue(v_res["live_mint_melt_roundtrip"])

        # Stop local mint and switch back to Mainnet
        res2, status2 = self._post("/api/network", {"testnet": False, "start_local_mint": False})
        self.assertEqual(status2, 200)
        self.assertFalse(res2["testnet"])

    def test_wallet_overview_and_bridge_transfer(self):
        data, status = self._get("/api/wallet")
        self.assertEqual(status, 200)
        self.assertIn("networks", data)
        self.assertIn("Bitcoin", data["networks"])
        self.assertIn("Ethereum", data["networks"])
        self.assertIn("Arbitrum", data["networks"])
        self.assertIn("assets", data)

        # Mock fee estimation for dry-run bridge transfer when local node is offline
        self.mm.channel_mgr.ensure_peer_connected = MagicMock(return_value=True)
        self.mm.channel_mgr.estimate_open_channel_fee = MagicMock(return_value={"fee": {"value": "0.0001"}})
        self.mm.channel_mgr.client.estimate_deposit_channel_fee = MagicMock(return_value={"fee": {"value": "0.0001"}})
        self.mm.channel_mgr.client.estimate_withdraw_channel_fee = MagicMock(return_value={"fee": {"value": "0.0001"}})
        self.mm.channel_mgr.client.get_wallet_balances = MagicMock(
            return_value={
                "0x0000000000000000000000000000000000000000": {
                    "onchain": {"confirmed": {"value": "0.25"}}
                }
            }
        )

        # Test On-Chain -> Off-Chain transfer
        dep_res, dep_status = self._post(
            "/api/wallet/transfer",
            {
                "direction": "onchain_to_offchain",
                "network": "Ethereum",
                "asset": "ETH",
                "amount": 0.01,
                "action_subtype": "open",
            },
        )
        self.assertEqual(dep_status, 200)
        self.assertEqual(dep_res["status"], "SUCCESS")

        # Test Off-Chain -> On-Chain transfer
        wd_res, wd_status = self._post(
            "/api/wallet/transfer",
            {
                "direction": "offchain_to_onchain",
                "network": "Ethereum",
                "asset": "ETH",
                "amount": 0.005,
                "channel_id": "0x7ea782a16e5984f28cabc75eb20a033bfa6472ee5f3445b7f36cae508a83f9ad",
            },
        )
        self.assertEqual(wd_status, 200)
        self.assertEqual(wd_res["status"], "SUCCESS")
        self.assertEqual(wd_res["action"], "WITHDRAW_CHANNEL")

    def test_channel_advisor_and_automation_log(self):
        # Simulate constrained USDC receive capacity (1.10 USDC) on Arbitrum
        self.mm.sim_assets[("Arbitrum", "USDC")] = {
            "free_local": 250.0,
            "free_remote": 1.10,
            "total_capacity": 251.10,
        }
        self.mm.generate_orders_for_market("BTC/USDC")

        wallet_data, status = self._get("/api/wallet")
        self.assertEqual(status, 200)
        self.assertIn("channel_management_log", wallet_data)
        self.assertIn("automation", wallet_data)

        arb_assets = wallet_data["networks"]["Arbitrum"]["assets"]
        usdc_entry = next(a for a in arb_assets if a["symbol"] == "USDC")
        self.assertTrue(usdc_entry["recv_constrained"])
        self.assertTrue(len(usdc_entry["advisor_messages"]) > 0)
        self.assertIn("Advise renting", usdc_entry["advisor_text"])

        # Toggle automation switches via /api/channel-management/toggle
        tog_res, tog_status = self._post(
            "/api/channel-management/toggle",
            {
                "auto_cashu": True,
                "auto_btc_channels": True,
                "auto_lithium_channels": False,
            },
        )
        self.assertEqual(tog_status, 200)
        self.assertTrue(tog_res["automation"]["auto_cashu"])
        self.assertTrue(tog_res["automation"]["auto_btc_channels"])
        self.assertFalse(tog_res["automation"]["auto_lithium_channels"])


if __name__ == "__main__":
    unittest.main()
