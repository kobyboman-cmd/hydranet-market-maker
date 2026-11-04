"""
Hydranet DEX JSON-RPC Client.
Provides a robust Python interface to the local Hydranet node (port 5003),
including Orderbook, Pricing, Wallet, Channels (Watch-Only), and Liquidity Lease/Rental APIs.
"""

import time
import base64
import requests
from typing import Dict, Any, Optional, List


class HydraRPCError(Exception):
    """Exception raised when the Hydranet node returns a JSON-RPC error."""
    def __init__(self, code: int, message: str, data: Any = None):
        # Sanitize message to pure ASCII so Windows cp1252 consoles never fail on rust box-drawing chars
        clean_msg = message.encode("ascii", "replace").decode("ascii") if isinstance(message, str) else str(message)
        super().__init__(f"Hydra RPC Error [{code}]: {clean_msg} (data: {data})")
        self.code = code
        self.message = clean_msg
        self.data = data


class HydraClient:
    def __init__(self, base_url: str = "http://localhost:5003", timeout: float = 35.0):
        # Prefer 127.0.0.1 over localhost on Windows to avoid 2s IPv6 ::1 SYN timeout when node is offline
        normalized_url = base_url.rstrip("/").replace("http://localhost:", "http://127.0.0.1:")
        self.base_url = normalized_url
        self.endpoint = f"{self.base_url}/jsonrpc"
        self.timeout = timeout
        self._session = requests.Session()
        self._oracle_price_cache: Dict[str, float] = {}
        self._offline_until: float = 0.0
        self.awaiting_whitelist: bool = False
        self.identity_pubkey_hex: Optional[str] = None

    def get_identity_public_key(self) -> Optional[str]:
        """
        Fetches the node's Ed25519 identity public key (64 hex chars) via app_getPublicKey.
        Works even before the node identity is whitelisted on Mainnet or Staging.
        """
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "app_getPublicKey",
                "params": {},
                "id": int(time.time() * 1000),
            }
            res = self._session.post(self.base_url, json=payload, timeout=(0.35, 2.0))
            res.raise_for_status()
            b64_key = res.json().get("result", {}).get("publicKey", "")
            if b64_key:
                self.identity_pubkey_hex = base64.b64decode(b64_key).hex()
                return self.identity_pubkey_hex
        except Exception:
            pass
        return self.identity_pubkey_hex

    def redeem_invite(self, invite_code: str) -> Dict[str, Any]:
        """Redeems a Hydranet invite code on this node via app_redeemInvite."""
        payload = {
            "jsonrpc": "2.0",
            "method": "app_redeemInvite",
            "params": {"request": {"code": invite_code.strip()}},
            "id": int(time.time() * 1000),
        }
        res = self._session.post(self.base_url, json=payload, timeout=(1.0, 10.0))
        res.raise_for_status()
        data = res.json()
        if "error" in data and data["error"]:
            err = data["error"]
            raise HydraRPCError(err.get("code", -1), err.get("message", "Unknown error"), err.get("data"))
        self.awaiting_whitelist = False
        self._offline_until = 0.0
        return data.get("result", {})

    def call(
        self,
        method: str,
        request_params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Executes a JSON-RPC 2.0 call to the Hydranet node.
        Hydranet requires method format: <namespace>_<methodName>
        and params wrapped in: {"request": {...}}
        """
        if time.time() < self._offline_until:
            raise ConnectionError(f"Hydranet node on {self.base_url} is offline (fast-fail circuit breaker)")

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {
                "request": request_params or {}
            },
            "id": int(time.time() * 1000)
        }

        call_timeout = timeout or self.timeout
        try:
            req_timeout = (0.35, call_timeout) if isinstance(call_timeout, (int, float)) else call_timeout
            res = self._session.post(self.endpoint, json=payload, timeout=req_timeout)
            if res.status_code == 404 and self.endpoint != self.base_url:
                res = self._session.post(self.base_url, json=payload, timeout=req_timeout)
            res.raise_for_status()
            data = res.json()
            self._offline_until = 0.0
        except requests.exceptions.ConnectionError as e:
            self._offline_until = time.time() + 15.0
            raise ConnectionError(f"Failed to communicate with Hydranet node on {self.base_url}: {e}") from e
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Failed to communicate with Hydranet node on {self.base_url}: {e}") from e

        if "error" in data and data["error"]:
            err = data["error"]
            code = err.get("code", -1)
            msg = err.get("message", "Unknown error")
            # When hydra-app is running in pre-whitelist mode ("Identity <hex> is not whitelisted"),
            # non-app_* RPC methods return -32601 Method not found until RedeemInvite / whitelisting completes.
            if code == -32601 and not method.startswith("app_"):
                self.awaiting_whitelist = True
                if not self.identity_pubkey_hex:
                    self.get_identity_public_key()
                self._offline_until = time.time() + 10.0
            raise HydraRPCError(
                code=code,
                message=msg,
                data=err.get("data")
            )

        self.awaiting_whitelist = False
        return data.get("result", {})

    # ==================== ORDERBOOK SERVICES ====================

    def get_markets_info(self) -> List[Dict[str, Any]]:
        """Returns metadata for all markets available on the DEX."""
        res = self.call("orderbook_getMarketsInfo")
        return res.get("markets", [])

    def get_initialized_markets(self) -> List[Dict[str, Any]]:
        """Returns the list of currently initialized markets on this node."""
        res = self.call("orderbook_getInitializedMarkets")
        return res.get("markets", [])

    def init_market(
        self,
        base_currency: Dict[str, str],
        quote_currency: Dict[str, str],
        timeout: float = 25.0
    ) -> Dict[str, Any]:
        """Initializes a market pair on the node so it can actively stream and match orders."""
        return self.call("orderbook_initMarket", {
            "firstCurrency": base_currency,
            "otherCurrency": quote_currency
        }, timeout=timeout)

    def get_currencies(self) -> List[Dict[str, Any]]:
        """Returns the allowlisted currencies supported by the DEX."""
        res = self.call("orderbook_getCurrencies")
        return res.get("currencies", [])

    def get_orderbook(self, base_currency: Dict[str, str], quote_currency: Dict[str, str]) -> Dict[str, Any]:
        """Returns orderbook snapshot for the given market pair."""
        res = self.call("orderbook_getOrderbook", {
            "base": base_currency,
            "quote": quote_currency
        })
        return res.get("orderbook", {})

    def get_orderbook_balances(self) -> List[Dict[str, Any]]:
        """Returns DEX channel balances (sending, receiving, in-use) for all assets."""
        res = self.call("orderbook_getOrderbookBalances")
        return res.get("balances", [])

    def get_own_orders(self, base_currency: Dict[str, str], quote_currency: Dict[str, str]) -> Dict[str, Any]:
        """Returns own open orders for a specific market pair."""
        res = self.call("orderbook_getOwnOrders", {
            "base": base_currency,
            "quote": quote_currency
        })
        return res.get("orders", {})

    def get_all_own_orders(self) -> Dict[str, Any]:
        """Returns all own open orders across all markets."""
        res = self.call("orderbook_getAllOwnOrders")
        return res.get("orders", {})

    def get_trade_history(
        self, base_currency: Dict[str, str], quote_currency: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        """Returns public trade history for the specified market."""
        res = self.call("orderbook_getTradeHistory", {
            "base": base_currency,
            "quote": quote_currency
        })
        if isinstance(res, list):
            return res
        return res.get("trades", res.get("tradeHistory", []))

    def get_market_fee_rates(
        self, base_currency: Dict[str, str], quote_currency: Dict[str, str]
    ) -> Dict[str, Any]:
        """Returns effective fee rates (maker/taker) for this node."""
        res = self.call("orderbook_getMarketFeeRates", {
            "firstCurrency": base_currency,
            "otherCurrency": quote_currency
        })
        return res.get("feeRates", {})

    def create_limit_order(
        self,
        base_currency: Dict[str, str],
        quote_currency: Dict[str, str],
        side: str,
        price: str,
        amount_base: str,
        client_order_id: Optional[str] = None
    ) -> str:
        """Creates and places a limit maker order on Hydranet state channels."""
        side_enum = "ORDER_SIDE_BUY" if side.upper() == "BUY" else "ORDER_SIDE_SELL"
        req = {
            "orderVariant": {
                "limitOrder": {
                    "base": base_currency,
                    "quote": quote_currency,
                    "side": side_enum,
                    "price": {"value": str(price)},
                    "amount": {
                        "base": {
                            "amount": {"value": str(amount_base)}
                        }
                    }
                }
            }
        }
        if client_order_id:
            req["clientOrderId"] = client_order_id

        res = self.call("orderbook_createOrder", req)
        return res.get("orderId", "")

    def estimate_order(
        self,
        base_currency: Dict[str, str],
        quote_currency: Dict[str, str],
        side: str,
        price: str,
        amount_base: str
    ) -> Dict[str, Any]:
        """Estimates order matching without placing the order on the book."""
        side_enum = "ORDER_SIDE_BUY" if side.upper() == "BUY" else "ORDER_SIDE_SELL"
        req = {
            "orderVariant": {
                "limitOrder": {
                    "base": base_currency,
                    "quote": quote_currency,
                    "side": side_enum,
                    "price": {"value": str(price)},
                    "amount": {
                        "base": {
                            "amount": {"value": str(amount_base)}
                        }
                    }
                }
            }
        }
        return self.call("orderbook_estimateOrder", req)

    def cancel_order(self, order_id: str) -> bool:
        """Cancels a specific open order by order_id."""
        res = self.call("orderbook_cancelOrder", {"orderId": order_id})
        return res.get("removed", True)

    def cancel_all_orders(self) -> None:
        """Cancels all active orders placed by this node."""
        self.call("orderbook_cancelAllOrders")

    def get_fee_discount(self) -> Dict[str, Any]:
        """Queries the node's fee discount standing, volume tier, and next tier requirements."""
        return self.call("orderbook_getFeeDiscount", {})

    def get_market_fee_rates(
        self,
        first_currency: Dict[str, str],
        other_currency: Dict[str, str]
    ) -> Dict[str, Any]:
        """Queries the effective fee rates for a specific trading pair including active discounts/rebates."""
        req = {
            "firstCurrency": {
                "protocol": first_currency["protocol"],
                "networkId": first_currency.get("network_id") or first_currency.get("networkId"),
                "assetId": first_currency.get("asset_id") or first_currency.get("assetId")
            },
            "otherCurrency": {
                "protocol": other_currency["protocol"],
                "networkId": other_currency.get("network_id") or other_currency.get("networkId"),
                "assetId": other_currency.get("asset_id") or other_currency.get("assetId")
            }
        }
        return self.call("orderbook_getMarketFeeRates", req)

    # ==================== PRICING SERVICES ====================

    def get_fiat_price(self, symbol: str, fiat_currency: str = "FIAT_CURRENCY_USD") -> Optional[float]:
        """
        Queries the node's price oracle for the current fiat price of an asset.
        Caches the response so that subsequent fallbacks always reference the node's oracle
        rather than hardcoded values.
        """
        try:
            res = self.call(
                "pricing_getAssetFiatPriceBySymbol",
                {
                    "assetSymbol": symbol,
                    "fiatCurrency": fiat_currency,
                },
                timeout=1.5,
            )
            val = res.get("price", {}).get("value")
            if val:
                f_val = float(val)
                if f_val > 0:
                    self._oracle_price_cache[symbol] = f_val
                    return f_val
        except Exception:
            pass

        return self._oracle_price_cache.get(symbol)

    def get_all_cached_fiat_prices(self) -> Dict[str, float]:
        """Returns the dictionary of all live prices retrieved from the node's price oracle."""
        return dict(self._oracle_price_cache)

    def refresh_oracle_prices(self, symbols: Optional[List[str]] = None) -> Dict[str, float]:
        """
        Pre-fetches fiat prices from the hydra-app oracle for all trading symbols.
        Guarantees that the oracle cache is primed from the hydra-app at all times.
        """
        target_symbols = symbols or ["BTC", "ETH", "USDC", "HDN"]
        for sym in target_symbols:
            self.get_fiat_price(sym)
        return dict(self._oracle_price_cache)

    # ==================== WALLET SERVICES ====================

    def get_wallet_balances(self, protocol: str, network_id: str) -> Dict[str, Any]:
        """Returns on-chain and off-chain wallet balances for a network."""
        res = self.call("wallet_getBalances", {
            "network": {
                "protocol": protocol,
                "id": network_id
            }
        })
        return res.get("balances", {})

    # ==================== WATCH-ONLY NODE (CHANNELS) ====================

    def get_channels(self, protocol: str, network_id: str) -> List[Dict[str, Any]]:
        """Returns all state channels associated with the node on the specified network."""
        res = self.call("watchOnlyNode_getChannels", {
            "network": {
                "protocol": protocol,
                "id": network_id
            }
        })
        return res.get("channels", [])

    def get_node_id(self, protocol: str, network_id: str) -> str:
        """Returns local node's public key (node ID) for the specified network."""
        res = self.call("watchOnlyNode_getNodeId", {
            "network": {
                "protocol": protocol,
                "id": network_id
            }
        })
        return res.get("nodeId", "")

    # ==================== LIQUIDITY SERVICE (LEASE / RENTAL API) ====================

    def get_liquidity_service_info(self) -> Dict[str, Any]:
        """Returns liquidity service capacity bounds, allowed durations, and fee configs."""
        return self.call("liquidity_getLiquidityServiceInfo")

    def get_leaseable_asset_info(
        self, protocol: str, network_id: str, asset_id: str
    ) -> Dict[str, Any]:
        """Returns liquidity bounds and fee ratio for a specific asset on the Liquidity Hub."""
        return self.call("liquidity_getLeaseableAssetInfo", {
            "network": {"protocol": protocol, "id": network_id},
            "assetId": asset_id
        })

    def get_leases(self, protocol: str, network_id: str) -> List[Dict[str, Any]]:
        """Returns active client liquidity leases on the specified network."""
        res = self.call("liquidity_getLeases", {
            "network": {"protocol": protocol, "id": network_id}
        })
        return res.get("leases", [])

    def estimate_request_channel_liquidity_fee(
        self,
        network: Dict[str, str],
        operation: Dict[str, Any],
        duration_seconds: int,
        payment_network: Dict[str, str],
        payment_asset_id: str,
        fee_payment_type: str = "offchainFeePayment"
    ) -> Dict[str, Any]:
        """Estimates the service fee for an inbound channel liquidity lease request."""
        req = {
            "network": network,
            "operation": operation,
            "leaseDurationSeconds": str(duration_seconds),
            "paymentNetwork": payment_network,
            "paymentAssetId": payment_asset_id,
            fee_payment_type: {}
        }
        return self.call("liquidity_estimateRequestChannelLiquidityFee", req, timeout=15.0)

    def request_channel_liquidity(
        self,
        network: Dict[str, str],
        operation: Dict[str, Any],
        duration_seconds: int,
        payment_network: Dict[str, str],
        payment_asset_id: str,
        fee_payment_type: str = "offchainFeePayment",
        quote_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Executes an inbound channel liquidity lease request on the Liquidity Hub."""
        req = {
            "network": network,
            "operation": operation,
            "leaseDurationSeconds": str(duration_seconds),
            "paymentNetwork": payment_network,
            "paymentAssetId": payment_asset_id,
            fee_payment_type: {}
        }
        if quote_id:
            req["quoteId"] = quote_id
        return self.call("liquidity_requestChannelLiquidity", req, timeout=60.0)

    def estimate_request_channel_release_fee(
        self,
        network: Dict[str, str],
        operation: Dict[str, Any],
        payment_network: Dict[str, str],
        payment_asset_id: str,
        fee_payment_type: str = "offchainFeePayment"
    ) -> Dict[str, Any]:
        """Estimates fee to withdraw or cooperatively close a leased channel."""
        req = {
            "network": network,
            "operation": operation,
            "paymentNetwork": payment_network,
            "paymentAssetId": payment_asset_id,
            fee_payment_type: {}
        }
        return self.call("liquidity_estimateRequestChannelReleaseFee", req, timeout=15.0)

    def request_channel_release(
        self,
        network: Dict[str, str],
        operation: Dict[str, Any],
        payment_network: Dict[str, str],
        payment_asset_id: str,
        fee_payment_type: str = "offchainFeePayment",
        quote_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Executes a cooperative channel lease release or withdrawal."""
        req = {
            "network": network,
            "operation": operation,
            "paymentNetwork": payment_network,
            "paymentAssetId": payment_asset_id,
            fee_payment_type: {}
        }
        if quote_id:
            req["quoteId"] = quote_id
        return self.call("liquidity_requestChannelRelease", req, timeout=60.0)

    # ==================== NODE SERVICE (CHANNEL LIFECYCLE) ====================

    def estimate_open_channel_fee(
        self,
        network: Dict[str, str],
        node_id: str,
        asset_amounts: Dict[str, str],
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """Estimates on-chain fee for opening a channel with a peer."""
        req = {
            "network": network,
            "nodeId": node_id,
            "assetAmounts": {k: {"exact": {"amount": {"value": str(v)}}} for k, v in asset_amounts.items()},
            "feeOption": {fee_option: {}}
        }
        return self.call("node_estimateOpenChannelFee", req, timeout=30.0)

    def open_channel(
        self,
        network: Dict[str, str],
        node_id: str,
        asset_amounts: Dict[str, str],
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """Opens and funds a payment channel with a peer on the given network."""
        req = {
            "network": network,
            "nodeId": node_id,
            "assetAmounts": {k: {"exact": {"amount": {"value": str(v)}}} for k, v in asset_amounts.items()},
            "feeOption": {fee_option: {}}
        }
        return self.call("node_openChannel", req, timeout=60.0)

    def estimate_deposit_channel_fee(
        self,
        network: Dict[str, str],
        channel_id: str,
        asset_amounts: Dict[str, str],
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """Estimates on-chain fee for depositing funds into an existing payment channel."""
        req = {
            "network": network,
            "channelId": channel_id,
            "assetAmounts": {k: {"exact": {"amount": {"value": str(v)}}} for k, v in asset_amounts.items()},
            "feeOption": {fee_option: {}}
        }
        return self.call("node_estimateDepositChannelFee", req, timeout=30.0)

    def deposit_channel(
        self,
        network: Dict[str, str],
        channel_id: str,
        asset_amounts: Dict[str, str],
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """Deposits on-chain funds into an existing payment channel."""
        req = {
            "network": network,
            "channelId": channel_id,
            "assetAmounts": {k: {"exact": {"amount": {"value": str(v)}}} for k, v in asset_amounts.items()},
            "feeOption": {fee_option: {}}
        }
        return self.call("node_depositChannel", req, timeout=60.0)

    def estimate_withdraw_channel_fee(
        self,
        network: Dict[str, str],
        channel_id: str,
        asset_amounts: Dict[str, str],
        counterparty_amounts: Optional[Dict[str, str]] = None,
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """Estimates on-chain fee for withdrawing assets from an existing payment channel."""
        c_amounts = counterparty_amounts or {k: "0" for k in asset_amounts.keys()}
        req = {
            "network": network,
            "channelId": channel_id,
            "assetAmounts": {
                k: {
                    "selfWithdrawal": {"exact": {"amount": {"value": str(v)}}},
                    "counterpartyWithdrawal": {"exact": {"amount": {"value": str(c_amounts.get(k, "0"))}}}
                }
                for k, v in asset_amounts.items()
            },
            "feeOption": {fee_option: {}}
        }
        return self.call("node_estimateWithdrawChannelFee", req, timeout=30.0)

    def withdraw_channel(
        self,
        network: Dict[str, str],
        channel_id: str,
        asset_amounts: Dict[str, str],
        counterparty_amounts: Optional[Dict[str, str]] = None,
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """Withdraws assets from an existing payment channel to the on-chain wallet."""
        c_amounts = counterparty_amounts or {k: "0" for k in asset_amounts.keys()}
        req = {
            "network": network,
            "channelId": channel_id,
            "assetAmounts": {
                k: {
                    "selfWithdrawal": {"exact": {"amount": {"value": str(v)}}},
                    "counterpartyWithdrawal": {"exact": {"amount": {"value": str(c_amounts.get(k, "0"))}}}
                }
                for k, v in asset_amounts.items()
            },
            "feeOption": {fee_option: {}}
        }
        return self.call("node_withdrawChannel", req, timeout=60.0)


    def estimate_close_channel_fee(
        self,
        network: Dict[str, str],
        channel_id: str,
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """Estimates on-chain fee for cooperatively closing a channel."""
        req = {
            "network": network,
            "channelId": channel_id,
            "feeOption": {fee_option: {}}
        }
        return self.call("node_estimateCloseChannelFee", req, timeout=30.0)

    def close_channel(
        self,
        network: Dict[str, str],
        channel_id: str,
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """Cooperatively closes an existing channel."""
        req = {
            "network": network,
            "channelId": channel_id,
            "feeOption": {fee_option: {}}
        }
        return self.call("node_closeChannel", req, timeout=60.0)

    # ==================== BLOCKCHAIN & TOKEN ALLOWANCE SERVICES ====================

    def get_token_allowance(
        self,
        network: Dict[str, str],
        owner: str,
        spender: str,
        token_id: str
    ) -> Dict[str, Any]:
        """Queries the on-chain token allowance approved for a spender."""
        req = {
            "network": network,
            "owner": owner,
            "spender": spender,
            "tokenId": token_id
        }
        return self.call("blockchain_getTokenAllowance", req)

    def set_token_allowance(
        self,
        network: Dict[str, str],
        spender: str,
        token_id: str,
        amount: str = "unlimited",
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """Sets on-chain token allowance (approve) for a spender contract."""
        if str(amount).lower() == "unlimited":
            amt_spec = {"unlimited": {}}
        else:
            amt_spec = {"exact": {"amount": {"value": str(amount)}}}

        req = {
            "network": network,
            "spender": spender,
            "allowance": {
                "token": {
                    "tokenId": token_id,
                    "amount": amt_spec
                }
            },
            "feeOption": {fee_option: {}}
        }
        return self.call("client_setTokenAllowance", req, timeout=30.0)

    def send_transaction(
        self,
        network: Dict[str, str],
        to: str,
        amount: Dict[str, Any],
        fee_option: str = "medium"
    ) -> Dict[str, Any]:
        """
        Sends an on-chain native asset transaction (e.g. BTC or ETH) to an external address.
        Returns response dict containing the broadcasted 'txid'.
        """
        req = {
            "network": network,
            "to": to,
            "amount": amount,
            "feeOption": {fee_option: {}}
        }
        return self.call("client_sendTransaction", req, timeout=60.0)

    # ==================== PEER CONNECTION MANAGEMENT ====================

    def connect_to_peer(self, protocol: str, network_id: str, peer_url: str) -> Dict[str, Any]:
        """Connects the local node to a remote peer on the given network."""
        req = {
            "network": {
                "protocol": protocol,
                "id": network_id
            },
            "peerUrl": peer_url
        }
        return self.call("node_connectToPeer", req, timeout=15.0)

    def disconnect_from_peer(self, protocol: str, network_id: str, node_id: str) -> Dict[str, Any]:
        """Disconnects the local node from a remote peer on the given network."""
        req = {
            "network": {
                "protocol": protocol,
                "id": network_id
            },
            "nodeId": node_id
        }
        return self.call("node_disconnectFromPeer", req)

    def is_peer_connected(self, protocol: str, network_id: str, node_id: str) -> bool:
        """Checks if a specific peer is currently connected on the given network."""
        req = {
            "network": {
                "protocol": protocol,
                "id": network_id
            },
            "nodeId": node_id
        }
        res = self.call("node_isPeerConnected", req)
        return bool(res.get("isConnected", False))

    def get_connected_peers(self, protocol: str, network_id: str) -> List[str]:
        """Returns the list of connected peer node IDs on the given network."""
        req = {
            "network": {
                "protocol": protocol,
                "id": network_id
            }
        }
        res = self.call("node_getConnectedPeers", req)
        return res.get("nodeIds", [])

    # ==================== LIGHTNING NETWORK SERVICES ====================

    def create_lightning_invoice(
        self,
        amount_btc: float,
        network_id: str = "f9beb4d9",
        asset_id: str = "0x0000000000000000000000000000000000000000000000000000000000000000"
    ) -> Dict[str, Any]:
        """
        Creates a Bitcoin Lightning Network invoice (BOLT-11) on the local Hydranet node.
        Returns the invoice dict containing paymentRequest, paymentHash, recipient, and amount.
        """
        req = {
            "network": {
                "protocol": "PROTOCOL_BITCOIN",
                "id": network_id
            },
            "asset_id": asset_id,
            "amount": {
                "value": f"{amount_btc:.8f}".rstrip("0").rstrip(".") or "0"
            }
        }
        res = self.call("node_createInvoice", req)
        return res.get("invoice", {})

    def decode_lightning_invoice(
        self,
        payment_request: str,
        network_id: str = "f9beb4d9"
    ) -> Dict[str, Any]:
        """
        Decodes an external or internal BOLT-11 Lightning payment request.
        Returns recipient pubkey, satoshi amount, payment secret, and expiry timestamp.
        """
        req = {
            "network": {
                "protocol": "PROTOCOL_BITCOIN",
                "id": network_id
            },
            "paymentRequest": payment_request
        }
        res = self.call("node_decodeInvoice", req)
        return res.get("invoice", {})

    def pay_lightning_invoice(
        self,
        payment_request: str,
        network_id: str = "f9beb4d9",
        timeout: float = 60.0
    ) -> Dict[str, Any]:
        """
        Pays an external BOLT-11 Lightning invoice using the local node's outbound channel capacity.
        """
        req = {
            "network": {
                "protocol": "PROTOCOL_BITCOIN",
                "id": network_id
            },
            "paymentRequest": payment_request
        }
        return self.call("node_payInvoice", req, timeout=timeout)

    def send_lightning_payment(
        self,
        payment_request: str,
        network_id: str = "f9beb4d9",
        timeout: float = 60.0
    ) -> Dict[str, Any]:
        """Alias for pay_lightning_invoice used by the Cashu liquidity bridge."""
        return self.pay_lightning_invoice(payment_request=payment_request, network_id=network_id, timeout=timeout)
