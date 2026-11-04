# Hydranet DLMM Market Maker

> **Turnkey, Visual-First Dynamic Liquidity Market Maker for Hydranet Layer 3 DEX with Chaumian Ecash Lightning Liquidity Rails & Autonomous State Channel Management.**

---

## Overview

`hydranet-market-maker` is an open-source, non-custodial market making agent engineered specifically for the [Hydranet DEX](https://hydranet.ai) (`hydra-app` JSON-RPC). It provides passive, two-sided maker liquidity across **Bitcoin Lightning**, **Ethereum Lithium**, and **Arbitrum Lithium** off-chain state channels on both **Hydranet Testnet** (Bitcoin Signet + Sepolia + Arbitrum Sepolia) and **Hydranet Mainnet**.

### Key Highlights

- **Pure Passive Maker Rebates + Spread Capture**:
  - Places 15 passive limit Bid rungs and 15 passive limit Ask rungs (`30` orders per pair) to earn Hydranet's negative maker fee rebates (`+0.10%` on `BTC/USDC` & `ETH/BTC`, `+0.075%` on `ETH/USDC`, `+0.05%` on `HDN/USDC`, `ETH(Arb)/ETH(Eth)`, and `USDC(Arb)/USDC(Eth)`) plus bid/ask spread.
- **Two Quoting Engines (Directional DLMM + Cross-Chain 1:1 Stableswap Pegs)**:
  - **Directional DLMM Pairs (`BTC/USDC`, `ETH/USDC`, `ETH/BTC`, `HDN/USDC`)**: Uses a Gaussian Bell Curve combined with Avellaneda-Stoikov inventory skewing to concentrate liquidity near spot when balanced (`40%–60%`, $\sigma = 0.98$) and dynamically widen/skew rungs toward a `50/50` inventory equilibrium as fills occur.
  - **1:1 Cross-Chain Stableswap Peg Pairs (`ETH(Arb)/ETH(Eth)`, `USDC(Arb)/USDC(Eth)`)**: Locked to an exact `1.0000` theoretical peg (zero external spot volatility risk) with tight `0.10%` half-spreads, `0.05%` rung spacing, concentrated bell curves ($\sigma = 0.75$), and gentle inventory shading ($\pm 0.25\%$ max shift) so filled Bids are always profitable to flip back on Asks across Ethereum and Arbitrum channels.
- **Sticky `0.35%` Rebalance Deadband**:
  - Inspired by Uniswap v3 range stability: once a 30-rung ladder is resting on the order book, the agent **does not cancel and re-place orders on minor spot ticks**. Orders remain resting until the Reservation Center drifts by $\ge 0.35\%$, a fill occurs (`rung count < 30`), or a pair/channel state changes—maximizing queue priority and fill rate.
- **Chaumian Ecash Lightning Liquidity Rail (Cashu `NUT-00` to `NUT-05`)**:
  - Eliminates the need to run a personal Lightning routing node or continually rent new BTC channels when one-sided order flow fills your channel:
    - **Auto-Sweep (`NUT-04` Mint)**: When Bid fills accumulate BTC on the local side of your Lightning channel (depleting Inbound/Receivable capacity), excess BTC is swept over Lightning into blinded bearer Ecash proofs stored locally—**instantly restoring Inbound Receive capacity** on your existing channel. Verified against LND invoice settlement (`state == "SETTLED"`) and capped to real routable channel capacity.
    - **Auto-Refuel (`NUT-05` Melt)**: When Ask fills deplete your channel's Outbound Send capacity, local Ecash proofs are melted to pay a Hydranet Lightning invoice—**instantly refilling Outbound Send capacity**.
    - **1-Click Export & Import**: Export accumulated sats as a standard `cashuA...` token string (`--cashu-export` or via the Web UI) or import external tokens.
- **Channel Health Advisor & 3-Pillar Autonomous Channel Management**:
  - Continuously monitors both **Outbound Send** and **Inbound Receive** constraints across Bitcoin, Ethereum, and Arbitrum channels, flagging depth throttling whenever actual book depth falls below `65%` of Ideal curve depth.
  - **Zero-On-Chain Arbitrum / EVM Bootstrapping (`openOrDeposit` Inbound Rental)**: Open a brand-new Arbitrum (or Ethereum) Lithium channel with **zero on-chain Arbitrum tokens and zero Arbitrum gas** by renting inbound capacity (`USDC`, `ETH`, or `HDN`) via `openOrDeposit` and paying the `~$0.11` service fee off-chain from your existing Ethereum `USDC`/`ETH` or Bitcoin `BTC` channel balance. Your Bid rungs immediately buy into that rented Inbound capacity, organically populating your Outbound Send balance as Bids fill.
  - **Persistent Automation Toggles**: Independently toggle **Auto Cashu Sweep/Refuel**, **Auto BTC Channel Ops**, and **Auto Lithium Channels** in the UI (persisted across restarts in `data/channel_automation.json`).

---

## Supported Markets & Networks

### Networks
| Mode | Bitcoin Lightning | Ethereum Lithium | Arbitrum Lithium |
| :--- | :--- | :--- | :--- |
| **Testnet (`--testnet`)** | Bitcoin Signet (`0a03cf40`) | Ethereum Sepolia (`11155111`) | Arbitrum Sepolia (`421614`) |
| **Mainnet** | Bitcoin Mainnet (`00000000`) | Ethereum Mainnet (`1`) | Arbitrum One (`42161`) |

### Trading Pairs
| Pair | Strategy Mode | Base Asset (Network) | Quote Asset (Network) | Maker Rebate | Base Half-Spread |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`BTC/USDC`** | Directional DLMM | `BTC` (Bitcoin Lightning) | `USDC` (Ethereum / Arbitrum) | `+0.10%` | `0.40%` |
| **`ETH/USDC`** | Directional DLMM | `ETH` (Ethereum Lithium) | `USDC` (Ethereum / Arbitrum) | `+0.075%` | `0.40%` |
| **`ETH/BTC`** | Directional DLMM | `ETH` (Ethereum Lithium) | `BTC` (Bitcoin Lightning) | `+0.10%` | `0.40%` |
| **`HDN/USDC`** | Directional DLMM | `HDN` (Arbitrum Lithium) | `USDC` (Arbitrum / Ethereum) | `+0.05%` | `0.50%` |
| **`ETH(Arb)/ETH(Eth)`** | `1:1 STABLESWAP PEG` | `ETH` (Arbitrum Lithium) | `ETH` (Ethereum Lithium) | `+0.05%` | `0.10%` |
| **`USDC(Arb)/USDC(Eth)`** | `1:1 STABLESWAP PEG` | `USDC` (Arbitrum Lithium) | `USDC` (Ethereum Lithium) | `+0.05%` | `0.10%` |

---

## Architecture

```text
hydranet-market-maker/
├── agent.py                     # CLI entrypoint and orchestrator (--live, --testnet, --dry-run)
├── config.py                    # Testnet/Mainnet asset registry, pair parameters & BotConfig
├── market_maker.py              # 30-rung Gaussian DLMM & Stableswap Peg engine + Channel Advisor
├── position_supervisor.py       # Avellaneda-Stoikov 50/50 equilibrium & peg inventory skew engine
├── channel_manager.py           # Bitcoin Lightning & EVM Lithium channel manager + off-chain rentals
├── hydra_client.py              # Hydranet Core JSON-RPC & WebSocket client (port 5003)
├── cashu_client.py              # Pure-Python Chaumian Ecash client (secp256k1 BDHKE, NUT-00 to NUT-05)
├── cashu_bridge.py              # Automated Lightning-to-Cashu sweep & refuel valve
├── cashu_signet_mint.py         # Bitcoin Signet Cashu Mint server (LND Signet REST + WebSocket bridge)
├── external_oracle.py           # Multi-source spot price oracle (Coinbase, Kraken, Binance, CoinGecko)
├── pnl_tracker.py               # Rolling 30d maker volume, rebate tier & spread yield accounting
├── tax_logger.py                # Permanent CSV audit trail for fills and maker rebates
├── web_server.py                # Multi-threaded REST API server for the live Web UI
├── web/                         # Visual-first glassmorphism web dashboard (http://localhost:8080)
│   ├── index.html               # Trading & Channel Management views
│   ├── style.css                # Cyber dark-mode styling & interactive tooltips
│   └── app.js                   # SVG Gaussian curve chart, channel meters & inline rental drawers
├── docker-compose.testnet.yml   # Optional Docker stack for local Signet LND + Cashu Mint
├── tests/                       # Automated unit test suite
│   ├── test_cashu.py            # BDHKE cryptography & NUT-00 to NUT-05 tests
│   ├── test_market_maker.py     # DLMM & Stableswap Peg order ladder tests
│   └── test_web_server.py       # REST API & live pair toggle tests
└── requirements.txt             # Lightweight Python dependencies (requests, cryptography)
```

---

## Quickstart

### 1. Prerequisites
- **Python 3.10+**
- **Hydranet Core (`hydra-app`)** running with JSON-RPC enabled on `http://localhost:5003` (default).

### 2. Installation
```bash
git clone https://github.com/kobyboman-cmd/hydranet-market-maker.git
cd hydranet-market-maker

# Install lightweight dependencies
pip install -r requirements.txt

# Copy default environment configuration
cp .env.example .env
```

### 3. Running the Testnet Agent (Live on Bitcoin Signet + Sepolia + Arbitrum Sepolia)

To connect to your running Hydranet Testnet node (`http://localhost:5003`) and start the live market maker and web dashboard:

```bash
python agent.py --live --testnet
```

Then open **`http://localhost:8080`** in your browser:
1. **Trading Tab**:
   - Toggle any of the 6 pairs (`BTC / USDC`, `ETH / USDC`, `ETH / BTC`, `HDN / USDC`, `ETH(Arb) / ETH(Eth)`, `USDC(Arb) / USDC(Eth)`) on or off with one click.
   - Inspect real-time 30-rung **Gaussian Bell Curves** and **1:1 Stableswap Peg Ladders**, comparing **Confirmed Book** bars against the **Ideal** theoretical curve, `SPOT` / `1:1 PEG` markers, and `RESERVATION CENTER`.
   - Hover over any bar to inspect exact rung price, offset from center, confirmed order size vs. ideal size, maker rebate, and spread yield.
2. **Channel Management Tab**:
   - View **Channel Health & Advisor** diagnostics for every asset across **Bitcoin Lightning**, **Ethereum Lithium**, and **Arbitrum Lithium**.
   - **One-Click Inline Actions**: Deposit on-chain funds (`+ Outbound`), rent inbound capacity (`+ Inbound`), or bootstrap an unopened Arbitrum/Ethereum channel with zero on-chain tokens (`+ Rent Inbound to Open`, paying the `~$0.11` fee off-chain from an existing channel).
   - **Autonomous Toggles**: Enable **Auto Cashu Sweep/Refuel**, **Auto BTC Channel Ops**, and **Auto Lithium Channels** for hands-free channel maintenance.

### 4. Other Launch Modes

```bash
# Paper Trading / Simulation (Dry Run)
python agent.py --dry-run --testnet

# Run with Embedded Local Bitcoin Signet Cashu Mint on port 3338
python agent.py --live --testnet --start-local-mint

# Live Mainnet Execution
python agent.py --live
```

On Windows, you can also double-click **`start_app.bat`** or pass flags directly (`start_app.bat --live --testnet`).

---

## Chaumian Ecash (Cashu) CLI Operations

Manage your local Bitcoin Ecash vault (`data/cashu_wallet.json`) from the terminal:

### Check Vault Status
```bash
python agent.py --testnet --cashu-status
```

### 1-Click Ecash Token Export
Export your stored sats into a standard `cashuA...` bearer token string:
```bash
python agent.py --testnet --cashu-export
```

### Manual Sweep & Refuel
```bash
# Sweep $25 USD of accumulated Lightning channel BTC into Cashu Ecash (restores BTC Receive capacity)
python agent.py --testnet --cashu-sweep 25.0

# Melt $25 USD of Cashu Ecash back into your Lightning channel (restores BTC Send capacity)
python agent.py --testnet --cashu-refuel 25.0
```

---

## Running the Unit Tests

Execute the automated test suite:
```bash
python -m unittest discover -s tests -v
```

---

## License

MIT License. Free for community use, modification, and distribution.
