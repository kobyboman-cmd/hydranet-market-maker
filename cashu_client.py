"""
Cashu (NUTs) Chaumian Ecash Protocol Client for Bitcoin Lightning.
Pure Python implementation of NUT-00 through NUT-05:
- NUT-00: Cryptographic primitives (BDHKE over secp256k1, hash-to-curve, token format)
- NUT-01: Mint public keys
- NUT-02: Keysets and denominations
- NUT-04: Minting ecash via Lightning BOLT11 quotes
- NUT-05: Melting ecash to pay Lightning BOLT11 invoices
"""

import os
import json
import time
import base64
import hashlib
import secrets
import logging
import requests
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("CashuClient")

# ==================== SECP256K1 CURVE CONSTANTS ====================
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BB5BF5670AAC9C297
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
G = (Gx, Gy)


def point_add(p1: Optional[Tuple[int, int]], p2: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """Elliptic curve point addition on secp256k1."""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2:
        return None
    if x1 == x2:
        m = (3 * x1 * x1 * pow(2 * y1, P - 2, P)) % P
    else:
        m = ((y2 - y1) * pow(x2 - x1, P - 2, P)) % P
    x3 = (m * m - x1 - x2) % P
    y3 = (m * (x1 - x3) - y1) % P
    return (x3, y3)


def point_mul(k: int, p: Tuple[int, int]) -> Optional[Tuple[int, int]]:
    """Elliptic curve scalar multiplication on secp256k1."""
    res = None
    addend = p
    k = k % N
    while k:
        if k & 1:
            res = point_add(res, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return res


def decompress_point(b: bytes) -> Optional[Tuple[int, int]]:
    """Decompresses a 33-byte compressed secp256k1 public key into an (x, y) point."""
    if len(b) != 33:
        return None
    prefix = b[0]
    if prefix not in (2, 3):
        return None
    x = int.from_bytes(b[1:], "big")
    if x >= P:
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        return None
    if (prefix == 2 and y % 2 != 0) or (prefix == 3 and y % 2 == 0):
        y = P - y
    return (x, y)


def compress_point(p: Tuple[int, int]) -> bytes:
    """Serializes an (x, y) secp256k1 point into a 33-byte compressed public key."""
    prefix = 2 if p[1] % 2 == 0 else 3
    return bytes([prefix]) + p[0].to_bytes(32, "big")


def hash_to_curve(message: bytes) -> Tuple[int, int]:
    """
    NUT-00 Hash-to-curve algorithm.
    Hashes an arbitrary secret string onto a valid secp256k1 curve point Y.
    """
    domain_separator = b"Secp256k1_HashToCurve_Cashu_"
    msg_hash = hashlib.sha256(domain_separator + message).digest()
    for counter in range(65536):
        h = hashlib.sha256(msg_hash + counter.to_bytes(4, "little")).digest()
        p = decompress_point(b"\x02" + h)
        if p is not None:
            return p
    raise RuntimeError("Failed to hash message to secp256k1 curve within 65536 iterations")


def split_amount_to_powers_of_2(amount: int) -> List[int]:
    """Splits an integer satoshi amount into standard binary powers of 2."""
    if amount <= 0:
        return []
    powers = []
    bit = 1
    while bit <= amount:
        if amount & bit:
            powers.append(bit)
        bit <<= 1
    return powers


# ==================== CASHU CLIENT ====================

class CashuClient:
    """
    Cashu Ecash Protocol Client (NUT-00 to NUT-05).
    Communicates with Cashu Mints via REST API for non-custodial ecash minting and melting.
    """

    def __init__(
        self,
        mint_url: str = "https://mint.minibits.cash/Bitcoin",
        dry_run: bool = False,
        timeout: float = 15.0,
    ):
        self.mint_url = mint_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout = timeout
        self.session = requests.Session()
        self._keysets: Dict[str, Dict[str, Any]] = {}
        self._keys: Dict[str, Dict[int, str]] = {}
        self._active_keyset_id: Optional[str] = None

    def set_mint_url(self, mint_url: str):
        """Updates the target Cashu Mint URL and resets cached keyset metadata."""
        self.mint_url = mint_url.rstrip("/")
        self._keysets.clear()
        self._keys.clear()
        self._active_keyset_id = None

    def get_info(self) -> Dict[str, Any]:
        """NUT-06: Retrieves Mint information and supported NUT specifications."""
        if self.dry_run:
            return {
                "name": "Simulated Cashu Mint",
                "version": "simulated/1.0",
                "description": "Simulated local Cashu mint for dry-run testing",
                "nuts": {"4": {"methods": [{"method": "bolt11", "unit": "sat"}]}},
            }
        url = f"{self.mint_url}/v1/info"
        res = self.session.get(url, timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def get_keysets(self) -> List[Dict[str, Any]]:
        """NUT-02: Retrieves available keysets from the Mint."""
        if self.dry_run:
            sim_id = "009a1f293252e123"
            self._active_keyset_id = sim_id
            return [{"id": sim_id, "unit": "sat", "active": True}]
        url = f"{self.mint_url}/v1/keysets"
        res = self.session.get(url, timeout=self.timeout)
        res.raise_for_status()
        data = res.json()
        keysets = data.get("keysets", [])
        for ks in keysets:
            kid = ks.get("id")
            if kid:
                self._keysets[kid] = ks
                if ks.get("active") and ks.get("unit") == "sat":
                    self._active_keyset_id = kid
        return keysets

    def get_keys(self) -> Dict[str, Dict[int, str]]:
        """NUT-01: Retrieves public keys for all active keysets from the Mint."""
        if self.dry_run:
            sim_id = self._active_keyset_id or "009a1f293252e123"
            # Simulated mint keys for powers of 2
            sim_keys = {
                2**i: compress_point(point_mul(100 + i, G)).hex()
                for i in range(20)
            }
            self._keys[sim_id] = sim_keys
            return {sim_id: sim_keys}

        url = f"{self.mint_url}/v1/keys"
        res = self.session.get(url, timeout=self.timeout)
        res.raise_for_status()
        data = res.json()
        raw_keysets = data.get("keysets", [])
        parsed: Dict[str, Dict[int, str]] = {}
        for ks in raw_keysets:
            kid = ks.get("id")
            raw_keys = ks.get("keys", {})
            int_keys = {int(amt): pubkey for amt, pubkey in raw_keys.items()}
            parsed[kid] = int_keys
            self._keys[kid] = int_keys
            if ks.get("unit") == "sat" and not self._active_keyset_id:
                self._active_keyset_id = kid
        return parsed

    def _ensure_keyset(self) -> str:
        """Ensures an active satoshi keyset is loaded and returns its ID."""
        if not self._active_keyset_id or self._active_keyset_id not in self._keys:
            self.get_keysets()
            self.get_keys()
        if not self._active_keyset_id:
            raise RuntimeError(f"No active satoshi keyset found at mint {self.mint_url}")
        return self._active_keyset_id

    # ==================== NUT-04: MINTING (SWEEP / SINK) ====================

    def request_mint_quote(self, amount_sats: int, unit: str = "sat") -> Dict[str, Any]:
        """
        NUT-04: Requests a Lightning invoice from the Mint to deposit/sweep sats into Ecash.
        Returns quote ID and BOLT11 invoice payment request.
        """
        if amount_sats <= 0:
            raise ValueError(f"Invalid mint amount: {amount_sats} sats")

        if self.dry_run:
            sim_quote = f"sim_quote_{int(time.time() * 1000)}"
            return {
                "quote": sim_quote,
                "request": f"lnbc{amount_sats}00u1psimulatedpaymentrequest",
                "amount": amount_sats,
                "unit": unit,
                "state": "UNPAID",
                "expiry": int(time.time() + 3600),
            }

        url = f"{self.mint_url}/v1/mint/quote/bolt11"
        payload = {"amount": amount_sats, "unit": unit}
        res = self.session.post(url, json=payload, timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def check_mint_quote(self, quote_id: str) -> Dict[str, Any]:
        """NUT-04: Checks the payment state of a mint quote."""
        if self.dry_run:
            return {"quote": quote_id, "state": "PAID", "paid": True}

        url = f"{self.mint_url}/v1/mint/quote/bolt11/{quote_id}"
        res = self.session.get(url, timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def mint(self, quote_id: str, amount_sats: int) -> List[Dict[str, Any]]:
        """
        NUT-04: Blinds secrets, requests blind signatures from the Mint for a paid quote,
        unblinds them, and returns standard unblinded Proofs.
        """
        kid = self._ensure_keyset()
        mint_keys = self._keys.get(kid, {})
        denominations = split_amount_to_powers_of_2(amount_sats)

        # 1. Blind secrets for each denomination
        blinded_outputs = []
        secrets_data = []

        for amt in denominations:
            sec_bytes = secrets.token_bytes(32)
            sec_hex = sec_bytes.hex()
            r = 1 + secrets.randbelow(N - 1)
            # NUT-00: Hash the UTF-8 secret string onto secp256k1
            Y = hash_to_curve(sec_hex.encode("utf-8"))
            rG = point_mul(r, G)
            B_ = point_add(Y, rG)
            if B_ is None:
                continue
            B_hex = compress_point(B_).hex()
            blinded_outputs.append({
                "amount": amt,
                "id": kid,
                "B_": B_hex,
            })
            secrets_data.append({
                "amount": amt,
                "id": kid,
                "secret": sec_hex,
                "r": r,
                "B_": B_hex,
            })

        if self.dry_run:
            # Simulate unblinded proofs
            proofs = []
            for item in secrets_data:
                proofs.append({
                    "id": item["id"],
                    "amount": item["amount"],
                    "secret": item["secret"],
                    "C": compress_point(point_mul(12345, G)).hex(),
                })
            return proofs

        # 2. Submit blinded outputs to Mint (with FakeWallet quote state check retry)
        url = f"{self.mint_url}/v1/mint/bolt11"
        payload = {"quote": quote_id, "outputs": blinded_outputs}
        res = self.session.post(url, json=payload, timeout=self.timeout)
        if not res.ok:
            for _ in range(3):
                try:
                    st = self.check_mint_quote(quote_id)
                    if st.get("state") == "PAID" or st.get("paid"):
                        break
                except Exception:
                    pass
                time.sleep(0.35)
            res = self.session.post(url, json=payload, timeout=self.timeout)
        res.raise_for_status()
        data = res.json()

        signatures = data.get("signatures", [])
        if len(signatures) != len(secrets_data):
            raise RuntimeError(f"Mint returned {len(signatures)} signatures, expected {len(secrets_data)}")

        # 3. Unblind signatures: C = C_ - rK
        proofs = []
        for sig, sdata in zip(signatures, secrets_data):
            c_prime_hex = sig.get("C_")
            amt = sig.get("amount")
            C_prime = decompress_point(bytes.fromhex(c_prime_hex))
            mint_pubkey_hex = mint_keys.get(amt)
            if not mint_pubkey_hex:
                raise RuntimeError(f"Mint public key missing for denomination {amt} sats")
            K = decompress_point(bytes.fromhex(mint_pubkey_hex))
            if C_prime is None or K is None:
                raise RuntimeError("Failed to decompress point during unblinding")

            rK = point_mul(sdata["r"], K)
            rK_neg = (rK[0], (P - rK[1]) % P) if rK else None
            C = point_add(C_prime, rK_neg)
            if C is None:
                raise RuntimeError("Point addition produced identity during unblinding")

            proofs.append({
                "id": kid,
                "amount": amt,
                "secret": sdata["secret"],
                "C": compress_point(C).hex(),
            })

        logger.info(f"Successfully minted {len(proofs)} ecash proofs totaling {amount_sats:,} sats")
        return proofs

    # ==================== NUT-05: MELTING (REFUEL) ====================

    def request_melt_quote(self, bolt11: str, unit: str = "sat") -> Dict[str, Any]:
        """
        NUT-05: Requests a melt quote from the Mint to pay a Lightning invoice (channel refuel).
        Returns quote ID, required amount in sats, and fee reserve.
        """
        if self.dry_run:
            return {
                "quote": f"sim_melt_{int(time.time() * 1000)}",
                "amount": 30000,
                "fee_reserve": 100,
                "paid": False,
                "state": "UNPAID",
            }

        url = f"{self.mint_url}/v1/melt/quote/bolt11"
        payload = {"request": bolt11, "unit": unit}
        res = self.session.post(url, json=payload, timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def check_melt_quote(self, quote_id: str) -> Dict[str, Any]:
        """NUT-05: Checks the state of a melt quote."""
        if self.dry_run:
            return {"quote": quote_id, "paid": True, "state": "PAID", "payment_preimage": "sim_preimage_123"}

        url = f"{self.mint_url}/v1/melt/quote/bolt11/{quote_id}"
        res = self.session.get(url, timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def melt(
        self,
        quote_id: str,
        proofs: List[Dict[str, Any]],
        target_amount_sats: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        NUT-05: Melts ecash proofs via the Mint to settle a Lightning invoice.
        Automatically generates blinded change outputs when input proofs exceed target_amount_sats,
        and unblinds any returned change signatures.
        """
        if self.dry_run:
            return {
                "paid": True,
                "payment_preimage": "sim_preimage_cashu_melt",
                "change": [],
                "simulated": True,
            }

        input_sum = sum(int(p.get("amount", 0)) for p in proofs)
        blinded_outputs = []
        secrets_data = []
        kid = None
        mint_keys = {}

        if target_amount_sats is not None and input_sum > target_amount_sats:
            try:
                kid = self._ensure_keyset()
                mint_keys = self._keys.get(kid, {})
                change_sats = input_sum - target_amount_sats
                for amt in split_amount_to_powers_of_2(change_sats):
                    sec_hex = secrets.token_bytes(32).hex()
                    r = 1 + secrets.randbelow(N - 1)
                    Y = hash_to_curve(sec_hex.encode("utf-8"))
                    rG = point_mul(r, G)
                    B_ = point_add(Y, rG)
                    if B_ is None:
                        continue
                    B_hex = compress_point(B_).hex()
                    blinded_outputs.append({"amount": amt, "id": kid, "B_": B_hex})
                    secrets_data.append({"amount": amt, "id": kid, "secret": sec_hex, "r": r, "B_": B_hex})
            except Exception as e:
                logger.debug(f"Could not prepare change outputs for melt: {e}")

        url = f"{self.mint_url}/v1/melt/bolt11"
        payload: Dict[str, Any] = {"quote": quote_id, "inputs": proofs}
        if blinded_outputs:
            payload["outputs"] = blinded_outputs
        res = self.session.post(url, json=payload, timeout=self.timeout)
        res.raise_for_status()
        data = res.json()

        raw_change = data.get("change", [])
        if raw_change and secrets_data and kid:
            unblinded_change = []
            for sig, sdata in zip(raw_change, secrets_data):
                c_prime_hex = sig.get("C_")
                amt = sig.get("amount")
                if not c_prime_hex or not amt:
                    continue
                C_prime = decompress_point(bytes.fromhex(c_prime_hex))
                mint_pubkey_hex = mint_keys.get(amt)
                if not mint_pubkey_hex or C_prime is None:
                    continue
                K = decompress_point(bytes.fromhex(mint_pubkey_hex))
                if K is None:
                    continue
                rK = point_mul(sdata["r"], K)
                rK_neg = (rK[0], (P - rK[1]) % P) if rK else None
                C = point_add(C_prime, rK_neg)
                if C is not None:
                    unblinded_change.append({
                        "id": kid,
                        "amount": amt,
                        "secret": sdata["secret"],
                        "C": compress_point(C).hex(),
                    })
            data["change"] = unblinded_change
        return data

    # ==================== TOKEN SERIALIZATION (NUT-00) ====================

    def encode_token_v3(self, proofs: List[Dict[str, Any]]) -> str:
        """Serializes proofs into a standard Cashu V3 token string (cashuA...)."""
        token_obj = {
            "token": [
                {
                    "mint": self.mint_url,
                    "proofs": proofs,
                }
            ]
        }
        json_bytes = json.dumps(token_obj, separators=(",", ":")).encode("utf-8")
        b64 = base64.urlsafe_b64encode(json_bytes).decode("utf-8").rstrip("=")
        return f"cashuA{b64}"

    @staticmethod
    def decode_token_v3(token_str: str) -> Dict[str, Any]:
        """Parses a Cashu V3 token string (cashuA...) back into token dictionary."""
        if not token_str.startswith("cashuA"):
            raise ValueError("Invalid token format: must start with cashuA")
        raw_b64 = token_str[6:]
        padding = 4 - (len(raw_b64) % 4)
        if padding != 4:
            raw_b64 += "=" * padding
        json_bytes = base64.urlsafe_b64decode(raw_b64.encode("utf-8"))
        return json.loads(json_bytes.decode("utf-8"))
