"""
Unit tests for Hydranet Pure DLMM Market Maker Engine.
Tests Gaussian Bell Curve concentration, automatic 50/50 Avellaneda-Stoikov
reservation price shading, Maker Rebate passive income accounting, and
Cashu Mint/Melt Lightning channel liquidity recycling.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from config import BotConfig, MARKETS
from market_maker import MarketMaker
from cashu_bridge import CashuBridge
from hydra_client import HydraClient


class TestMarketMaker(unittest.TestCase):

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.config = BotConfig(
            dry_run=True,
            active_pair="BTC/USDC",
            target_base_ratio=0.50,
            base_half_spread_pct=0.008,
            rungs_per_side=15,
            bell_curve_sigma=1.2,
            cashu_storage_path=os.path.join(self._tmp_dir.name, "test_wallet.json"),
        )
        client = HydraClient(base_url=self.config.node_url)
        bridge = CashuBridge(
            client,
            config=self.config,
            wallet_path=os.path.join(self._tmp_dir.name, "test_wallet.json"),
            history_path=os.path.join(self._tmp_dir.name, "test_history.json"),
        )
        self.mm = MarketMaker(config=self.config, hydra_client=client, cashu_bridge=bridge)
        self.mm.oracle.get_price = MagicMock(return_value=65000.0)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_bell_curve_weights_properties(self):
        """Test that bell curve weights normalize to 1.0 and taper smoothly."""
        weights = self.mm.compute_bell_curve_weights(15, 1.2)
        self.assertEqual(len(weights), 15)
        self.assertAlmostEqual(sum(weights), 1.0, places=5)
        # Rung 0 (near mid) should have higher weight than Rung 14 (deep)
        self.assertGreater(weights[0], weights[-1])
        # Weights should be monotonically decreasing
        for i in range(len(weights) - 1):
            self.assertGreaterEqual(weights[i], weights[i + 1])

    def test_generate_orders_balanced_with_maker_rebates(self):
        """Test generating 15 bids and 15 asks when inventory is balanced at 50/50."""
        mock_audit = MagicMock()
        mock_audit.base_free_sendable = 0.05
        mock_audit.base_sendable = 0.05
        mock_audit.base_receivable = 0.05
        mock_audit.quote_free_sendable = 3250.0
        mock_audit.quote_sendable = 3250.0
        mock_audit.quote_receivable = 3250.0
        self.mm.channel_mgr.audit_all_channels = MagicMock(return_value={"BTC/USDC": mock_audit})

        bids, asks = self.mm.generate_orders_for_market("BTC/USDC")
        self.assertEqual(len(bids), 15)
        self.assertEqual(len(asks), 15)

        # Bids should descend in price away from mid and include estimated maker rebate
        for i in range(len(bids) - 1):
            self.assertGreater(bids[i]["price"], bids[i + 1]["price"])
            self.assertEqual(bids[i]["side"], "BUY")
            self.assertGreater(bids[i]["est_rebate_usd"], 0.0)

        # Asks should ascend in price away from mid and include estimated maker rebate
        for i in range(len(asks) - 1):
            self.assertLess(asks[i]["price"], asks[i + 1]["price"])
            self.assertEqual(asks[i]["side"], "SELL")
            self.assertGreater(asks[i]["est_rebate_usd"], 0.0)

        # Top bid must be strictly below spot price, top ask strictly above
        self.assertLess(bids[0]["price"], 65000.0)
        self.assertGreater(asks[0]["price"], 65000.0)

    def test_auto_inventory_skew_reservation_shift(self):
        """Test that inventory imbalance automatically shifts reservation price toward 50/50 equilibrium."""
        # 1. Underweight BTC (20% BTC / 80% USDC) -> Reservation price shifts UP to attract BTC sellers
        audit_underweight = MagicMock()
        audit_underweight.base_free_sendable = 0.015
        audit_underweight.base_sendable = 0.015
        audit_underweight.base_receivable = 0.085
        audit_underweight.quote_free_sendable = 4000.0
        audit_underweight.quote_sendable = 4000.0
        audit_underweight.quote_receivable = 4000.0
        self.mm.channel_mgr.audit_all_channels = MagicMock(return_value={"BTC/USDC": audit_underweight})

        self.mm.generate_orders_for_market("BTC/USDC")
        res_underweight = self.mm.market_audits["BTC/USDC"].reservation_price

        # 2. Overweight BTC (80% BTC / 20% USDC) -> Reservation price shifts DOWN to attract BTC buyers
        audit_overweight = MagicMock()
        audit_overweight.base_free_sendable = 0.085
        audit_overweight.base_sendable = 0.085
        audit_overweight.base_receivable = 0.015
        audit_overweight.quote_free_sendable = 1200.0
        audit_overweight.quote_sendable = 1200.0
        audit_overweight.quote_receivable = 4000.0
        self.mm.channel_mgr.audit_all_channels = MagicMock(return_value={"BTC/USDC": audit_overweight})

        self.mm.generate_orders_for_market("BTC/USDC")
        res_overweight = self.mm.market_audits["BTC/USDC"].reservation_price

        self.assertGreater(res_underweight, res_overweight)
        self.assertEqual(self.mm.market_audits["BTC/USDC"].target_base_ratio, 0.50)

    def test_simulate_passive_income_and_cashu_loop(self):
        """Test that simulated maker fills accrue maker rebates and trigger Cashu sweep/refuel."""
        res_ping = self.mm.simulate_scenario("ping_pong", "BTC/USDC")
        self.assertEqual(res_ping["status"], "SUCCESS")
        self.assertGreater(res_ping["earned_rebates_usd"], 0.0)
        self.assertGreater(self.mm.maker_rebates_usd, 0.0)

        # Simulate Bid Wave (takers sell BTC into our Bids -> depletes LN Inbound -> triggers Cashu Auto-Sweep)
        res_bid_wave = self.mm.simulate_scenario("bid_wave", "BTC/USDC")
        self.assertEqual(res_bid_wave["status"], "SUCCESS")
        self.assertIsNotNone(res_bid_wave["cashu_action"])
        self.assertEqual(res_bid_wave["cashu_action"]["type"], "SWEEP")

    def test_pure_maker_no_taker_trades(self):
        """Ensure market maker class does not expose or execute triangular arbitrage or taker rebalancing."""
        self.assertFalse(hasattr(self.mm, "triangular_arbitrage"))
        self.assertFalse(hasattr(self.mm, "execute_taker_trade"))
        self.assertFalse(hasattr(self.mm, "_execute_active_rebalance"))

    def test_cross_chain_peg_pairs_stableswap_curve(self):
        """Test that ETH(Arb)/ETH(Eth) and USDC(Arb)/USDC(Eth) use 1.00000 peg and 4-25 bps Stableswap curve."""
        mock_audit = MagicMock()
        mock_audit.base_free_sendable = 1500.0
        mock_audit.base_sendable = 1500.0
        mock_audit.base_receivable = 1500.0
        mock_audit.quote_free_sendable = 1500.0
        mock_audit.quote_sendable = 1500.0
        mock_audit.quote_receivable = 1500.0
        self.mm.channel_mgr.audit_all_channels = MagicMock(return_value={"USDC(Arb)/USDC(Eth)": mock_audit})

        bids, asks = self.mm.generate_orders_for_market("USDC(Arb)/USDC(Eth)")
        audit = self.mm.market_audits["USDC(Arb)/USDC(Eth)"]
        self.assertEqual(audit.mid_price, 1.0)
        self.assertEqual(audit.curve_concentration, "STABLESWAP PEG")
        self.assertAlmostEqual(audit.maker_rebate_rate, 0.00025, places=6)
        self.assertEqual(len(bids), 15)
        self.assertEqual(len(asks), 15)
        # Balanced 50/50 Rung #0 sits at 4 bps (0.99960 Bid / 1.00040 Ask) and Rung #14 at 25 bps (0.99750 / 1.00250)
        self.assertAlmostEqual(bids[0]["price"], 0.99960, places=5)
        self.assertAlmostEqual(asks[0]["price"], 1.00040, places=5)
        self.assertAlmostEqual(bids[-1]["price"], 0.99750, places=5)
        self.assertAlmostEqual(asks[-1]["price"], 1.00250, places=5)


if __name__ == "__main__":
    unittest.main()
