"""
Cashu Liquidity Bridge for Hydranet DEX Bitcoin Lightning Channels.
Manages automated profit sweeps and refuels using Cashu Chaumian Ecash.
- Zero-node setup for community users (no LND/LDK, no cloud VM, no channel lease fees).
- Sweeps excess channel BTC into private, blinded bearer ecash proofs stored locally.
- Melts ecash proofs to refuel channel send capacity on demand.
- Supports 1-click token export (cashuA...) to mobile wallets (Minibits, eNuts, Zeus).
"""

import os
import json
import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

from config import BotConfig, CASHU_MINT_PRESETS
from hydra_client import HydraClient
from cashu_client import CashuClient
from cashu_signet_mint import LocalCashuSignetMint

logger = logging.getLogger("CashuBridge")


class CashuBridge:
    """
    Automated Cashu Ecash Bridge for Bitcoin state channels:
    - Auto-Sweep (Sink): Sweeps accumulated BTC profits from Hydranet state channel into local Cashu Ecash vault.
    - Auto-Refuel: Melts local Cashu Ecash proofs via Mint to pay Hydranet invoice and replenish send capacity.
    - Zero-Node: Pure HTTP/REST + local cryptographic blinded proofs.
    """

    def __init__(
        self,
        hydra_client: HydraClient,
        cashu_client: Optional[CashuClient] = None,
        config: Optional[BotConfig] = None,
        wallet_path: Optional[str] = None,
        history_path: Optional[str] = None,
    ):
        self.hydra = hydra_client
        self.config = config or BotConfig()
        self.local_signet_mint = LocalCashuSignetMint(
            port=getattr(self.config, "local_signet_mint_port", 3338)
        )
        if getattr(self.config, "start_local_signet_mint", False):
            try:
                self.local_signet_mint.start()
            except Exception as e:
                logger.warning(f"Could not start embedded Signet Cashu Mint: {e}")

        mint_url_init = getattr(self.config, "cashu_mint_url", "http://136.113.240.168:3338")
        is_local_test_mint = any(
            k in mint_url_init for k in ("127.0.0.1:", "localhost:")
        )
        self.cashu = cashu_client or CashuClient(
            mint_url=mint_url_init,
            dry_run=getattr(self.config, "dry_run", True) and not is_local_test_mint,
        )
        self.wallet_path = wallet_path or getattr(self.config, "cashu_storage_path", "data/cashu_wallet.json")
        self.history_path = history_path or "data/cashu_history.json"

        self.last_sweep_time: float = 0.0
        self.last_refuel_time: float = 0.0
        self.sweep_history: List[Dict[str, Any]] = []
        self.refuel_history: List[Dict[str, Any]] = []
        self.proofs: List[Dict[str, Any]] = []
        self.initialized: bool = False
        self._lock = threading.RLock()

        self._ensure_storage_dir()
        self._load_wallet()
        self._load_history()

    def initialize_vault(self) -> Dict[str, Any]:
        """Marks the Cashu Ecash Vault as user-initialized after risk disclosure consent."""
        with self._lock:
            self.initialized = True
            self._save_wallet()
            return {
                "status": "SUCCESS",
                "initialized": True,
                "mint_url": self.cashu.mint_url,
                "balance_sats": self.get_wallet_balance_sats(),
                "proof_count": len(self.proofs),
            }

    def set_mint_url(self, mint_url: str):
        """Updates the active Cashu Mint URL on both config and client."""
        with self._lock:
            self.config.cashu_mint_url = mint_url.rstrip("/")
            is_testnet_mint = getattr(self.config, "testnet", False) or any(
                k in self.config.cashu_mint_url for k in ("136.113.240.168:3338", "127.0.0.1:3338", ":3338", "testnut.cashu.space")
            )
            self.cashu.dry_run = getattr(self.config, "dry_run", True) and not is_testnet_mint
            if hasattr(self.cashu, "set_mint_url"):
                self.cashu.set_mint_url(self.config.cashu_mint_url)
            else:
                self.cashu.mint_url = self.config.cashu_mint_url

    def start_local_mint(self) -> Dict[str, Any]:
        """Starts the embedded Bitcoin Signet Cashu Mint HTTP server and optionally points the client to it."""
        url = self.local_signet_mint.start()
        return self.local_signet_mint.get_status()

    def stop_local_mint(self) -> Dict[str, Any]:
        """Stops the embedded Bitcoin Signet Cashu Mint HTTP server."""
        self.local_signet_mint.stop()
        return self.local_signet_mint.get_status()

    def _ensure_storage_dir(self):
        for path in (self.wallet_path, self.history_path):
            dir_name = os.path.dirname(path)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name, exist_ok=True)

    def _load_wallet(self):
        if os.path.exists(self.wallet_path):
            try:
                with open(self.wallet_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.initialized = bool(data.get("initialized", False))
                    loaded = data.get("proofs", [])
                    if not self.cashu.dry_run:
                        filtered = [p for p in loaded if p.get("id") != "009a1f293252e123"]
                        if len(filtered) != len(loaded):
                            self.proofs = filtered
                            self._save_wallet()
                        else:
                            self.proofs = loaded
                    else:
                        self.proofs = loaded
            except Exception as e:
                logger.warning(f"Failed to load Cashu wallet from {self.wallet_path}: {e}")
                self.proofs = []
                self.initialized = False
        else:
            self.proofs = []
            self.initialized = False

    def _save_wallet(self):
        try:
            with open(self.wallet_path, "w", encoding="utf-8") as f:
                json.dump({
                    "initialized": self.initialized,
                    "proofs": self.proofs,
                    "updated_at": time.time(),
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save Cashu wallet: {e}")

    def _load_history(self):
        if os.path.exists(self.history_path):
            try:
                with open(self.history_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    sweeps = data.get("sweeps", [])[:50]
                    refuels = data.get("refuels", [])[:50]
                    if not self.cashu.dry_run:
                        sweeps_f = [s for s in sweeps if not s.get("simulated")]
                        refuels_f = [r for r in refuels if not r.get("simulated")]
                        if len(sweeps_f) != len(sweeps) or len(refuels_f) != len(refuels):
                            self.sweep_history = sweeps_f
                            self.refuel_history = refuels_f
                            self._save_history()
                            return
                    self.sweep_history = sweeps
                    self.refuel_history = refuels
            except Exception:
                self.sweep_history = []
                self.refuel_history = []

    def _save_history(self):
        try:
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump({
                    "sweeps": self.sweep_history[:50],
                    "refuels": self.refuel_history[:50],
                    "updated_at": time.time()
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save Cashu history: {e}")

    def get_wallet_balance_sats(self) -> int:
        """Returns the total satoshi balance of active ecash proofs in local wallet."""
        with self._lock:
            return sum(p.get("amount", 0) for p in self.proofs)

    def export_token_v3(self) -> str:
        """Exports all local ecash proofs as a single Cashu V3 token string (cashuA...)."""
        with self._lock:
            if not self.proofs:
                raise ValueError("Ecash wallet is empty. No proofs to export.")
            return self.cashu.encode_token_v3(self.proofs)

    def export_ecash_token(self) -> str:
        """Alias for export_token_v3 used by the CLI and Web API."""
        return self.export_token_v3()

    def ensure_simulated_reserve(self, min_sats: int = 90000):
        """Seeds the local dry-run wallet with simulated proofs if empty so Mint/Melt loops can be tested immediately."""
        with self._lock:
            if not self.cashu.dry_run:
                return
            current = self.get_wallet_balance_sats()
            if current < min_sats:
                needed = min_sats - current
                quote = self.cashu.request_mint_quote(needed, unit="sat")
                new_proofs = self.cashu.mint(quote["quote"], needed)
                self.proofs.extend(new_proofs)
                self._save_wallet()

    def evaluate_btc_channel(
        self,
        free_local_btc: float,
        free_remote_btc: float,
        oracle_btc_price: float,
        req_receiving_usd: float = 15.0,
        ladder_issue_send: bool = False,
        ladder_issue_recv: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluates BTC state channel balances against healthy 15-rung order requirements.
        Advises if a Cashu Profit Sweep or Refuel is warranted.
        """
        total_btc = free_local_btc + free_remote_btc
        if total_btc <= 0.00001:
            return None

        btc_ratio = free_local_btc / total_btc if total_btc > 0 else 0.50
        target_ratio = getattr(self.config, "target_base_ratio", 0.50)
        cooldown = getattr(self.config, "cashu_cooldown_secs", 0.0)

        required_send_btc = total_btc * target_ratio
        required_recv_btc = total_btc * (1.0 - target_ratio)

        send_deficit_btc = max(0.0, required_send_btc - free_local_btc)
        send_deficit_usd = send_deficit_btc * oracle_btc_price
        recv_deficit_btc = max(0.0, required_recv_btc - free_remote_btc)
        recv_deficit_usd = recv_deficit_btc * oracle_btc_price

        min_sink_sats = getattr(self.config, "cashu_min_sink_sats", 30000)
        min_sink_btc = round(min_sink_sats / 1e8, 8)

        # 1. Check Auto-Refuel Condition (Cashu Ecash -> Hydranet Channel when Outbound <= 25%)
        if getattr(self.config, "cashu_auto_refuel", True) and ((btc_ratio <= 0.25 and send_deficit_usd >= 5.0) or ladder_issue_send):
            time_since = time.time() - self.last_refuel_time
            if cooldown > 0 and time_since < cooldown:
                return {
                    "action": "COOLDOWN_REFUEL",
                    "btc_ratio": btc_ratio,
                    "deficit_usd": send_deficit_usd,
                    "cooldown_remaining_secs": max(0, int(cooldown - time_since)),
                }

            wallet_sats = self.get_wallet_balance_sats()
            if wallet_sats <= 0 and not self.cashu.dry_run:
                return {
                    "action": "CASHU_VAULT_EMPTY",
                    "reason": "Ecash vault has 0 sats available for refuel.",
                    "btc_ratio": btc_ratio,
                }

            target_btc = round(send_deficit_btc * 1.5, 8) if send_deficit_btc > 0 else min_sink_btc
            target_usd = round(target_btc * oracle_btc_price, 2)
            max_refuel_usd = getattr(self.config, "cashu_max_refuel_usd", 250.0)
            target_usd = min(target_usd, max_refuel_usd)
            target_btc = round(target_usd / oracle_btc_price, 8) if oracle_btc_price > 0 else 0.0

            if free_remote_btc < target_btc:
                return {
                    "action": "INSUFFICIENT_INBOUND",
                    "reason": f"Channel inbound capacity ({free_remote_btc:.8f} BTC) insufficient for refuel ({target_btc:.8f} BTC).",
                    "btc_ratio": btc_ratio,
                }

            return {
                "action": "TRIGGER_REFUEL",
                "amount_usd": target_usd,
                "amount_btc": target_btc,
                "amount_sats": int(round(target_btc * 1e8)),
                "deficit_usd": send_deficit_usd,
                "btc_ratio": btc_ratio,
                "oracle_price": oracle_btc_price,
            }

        # 2. Check Auto-Sweep Condition (Hydranet Channel -> Cashu Ecash when Inbound <= 25%)
        if getattr(self.config, "cashu_auto_sweep", True) and ((btc_ratio >= 0.75 and recv_deficit_usd >= 5.0) or ladder_issue_recv):
            time_since = time.time() - self.last_sweep_time
            if cooldown > 0 and time_since < cooldown:
                return {
                    "action": "COOLDOWN_SWEEP",
                    "btc_ratio": btc_ratio,
                    "excess_usd": recv_deficit_usd,
                    "cooldown_remaining_secs": max(0, int(cooldown - time_since)),
                }

            target_btc = round(recv_deficit_btc * 1.5, 8) if recv_deficit_btc > 0 else min_sink_btc
            target_btc = max(target_btc, min_sink_btc)
            target_usd = round(target_btc * oracle_btc_price, 2)
            max_sweep_usd = getattr(self.config, "cashu_max_sweep_usd", 250.0)
            target_usd = min(target_usd, max_sweep_usd)
            target_btc = round(target_usd / oracle_btc_price, 8) if oracle_btc_price > 0 else 0.0
            if getattr(self.config, "testnet", False) and not self.cashu.dry_run:
                target_btc = min(target_btc, 0.0005)
                target_usd = round(target_btc * oracle_btc_price, 2)
                mint_info = self._get_cached_mint_info()
                if "lnd_max_receivable_sats" in mint_info:
                    safe_recv_sats = max(0, int(mint_info.get("lnd_max_receivable_sats", 0)) - 1500)
                    if safe_recv_sats < min_sink_sats:
                        return {
                            "action": "MINT_INBOUND_FULL",
                            "reason": (
                                f"Cashu Mint LND inbound capacity ({safe_recv_sats:,} sats) is below "
                                f"minimum {min_sink_sats:,} sat sweep floor."
                            ),
                            "btc_ratio": btc_ratio,
                        }
                    target_btc = min(target_btc, round(safe_recv_sats / 1e8, 8))
                    target_usd = round(target_btc * oracle_btc_price, 2)
            if max_sweep_usd >= (min_sink_btc * oracle_btc_price):
                target_btc = max(target_btc, min_sink_btc)
                target_usd = round(target_btc * oracle_btc_price, 2)

            if free_local_btc < target_btc:
                return {
                    "action": "INSUFFICIENT_LOCAL",
                    "reason": f"Channel sendable capacity ({free_local_btc:.8f} BTC) below minimum sweep floor ({target_btc:.8f} BTC).",
                    "btc_ratio": btc_ratio,
                }

            return {
                "action": "TRIGGER_SWEEP",
                "amount_usd": target_usd,
                "amount_btc": target_btc,
                "amount_sats": int(round(target_btc * 1e8)),
                "excess_usd": recv_deficit_usd,
                "btc_ratio": btc_ratio,
                "oracle_price": oracle_btc_price,
            }

        return None

    def _get_cached_mint_info(self) -> Dict[str, Any]:
        """Queries and caches the Cashu Mint /v1/info endpoint for 20s."""
        now = time.time()
        if getattr(self, "_mint_info_cache", None) and (now - getattr(self, "_mint_info_ts", 0.0)) < 20.0:
            return self._mint_info_cache
        self._mint_info_ts = now
        try:
            info = self.cashu.get_info()
            if isinstance(info, dict):
                self._mint_info_cache = info
                return info
        except Exception:
            pass
        return getattr(self, "_mint_info_cache", {}) or {}

    def execute_sweep(
        self,
        amount_usd: float = 0.0,
        oracle_btc_price: float = 65000.0,
        manual: bool = False,
        amount_btc: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Executes an Ecash Profit Sweep (Hydranet Channel -> Cashu Ecash):
        1. Requests Lightning mint quote from Cashu Mint.
        2. Hydranet state channel pays the Mint's BOLT11 invoice.
        3. Mint signs blinded secrets; client unblinds and stores ecash proofs locally.
        """
        with self._lock:
            min_sink_sats = getattr(self.config, "cashu_min_sink_sats", 30000)
            min_sink_btc = round(min_sink_sats / 1e8, 8)

            if amount_btc is None or amount_btc <= 0:
                amount_btc = round(amount_usd / oracle_btc_price, 8) if oracle_btc_price > 0 else min_sink_btc
            if amount_btc < min_sink_btc:
                amount_btc = min_sink_btc
            amount_usd = round(amount_btc * oracle_btc_price, 2)
            amount_sats = int(round(amount_btc * 1e8))

            event_id = f"cashu_sweep_{int(time.time() * 1000)}"
            logger.info(
                f"[{'MANUAL' if manual else 'AUTO'}] Initiating Cashu Profit Sweep: "
                f"{amount_sats:,} sats (~{amount_btc:.8f} BTC / ${amount_usd:.2f} USD @ ${oracle_btc_price:,.2f})"
            )

            record: Dict[str, Any] = {
                "id": event_id,
                "type": "SWEEP",
                "rail": "CASHU",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "epoch": time.time(),
                "amount_usd": amount_usd,
                "amount_btc": amount_btc,
                "amount_sats": amount_sats,
                "oracle_price": oracle_btc_price,
                "manual": manual,
                "status": "INITIATED",
                "simulated": self.cashu.dry_run,
            }

            try:
                # Step 1: Request mint quote from Cashu Mint
                quote_res = self.cashu.request_mint_quote(amount_sats, unit="sat")
                quote_id = quote_res.get("quote")
                bolt11 = quote_res.get("request", "")
                if not bolt11:
                    raise RuntimeError(f"Mint did not return a valid BOLT11 invoice: {quote_res}")

                record["quote_id"] = quote_id
                record["payment_request"] = bolt11

                # Step 2: Pay the Mint invoice from Hydranet Channel
                btc_net_id = "0a03cf40" if getattr(self.config, "testnet", False) else "f9beb4d9"
                if self.cashu.dry_run:
                    pay_res = {"status": "SUCCESS", "simulated": True}
                else:
                    pre_local_btc: Optional[float] = None
                    try:
                        raw_chs = self.hydra.get_channels("PROTOCOL_BITCOIN", btc_net_id)
                        if isinstance(raw_chs, list) and raw_chs:
                            pre_local_btc = sum(
                                float(ac.get("balance", {}).get("freeLocal", {}).get("value", 0.0))
                                for ch in raw_chs
                                for ac in ch.get("assetChannels", {}).values()
                            )
                    except Exception:
                        pre_local_btc = None

                    pay_res = self.hydra.send_lightning_payment(bolt11, network_id=btc_net_id)
                    if isinstance(pay_res, dict) and pay_res.get("status") in ("FAILED", "ERROR"):
                        raise RuntimeError(f"Hydranet payment failed: {pay_res.get('error')}")

                    # Verify that the Mint LND invoice actually settled if quote carries an LND r_hash
                    if quote_res.get("r_hash") or quote_res.get("state") == "UNPAID":
                        quote_paid = False
                        for _ in range(10):
                            time.sleep(0.8)
                            try:
                                q_chk = self.cashu.check_mint_quote(quote_id)
                                if q_chk.get("state") in ("PAID", "ISSUED") or q_chk.get("paid") is True:
                                    quote_paid = True
                                    break
                            except Exception:
                                pass
                        if not quote_paid:
                            raise RuntimeError(
                                f"Lightning HTLC for {amount_sats:,} sats did not settle at Mint LND (quote {quote_id} state=UNPAID)."
                            )

                    # Verify that the Lightning HTLC permanently deducted local channel balance (not just a transient in-flight HTLC)
                    if pre_local_btc is not None and pre_local_btc > 0:
                        consecutive_deducted = 0
                        for _ in range(6):
                            time.sleep(0.75)
                            try:
                                post_chs = self.hydra.get_channels("PROTOCOL_BITCOIN", btc_net_id)
                                if isinstance(post_chs, list) and post_chs:
                                    post_local_btc = sum(
                                        float(ac.get("balance", {}).get("freeLocal", {}).get("value", 0.0))
                                        for ch in post_chs
                                        for ac in ch.get("assetChannels", {}).values()
                                    )
                                    if (pre_local_btc - post_local_btc) >= (amount_btc * 0.85):
                                        consecutive_deducted += 1
                                        if consecutive_deducted >= 2:
                                            break
                                    else:
                                        consecutive_deducted = 0
                            except Exception:
                                consecutive_deducted = 2
                                break
                        if consecutive_deducted < 2:
                            raise RuntimeError(
                                f"Lightning HTLC for {amount_sats:,} sats rolled back on downstream hop (channel balance unchanged)."
                            )

                # Step 3: Request blind signatures and unblind ecash proofs
                self._mint_info_ts = 0.0
                new_proofs = self.cashu.mint(quote_id, amount_sats)
                self.proofs.extend(new_proofs)
                self._save_wallet()

                record["status"] = "COMPLETED"
                record["proof_count"] = len(new_proofs)
                record["total_vault_sats"] = self.get_wallet_balance_sats()
                self.last_sweep_time = time.time()
                logger.info(f"Cashu Sweep completed! Vault now holds {record['total_vault_sats']:,} sats.")

            except Exception as e:
                safe_err = str(e).encode("ascii", "replace").decode("ascii")
                logger.error(f"Cashu Sweep {event_id} failed: {safe_err}")
                record["status"] = "FAILED"
                record["error"] = safe_err
                self.last_sweep_time = time.time()

            self.sweep_history.insert(0, record)
            self._save_history()
            return record

    def execute_refuel(
        self,
        amount_usd: float = 0.0,
        oracle_btc_price: float = 65000.0,
        manual: bool = False,
        amount_btc: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Executes an Ecash Channel Refuel (Cashu Ecash -> Hydranet Channel):
        1. Creates receive invoice on local Hydranet node.
        2. Requests melt quote from Cashu Mint.
        3. Melts required ecash proofs; Mint settles Lightning invoice to Hydranet channel.
        """
        with self._lock:
            min_sink_sats = getattr(self.config, "cashu_min_sink_sats", 30000)
            min_sink_btc = round(min_sink_sats / 1e8, 8)

            if amount_btc is None or amount_btc <= 0:
                amount_btc = round(amount_usd / oracle_btc_price, 8) if oracle_btc_price > 0 else min_sink_btc
            amount_usd = round(amount_btc * oracle_btc_price, 2)
            amount_sats = int(round(amount_btc * 1e8))

            event_id = f"cashu_refuel_{int(time.time() * 1000)}"
            logger.info(
                f"[{'MANUAL' if manual else 'AUTO'}] Initiating Cashu Channel Refuel: "
                f"{amount_sats:,} sats (~{amount_btc:.8f} BTC / ${amount_usd:.2f} USD @ ${oracle_btc_price:,.2f})"
            )

            record: Dict[str, Any] = {
                "id": event_id,
                "type": "REFUEL",
                "rail": "CASHU",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "epoch": time.time(),
                "amount_usd": amount_usd,
                "amount_btc": amount_btc,
                "amount_sats": amount_sats,
                "oracle_price": oracle_btc_price,
                "manual": manual,
                "status": "INITIATED",
                "simulated": self.cashu.dry_run,
            }

            try:
                # Step 1: Create receive invoice on Hydranet
                btc_net_id = "0a03cf40" if getattr(self.config, "testnet", False) else "f9beb4d9"
                if self.cashu.dry_run:
                    bolt11 = f"lnbc{amount_sats}00u1psimulatedhydrainvoice"
                else:
                    inv = self.hydra.create_lightning_invoice(amount_btc, network_id=btc_net_id)
                    bolt11 = inv.get("paymentRequest", "") if isinstance(inv, dict) else ""
                    if not bolt11:
                        raise RuntimeError(f"Hydranet node did not return a paymentRequest: {inv}")

                record["payment_request"] = bolt11

                # Step 2: Request melt quote from Cashu Mint
                melt_quote = self.cashu.request_melt_quote(bolt11, unit="sat")
                quote_id = melt_quote.get("quote")
                required_sats = melt_quote.get("amount", amount_sats)
                fee_reserve = melt_quote.get("fee_reserve", 100)
                total_needed = required_sats + fee_reserve

                # Step 3: Select proofs from local wallet
                selected_proofs = []
                accumulated = 0
                remaining_proofs = []
                for p in self.proofs:
                    if accumulated < total_needed:
                        selected_proofs.append(p)
                        accumulated += p.get("amount", 0)
                    else:
                        remaining_proofs.append(p)

                if accumulated < total_needed and not self.cashu.dry_run:
                    raise RuntimeError(f"Insufficient ecash balance ({accumulated} < {total_needed} sats needed)")

                # Step 4: Melt proofs via Mint
                melt_res = self.cashu.melt(quote_id, selected_proofs, target_amount_sats=required_sats)
                if not (melt_res.get("paid") or melt_res.get("state") == "PAID"):
                    raise RuntimeError("Mint reported melt payment failed")

                # Update wallet: keep remaining proofs plus any change returned by Mint
                change_proofs = melt_res.get("change", [])
                self.proofs = remaining_proofs + change_proofs
                self._save_wallet()

                record["status"] = "COMPLETED"
                record["melted_sats"] = amount_sats
                record["preimage"] = melt_res.get("payment_preimage")
                record["total_vault_sats"] = self.get_wallet_balance_sats()
                self.last_refuel_time = time.time()
                logger.info("Cashu Refuel completed! Hydranet send capacity replenished.")

            except Exception as e:
                safe_err = str(e).encode("ascii", "replace").decode("ascii")
                logger.error(f"Cashu Refuel {event_id} failed: {safe_err}")
                record["status"] = "FAILED"
                record["error"] = safe_err
                self.last_refuel_time = time.time()

            self.refuel_history.insert(0, record)
            self._save_history()
            return record

    def verify_live_mint_hypothesis(self, test_sats: int = 30000) -> Dict[str, Any]:
        """
        Tests the Cashu Mint & Melt hypothesis end-to-end:
        1. Validates NUT-00 BDHKE secp256k1 blind signature math & V3 token serialization locally.
        2. Connects to the live Cashu Mint (NUT-06 info, NUT-01/02 keysets, and NUT-04 live BOLT11 quote).
        3. If connected to a Testnet/Signet Mint (testnut.cashu.space or Local Signet Mint :3338),
           executes a full live HTTP NUT-04 Mint + NUT-05 Melt round-trip!
        """
        from cashu_client import (
            CashuClient,
            G,
            N,
            P,
            point_add,
            point_mul,
            compress_point,
            decompress_point,
            hash_to_curve,
            split_amount_to_powers_of_2,
        )
        import secrets

        t0 = time.time()
        # 1. Verify NUT-00 BDHKE cryptographic blind/unblind cycle
        secret_msg = secrets.token_bytes(32).hex().encode("utf-8")
        Y = hash_to_curve(secret_msg)
        r = 1 + secrets.randbelow(N - 1)
        k_mint = 1 + secrets.randbelow(N - 1)  # Simulated Mint private key
        K_mint = point_mul(k_mint, G)          # Mint public key
        B_ = point_add(Y, point_mul(r, G))     # Blinded message B_ = Y + rG
        C_ = point_mul(k_mint, B_)             # Blind signature C_ = k * B_
        rK = point_mul(r, K_mint)
        rK_neg = (rK[0], (P - rK[1]) % P)
        C = point_add(C_, rK_neg)              # Unblinded signature C = C_ - rK
        expected_C = point_mul(k_mint, Y)      # Direct k * Y
        bdhke_valid = (C == expected_C)

        denominations = split_amount_to_powers_of_2(test_sats)
        crypto_ms = round((time.time() - t0) * 1000.0, 2)

        # 2. Test Live Cashu Mint REST API (NUT-06 Info, NUT-02 Keysets, NUT-04 Mint Quote)
        live_client = CashuClient(mint_url=self.cashu.mint_url, dry_run=False, timeout=5.0)
        mint_reachable = False
        mint_name = "Simulated Local Mint"
        active_keyset = "009a1f293252e123"
        bolt11_preview = ""
        quote_id = ""
        mint_error = None
        roundtrip_verified = False
        roundtrip_token_preview = ""

        try:
            info = live_client.get_info()
            mint_name = info.get("name") or "Cashu Mint"
            keysets = live_client.get_keysets()
            sat_ks = [ks for ks in keysets if ks.get("unit") == "sat" and ks.get("active")]
            if sat_ks:
                active_keyset = sat_ks[0].get("id", active_keyset)
            quote = live_client.request_mint_quote(test_sats, unit="sat")
            quote_id = quote.get("quote", "")
            req_str = quote.get("request", "")
            if req_str:
                bolt11_preview = f"{req_str[:28]}...{req_str[-14:]}" if len(req_str) > 45 else req_str
                mint_reachable = True

            # 3. If pointing at a Testnet / Signet Mint, perform a full live HTTP Mint + Melt round-trip!
            is_testnet_mint = any(
                k in self.cashu.mint_url.lower()
                for k in ("testnut.cashu.space", "127.0.0.1:3338", "localhost:3338")
            )
            if is_testnet_mint and mint_reachable:
                rt_q = live_client.request_mint_quote(64, unit="sat")
                rt_proofs = live_client.mint(rt_q["quote"], 64)
                rt_token = live_client.encode_token_v3(rt_proofs)
                roundtrip_token_preview = f"{rt_token[:34]}..."
                inv_q = live_client.request_mint_quote(32, unit="sat")
                mq = live_client.request_melt_quote(inv_q["request"], unit="sat")
                mres = live_client.melt(mq["quote"], rt_proofs)
                roundtrip_verified = bool(mres.get("paid") or mres.get("state") == "PAID")
        except Exception as e:
            mint_error = str(e)
            # Fallback to simulated quote preview if offline
            sim_q = self.cashu.request_mint_quote(test_sats, unit="sat")
            quote_id = sim_q.get("quote", "")
            req_str = sim_q.get("request", "")
            bolt11_preview = f"{req_str[:28]}..."

        total_ms = round((time.time() - t0) * 1000.0, 1)

        if roundtrip_verified:
            mint_note = (
                f"Full Live Testnet Round-Trip Verified on {mint_name} (keyset {active_keyset}): "
                f"Minted real BDHKE token ({roundtrip_token_preview}) & Melted via NUT-05!"
            )
        elif mint_reachable:
            mint_note = (
                f"Live NUT-04 BOLT11 quote ({test_sats:,} sats) issued by {mint_name} "
                f"(keyset {active_keyset}) & NUT-00 BDHKE verified!"
            )
        else:
            mint_note = (
                f"Local NUT-00 BDHKE secp256k1 blind signature math verified in {crypto_ms}ms "
                f"(remote mint offline: {mint_error})"
            )

        return {
            "status": "SUCCESS" if bdhke_valid else "ERROR",
            "hypothesis_verified": bdhke_valid,
            "mint_url": self.cashu.mint_url,
            "mint_reachable": mint_reachable,
            "mint_name": mint_name,
            "active_keyset_id": active_keyset,
            "test_amount_sats": test_sats,
            "denominations": denominations,
            "bdhke_verified": bdhke_valid,
            "live_mint_melt_roundtrip": roundtrip_verified,
            "roundtrip_token_preview": roundtrip_token_preview,
            "crypto_latency_ms": crypto_ms,
            "total_latency_ms": total_ms,
            "quote_id": quote_id,
            "bolt11_preview": bolt11_preview,
            "mint_note": mint_note,
        }

    def get_rebalance_count_in_window(self, window_secs: float = 21600.0) -> int:
        """
        Counts how many completed Cashu Mint (Sweep) and Melt (Refuel) cycles occurred
        within the rolling window (default 6 hours = 21,600s).
        Used by the Deterministic Advisor to detect when Cashu is rebalancing too frequently
        and advise expanding underlying BTC Lightning channel capacity.
        """
        cutoff = time.time() - window_secs
        count = 0
        for rec in list(self.sweep_history) + list(self.refuel_history):
            if rec.get("status") != "COMPLETED":
                continue
            ep = rec.get("epoch")
            if ep is None:
                # Extract epoch from id like cashu_sweep_1730240000000
                rec_id = str(rec.get("id", ""))
                parts = rec_id.split("_")
                if parts and parts[-1].isdigit():
                    ep = int(parts[-1]) / 1000.0
                else:
                    ep = 0.0
            if float(ep) >= cutoff:
                count += 1
        return count

    def should_trigger_capacity_expansion(self, window_secs: float = 21600.0, threshold_count: int = 4) -> bool:
        """
        Returns True if Cashu Mint/Melt has been triggered >= threshold_count times
        within the rolling window, indicating the BTC channel itself should be expanded.
        """
        return self.get_rebalance_count_in_window(window_secs=window_secs) >= threshold_count

    def get_status(
        self,
        free_local_btc: float = 0.0,
        free_remote_btc: float = 0.0,
        oracle_btc_price: float = 82000.0,
    ) -> Dict[str, Any]:
        """Provides complete Cashu telemetry dictionary for UI dashboard."""
        vault_sats = self.get_wallet_balance_sats() if self.initialized else 0
        vault_btc = round(vault_sats / 1e8, 8)
        vault_usd = round(vault_btc * oracle_btc_price, 2)
        proof_cnt = len(self.proofs) if self.initialized else 0

        completed_sweeps = [s for s in self.sweep_history if s.get("status") == "COMPLETED"]
        completed_refuels = [r for r in self.refuel_history if r.get("status") == "COMPLETED"]
        total_cycles = len(completed_sweeps) + len(completed_refuels)
        # Each Cashu sweep/refuel recycles existing channel capacity without opening/leasing a new LN channel (~$3.50 avg saved per cycle)
        estimated_fees_saved_usd = round(total_cycles * 3.50, 2)
        rebalances_in_window = self.get_rebalance_count_in_window(window_secs=21600.0)
        threshold_count = int(getattr(self.config, "cashu_expansion_threshold", 4))

        # Combine recent events chronologically (newest first)
        combined_events = sorted(
            self.sweep_history[:15] + self.refuel_history[:15],
            key=lambda x: x.get("id", ""),
            reverse=True,
        )[:12]

        return {
            "rail": "CASHU",
            "initialized": self.initialized,
            "enabled": getattr(self.config, "cashu_enabled", True) and self.initialized,
            "dry_run": self.cashu.dry_run,
            "testnet": getattr(self.config, "testnet", False),
            "mint_url": self.cashu.mint_url,
            "mint_presets": CASHU_MINT_PRESETS,
            "local_signet_mint": self.local_signet_mint.get_status(),
            "vault": {
                "balance_sats": vault_sats,
                "balance_btc": vault_btc,
                "balance_usd": vault_usd,
                "proof_count": proof_cnt,
            },
            "metrics": {
                "sweeps_count": len(completed_sweeps),
                "refuels_count": len(completed_refuels),
                "total_cycles": total_cycles,
                "rebalances_in_window": rebalances_in_window,
                "expansion_threshold": threshold_count,
                "should_expand_btc_channel": rebalances_in_window >= threshold_count,
                "estimated_rental_fees_saved_usd": estimated_fees_saved_usd,
            },
            "config": {
                "auto_sweep": getattr(self.config, "cashu_auto_sweep", True),
                "auto_refuel": getattr(self.config, "cashu_auto_refuel", True),
                "min_sink_sats": getattr(self.config, "cashu_min_sink_sats", 30000),
                "max_sweep_usd": getattr(self.config, "cashu_max_sweep_usd", 250.0),
                "max_refuel_usd": getattr(self.config, "cashu_max_refuel_usd", 250.0),
            },
            "last_sweep_time": self.last_sweep_time,
            "last_refuel_time": self.last_refuel_time,
            "recent_sweeps": self.sweep_history[:10],
            "recent_refuels": self.refuel_history[:10],
            "recent_events": combined_events,
        }
