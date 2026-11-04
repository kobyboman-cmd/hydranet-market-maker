"""
Unit tests for Cashu Client and Cashu Liquidity Bridge.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from config import BotConfig
from cashu_client import (
    CashuClient,
    split_amount_to_powers_of_2,
    point_add,
    point_mul,
    decompress_point,
    compress_point,
    hash_to_curve,
    G,
)
from cashu_bridge import CashuBridge
from cashu_signet_mint import LocalCashuSignetMint


class TestCashuCrypto(unittest.TestCase):

    def test_split_amount_to_powers_of_2(self):
        self.assertEqual(split_amount_to_powers_of_2(0), [])
        self.assertEqual(split_amount_to_powers_of_2(1), [1])
        self.assertEqual(split_amount_to_powers_of_2(15), [1, 2, 4, 8])
        self.assertEqual(split_amount_to_powers_of_2(30000), [16, 32, 256, 1024, 4096, 8192, 16384])
        self.assertEqual(sum(split_amount_to_powers_of_2(30000)), 30000)

    def test_point_add_and_mul(self):
        p1 = G
        p2 = point_mul(2, G)
        p3 = point_add(p1, p1)
        self.assertEqual(p2, p3)

    def test_point_compression(self):
        comp = compress_point(G)
        self.assertEqual(len(comp), 33)
        decomp = decompress_point(comp)
        self.assertEqual(decomp, G)

    def test_hash_to_curve(self):
        msg = b"test_secret_message_for_cashu"
        p = hash_to_curve(msg)
        self.assertIsNotNone(p)
        comp = compress_point(p)
        self.assertEqual(len(comp), 33)


class TestCashuClient(unittest.TestCase):

    def setUp(self):
        self.client = CashuClient(dry_run=True)

    def test_get_info(self):
        info = self.client.get_info()
        self.assertIn("name", info)

    def test_mint_quote_and_mint_simulation(self):
        quote = self.client.request_mint_quote(30000)
        self.assertEqual(quote["amount"], 30000)
        self.assertEqual(quote["state"], "UNPAID")

        proofs = self.client.mint(quote["quote"], 30000)
        self.assertTrue(len(proofs) > 0)
        self.assertEqual(sum(p["amount"] for p in proofs), 30000)

    def test_token_v3_encode_decode(self):
        proofs = [
            {"id": "009a", "amount": 1000, "secret": "sec1", "C": "02abcdef"},
            {"id": "009a", "amount": 2000, "secret": "sec2", "C": "02fedcba"},
        ]
        token = self.client.encode_token_v3(proofs)
        self.assertTrue(token.startswith("cashuA"))
        decoded = CashuClient.decode_token_v3(token)
        self.assertIn("token", decoded)
        self.assertEqual(len(decoded["token"][0]["proofs"]), 2)
        self.assertEqual(decoded["token"][0]["proofs"][0]["amount"], 1000)


class TestCashuBridge(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.wallet_path = os.path.join(self.temp_dir, "test_wallet.json")
        self.history_path = os.path.join(self.temp_dir, "test_history.json")

        self.mock_hydra = MagicMock()
        self.cfg = BotConfig(dry_run=True, cashu_storage_path=self.wallet_path)
        self.bridge = CashuBridge(
            hydra_client=self.mock_hydra,
            config=self.cfg,
            wallet_path=self.wallet_path,
            history_path=self.history_path,
        )

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_initial_wallet_empty(self):
        self.assertEqual(self.bridge.get_wallet_balance_sats(), 0)

    def test_execute_sweep_success(self):
        rec = self.bridge.execute_sweep(amount_usd=25.0, oracle_btc_price=80000.0, manual=True)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(rec["rail"], "CASHU")
        self.assertGreaterEqual(rec["amount_sats"], 30000)
        self.assertEqual(self.bridge.get_wallet_balance_sats(), rec["amount_sats"])
        self.assertTrue(os.path.exists(self.wallet_path))

    def test_execute_refuel_success(self):
        # First sweep to fund the wallet
        self.bridge.execute_sweep(amount_usd=50.0, oracle_btc_price=80000.0, manual=True)
        initial_sats = self.bridge.get_wallet_balance_sats()
        self.assertGreater(initial_sats, 0)

        # Refuel
        rec = self.bridge.execute_refuel(amount_usd=25.0, oracle_btc_price=80000.0, manual=True)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(rec["rail"], "CASHU")

    def test_export_token_v3(self):
        self.bridge.execute_sweep(amount_usd=25.0, oracle_btc_price=80000.0, manual=True)
        token = self.bridge.export_token_v3()
        self.assertTrue(token.startswith("cashuA"))

    def test_evaluate_btc_channel(self):
        # Channel send depleted
        advice = self.bridge.evaluate_btc_channel(
            free_local_btc=0.0001,
            free_remote_btc=0.0050,
            oracle_btc_price=80000.0,
            ladder_issue_send=True,
        )
        self.assertIsNotNone(advice)
        self.assertEqual(advice["action"], "TRIGGER_REFUEL")

        # Channel receive depleted
        advice2 = self.bridge.evaluate_btc_channel(
            free_local_btc=0.0050,
            free_remote_btc=0.0001,
            oracle_btc_price=80000.0,
            ladder_issue_recv=True,
        )
        self.assertIsNotNone(advice2)
        self.assertEqual(advice2["action"], "TRIGGER_SWEEP")
        self.assertGreaterEqual(advice2["amount_sats"], 30000)


class TestLocalCashuSignetMint(unittest.TestCase):
    """Tests the self-hosted Bitcoin Signet (0a03cf40) Cashu Mint Server end-to-end over HTTP."""

    def setUp(self):
        self.mint = LocalCashuSignetMint(host="127.0.0.1", port=0)
        self.mint_url = self.mint.start()
        self.client = CashuClient(mint_url=self.mint_url, dry_run=False)

    def tearDown(self):
        self.mint.stop()

    def test_signet_mint_full_nut04_and_nut05_roundtrip(self):
        # 1. NUT-06 Info
        info = self.client.get_info()
        self.assertEqual(info["network_id"], "0a03cf40")
        self.assertEqual(info["network"], "signet")

        # 2. NUT-04 Mint Quote (Bitcoin Signet lntbs... invoice) + Blind Signature Minting
        quote = self.client.request_mint_quote(1024, unit="sat")
        self.assertTrue(quote["request"].startswith("lntbs"))
        proofs = self.client.mint(quote["quote"], 1024)
        self.assertEqual(sum(p["amount"] for p in proofs), 1024)

        # 3. NUT-05 Melt Quote + Proof Verification & Settlement
        inv_quote = self.client.request_mint_quote(512, unit="sat")
        melt_q = self.client.request_melt_quote(inv_quote["request"], unit="sat")
        self.assertEqual(melt_q["amount"], 512)
        melt_res = self.client.melt(melt_q["quote"], proofs)
        self.assertTrue(melt_res["paid"])
        self.assertEqual(melt_res["state"], "PAID")


if __name__ == "__main__":
    unittest.main()
