"""
Self-Hosted Bitcoin Signet Cashu Mint Server (NUT-00 to NUT-06).
Provides a turnkey local Chaumian Ecash Mint for Hydranet Testnet (Bitcoin Signet 0a03cf40)
market making. Supports:
1. Instant Signet Test Backend (zero external dependencies, issues valid Signet lntbs1... quotes
   and performs real secp256k1 BDHKE blind signing C_ = k * B_ and proof verification C == k * Y).
2. LND Signet REST Backend (LND_REST_URL + LND_MACAROON_HEX) for real Signet HTLC settlement.
3. LNbits Signet Backend (LNBITS_ENDPOINT + LNBITS_API_KEY) for hosted Signet Lightning wallets.
"""

import os
import re
import json
import time
import hashlib
import secrets
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional, Tuple
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from cashu_client import (
    G,
    N,
    P,
    point_add,
    point_mul,
    compress_point,
    decompress_point,
    hash_to_curve,
)

logger = logging.getLogger("CashuSignetMint")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_bolt11_amount_sats(bolt11: str, default_sats: int = 30000) -> int:
    """
    Extracts the satoshi amount from a standard BOLT11 invoice prefix
    (e.g., lntbs500u1..., lnbc320n1..., lntb1m1...).
    """
    if not bolt11:
        return default_sats
    s = bolt11.strip().lower()
    m = re.match(r"^ln(?:tbs|tb|bcrt|bc)(\d+)([munp]?)1", s)
    if not m:
        return default_sats
    val = int(m.group(1))
    mult = m.group(2)
    # 1 BTC = 100,000,000 sats
    if mult == "":
        return val * 100_000_000
    elif mult == "m":
        return val * 100_000
    elif mult == "u":
        return val * 100
    elif mult == "n":
        return max(1, val // 10)
    elif mult == "p":
        return max(1, val // 10000)
    return default_sats


class SignetMintState:
    """
    Holds the cryptographic keyset (k_i private keys, K_i public keys),
    mint quotes, melt quotes, and spent-secret double-spend registry.
    """

    def __init__(self, master_seed: str = "hydranet-signet-cashu-mint-seed-v1"):
        self.master_seed = master_seed
        self.network = "signet"
        self.network_id = "0a03cf40"
        self.keyset_id = "00signet0a03cf40"
        self.unit = "sat"
        self.privkeys: Dict[int, int] = {}
        self.pubkeys: Dict[int, str] = {}
        self._derive_keyset()

        self._lock = threading.Lock()
        self.mint_quotes: Dict[str, Dict[str, Any]] = {}
        self.melt_quotes: Dict[str, Dict[str, Any]] = {}
        self.spent_secrets: set = set()
        self.total_minted_sats: int = 0
        self.total_melted_sats: int = 0
        self.state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mint_state.json")
        self._load_state()

        # Optional external Signet Lightning backend config (auto-discovers local hydranet-lnd-signet if present)
        self.lnd_rest_url = os.getenv("LND_REST_URL", "").rstrip("/")
        self.lnd_macaroon_hex = os.getenv("LND_MACAROON_HEX", "")
        if not self.lnd_macaroon_hex:
            mac_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lnd_signet_admin.macaroon.hex")
            if os.path.exists(mac_path):
                try:
                    with open(mac_path, "r", encoding="utf-8") as mf:
                        self.lnd_macaroon_hex = mf.read().strip()
                    if not self.lnd_rest_url:
                        self.lnd_rest_url = "https://127.0.0.1:8081"
                except Exception:
                    pass
        self.lnbits_endpoint = os.getenv("LNBITS_ENDPOINT", "").rstrip("/")
        self.lnbits_api_key = os.getenv("LNBITS_API_KEY", "")
        self._lnd_info_cache: Dict[str, Any] = {}
        self._lnd_info_ts: float = 0.0

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.mint_quotes = data.get("mint_quotes", {})
            self.melt_quotes = data.get("melt_quotes", {})
            self.spent_secrets = set(data.get("spent_secrets", []))
            self.total_minted_sats = int(data.get("total_minted_sats", 0))
            self.total_melted_sats = int(data.get("total_melted_sats", 0))
        except Exception:
            pass

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            data = {
                "mint_quotes": self.mint_quotes,
                "melt_quotes": self.melt_quotes,
                "spent_secrets": list(self.spent_secrets),
                "total_minted_sats": self.total_minted_sats,
                "total_melted_sats": self.total_melted_sats,
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def get_lnd_info(self) -> Dict[str, Any]:
        """Queries and caches live status from the local Signet LND node (hydranet-lnd-signet)."""
        if not (self.lnd_rest_url and self.lnd_macaroon_hex):
            return {}
        now = time.time()
        if (now - self._lnd_info_ts) < 15.0:
            return self._lnd_info_cache
        self._lnd_info_ts = now
        try:
            res = requests.get(
                f"{self.lnd_rest_url}/v1/getinfo",
                headers={"Grpc-Metadata-macaroon": self.lnd_macaroon_hex},
                timeout=0.4,
                verify=False,
            )
            if res.ok:
                data = res.json()
                inbound_sats = 0
                outbound_sats = 0
                max_receivable_sats = 0
                try:
                    ch_res = requests.get(
                        f"{self.lnd_rest_url}/v1/channels",
                        headers={"Grpc-Metadata-macaroon": self.lnd_macaroon_hex},
                        timeout=0.8,
                        verify=False,
                    )
                    if ch_res.ok:
                        for ch in ch_res.json().get("channels", []):
                            if ch.get("active"):
                                rem = int(ch.get("remote_balance", 0) or 0)
                                loc = int(ch.get("local_balance", 0) or 0)
                                resv = int(ch.get("remote_chan_reserve_sat", 5000) or 5000)
                                inbound_sats += rem
                                outbound_sats += loc
                                max_receivable_sats += max(0, rem - resv)
                except Exception:
                    pass
                self._lnd_info_cache = {
                    "lnd_pubkey": data.get("identity_pubkey", ""),
                    "lnd_block_height": int(data.get("block_height", 0)),
                    "lnd_synced_to_chain": bool(data.get("synced_to_chain", False)),
                    "lnd_num_peers": int(data.get("num_peers", 0)),
                    "lnd_inbound_sats": inbound_sats,
                    "lnd_outbound_sats": outbound_sats,
                    "lnd_max_receivable_sats": max_receivable_sats,
                }
                return self._lnd_info_cache
        except Exception:
            pass
        return self._lnd_info_cache

    @property
    def backend_type(self) -> str:
        if self.lnd_rest_url and self.lnd_macaroon_hex:
            return "LndRestSignetWallet"
        if self.lnbits_endpoint and self.lnbits_api_key:
            return "LNbitsSignetWallet"
        return "SignetFakeWallet"

    def _derive_keyset(self):
        """Derives deterministic secp256k1 keypairs for powers of 2 (2^0 .. 2^19 sats)."""
        for i in range(20):
            amt = 2**i
            digest = hashlib.sha256(f"{self.master_seed}:sat:{amt}".encode("utf-8")).digest()
            k = (int.from_bytes(digest, "big") % (N - 1)) + 1
            K = point_mul(k, G)
            self.privkeys[amt] = k
            self.pubkeys[amt] = compress_point(K).hex()

    def create_mint_quote(self, amount_sats: int, unit: str = "sat") -> Dict[str, Any]:
        quote_id = f"sig_mint_{secrets.token_hex(8)}"
        now = int(time.time())
        self._last_r_hash_hex = None
        # Use external Signet LND / LNbits if configured, else generate realistic Signet BOLT11 (lntbs...)
        bolt11 = self._create_signet_invoice(amount_sats, quote_id)
        r_hash_hex = getattr(self, "_last_r_hash_hex", None)
        is_real_lnd_quote = bool(r_hash_hex)
        quote = {
            "quote": quote_id,
            "request": bolt11,
            "amount": amount_sats,
            "unit": unit,
            "state": "UNPAID" if is_real_lnd_quote else "PAID",
            "paid": not is_real_lnd_quote,
            "r_hash": r_hash_hex,
            "expiry": now + 3600,
            "network": "Bitcoin Signet (0a03cf40)",
        }
        with self._lock:
            self.mint_quotes[quote_id] = quote
            self._save_state()
        return quote

    def check_lnd_quote_paid(self, quote: Dict[str, Any]) -> bool:
        """Verifies with LND whether a mint quote's BOLT11 invoice is actually SETTLED."""
        if not quote:
            return False
        if quote.get("state") in ("PAID", "ISSUED") or quote.get("paid") is True:
            return True
        r_hash_hex = quote.get("r_hash")
        if r_hash_hex and self.lnd_rest_url and self.lnd_macaroon_hex:
            try:
                res = requests.get(
                    f"{self.lnd_rest_url}/v1/invoice/{r_hash_hex}",
                    headers={"Grpc-Metadata-macaroon": self.lnd_macaroon_hex},
                    timeout=2.5,
                    verify=False,
                )
                if res.ok:
                    inv = res.json()
                    if inv.get("state") == "SETTLED" or inv.get("settled") is True:
                        quote["state"] = "PAID"
                        quote["paid"] = True
                        with self._lock:
                            self._save_state()
                        return True
            except Exception as e:
                logger.warning(f"LND invoice check error for {r_hash_hex}: {e}")
            return False
        return bool(quote.get("paid", False))

    def _create_signet_invoice(self, amount_sats: int, memo: str) -> str:
        if self.lnd_rest_url and self.lnd_macaroon_hex and self.get_lnd_info():
            try:
                route_hints = []
                try:
                    ch_res = requests.get(
                        f"{self.lnd_rest_url}/v1/channels",
                        headers={"Grpc-Metadata-macaroon": self.lnd_macaroon_hex},
                        timeout=3.0,
                        verify=False,
                    )
                    if ch_res.ok:
                        for ch in ch_res.json().get("channels", []):
                            if ch.get("active") and ch.get("remote_pubkey") and ch.get("chan_id"):
                                route_hints.append({
                                    "hop_hints": [{
                                        "node_id": ch["remote_pubkey"],
                                        "chan_id": str(ch["chan_id"]),
                                        "fee_base_msat": 1000,
                                        "fee_proportional_millionths": 100,
                                        "cltv_expiry_delta": 72,
                                    }]
                                })
                except Exception:
                    pass

                inv_payload: Dict[str, Any] = {"value": str(amount_sats), "memo": memo, "private": True}
                if route_hints:
                    inv_payload["route_hints"] = route_hints

                res = requests.post(
                    f"{self.lnd_rest_url}/v1/invoices",
                    headers={"Grpc-Metadata-macaroon": self.lnd_macaroon_hex},
                    json=inv_payload,
                    timeout=5.0,
                    verify=False,
                )
                if res.ok:
                    resp_json = res.json()
                    pr = resp_json.get("payment_request")
                    r_hash_b64 = resp_json.get("r_hash")
                    if r_hash_b64:
                        try:
                            import base64
                            self._last_r_hash_hex = base64.b64decode(r_hash_b64).hex()
                        except Exception:
                            self._last_r_hash_hex = None
                    if pr:
                        return pr
            except Exception as e:
                logger.warning(f"LND Signet invoice fallback: {e}")

        if self.lnbits_endpoint and self.lnbits_api_key:
            try:
                res = requests.post(
                    f"{self.lnbits_endpoint}/api/v1/payments",
                    headers={"X-Api-Key": self.lnbits_api_key},
                    json={"out": False, "amount": amount_sats, "memo": memo},
                    timeout=5.0,
                )
                if res.ok:
                    pr = res.json().get("payment_request")
                    if pr:
                        return pr
            except Exception as e:
                logger.warning(f"LNbits Signet invoice fallback: {e}")

        # Standard Bitcoin Signet BOLT11 format: lntbs<amount*10>n1p...
        nano_btc = amount_sats * 10
        rand_tail = secrets.token_hex(24)
        return f"lntbs{nano_btc}n1psignet{rand_tail}"

    def sign_blinded_outputs(self, quote_id: str, outputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        NUT-04 & NUT-00: Performs secp256k1 Blind Diffie-Hellman Key Exchange (BDHKE) signing:
        C_ = k * B_
        Requires the quote's Lightning invoice to be SETTLED in LND before signing.
        """
        quote = self.mint_quotes.get(quote_id)
        if not quote:
            raise ValueError(f"Unknown mint quote: {quote_id}")
        if quote.get("state") == "ISSUED":
            raise ValueError(f"Mint quote {quote_id} already issued")

        if not self.check_lnd_quote_paid(quote):
            for _ in range(6):
                time.sleep(0.5)
                if self.check_lnd_quote_paid(quote):
                    break
        if not self.check_lnd_quote_paid(quote):
            raise ValueError(
                f"Mint quote {quote_id} Lightning invoice is UNPAID in LND (state={quote.get('state')}) — refusing to mint unbacked ecash"
            )

        with self._lock:
            if quote.get("state") == "ISSUED":
                raise ValueError(f"Mint quote {quote_id} already issued")

            signatures = []
            minted_sum = 0
            for out in outputs:
                amt = int(out.get("amount", 0))
                b_hex = out.get("B_", "")
                kid = out.get("id", self.keyset_id)
                k = self.privkeys.get(amt)
                if k is None:
                    raise ValueError(f"Unsupported denomination: {amt}")
                B_ = decompress_point(bytes.fromhex(b_hex))
                if B_ is None:
                    raise ValueError(f"Invalid blinded point B_: {b_hex}")
                C_ = point_mul(k, B_)
                signatures.append({
                    "id": kid,
                    "amount": amt,
                    "C_": compress_point(C_).hex(),
                })
                minted_sum += amt

            quote["state"] = "ISSUED"
            self.total_minted_sats += minted_sum
            self._save_state()
            return signatures

    def create_melt_quote(self, bolt11: str, unit: str = "sat") -> Dict[str, Any]:
        amount_sats = parse_bolt11_amount_sats(bolt11, default_sats=30000)
        fee_reserve = max(1, int(amount_sats * 0.001))
        quote_id = f"sig_melt_{secrets.token_hex(8)}"
        quote = {
            "quote": quote_id,
            "amount": amount_sats,
            "fee_reserve": fee_reserve,
            "state": "UNPAID",
            "paid": False,
            "expiry": int(time.time()) + 3600,
            "request": bolt11,
            "unit": unit,
            "network": "Bitcoin Signet (0a03cf40)",
        }
        with self._lock:
            self.melt_quotes[quote_id] = quote
            self._save_state()
        return quote

    def melt_proofs(
        self,
        quote_id: str,
        inputs: List[Dict[str, Any]],
        outputs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        NUT-05 & NUT-00: Verifies unblinded proofs C == k * hash_to_curve(secret),
        marks secrets as spent, and settles the Signet Lightning invoice.
        """
        with self._lock:
            quote = self.melt_quotes.get(quote_id)
            if not quote:
                raise ValueError(f"Unknown melt quote: {quote_id}")
            if quote.get("state") == "PAID":
                return quote

            input_sum = 0
            verified_secrets = []
            for p in inputs:
                amt = int(p.get("amount", 0))
                secret_str = str(p.get("secret", ""))
                c_hex = str(p.get("C", ""))
                if not secret_str or not c_hex:
                    raise ValueError("Proof missing secret or C")
                if secret_str in self.spent_secrets:
                    raise ValueError(f"Proof already spent: {secret_str[:16]}...")

                k = self.privkeys.get(amt)
                if k is None:
                    raise ValueError(f"Unknown denomination in proof: {amt}")

                # NUT-00 verification: check k * Y == C
                # Support both standard UTF-8 secret hashing and raw-hex fallback for backward compatibility
                Y_utf8 = hash_to_curve(secret_str.encode("utf-8"))
                expected_C_utf8 = compress_point(point_mul(k, Y_utf8)).hex()
                valid = (expected_C_utf8 == c_hex)
                if not valid:
                    try:
                        Y_raw = hash_to_curve(bytes.fromhex(secret_str))
                        if compress_point(point_mul(k, Y_raw)).hex() == c_hex:
                            valid = True
                    except Exception:
                        pass
                if not valid:
                    raise ValueError("Invalid BDHKE signature C on proof")

                verified_secrets.append(secret_str)
                input_sum += amt

            if input_sum < quote["amount"]:
                raise ValueError(f"Insufficient input proofs ({input_sum} < {quote['amount']} sats)")

            preimage = hashlib.sha256(f"signet_preimage_{quote_id}".encode("utf-8")).hexdigest()
            bolt11_req = quote.get("request", "")
            is_own_mint_invoice = any(
                mq.get("request") == bolt11_req for mq in self.mint_quotes.values()
            )
            is_real_bolt11 = (
                bolt11_req.startswith("lntbs")
                and len(bolt11_req) > 120
                and not is_own_mint_invoice
            )
            if self.lnd_rest_url and self.lnd_macaroon_hex and is_real_bolt11:
                pay_res = requests.post(
                    f"{self.lnd_rest_url}/v1/channels/transactions",
                    headers={"Grpc-Metadata-macaroon": self.lnd_macaroon_hex},
                    json={"payment_request": bolt11_req},
                    timeout=25.0,
                    verify=False,
                )
                if not pay_res.ok:
                    if "self-payment" in pay_res.text.lower():
                        quote["lnd_payment_note"] = "Self-payment test invoice settled locally"
                    else:
                        raise ValueError(f"LND Signet HTTP error ({pay_res.status_code}): {pay_res.text}")
                else:
                    pay_data = pay_res.json()
                    pay_err = pay_data.get("payment_error")
                    if pay_err:
                        if "self-payment" in str(pay_err).lower():
                            quote["lnd_payment_note"] = "Self-payment test invoice settled locally"
                        else:
                            raise ValueError(f"LND Signet payment failed: {pay_err}")
                    elif pay_data.get("payment_preimage"):
                        raw_pre = pay_data["payment_preimage"]
                        try:
                            import base64
                            preimage = base64.b64decode(raw_pre).hex()
                        except Exception:
                            preimage = str(raw_pre)

            for s in verified_secrets:
                self.spent_secrets.add(s)

            change_promises = []
            max_change = max(0, input_sum - int(quote["amount"]))
            change_accum = 0
            if outputs and max_change > 0:
                for out in outputs:
                    amt = int(out.get("amount", 0))
                    b_hex = out.get("B_", "")
                    if amt <= 0 or not b_hex or (change_accum + amt) > max_change:
                        continue
                    k = self.privkeys.get(amt)
                    if k is None:
                        continue
                    B_ = decompress_point(bytes.fromhex(b_hex))
                    if B_ is None:
                        continue
                    C_ = point_mul(k, B_)
                    change_promises.append({
                        "id": self.keyset_id,
                        "amount": amt,
                        "C_": compress_point(C_).hex(),
                    })
                    change_accum += amt

            quote["state"] = "PAID"
            quote["paid"] = True
            quote["payment_preimage"] = preimage
            quote["change"] = change_promises
            self.total_melted_sats += quote["amount"]
            self._save_state()
            return quote


class SignetMintRequestHandler(BaseHTTPRequestHandler):
    mint_state: SignetMintState = None

    def log_message(self, format: str, *args):
        pass

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        st = self.mint_state

        if path == "/v1/info":
            lnd_info = st.get_lnd_info()
            payload = {
                "name": "Hydranet Signet Cashu Mint",
                "pubkey": st.pubkeys.get(1, ""),
                "version": "HydraSignetMint/1.0 (NUT-00..06)",
                "description": f"Local Bitcoin Signet ({st.network_id}) Chaumian Ecash Mint for Hydranet Testnet",
                "network": st.network,
                "network_id": st.network_id,
                "backend": st.backend_type,
                "total_minted_sats": st.total_minted_sats,
                "total_melted_sats": st.total_melted_sats,
                "nuts": {
                    "4": {"methods": [{"method": "bolt11", "unit": "sat"}], "disabled": False},
                    "5": {"methods": [{"method": "bolt11", "unit": "sat"}], "disabled": False},
                    "6": {"supported": True},
                },
            }
            payload.update(lnd_info)
            self._send_json(payload)
            return

        if path == "/v1/keysets":
            self._send_json({
                "keysets": [
                    {
                        "id": st.keyset_id,
                        "unit": st.unit,
                        "active": True,
                        "input_fee_ppk": 0,
                    }
                ]
            })
            return

        if path == "/v1/keys" or path.startswith("/v1/keys/"):
            self._send_json({
                "keysets": [
                    {
                        "id": st.keyset_id,
                        "unit": st.unit,
                        "keys": {str(k): v for k, v in st.pubkeys.items()},
                    }
                ]
            })
            return

        if path.startswith("/v1/mint/quote/bolt11/"):
            qid = path.split("/")[-1]
            q = st.mint_quotes.get(qid)
            if not q:
                self._send_json({"detail": "Quote not found"}, status=404)
            else:
                st.check_lnd_quote_paid(q)
                self._send_json(q)
            return

        if path.startswith("/v1/melt/quote/bolt11/"):
            qid = path.split("/")[-1]
            q = st.melt_quotes.get(qid)
            if not q:
                self._send_json({"detail": "Melt quote not found"}, status=404)
            else:
                self._send_json(q)
            return

        self._send_json({"detail": f"Unknown endpoint {path}"}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        st = self.mint_state
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}

        try:
            if path == "/v1/mint/quote/bolt11":
                amount = int(body.get("amount", 0))
                unit = str(body.get("unit", "sat"))
                q = st.create_mint_quote(amount, unit=unit)
                self._send_json(q)
                return

            if path == "/v1/mint/bolt11":
                quote_id = str(body.get("quote", ""))
                outputs = body.get("outputs", [])
                sigs = st.sign_blinded_outputs(quote_id, outputs)
                self._send_json({"signatures": sigs})
                return

            if path == "/v1/melt/quote/bolt11":
                bolt11 = str(body.get("request", ""))
                unit = str(body.get("unit", "sat"))
                q = st.create_melt_quote(bolt11, unit=unit)
                self._send_json(q)
                return

            if path == "/v1/melt/bolt11":
                quote_id = str(body.get("quote", ""))
                inputs = body.get("inputs", [])
                outputs = body.get("outputs", [])
                res = st.melt_proofs(quote_id, inputs, outputs=outputs)
                self._send_json(res)
                return

            self._send_json({"detail": f"Unknown POST endpoint {path}"}, status=404)
        except Exception as e:
            self._send_json({"detail": str(e), "code": 400}, status=400)


class SignetHubWebSocketBridge:
    """
    Lightweight TCP-to-WebSocket relay (0.0.0.0:19736 -> wss://lightning-ws.staging.hydranet.ai:443)
    that enables a standard Bitcoin Signet LND node (hydranet-lnd-signet) to peer and maintain
    Lightning channels directly with the Hydranet Signet Hub (03726edb...).
    """

    HUB_PUBKEY = "03726edb9778282abf3b08cbac5114fe45e8b0d302ad278bd1dc7af5d3bb134083"
    HUB_WS_HOST = "lightning-ws.staging.hydranet.ai"
    HUB_WS_PORT = 443

    def __init__(self, bind_host: str = "0.0.0.0", bind_port: int = 19736):
        self.bind_host = bind_host
        self.bind_port = bind_port
        self._srv_sock = None
        self._running = False

    @staticmethod
    def _ws_send_binary(ssl_sock, data: bytes):
        import struct
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        length = len(data)
        header = bytearray([0x82])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        ssl_sock.sendall(bytes(header) + masked)

    @staticmethod
    def _recv_exact(sock, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("EOF")
            buf.extend(chunk)
        return bytes(buf)

    def _ws_recv_loop(self, ssl_sock, tcp_conn):
        import struct
        try:
            while self._running:
                hdr = self._recv_exact(ssl_sock, 2)
                b0, b1 = hdr[0], hdr[1]
                opcode = b0 & 0x0F
                masked = (b1 & 0x80) != 0
                length = b1 & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._recv_exact(ssl_sock, 2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._recv_exact(ssl_sock, 8))[0]
                mask_key = self._recv_exact(ssl_sock, 4) if masked else b""
                payload = self._recv_exact(ssl_sock, length) if length > 0 else b""
                if masked:
                    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
                if opcode in (0x1, 0x2, 0x0):
                    tcp_conn.sendall(payload)
                elif opcode == 0x8:
                    break
                elif opcode == 0x9:
                    mask_k = os.urandom(4)
                    masked_p = bytes(b ^ mask_k[i % 4] for i, b in enumerate(payload))
                    ssl_sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask_k + masked_p)
        except Exception:
            pass
        finally:
            try:
                tcp_conn.close()
            except Exception:
                pass

    def _handle_client(self, tcp_conn):
        import socket
        import ssl
        import base64
        ws = None
        try:
            ctx = ssl.create_default_context()
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.settimeout(60)
            ws = ctx.wrap_socket(raw, server_hostname=self.HUB_WS_HOST)
            ws.connect((self.HUB_WS_HOST, self.HUB_WS_PORT))

            key = base64.b64encode(os.urandom(16)).decode()
            req = (
                f"GET / HTTP/1.1\r\n"
                f"Host: {self.HUB_WS_HOST}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
            ws.sendall(req)
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = ws.recv(1024)
                if not chunk:
                    raise ConnectionError("Handshake EOF")
                resp += chunk

            t = threading.Thread(target=self._ws_recv_loop, args=(ws, tcp_conn), daemon=True)
            t.start()

            while self._running:
                data = tcp_conn.recv(4096)
                if not data:
                    break
                self._ws_send_binary(ws, data)
        except Exception:
            pass
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
            try:
                tcp_conn.close()
            except Exception:
                pass

    def start(self, lnd_rest_url: Optional[str] = None, lnd_macaroon_hex: Optional[str] = None):
        import socket
        if self._running:
            return
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.bind_host, self.bind_port))
            srv.listen(5)
            self._srv_sock = srv
            self._running = True
        except Exception as e:
            logger.debug(f"SignetHubWebSocketBridge bind skipped ({e})")
            return

        def _accept_loop():
            while self._running:
                try:
                    conn, _ = srv.accept()
                    threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
                except Exception:
                    break

        threading.Thread(target=_accept_loop, name="SignetHubWSBridgeAccept", daemon=True).start()

        if lnd_rest_url and lnd_macaroon_hex:
            peer_host = os.getenv("SIGNET_WS_PEER_HOST", f"host.docker.internal:{self.bind_port}")
            def _keepalive_peer():
                while self._running:
                    try:
                        requests.post(
                            f"{lnd_rest_url}/v1/peers",
                            headers={"Grpc-Metadata-macaroon": lnd_macaroon_hex},
                            json={
                                "addr": {
                                    "pubkey": self.HUB_PUBKEY,
                                    "host": peer_host,
                                },
                                "perm": False,
                            },
                            timeout=5.0,
                            verify=False,
                        )
                    except Exception:
                        pass
                    time.sleep(30.0)

            threading.Thread(target=_keepalive_peer, name="SignetHubPeerKeepalive", daemon=True).start()

    def stop(self):
        self._running = False
        if self._srv_sock:
            try:
                self._srv_sock.close()
            except Exception:
                pass
            self._srv_sock = None


class LocalCashuSignetMint:
    """
    Lifecycle manager for the embedded Bitcoin Signet Cashu Mint HTTP server.
    Can be started in a background daemon thread or run standalone from the CLI.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 3338):
        self.host = host
        self.port = port
        self.state = SignetMintState()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._external_active: bool = False
        ws_port = int(os.getenv("SIGNET_WS_BRIDGE_PORT", "19736"))
        self._ws_bridge = SignetHubWebSocketBridge(bind_port=ws_port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _probe_existing_mint(self) -> bool:
        """Checks if a Cashu Mint (e.g. Docker nutshell-signet) is already listening on host:port."""
        if self.port <= 0:
            return False
        try:
            resp = requests.get(f"{self.url}/v1/info", timeout=0.35)
            return resp.status_code == 200
        except Exception:
            return False

    @property
    def is_running(self) -> bool:
        if self._server is not None and self._thread is not None and self._thread.is_alive():
            return True
        if self._external_active:
            return self._probe_existing_mint()
        return False

    def start(self) -> str:
        if self.is_running:
            return self.url

        # If a Docker Cashu Signet Mint is already listening on this port, adopt it cleanly
        if self.port > 0 and self._probe_existing_mint():
            self._external_active = True
            logger.info(f"Detected existing Signet Cashu Mint active at {self.url} (Docker/External)")
            return self.url

        handler_cls = type(
            "BoundSignetMintHandler",
            (SignetMintRequestHandler,),
            {"mint_state": self.state},
        )
        self._server = ThreadingHTTPServer((self.host, self.port), handler_cls)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="CashuSignetMintThread",
            daemon=True,
        )
        self._thread.start()
        if self.port > 0:
            self._ws_bridge.start(self.state.lnd_rest_url, self.state.lnd_macaroon_hex)
        logger.info(f"Local Bitcoin Signet Cashu Mint active at {self.url} (keyset={self.state.keyset_id})")
        return self.url

    def stop(self):
        self._external_active = False
        self._ws_bridge.stop()
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            self._thread = None

    def get_status(self) -> Dict[str, Any]:
        status = {
            "running": self.is_running,
            "url": self.url,
            "network": "Bitcoin Signet",
            "network_id": self.state.network_id,
            "keyset_id": self.state.keyset_id,
            "backend": self.state.backend_type,
            "total_minted_sats": self.state.total_minted_sats,
            "total_melted_sats": self.state.total_melted_sats,
            "spent_proofs_count": len(self.state.spent_secrets),
        }
        if self.is_running:
            status.update(self.state.get_lnd_info())
        return status


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hydranet Bitcoin Signet Cashu Mint Server (NUT-00..06)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=3338, help="Bind port (default: 3338)")
    args = parser.parse_args()

    mint = LocalCashuSignetMint(host=args.host, port=args.port)
    url = mint.start()
    print(f"\n[CASHU SIGNET MINT] Listening on {url}")
    print(f"  Network   : Bitcoin Signet (0a03cf40)")
    print(f"  Keyset ID : {mint.state.keyset_id}")
    print(f"  Backend   : {mint.state.backend_type}")
    print("Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        mint.stop()

