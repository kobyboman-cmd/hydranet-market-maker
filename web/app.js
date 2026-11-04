/**
 * Hydranet DLMM Set-It-And-Forget-It Maker & Cashu Liquidity Flywheel Client.
 * Renders the interactive SVG Gaussian Maker Curve, Bitcoin Lightning + Cashu
 * Mint/Melt Piston, Multi-Asset Lithium State Channels, and Maker Rebate Stream.
 */

class DLMMApp {
  constructor() {
    this.selectedPair = "BTC/USDC";
    this.activeViewTab = "trading";
    this.pollIntervalMs = 2000;
    this.pollTimer = null;
    this.isPaused = false;

    this.lastSummary = null;
    this.lastOrders = {};
    this.lastChannels = [];
    this.lastCashu = null;

    this.bridgeDirection = "onchain_to_offchain";
    this.bridgeSelectedAsset = "Ethereum:ETH";
    this.bridgeSelectedChannel = "";

    this.initDOMElements();
    this.initEventListeners();
    this.startPolling();
  }

  initDOMElements() {
    // Primary View Tabs (Trading vs Channel Management)
    this.btnTabTrading = document.getElementById("btnTabTrading");
    this.btnTabChannels = document.getElementById("btnTabChannels");
    this.tabChannelsAttentionBadge = document.getElementById("tabChannelsAttentionBadge");
    this.tabPanelTrading = document.getElementById("tabPanelTrading");
    this.tabPanelChannels = document.getElementById("tabPanelChannels");

    // Nav & Badges
    this.marketSelect = document.getElementById("marketSelect");
    this.marketTabs = document.querySelectorAll(".market-tab");
    this.modeBadge = document.getElementById("modeBadge");
    this.statusBadge = document.getElementById("statusBadge");
    this.btnPause = document.getElementById("btnPause");
    this.btnTick = document.getElementById("btnTick");
    this.btnRailMainnet = document.getElementById("btnRailMainnet");
    this.btnRailTestnet = document.getElementById("btnRailTestnet");
    this.mintPresetBtns = document.querySelectorAll("#mintPresetSelector .pill-btn");
    this.localMintStatusBadge = document.getElementById("localMintStatusBadge");
    this.btnToggleLocalMint = document.getElementById("btnToggleLocalMint");

    // Hero Metrics (Top 4 Cards)
    // 1. Total Asset Balance
    this.metricTotalAssetBadge = document.getElementById("metricTotalAssetBadge");
    this.metricTotalAssetBalance = document.getElementById("metricTotalAssetBalance");
    this.metricAssetBreakdown = document.getElementById("metricAssetBreakdown");

    // 2. Passive Earnings
    this.activeRebateBadge = document.getElementById("activeRebateBadge");
    this.metricPassiveIncome = document.getElementById("metricPassiveIncome");
    this.metricRebateBreakdown = document.getElementById("metricRebateBreakdown");
    this.metricFillsCount = document.getElementById("metricFillsCount");

    // 3. Rolling 30d Volume & Maker Tier Progress
    this.metricVolumeTierBadge = document.getElementById("metricVolumeTierBadge");
    this.metricVolume30d = document.getElementById("metricVolume30d");
    this.volumeTierProgressFill = document.getElementById("volumeTierProgressFill");
    this.metricVolumeNextTierText = document.getElementById("metricVolumeNextTierText");

    // 4. Channel Health
    this.channelHealthCard = document.getElementById("channelHealthCard");
    this.metricChannelHealthBadge = document.getElementById("metricChannelHealthBadge");
    this.metricChannelHealthStatus = document.getElementById("metricChannelHealthStatus");
    this.metricChannelHealthSub = document.getElementById("metricChannelHealthSub");

    // Legacy optional refs
    this.metricMidPrice = document.getElementById("metricMidPrice");
    this.metricReservationPrice = document.getElementById("metricReservationPrice");
    this.metricSpreadBadge = document.getElementById("metricSpreadBadge");
    this.metricInventoryRatio = document.getElementById("metricInventoryRatio");
    this.miniBalanceFill = document.getElementById("miniBalanceFill");
    this.metricInventorySkew = document.getElementById("metricInventorySkew");
    this.cashuBalanceSats = document.getElementById("cashuBalanceSats");
    this.cashuBalanceUsd = document.getElementById("cashuBalanceUsd");
    this.rentalSavedText = document.getElementById("rentalSavedText");
    this.mintBadge = document.getElementById("mintBadge");

    // Toast Banner
    this.toastBanner = document.getElementById("toastBanner");
    this.toastIcon = document.getElementById("toastIcon");
    this.toastMsg = document.getElementById("toastMsg");
    this.btnCloseToast = document.getElementById("btnCloseToast");

    // Curve Visualizer (Multi-Pair Card)
    this.curveVisualizerTitle = document.getElementById("curveVisualizerTitle");
    this.activePairsCountBadge = document.getElementById("activePairsCountBadge");
    this.pairTogglesBar = document.getElementById("pairTogglesBar");
    this.pairToggleInputs = document.querySelectorAll(".pair-toggle-input");
    this.curvesGridContainer = document.getElementById("curvesGridContainer");

    // Channel Management Card: Cashu Vault, Arrows, Toggles, Channels & Log
    this.proofCountBadge = document.getElementById("proofCountBadge");
    this.fwVaultSats = document.getElementById("fwVaultSats");
    this.fwVaultUsd = document.getElementById("fwVaultUsd");
    this.cashuVaultUninitView = document.getElementById("cashuVaultUninitView");
    this.cashuVaultActiveView = document.getElementById("cashuVaultActiveView");
    this.btnOpenCashuInitModal = document.getElementById("btnOpenCashuInitModal");

    this.chkAutoCashu = document.getElementById("chkAutoCashu");
    this.chkAutoBtcChannels = document.getElementById("chkAutoBtcChannels");
    this.chkAutoLithiumChannels = document.getElementById("chkAutoLithiumChannels");
    this.btnManualSweep = document.getElementById("btnManualSweep");
    this.btnManualRefuel = document.getElementById("btnManualRefuel");
    this.btnExportToken = document.getElementById("btnExportToken");
    this.btnQuickExport = document.getElementById("btnQuickExport");

    // Channels, Inline Transfer Drawer & Channel Management Log
    this.walletTotalsBadge = document.getElementById("walletTotalsBadge");
    this.inlineTransferDrawer = document.getElementById("inlineTransferDrawer");
    this.drawerTitle = document.getElementById("drawerTitle");
    this.drawerAvailText = document.getElementById("drawerAvailText");
    this.inpBridgeAmount = document.getElementById("inpBridgeAmount");
    this.bridgePctBtns = document.querySelectorAll("#inlineTransferDrawer .btn-pct");
    this.btnExecuteBridge = document.getElementById("btnExecuteBridge");
    this.btnCancelBridge = document.getElementById("btnCancelBridge");
    this.btcChannelContainer = document.getElementById("btcChannelContainer");
    this.channelsContainer = document.getElementById("channelsContainer");
    this.cmLogCountBadge = document.getElementById("cmLogCountBadge");
    this.channelMgmtLogBody = document.getElementById("channelMgmtLogBody");

    // Trade Log Section
    this.tradeLogTitle = document.getElementById("tradeLogTitle");
    this.tradeLogSummaryBadge = document.getElementById("tradeLogSummaryBadge");
    this.tradeLogBody = document.getElementById("tradeLogBody");

    // Export Modal
    this.exportModal = document.getElementById("exportModal");
    this.btnCloseModal = document.getElementById("btnCloseModal");
    this.txtTokenString = document.getElementById("txtTokenString");
    this.modalTokenAmount = document.getElementById("modalTokenAmount");
    this.btnCopyToken = document.getElementById("btnCopyToken");

    // Cashu Vault Initialization & Risk Disclosure Modal
    this.cashuInitModal = document.getElementById("cashuInitModal");
    this.btnCloseCashuInitModal = document.getElementById("btnCloseCashuInitModal");
    this.btnCancelCashuInit = document.getElementById("btnCancelCashuInit");
    this.btnConfirmCashuInit = document.getElementById("btnConfirmCashuInit");
  }

  initEventListeners() {
    // Top View Switcher Tabs (Trading vs Channel Management)
    if (this.btnTabTrading) {
      this.btnTabTrading.addEventListener("click", () => this.switchViewTab("trading"));
    }
    if (this.btnTabChannels) {
      this.btnTabChannels.addEventListener("click", () => this.switchViewTab("channels"));
    }
    if (this.channelHealthCard) {
      this.channelHealthCard.style.cursor = "pointer";
      this.channelHealthCard.title = "Click to open Channel Management tab";
      this.channelHealthCard.addEventListener("click", () => this.switchViewTab("channels"));
    }

    // Pair Trading Toggles at top of Maker Liquidity Curves card
    if (this.pairToggleInputs) {
      this.pairToggleInputs.forEach((inp) => {
        inp.addEventListener("change", () => {
          const pair = inp.getAttribute("data-pair");
          if (pair) {
            this.togglePairTrading(pair, inp.checked);
          }
        });
      });
    }

    // Legacy Market Tabs (if present)
    this.marketTabs.forEach((btn) => {
      btn.addEventListener("click", () => {
        const pair = btn.getAttribute("data-pair");
        this.selectMarket(pair);
      });
    });

    if (this.marketSelect) {
      this.marketSelect.addEventListener("change", (e) => {
        this.selectMarket(e.target.value);
      });
    }

    // Control buttons
    this.btnPause.addEventListener("click", () => this.togglePause());
    this.btnTick.addEventListener("click", () => this.forceTick());

    if (this.btnRailMainnet) {
      this.btnRailMainnet.addEventListener("click", () => this.switchNetworkRail(false));
    }
    if (this.btnRailTestnet) {
      this.btnRailTestnet.addEventListener("click", () => this.switchNetworkRail(true));
    }

    if (this.mintPresetBtns) {
      this.mintPresetBtns.forEach((btn) => {
        btn.addEventListener("click", () => {
          const mintUrl = btn.getAttribute("data-mint");
          const isTestnet = btn.getAttribute("data-rail") === "testnet";
          const isLocal = mintUrl.includes("127.0.0.1:3338");
          this.switchNetworkRail(isTestnet, mintUrl, isLocal ? true : undefined);
        });
      });
    }

    if (this.btnToggleLocalMint) {
      this.btnToggleLocalMint.addEventListener("click", () => this.toggleLocalSignetMint());
    }

    if (this.btnCloseToast) {
      this.btnCloseToast.addEventListener("click", () => {
        this.toastBanner.style.display = "none";
      });
    }

    // Channel Management Automation Toggles
    if (this.chkAutoCashu) {
      this.chkAutoCashu.addEventListener("change", () => {
        const isInit = !!(this.lastSummary?.cashu?.initialized);
        if (!isInit && this.chkAutoCashu.checked) {
          this.chkAutoCashu.checked = false;
          const pill = this.chkAutoCashu.closest(".pair-toggle-pill");
          if (pill) pill.classList.remove("active");
          this.openCashuInitModal();
          return;
        }
        const pill = this.chkAutoCashu.closest(".pair-toggle-pill");
        if (pill) pill.classList.toggle("active", this.chkAutoCashu.checked);
        this.toggleChannelAuto("auto_cashu", this.chkAutoCashu.checked);
      });
    }
    if (this.chkAutoBtcChannels) {
      this.chkAutoBtcChannels.addEventListener("change", () => {
        const pill = this.chkAutoBtcChannels.closest(".pair-toggle-pill");
        if (pill) pill.classList.toggle("active", this.chkAutoBtcChannels.checked);
        this.toggleChannelAuto("auto_btc_channels", this.chkAutoBtcChannels.checked);
      });
    }
    if (this.chkAutoLithiumChannels) {
      this.chkAutoLithiumChannels.addEventListener("change", () => {
        const pill = this.chkAutoLithiumChannels.closest(".pair-toggle-pill");
        if (pill) pill.classList.toggle("active", this.chkAutoLithiumChannels.checked);
        this.toggleChannelAuto("auto_lithium_channels", this.chkAutoLithiumChannels.checked);
      });
    }

    // Cashu Actions
    if (this.btnOpenCashuInitModal) {
      this.btnOpenCashuInitModal.addEventListener("click", () => this.openCashuInitModal());
    }
    if (this.btnCloseCashuInitModal) {
      this.btnCloseCashuInitModal.addEventListener("click", () => this.closeCashuInitModal());
    }
    if (this.btnCancelCashuInit) {
      this.btnCancelCashuInit.addEventListener("click", () => this.closeCashuInitModal());
    }
    if (this.btnConfirmCashuInit) {
      this.btnConfirmCashuInit.addEventListener("click", () => this.confirmInitializeCashuVault());
    }
    if (this.cashuInitModal) {
      this.cashuInitModal.addEventListener("click", (e) => {
        if (e.target === this.cashuInitModal) this.closeCashuInitModal();
      });
    }

    if (this.btnManualSweep) {
      this.btnManualSweep.addEventListener("click", () => this.executeManualSweep());
    }
    if (this.btnManualRefuel) {
      this.btnManualRefuel.addEventListener("click", () => this.executeManualRefuel());
    }
    if (this.btnExportToken) {
      this.btnExportToken.addEventListener("click", () => this.openExportModal());
    }
    if (this.btnQuickExport) {
      this.btnQuickExport.addEventListener("click", () => this.openExportModal());
    }

    // Inline Transfer Drawer Controls
    if (this.bridgePctBtns) {
      this.bridgePctBtns.forEach((btn) => {
        btn.addEventListener("click", () => {
          const pct = parseFloat(btn.getAttribute("data-pct") || "1.0");
          this.fillBridgeAmountByPct(pct);
        });
      });
    }

    if (this.btnExecuteBridge) {
      this.btnExecuteBridge.addEventListener("click", () => this.executeBridgeTransfer());
    }

    if (this.btnCancelBridge) {
      this.btnCancelBridge.addEventListener("click", () => {
        if (this.inlineTransferDrawer) this.inlineTransferDrawer.style.display = "none";
      });
    }

    // Modal
    this.btnCloseModal.addEventListener("click", () => this.closeExportModal());
    this.btnCopyToken.addEventListener("click", () => this.copyTokenToClipboard());
    this.exportModal.addEventListener("click", (e) => {
      if (e.target === this.exportModal) this.closeExportModal();
    });
  }

  selectMarket(pair) {
    this.selectedPair = pair;
    if (this.marketSelect) this.marketSelect.value = pair;
    this.marketTabs.forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-pair") === pair);
    });
    this.renderCurrentState();
  }

  showToast(msg, icon = "✨", isWarning = false) {
    if (!this.toastBanner) return;
    this.toastBanner.style.display = "flex";
    this.toastIcon.textContent = icon;
    this.toastMsg.textContent = msg;
    this.toastBanner.style.borderColor = isWarning ? "rgba(245, 158, 11, 0.5)" : "rgba(16, 185, 129, 0.45)";
    this.toastBanner.style.background = isWarning ? "rgba(245, 158, 11, 0.14)" : "rgba(16, 185, 129, 0.14)";
  }

  // ==================== POLLING & DATA FETCH ====================

  startPolling() {
    this.fetchAll();
    this.pollTimer = setInterval(() => this.fetchAll(), this.pollIntervalMs);
  }

  async fetchAll() {
    await Promise.all([
      this.fetchSummary(),
      this.fetchOrders(),
      this.fetchChannels(),
      this.fetchCashu(),
    ]);
    this.renderCurrentState();
  }

  async fetchSummary() {
    try {
      const reqStart = Date.now();
      const res = await fetch("/api/summary");
      if (!res.ok) return;
      const summary = await res.json();
      // Prevent an older in-flight poll response from overwriting a newer user pair or automation toggle click
      if (this.lastPairToggleTs && reqStart < this.lastPairToggleTs && this.optimisticActivePairs) {
        summary.active_pairs = [...this.optimisticActivePairs];
      }
      if (this.lastAutoToggleTs && reqStart < this.lastAutoToggleTs && this.optimisticAutomation && summary.wallet_overview) {
        summary.wallet_overview.automation = {
          ...(summary.wallet_overview.automation || {}),
          ...this.optimisticAutomation,
        };
      }
      this.lastSummary = summary;
    } catch (err) {
      console.error("Error fetching summary:", err);
    }
  }

  async fetchOrders() {
    try {
      const res = await fetch("/api/orders");
      if (!res.ok) return;
      const data = await res.json();
      this.lastOrders = data.orders || {};
    } catch (err) {
      console.error("Error fetching orders:", err);
    }
  }

  async fetchChannels() {
    try {
      const res = await fetch("/api/channels");
      if (!res.ok) return;
      const data = await res.json();
      this.lastChannels = data.channels || [];
    } catch (err) {
      console.error("Error fetching channels:", err);
    }
  }

  async fetchCashu() {
    try {
      const res = await fetch("/api/cashu");
      if (!res.ok) return;
      this.lastCashu = await res.json();
    } catch (err) {
      console.error("Error fetching Cashu status:", err);
    }
  }

  // ==================== MASTER RENDERER ====================

  renderCurrentState() {
    const data = this.lastSummary;
    if (!data) return;

    this.modeBadge.textContent = data.testnet ? `${data.mode} • SIGNET/SEPOLIA` : data.mode;
    if (this.btnRailMainnet && this.btnRailTestnet) {
      this.btnRailMainnet.classList.toggle("active", !data.testnet);
      this.btnRailTestnet.classList.toggle("active", !!data.testnet);
    }

    if (data.cashu && this.mintPresetBtns) {
      const currentMint = data.cashu.mint_url || "";
      this.mintPresetBtns.forEach((btn) => {
        btn.classList.toggle("active", btn.getAttribute("data-mint") === currentMint);
      });
      const localMint = data.cashu.local_signet_mint || {};
      if (this.localMintStatusBadge) {
        if (localMint.running) {
          this.localMintStatusBadge.textContent = "Signet Mint Online";
          this.localMintStatusBadge.className = "badge-mini badge-emerald";
        } else {
          this.localMintStatusBadge.textContent = "Mint Connected";
          this.localMintStatusBadge.className = "badge-mini badge-mint";
        }
      }
    }
    this.isPaused = data.is_paused;
    this.statusBadge.textContent = data.is_paused ? "PAUSED" : "ACTIVE";
    this.statusBadge.className = `badge ${data.is_paused ? "badge-mini" : "badge-success"}`;
    this.btnPause.textContent = data.is_paused ? "Resume" : "Pause";

    // 1. Card #1: Total Asset Balance (On-Chain + Off-Chain + Ecash)
    const wo = data.wallet_overview || {};
    const onchainUsd = Number(wo.total_onchain_usd || 0);
    const offchainUsd = Number(
      wo.total_offchain_owned_usd !== undefined
        ? wo.total_offchain_owned_usd
        : (wo.total_offchain_send_usd || 0) + (wo.total_closing_usd || 0)
    );
    const ecashUsd = Number(
      wo.total_ecash_usd !== undefined ? wo.total_ecash_usd : data.cashu?.balance_usd || 0
    );
    const totalAssetUsd =
      wo.total_asset_balance_usd !== undefined
        ? Number(wo.total_asset_balance_usd)
        : onchainUsd + offchainUsd + ecashUsd;

    if (this.metricTotalAssetBalance) {
      this.metricTotalAssetBalance.textContent = `$${totalAssetUsd.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;
    }
    if (this.metricAssetBreakdown) {
      this.metricAssetBreakdown.innerHTML = `On-Chain: $${Math.round(onchainUsd).toLocaleString("en-US")} &bull; Off-Chain: $${Math.round(offchainUsd).toLocaleString("en-US")} &bull; Ecash: $${Math.round(ecashUsd).toLocaleString("en-US")}`;
    }

    // 2. Card #2: Passive Income & Maker Rebates
    const pi = data.passive_income || {};
    const totalIncome = pi.total_passive_income_usd || 0.0;
    const rebatesUsd = pi.maker_rebates_usd || 0.0;
    const spreadUsd = pi.spread_capture_usd || 0.0;
    const fillsCount = pi.maker_fills_count || 0;
    const rentalSaved = pi.rental_fees_saved_usd || 0.0;

    if (this.metricPassiveIncome) {
      this.metricPassiveIncome.textContent = `+$${totalIncome.toFixed(4)}`;
    }
    if (this.metricRebateBreakdown) {
      this.metricRebateBreakdown.textContent = `Rebates: +$${rebatesUsd.toFixed(4)} • Spread: +$${spreadUsd.toFixed(4)}`;
    }
    if (this.metricFillsCount) {
      this.metricFillsCount.textContent = `${fillsCount} fills`;
    }

    // 3. Card #3: Rolling 30d Volume & Maker Fee Tier Progress
    const vol30 = pi.volume_30d || {};
    const settledVol = Number(
      vol30.settled_volume_usd !== undefined ? vol30.settled_volume_usd : pi.maker_volume_usd || 0
    );
    const curTier = Number(vol30.tier || 0);
    const nextTier = Number(vol30.next_tier !== undefined ? vol30.next_tier : curTier + 1);
    const nextTierVol = Number(vol30.next_tier_volume_usd || 10000);
    const nextTierDiscPct = Number(
      vol30.next_tier_maker_discount_pct !== undefined ? vol30.next_tier_maker_discount_pct : 20
    );
    const curDiscPct = Number(vol30.maker_discount_pct || 0);
    const volProg = Math.min(100, Math.max(0, Number(vol30.progress_pct !== undefined ? vol30.progress_pct : (settledVol / Math.max(nextTierVol, 1)) * 100)));

    if (this.metricVolume30d) {
      this.metricVolume30d.textContent = `$${settledVol.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;
    }
    if (this.metricVolumeTierBadge) {
      this.metricVolumeTierBadge.textContent =
        curDiscPct > 0 ? `Tier ${curTier} (+${curDiscPct}% Rebate)` : `Tier ${curTier}`;
    }
    if (this.volumeTierProgressFill) {
      this.volumeTierProgressFill.style.width = `${volProg}%`;
    }
    if (this.metricVolumeNextTierText) {
      this.metricVolumeNextTierText.textContent = `$${Math.round(settledVol).toLocaleString("en-US")} / $${Math.round(nextTierVol).toLocaleString("en-US")} to Tier ${nextTier} (+${nextTierDiscPct}% Rebate)`;
    }

    // 4. Card #4: Channel Health & Active Pairs State
    const allKnownPairs = Array.isArray(data.all_pairs)
      ? data.all_pairs
      : ["BTC/USDC", "ETH/USDC", "ETH/BTC", "HDN/USDC", "ETH(Arb)/ETH(Eth)", "USDC(Arb)/USDC(Eth)"];
    const enabledPairs = Array.isArray(data.active_pairs)
      ? data.active_pairs
      : allKnownPairs;

    if (this.activePairsCountBadge) {
      this.activePairsCountBadge.textContent = `${enabledPairs.length}/${allKnownPairs.length} Pairs Active`;
      this.activePairsCountBadge.className =
        enabledPairs.length > 0 ? "badge-mini badge-cyan" : "badge-mini badge-slate";
    }

    if (this.pairTogglesBar) {
      allKnownPairs.forEach((p) => {
        const isEnabled = enabledPairs.includes(p);
        const pillEl = this.pairTogglesBar.querySelector(`[data-pair-toggle="${p}"]`);
        const inpEl = this.pairTogglesBar.querySelector(`.pair-toggle-input[data-pair="${p}"]`);
        if (pillEl) pillEl.classList.toggle("active", isEnabled);
        if (inpEl && inpEl.checked !== isEnabled) inpEl.checked = isEnabled;
      });
    }

    const chHealth = wo.channel_health || {};
    let constrainedItems = chHealth.constrained_items;
    if (!Array.isArray(constrainedItems)) {
      constrainedItems = [];
      (wo.assets || []).forEach((a) => {
        if (a.send_constrained) constrainedItems.push(`${a.symbol} Send`);
        if (a.recv_constrained) constrainedItems.push(`${a.symbol} Recv`);
      });
    }
    const constrainedCount = constrainedItems.length;
    if (this.metricChannelHealthBadge && this.metricChannelHealthStatus && this.metricChannelHealthSub) {
      if (enabledPairs.length === 0) {
        this.metricChannelHealthBadge.textContent = "STANDBY";
        this.metricChannelHealthBadge.className = "badge-mini badge-slate";
        this.metricChannelHealthStatus.textContent = "No Pairs Active";
        this.metricChannelHealthStatus.className = "metric-val";
        this.metricChannelHealthSub.textContent = "Toggle on at least one pair to monitor channel health";
        if (this.channelHealthCard) this.channelHealthCard.classList.remove("health-attention-card");
      } else if (constrainedCount === 0) {
        this.metricChannelHealthBadge.textContent = "ALL HEALTHY";
        this.metricChannelHealthBadge.className = "badge-mini badge-emerald";
        this.metricChannelHealthStatus.textContent = "All Ready";
        this.metricChannelHealthStatus.className = "metric-val text-emerald";
        this.metricChannelHealthSub.textContent = `Full 15-rung Bids & Asks active across ${enabledPairs.length} ${enabledPairs.length === 1 ? "pair" : "pairs"}`;
        if (this.channelHealthCard) this.channelHealthCard.classList.remove("health-attention-card");
      } else {
        this.metricChannelHealthBadge.textContent = "NEEDS ATTENTION";
        this.metricChannelHealthBadge.className = "badge-mini badge-amber";
        this.metricChannelHealthStatus.textContent = `${constrainedCount} ${constrainedCount === 1 ? "Needs" : "Need"} Attention`;
        this.metricChannelHealthStatus.className = "metric-val text-gold";
        this.metricChannelHealthSub.textContent = `⚠️ ${constrainedItems.join(", ")} limiting 15 rungs`;
        if (this.channelHealthCard) this.channelHealthCard.classList.add("health-attention-card");
      }
    }

    // Update yellow attention badge on the Channel Management tab button
    if (this.tabChannelsAttentionBadge) {
      if (enabledPairs.length > 0 && constrainedCount > 0) {
        this.tabChannelsAttentionBadge.textContent = `⚠️ ${constrainedCount}`;
        this.tabChannelsAttentionBadge.style.display = "inline-flex";
      } else {
        this.tabChannelsAttentionBadge.style.display = "none";
      }
    }

    // 5. Passive Rebate Badge Summary
    if (this.activeRebateBadge) {
      this.activeRebateBadge.textContent =
        enabledPairs.length === 1 && data.markets?.[enabledPairs[0]]
          ? `+${((data.markets[enabledPairs[0]].maker_rebate_rate || 0.001) * 100).toFixed(3).replace(/\.?0+$/, "")}% Rebate`
          : `+0.05%–0.10% Rebate`;
    }

    // 6. Cashu Vault Summary & Automation Toggles
    const isCashuInit = !!(data.cashu && data.cashu.initialized);
    if (this.cashuVaultUninitView && this.cashuVaultActiveView) {
      this.cashuVaultUninitView.style.display = isCashuInit ? "none" : "flex";
      this.cashuVaultActiveView.style.display = isCashuInit ? "flex" : "none";
    }
    if (this.localMintStatusBadge) {
      if (!isCashuInit) {
        this.localMintStatusBadge.textContent = "Not Initialized";
        this.localMintStatusBadge.className = "badge-mini badge-slate";
      } else {
        this.localMintStatusBadge.textContent = "Mint Connected";
        this.localMintStatusBadge.className = "badge-mini badge-emerald";
      }
    }
    if (this.btnManualSweep) {
      this.btnManualSweep.disabled = !isCashuInit;
      this.btnManualSweep.classList.toggle("arrow-disabled", !isCashuInit);
    }
    if (this.btnManualRefuel) {
      this.btnManualRefuel.disabled = !isCashuInit;
      this.btnManualRefuel.classList.toggle("arrow-disabled", !isCashuInit);
    }

    if (data.cashu) {
      const sats = data.cashu.balance_sats || 0;
      const usd = data.cashu.balance_usd || 0.0;
      const proofs = (this.lastCashu && this.lastCashu.proofs_count) || 0;
      if (this.cashuBalanceSats) this.cashuBalanceSats.textContent = `${sats.toLocaleString()} sats`;
      if (this.fwVaultSats) this.fwVaultSats.textContent = `${sats.toLocaleString()} sats`;
      if (this.fwVaultUsd) {
        this.fwVaultUsd.innerHTML = `&approx; $${usd.toFixed(2)} &bull; <span id="proofCountBadge">${proofs} Proofs</span>`;
      }
      if (this.cashuBalanceUsd) {
        this.cashuBalanceUsd.innerHTML = `≈ $${usd.toFixed(2)} &bull; <strong class="text-gold">$${rentalSaved.toFixed(2)} saved</strong>`;
      }
      if (data.cashu.mint_url && this.mintBadge) {
        const shortMint = data.cashu.mint_url.replace("https://", "").replace("http://", "").split("/")[0];
        this.mintBadge.textContent = shortMint;
      }
    }

    const autoCfg = data.wallet_overview?.automation || {};
    if (this.chkAutoCashu && autoCfg.auto_cashu !== undefined) {
      const cashuAutoOn = isCashuInit && !!autoCfg.auto_cashu;
      this.chkAutoCashu.checked = cashuAutoOn;
      const pill = this.chkAutoCashu.closest(".pair-toggle-pill");
      if (pill) pill.classList.toggle("active", cashuAutoOn);
    }
    if (this.chkAutoBtcChannels && autoCfg.auto_btc_channels !== undefined) {
      const btcAutoOn = !!autoCfg.auto_btc_channels;
      this.chkAutoBtcChannels.checked = btcAutoOn;
      const pill = this.chkAutoBtcChannels.closest(".pair-toggle-pill");
      if (pill) pill.classList.toggle("active", btcAutoOn);
    }
    if (this.chkAutoLithiumChannels && autoCfg.auto_lithium_channels !== undefined) {
      const liAutoOn = !!autoCfg.auto_lithium_channels;
      this.chkAutoLithiumChannels.checked = liAutoOn;
      const pill = this.chkAutoLithiumChannels.closest(".pair-toggle-pill");
      if (pill) pill.classList.toggle("active", liAutoOn);
    }

    // 7. Render Multi-Pair Gaussian Maker Curves inside the single card
    this.renderAllMakerCurves(data, enabledPairs);

    // 8. Update On-Chain <-> Off-Chain Bridge, Render Channels & Channel Management Log
    this.updateBridgeControls(data.wallet_overview, false);
    this.renderChannels(this.lastChannels, data.wallet_overview);
    this.renderChannelManagementLog(data.wallet_overview, this.lastCashu);

    // 9. Render Bottom Trade Log (Executed Maker Fills & Rebates)
    this.renderTradeLog(pi);
  }

  // ==================== VIEW TAB SWITCHING ====================

  switchViewTab(tabName) {
    const target = tabName === "channels" ? "channels" : "trading";
    this.activeViewTab = target;

    if (this.btnTabTrading) {
      const isTrading = target === "trading";
      this.btnTabTrading.classList.toggle("active", isTrading);
      this.btnTabTrading.setAttribute("aria-selected", isTrading ? "true" : "false");
    }
    if (this.btnTabChannels) {
      const isChannels = target === "channels";
      this.btnTabChannels.classList.toggle("active", isChannels);
      this.btnTabChannels.setAttribute("aria-selected", isChannels ? "true" : "false");
    }
    if (this.tabPanelTrading) {
      this.tabPanelTrading.style.display = target === "trading" ? "flex" : "none";
    }
    if (this.tabPanelChannels) {
      this.tabPanelChannels.style.display = target === "channels" ? "flex" : "none";
    }
  }

  // ==================== VISUALIZER 1: MULTI-PAIR GAUSSIAN MAKER CURVES ====================

  async togglePairTrading(pair, enabled) {
    this.lastPairToggleTs = Date.now();
    // Optimistically update local summary & UI immediately
    if (this.lastSummary) {
      const allKnown = this.lastSummary.all_pairs || [
        "BTC/USDC",
        "ETH/USDC",
        "ETH/BTC",
        "HDN/USDC",
        "ETH(Arb)/ETH(Eth)",
        "USDC(Arb)/USDC(Eth)",
      ];
      const curSet = new Set(this.lastSummary.active_pairs || allKnown);
      if (enabled) curSet.add(pair);
      else curSet.delete(pair);
      this.lastSummary.active_pairs = allKnown.filter((p) => curSet.has(p));
      this.optimisticActivePairs = [...this.lastSummary.active_pairs];
      if (!enabled && this.lastOrders[pair]) {
        this.lastOrders[pair] = [];
      }
      this.renderCurrentState();
    }

    try {
      const res = await fetch("/api/pairs/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pair, enabled }),
      });
      if (res.ok) {
        const out = await res.json();
        if (Array.isArray(out.active_pairs)) {
          this.optimisticActivePairs = [...out.active_pairs];
          if (this.lastSummary) {
            this.lastSummary.active_pairs = out.active_pairs;
          }
        }
      }
      await this.fetchAll();
      // Poll again once background DEX order cancellation & water-filling replacement completes
      setTimeout(() => this.fetchAll(), 2500);
    } catch (err) {
      console.error(`Error toggling pair ${pair}:`, err);
    }
  }

  renderAllMakerCurves(data, enabledPairs) {
    if (!this.curvesGridContainer) return;

    this.curvesGridContainer.setAttribute("data-count", String(enabledPairs.length));

    if (enabledPairs.length === 0) {
      this.curvesGridContainer.innerHTML = `
        <div class="curves-empty-state">
          No trading pairs enabled. Toggle on at least one pair above to deploy 30-rung Gaussian maker curves and activate Channel Advisor monitoring.
        </div>
      `;
      return;
    }

    // Remove empty state if present
    const emptyEl = this.curvesGridContainer.querySelector(".curves-empty-state");
    if (emptyEl) emptyEl.remove();

    // Remove panels for disabled pairs
    const existingPanels = Array.from(this.curvesGridContainer.querySelectorAll(".pair-curve-panel"));
    existingPanels.forEach((panel) => {
      const pName = panel.getAttribute("data-curve-pair");
      if (!enabledPairs.includes(pName)) {
        panel.remove();
      }
    });

    // Ensure panels exist in canonical order and update each panel
    enabledPairs.forEach((pair) => {
      let panel = this.curvesGridContainer.querySelector(`.pair-curve-panel[data-curve-pair="${pair}"]`);
      if (!panel) {
        panel = document.createElement("div");
        panel.className = "pair-curve-panel";
        panel.setAttribute("data-curve-pair", pair);
        panel.innerHTML = `
          <div class="pair-curve-header">
            <div class="pair-curve-title-group">
              <span class="pair-curve-name">${pair.replace("/", " / ")}</span>
              <span class="badge-mini badge-emerald" data-role="rebate-pill">+0.10% Rebate</span>
              <span class="badge-mini badge-slate" data-role="rungs-badge">0/15B • 0/15A</span>
            </div>
            <div class="dynamic-curve-status">
              <span class="badge-mini badge-cyan" data-role="regime-badge">BALANCED (σ=1.20)</span>
              <span class="curve-reason-text" data-role="reason-text">Auto-balancing</span>
            </div>
          </div>
          <div class="curve-canvas-wrapper">
            <div class="curve-hud-overlay" data-role="hud-tooltip">
              <span class="hud-hint">Hover over any rung to inspect price, depth, and rebate</span>
            </div>
            <svg viewBox="0 0 1100 300" preserveAspectRatio="none" class="maker-curve-svg" data-role="curve-svg"></svg>
          </div>
          <div class="curve-footer-strip">
            <div class="strip-item">
              <span class="dot bid-dot"></span>
              <span><strong>Bids:</strong> <span data-role="total-bid-usd" class="mono">$0.00</span></span>
            </div>
            <div class="strip-item">
              <span class="dot spot-dot"></span>
              <span><strong>Spot:</strong> <span data-role="spot-label" class="mono">$--</span></span>
            </div>
            <div class="strip-item">
              <span class="dot res-dot"></span>
              <span><strong>Center:</strong> <span data-role="res-label" class="mono">$--</span></span>
            </div>
            <div class="strip-item">
              <span class="dot ask-dot"></span>
              <span><strong>Asks:</strong> <span data-role="total-ask-usd" class="mono">$0.00</span></span>
            </div>
            <div class="strip-item">
              <span class="dot rebate-dot"></span>
              <span><strong>Inventory:</strong> <span data-role="inv-label" class="mono">50% / 50%</span></span>
            </div>
          </div>
        `;
      }
      this.curvesGridContainer.appendChild(panel);

      const m = data.markets ? data.markets[pair] : null;
      const orders = this.lastOrders[pair] || [];
      this.renderSinglePairCurvePanel(panel, pair, m, orders, data.dlmm);
    });
  }

  renderSinglePairCurvePanel(panel, pair, marketTelemetry, orders, dlmmCfg) {
    const svgEl = panel.querySelector('[data-role="curve-svg"]');
    const hudEl = panel.querySelector('[data-role="hud-tooltip"]');
    const rebatePillEl = panel.querySelector('[data-role="rebate-pill"]');
    const rungsBadgeEl = panel.querySelector('[data-role="rungs-badge"]');
    const regimeBadgeEl = panel.querySelector('[data-role="regime-badge"]');
    const reasonTextEl = panel.querySelector('[data-role="reason-text"]');
    const totalBidEl = panel.querySelector('[data-role="total-bid-usd"]');
    const totalAskEl = panel.querySelector('[data-role="total-ask-usd"]');
    const spotLabelEl = panel.querySelector('[data-role="spot-label"]');
    const resLabelEl = panel.querySelector('[data-role="res-label"]');
    const invLabelEl = panel.querySelector('[data-role="inv-label"]');

    if (!svgEl) return;

    const bids = orders.filter((o) => o.side === "BUY").sort((a, b) => a.price - b.price);
    const asks = orders.filter((o) => o.side === "SELL").sort((a, b) => a.price - b.price);

    const [baseSym, quoteSym] = pair.split("/");
    const isPegPair =
      Boolean(marketTelemetry && marketTelemetry.is_peg_pair) ||
      pair.includes("(Arb)") ||
      baseSym.split("(")[0] === quoteSym.split("(")[0];
    const midP = marketTelemetry && marketTelemetry.mid_price > 0 ? marketTelemetry.mid_price : isPegPair ? 1.0 : 0;
    const resP = marketTelemetry && marketTelemetry.reservation_price > 0 ? marketTelemetry.reservation_price : midP;
    const prec = isPegPair ? 5 : midP < 1.0 ? 6 : 2;
    const prefix = isPegPair ? "" : pair.endsWith("/BTC") ? "₿" : "$";

    const rebateRatePct = (((marketTelemetry && marketTelemetry.maker_rebate_rate) || (isPegPair ? 0.00025 : 0.001)) * 100)
      .toFixed(3)
      .replace(/\.?0+$/, "");
    if (rebatePillEl) rebatePillEl.textContent = `+${rebateRatePct}% Rebate`;

    const bCount = marketTelemetry?.active_bids_count !== undefined ? marketTelemetry.active_bids_count : bids.length;
    const aCount = marketTelemetry?.active_asks_count !== undefined ? marketTelemetry.active_asks_count : asks.length;
    if (rungsBadgeEl) {
      rungsBadgeEl.textContent = `${bCount}/15 Bids • ${aCount}/15 Asks`;
      rungsBadgeEl.className =
        bCount >= 15 && aCount >= 15 ? "badge-mini badge-emerald" : "badge-mini badge-amber";
    }

    const regime = ((marketTelemetry && marketTelemetry.curve_concentration) || (isPegPair ? "STABLESWAP PEG" : "BALANCED")).toUpperCase();
    const dynSigma =
      marketTelemetry && marketTelemetry.dynamic_sigma !== undefined
        ? marketTelemetry.dynamic_sigma
        : isPegPair
          ? 0.65
          : dlmmCfg?.bell_curve_sigma || 1.2;
    if (regimeBadgeEl) {
      regimeBadgeEl.textContent = isPegPair
        ? `${regime} (4–25 bps)`
        : `${regime} (σ=${Number(dynSigma).toFixed(2)})`;
      if (regime === "CONCENTRATED") {
        regimeBadgeEl.className = "badge-mini badge-emerald";
      } else if (regime === "WIDE") {
        regimeBadgeEl.className = "badge-mini badge-mint";
      } else {
        regimeBadgeEl.className = "badge-mini badge-cyan";
      }
    }
    if (reasonTextEl) {
      reasonTextEl.textContent = (marketTelemetry && marketTelemetry.curve_profile_reason) || "Auto-balancing";
    }

    const totalBidUsd = bids.reduce((acc, o) => acc + (o.value_usd || 0), 0);
    const totalAskUsd = asks.reduce((acc, o) => acc + (o.value_usd || 0), 0);

    const numRungs = 15;
    const halfSpread = isPegPair ? 0.0004 : (dlmmCfg && dlmmCfg.base_half_spread_pct) || 0.008;
    const spreadStep = isPegPair ? (0.0025 - 0.0004) / Math.max(1, numRungs - 1) : halfSpread / numRungs;
    const bidSpreadMult = (marketTelemetry && marketTelemetry.bid_spread_multiplier) || 1.0;
    const askSpreadMult = (marketTelemetry && marketTelemetry.ask_spread_multiplier) || 1.0;
    const rebateRate = (marketTelemetry && marketTelemetry.maker_rebate_rate) || (isPegPair ? 0.00025 : 0.001);
    const effCenterP = resP > 0 ? resP : midP > 0 ? midP : isPegPair ? 1.0 : 100;

    // Compute fallback 15-rung Gaussian weights in case backend telemetry hasn't populated ideal_bids/ideal_asks yet
    const sigmaVal = Math.max(0.4, Number(dynSigma) || 1.2);
    const rawWeights = [];
    for (let i = 0; i < numRungs; i++) {
      const x = (i / (numRungs - 1)) * 2.0;
      rawWeights.push(Math.exp(-0.5 * Math.pow(x / sigmaVal, 2)));
    }
    const weightSum = rawWeights.reduce((a, b) => a + b, 0) || 1.0;
    const normWeights = rawWeights.map((w) => w / weightSum);

    const fallbackSideBudget = Math.max(totalBidUsd, totalAskUsd, 60.0);

    const idealBidsByRung = new Map();
    const rawIdealBids = Array.isArray(marketTelemetry?.ideal_bids) ? marketTelemetry.ideal_bids : [];
    for (let r = 0; r < numRungs; r++) {
      const found = rawIdealBids.find((ib) => Number(ib.rung) === r);
      if (found) {
        idealBidsByRung.set(r, found);
      } else {
        const sp = halfSpread * bidSpreadMult + r * spreadStep;
        const p = Number((effCenterP * (1 - sp)).toFixed(prec));
        const vUsd = Number((fallbackSideBudget * normWeights[r]).toFixed(2));
        idealBidsByRung.set(r, {
          rung: r,
          side: "BUY",
          price: p,
          value_usd: vUsd,
          spread_pct: Number((sp * 100).toFixed(3)),
          est_rebate_usd: Number((vUsd * rebateRate).toFixed(4)),
          est_spread_usd: Number((vUsd * sp).toFixed(4)),
        });
      }
    }

    const idealAsksByRung = new Map();
    const rawIdealAsks = Array.isArray(marketTelemetry?.ideal_asks) ? marketTelemetry.ideal_asks : [];
    for (let r = 0; r < numRungs; r++) {
      const found = rawIdealAsks.find((ia) => Number(ia.rung) === r);
      if (found) {
        idealAsksByRung.set(r, found);
      } else {
        const sp = halfSpread * askSpreadMult + r * spreadStep;
        const p = Number((effCenterP * (1 + sp)).toFixed(prec));
        const vUsd = Number((fallbackSideBudget * normWeights[r]).toFixed(2));
        idealAsksByRung.set(r, {
          rung: r,
          side: "SELL",
          price: p,
          value_usd: vUsd,
          spread_pct: Number((sp * 100).toFixed(3)),
          est_rebate_usd: Number((vUsd * rebateRate).toFixed(4)),
          est_spread_usd: Number((vUsd * sp).toFixed(4)),
        });
      }
    }

    const idealBidTotalUsd = Array.from(idealBidsByRung.values()).reduce((acc, o) => acc + (o.value_usd || 0), 0);
    const idealAskTotalUsd = Array.from(idealAsksByRung.values()).reduce((acc, o) => acc + (o.value_usd || 0), 0);

    if (totalBidEl) {
      totalBidEl.textContent = `$${totalBidUsd.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} / $${idealBidTotalUsd.toLocaleString("en-US", { maximumFractionDigits: 0 })} Ideal`;
    }
    if (totalAskEl) {
      totalAskEl.textContent = `$${totalAskUsd.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} / $${idealAskTotalUsd.toLocaleString("en-US", { maximumFractionDigits: 0 })} Ideal`;
    }
    if (spotLabelEl) spotLabelEl.textContent = midP > 0 ? `${prefix}${midP.toFixed(prec)}` : "$--";
    if (resLabelEl) resLabelEl.textContent = resP > 0 ? `${prefix}${resP.toFixed(prec)}` : "$--";
    if (invLabelEl && marketTelemetry) {
      const bRatio = (marketTelemetry.base_ratio !== undefined ? marketTelemetry.base_ratio : 0.5) * 100;
      const qRatio = 100 - bRatio;
      invLabelEl.textContent = `${bRatio.toFixed(0)}% ${baseSym} / ${qRatio.toFixed(0)}% ${quoteSym}`;
    }

    // Map confirmed orderbook orders to their rung index (0..14)
    const confirmedBidsByRung = new Map();
    bids.forEach((o) => {
      const r = Number(o.rung);
      if (Number.isInteger(r) && r >= 0 && r < numRungs && !confirmedBidsByRung.has(r)) {
        confirmedBidsByRung.set(r, o);
      }
    });
    const confirmedAsksByRung = new Map();
    asks.forEach((o) => {
      const r = Number(o.rung);
      if (Number.isInteger(r) && r >= 0 && r < numRungs && !confirmedAsksByRung.has(r)) {
        confirmedAsksByRung.set(r, o);
      }
    });

    const W = 1100;
    const H = 300;
    const padL = 45;
    const padR = 45;
    const padT = 54;
    const padB = 36;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const baselineY = padT + plotH;

    const safeId = pair.replace(/[^a-zA-Z0-9]/g, "-");
    const allMaxCandidates = [
      ...orders.map((o) => o.value_usd || 0),
      ...Array.from(idealBidsByRung.values()).map((o) => o.value_usd || 0),
      ...Array.from(idealAsksByRung.values()).map((o) => o.value_usd || 0),
      5,
    ];
    const maxVal = Math.max(...allMaxCandidates);

    const biasPct = marketTelemetry ? marketTelemetry.center_price_bias_pct || 0 : 0;
    const spotX = W / 2;
    const maxVisualBias = isPegPair ? 0.0004 : 0.004;
    const resX = Math.max(spotX - 75, Math.min(spotX + 75, spotX + (biasPct / maxVisualBias) * 55));

    const centerGapHalf = 28;
    const leftZoneW = resX - padL - centerGapHalf;
    const rightZoneW = W - padR - (resX + centerGapHalf);

    // Always 15 fixed slots on each side of Reservation Center
    const bidSlotW = leftZoneW / numRungs;
    const askSlotW = rightZoneW / numRungs;

    let svgParts = [];

    svgParts.push(`
      <defs>
        <linearGradient id="bidBarGrad-${safeId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#10b981" stop-opacity="0.88" />
          <stop offset="100%" stop-color="#059669" stop-opacity="0.22" />
        </linearGradient>
        <linearGradient id="askBarGrad-${safeId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#f43f5e" stop-opacity="0.88" />
          <stop offset="100%" stop-color="#8b5cf6" stop-opacity="0.22" />
        </linearGradient>
        <linearGradient id="bidAreaGrad-${safeId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#10b981" stop-opacity="0.20" />
          <stop offset="100%" stop-color="#10b981" stop-opacity="0.0" />
        </linearGradient>
        <linearGradient id="askAreaGrad-${safeId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#f43f5e" stop-opacity="0.20" />
          <stop offset="100%" stop-color="#f43f5e" stop-opacity="0.0" />
        </linearGradient>
        <linearGradient id="rebateZoneGrad-${safeId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#00f0ff" stop-opacity="0.14" />
          <stop offset="100%" stop-color="#10b981" stop-opacity="0.03" />
        </linearGradient>
      </defs>
    `);

    for (let i = 0; i <= 4; i++) {
      const gy = padT + (plotH * i) / 4;
      const labelVal = Math.round(maxVal * (1 - i / 4));
      svgParts.push(`
        <line x1="${padL}" y1="${gy}" x2="${W - padR}" y2="${gy}" stroke="rgba(255,255,255,0.05)" stroke-width="1" />
        <text x="${padL - 8}" y="${gy + 4}" text-anchor="end" fill="#64748b" font-family="JetBrains Mono, monospace" font-size="10">$${labelVal}</text>
      `);
    }

    svgParts.push(`
      <rect x="${resX - centerGapHalf}" y="${padT}" width="${centerGapHalf * 2}" height="${plotH}" fill="url(#rebateZoneGrad-${safeId})" rx="4" />
    `);

    // Build all 15 Bid slots from left (rung #14) to right (rung #0 closest to center)
    const bidSlots = [];
    for (let slotIdx = 0; slotIdx < numRungs; slotIdx++) {
      const rung = numRungs - 1 - slotIdx;
      const barW = Math.max(6, bidSlotW * 0.72);
      const cx = padL + slotIdx * bidSlotW + bidSlotW / 2;
      const idealObj = idealBidsByRung.get(rung);
      const confObj = confirmedBidsByRung.get(rung) || null;

      const idealUsd = idealObj ? idealObj.value_usd || 0 : 0;
      const idealH = Math.max(6, (idealUsd / maxVal) * (plotH - 16));
      const idealY = baselineY - idealH;

      const confUsd = confObj ? confObj.value_usd || 0 : 0;
      const confH = confObj && confUsd > 0 ? Math.max(6, (confUsd / maxVal) * (plotH - 16)) : 0;
      const confY = baselineY - confH;

      bidSlots.push({
        slotIdx,
        rung,
        cx,
        barW,
        idealY,
        idealH,
        idealOrder: idealObj,
        confY,
        confH,
        confirmedOrder: confObj,
      });
    }

    // Build all 15 Ask slots from left (rung #0 closest to center) to right (rung #14)
    const askSlots = [];
    for (let slotIdx = 0; slotIdx < numRungs; slotIdx++) {
      const rung = slotIdx;
      const barW = Math.max(6, askSlotW * 0.72);
      const cx = resX + centerGapHalf + slotIdx * askSlotW + askSlotW / 2;
      const idealObj = idealAsksByRung.get(rung);
      const confObj = confirmedAsksByRung.get(rung) || null;

      const idealUsd = idealObj ? idealObj.value_usd || 0 : 0;
      const idealH = Math.max(6, (idealUsd / maxVal) * (plotH - 16));
      const idealY = baselineY - idealH;

      const confUsd = confObj ? confObj.value_usd || 0 : 0;
      const confH = confObj && confUsd > 0 ? Math.max(6, (confUsd / maxVal) * (plotH - 16)) : 0;
      const confY = baselineY - confH;

      askSlots.push({
        slotIdx,
        rung,
        cx,
        barW,
        idealY,
        idealH,
        idealOrder: idealObj,
        confY,
        confH,
        confirmedOrder: confObj,
      });
    }

    // 1. Render shaded area under the 15-rung Ideal Gaussian Curves
    if (bidSlots.length > 1) {
      const pathPts = bidSlots.map((s) => `${s.cx.toFixed(1)},${s.idealY.toFixed(1)}`).join(" L ");
      const areaD = `M ${bidSlots[0].cx.toFixed(1)},${baselineY} L ${pathPts} L ${bidSlots[bidSlots.length - 1].cx.toFixed(1)},${baselineY} Z`;
      svgParts.push(`<path d="${areaD}" fill="url(#bidAreaGrad-${safeId})" />`);
    }

    if (askSlots.length > 1) {
      const pathPts = askSlots.map((s) => `${s.cx.toFixed(1)},${s.idealY.toFixed(1)}`).join(" L ");
      const areaD = `M ${askSlots[0].cx.toFixed(1)},${baselineY} L ${pathPts} L ${askSlots[askSlots.length - 1].cx.toFixed(1)},${baselineY} Z`;
      svgParts.push(`<path d="${areaD}" fill="url(#askAreaGrad-${safeId})" />`);
    }

    // 2. Render Confirmed Order Book Bars (only where an order is confirmed on the book)
    bidSlots.forEach((s, idx) => {
      const priceToShow = s.confirmedOrder ? s.confirmedOrder.price : s.idealOrder ? s.idealOrder.price : 0;
      const showLabel = s.rung === 0 || s.rung === 7 || s.rung === 14;
      const barRect = s.confirmedOrder
        ? `<rect x="${(s.cx - s.barW / 2).toFixed(1)}" y="${s.confY.toFixed(1)}" width="${s.barW.toFixed(1)}" height="${s.confH.toFixed(1)}" rx="3" fill="url(#bidBarGrad-${safeId})" stroke="rgba(16,185,129,0.55)" stroke-width="1" />`
        : `<line x1="${(s.cx - s.barW / 2).toFixed(1)}" y1="${baselineY - 1}" x2="${(s.cx + s.barW / 2).toFixed(1)}" y2="${baselineY - 1}" stroke="rgba(16,185,129,0.28)" stroke-width="2" stroke-dasharray="2 2" />`;

      svgParts.push(`
        <g class="curve-bar" data-side="BUY" data-idx="${idx}">
          <rect x="${(s.cx - bidSlotW / 2).toFixed(1)}" y="${padT}" width="${bidSlotW.toFixed(1)}" height="${plotH}" fill="transparent" />
          ${barRect}
          ${showLabel ? `<text x="${s.cx.toFixed(1)}" y="${baselineY + 14}" text-anchor="middle" fill="#10b981" font-family="JetBrains Mono, monospace" font-size="9.5">#${s.rung}</text>` : ""}
          ${showLabel && priceToShow > 0 ? `<text x="${s.cx.toFixed(1)}" y="${baselineY + 25}" text-anchor="middle" fill="#94a3b8" font-family="JetBrains Mono, monospace" font-size="9">${prefix}${priceToShow.toFixed(prec)}</text>` : ""}
        </g>
      `);
    });

    askSlots.forEach((s, idx) => {
      const priceToShow = s.confirmedOrder ? s.confirmedOrder.price : s.idealOrder ? s.idealOrder.price : 0;
      const showLabel = s.rung === 0 || s.rung === 7 || s.rung === 14;
      const barRect = s.confirmedOrder
        ? `<rect x="${(s.cx - s.barW / 2).toFixed(1)}" y="${s.confY.toFixed(1)}" width="${s.barW.toFixed(1)}" height="${s.confH.toFixed(1)}" rx="3" fill="url(#askBarGrad-${safeId})" stroke="rgba(244,63,94,0.55)" stroke-width="1" />`
        : `<line x1="${(s.cx - s.barW / 2).toFixed(1)}" y1="${baselineY - 1}" x2="${(s.cx + s.barW / 2).toFixed(1)}" y2="${baselineY - 1}" stroke="rgba(244,63,94,0.28)" stroke-width="2" stroke-dasharray="2 2" />`;

      svgParts.push(`
        <g class="curve-bar" data-side="SELL" data-idx="${idx}">
          <rect x="${(s.cx - askSlotW / 2).toFixed(1)}" y="${padT}" width="${askSlotW.toFixed(1)}" height="${plotH}" fill="transparent" />
          ${barRect}
          ${showLabel ? `<text x="${s.cx.toFixed(1)}" y="${baselineY + 14}" text-anchor="middle" fill="#f43f5e" font-family="JetBrains Mono, monospace" font-size="9.5">#${s.rung}</text>` : ""}
          ${showLabel && priceToShow > 0 ? `<text x="${s.cx.toFixed(1)}" y="${baselineY + 25}" text-anchor="middle" fill="#94a3b8" font-family="JetBrains Mono, monospace" font-size="9">${prefix}${priceToShow.toFixed(prec)}</text>` : ""}
        </g>
      `);
    });

    // 3. Render the 15-Rung Ideal Gaussian Line Curves on top so discrepancies are immediately visible
    if (bidSlots.length > 1) {
      const pathPts = bidSlots.map((s) => `${s.cx.toFixed(1)},${s.idealY.toFixed(1)}`).join(" L ");
      svgParts.push(`<path d="M ${pathPts}" fill="none" stroke="#10b981" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" pointer-events="none" />`);
      bidSlots.forEach((s) => {
        svgParts.push(`<circle cx="${s.cx.toFixed(1)}" cy="${s.idealY.toFixed(1)}" r="2.6" fill="#060912" stroke="#10b981" stroke-width="1.8" pointer-events="none" />`);
      });
    }

    if (askSlots.length > 1) {
      const pathPts = askSlots.map((s) => `${s.cx.toFixed(1)},${s.idealY.toFixed(1)}`).join(" L ");
      svgParts.push(`<path d="M ${pathPts}" fill="none" stroke="#f43f5e" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" pointer-events="none" />`);
      askSlots.forEach((s) => {
        svgParts.push(`<circle cx="${s.cx.toFixed(1)}" cy="${s.idealY.toFixed(1)}" r="2.6" fill="#060912" stroke="#f43f5e" stroke-width="1.8" pointer-events="none" />`);
      });
    }

    svgParts.push(`
      <line x1="${spotX}" y1="18" x2="${spotX}" y2="${baselineY}" stroke="rgba(255,255,255,0.45)" stroke-width="1.5" stroke-dasharray="4 4" />
      <text x="${spotX}" y="13" text-anchor="middle" fill="#e2e8f0" font-family="JetBrains Mono, monospace" font-size="10" font-weight="600">
        ${isPegPair ? "1:1 PEG" : "SPOT"} ${prefix}${midP.toFixed(prec)}
      </text>
    `);

    svgParts.push(`
      <line x1="${resX}" y1="${padT - 10}" x2="${resX}" y2="${baselineY}" stroke="#00f0ff" stroke-width="2.2" />
      <rect x="${resX - 58}" y="${padT - 28}" width="116" height="19" rx="4" fill="#060912" stroke="#00f0ff" stroke-width="1" />
      <text x="${resX}" y="${padT - 15}" text-anchor="middle" fill="#00f0ff" font-family="JetBrains Mono, monospace" font-size="9.5" font-weight="700">
        RESERVATION CENTER
      </text>
    `);

    svgEl.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svgEl.innerHTML = svgParts.join("");

    const allBars = svgEl.querySelectorAll(".curve-bar");
    allBars.forEach((barEl) => {
      barEl.addEventListener("mouseenter", () => {
        const side = barEl.getAttribute("data-side");
        const idx = parseInt(barEl.getAttribute("data-idx"), 10);
        const slot = side === "BUY" ? bidSlots[idx] : askSlots[idx];
        if (!slot || !hudEl) return;
        const confO = slot.confirmedOrder;
        const idealO = slot.idealOrder;
        const refO = confO || idealO;
        if (!refO) return;

        const sideColor = side === "BUY" ? "var(--green)" : "var(--red)";
        const confUsd = confO ? confO.value_usd || 0 : 0;
        const idealUsd = idealO ? idealO.value_usd || 0 : 0;
        const confAmt = confO ? confO.amount || 0 : 0;
        const rebateUsd = refO.est_rebate_usd !== undefined ? refO.est_rebate_usd : (confUsd || idealUsd) * rebateRate;
        const spreadUsd = refO.est_spread_usd !== undefined ? refO.est_spread_usd : (confUsd || idealUsd) * 0.008;
        const isMatched = confO && Math.abs(confUsd - idealUsd) <= Math.max(0.75, idealUsd * 0.15);

        hudEl.innerHTML = `
          <span>
            <strong style="color:${sideColor}">[${side} RUNG #${slot.rung}]</strong>
            Price: <strong>${prefix}${(refO.price || 0).toFixed(prec)}</strong> (${side === "BUY" ? "-" : "+"}${refO.spread_pct || 0.8}% from center)
          </span>
          <span>
            Confirmed Book: <strong class="${isMatched ? "text-emerald" : "text-gold"}">$${confUsd.toFixed(2)}</strong> (${confAmt} ${baseSym})
            &bull; Ideal: <strong>$${idealUsd.toFixed(2)}</strong>
          </span>
          <span class="text-emerald">
            Yield: <strong>+$${rebateUsd.toFixed(4)} Rebate</strong> + <strong>+$${spreadUsd.toFixed(4)} Spread</strong>
          </span>
        `;
      });
    });
  }

  // ==================== VISUALIZER 2: WALLETS, CHANNELS & INLINE DRAWER ====================

  getSelectedAssetEntry(walletOverview) {
    if (!walletOverview || !walletOverview.assets) return null;
    const [net, sym] = (this.bridgeSelectedAsset || "Ethereum:ETH").split(":");
    return (
      walletOverview.assets.find((a) => a.network_name === net && a.symbol === sym) ||
      walletOverview.assets[0] ||
      null
    );
  }

  updateBridgeControls(walletOverview, autoFillMax = false) {
    if (!walletOverview || !walletOverview.networks) return;

    if (this.walletTotalsBadge) {
      const oncUsd = Math.round(walletOverview.total_onchain_usd || 0).toLocaleString("en-US");
      const offUsd = Math.round(walletOverview.total_offchain_send_usd || 0).toLocaleString("en-US");
      const clsUsd = walletOverview.total_closing_usd || 0;
      const clsSuffix = clsUsd > 0 ? ` • Settling: $${Math.round(clsUsd)}` : "";
      this.walletTotalsBadge.textContent = `Wallet: $${oncUsd} • Channel: $${offUsd}${clsSuffix}`;
    }

    const entry = this.getSelectedAssetEntry(walletOverview);
    if (!entry) return;

    const dec = entry.symbol === "BTC" ? 6 : entry.symbol === "ETH" ? 5 : 2;
    let avail = 0;
    let title = "";
    let btnLabel = "Confirm";

    if (this.bridgeDirection === "onchain_to_offchain") {
      avail = entry.max_deposit_usable || 0;
      const isInit = entry.network_name === "Bitcoin" || !entry.primary_channel_id || this.bridgeActionSubtype === "open";
      title = isInit
        ? `Initiate ${entry.network_name} Channel (${entry.symbol})`
        : `Deposit ${entry.symbol} → ${entry.network_name} Channel`;
      btnLabel = isInit ? `Open Channel` : `Deposit ${entry.symbol}`;
      if (this.drawerAvailText) {
        this.drawerAvailText.textContent = `Wallet: ${(entry.onchain_confirmed || 0).toFixed(dec)} ${entry.symbol}`;
      }
    } else if (this.bridgeDirection === "offchain_to_onchain") {
      avail = entry.max_withdrawable || 0;
      const isClose = entry.network_name === "Bitcoin" || this.bridgeActionSubtype === "close";
      if (entry.network_name === "Bitcoin" && this.bridgeSelectedChannel) {
        const netInfo = walletOverview.networks?.[entry.network_name] || {};
        const chosenCh = (netInfo.individual_channels || []).find((c) => c.channel_id === this.bridgeSelectedChannel);
        if (chosenCh && chosenCh.assets && chosenCh.assets.BTC) {
          avail = chosenCh.assets.BTC.free_local || 0;
        }
      }
      title = isClose
        ? `Close ${entry.network_name} Channel → Wallet`
        : `Withdraw ${entry.symbol} → ${entry.network_name} Wallet`;
      btnLabel = isClose ? `Close & Sweep` : `Withdraw ${entry.symbol}`;
      if (this.drawerAvailText) {
        this.drawerAvailText.textContent = `Withdrawable: ${avail.toFixed(dec)} ${entry.symbol}`;
      }
    } else {
      avail = entry.offchain_recv || 0;
      const isOpenViaRent = entry.network_name !== "Bitcoin" && !entry.primary_channel_id;
      title = isOpenViaRent
        ? `Open ${entry.network_name} Channel via ${entry.symbol} Inbound Rental (Fee paid off-chain)`
        : `Add ${entry.symbol} Inbound Capacity (${entry.network_name})`;
      btnLabel = isOpenViaRent ? `Open & Rent Inbound` : `Rent Inbound`;
      if (this.drawerAvailText) {
        this.drawerAvailText.textContent = `Current Recv: ${avail.toFixed(dec)} ${entry.symbol}`;
      }
    }

    if (this.drawerTitle) this.drawerTitle.textContent = title;
    if (this.btnExecuteBridge) this.btnExecuteBridge.textContent = btnLabel;

    if (autoFillMax && this.inpBridgeAmount) {
      if (this.bridgeDirection === "rent_inbound") {
        this.inpBridgeAmount.value = entry.symbol === "BTC" ? 0.002 : entry.symbol === "ETH" ? 0.05 : 100;
      } else {
        this.inpBridgeAmount.value = avail > 0 ? Number(avail.toFixed(dec)) : "";
      }
    }
  }

  fillBridgeAmountByPct(pct) {
    const walletOverview = this.lastSummary?.wallet_overview;
    const entry = this.getSelectedAssetEntry(walletOverview);
    if (!entry || !this.inpBridgeAmount) return;

    const dec = entry.symbol === "BTC" ? 6 : entry.symbol === "ETH" ? 5 : 2;
    let baseAvail = 0;
    if (this.bridgeDirection === "onchain_to_offchain") {
      baseAvail = entry.max_deposit_usable || 0;
    } else if (this.bridgeDirection === "offchain_to_onchain") {
      baseAvail = entry.max_withdrawable || 0;
      if (entry.network_name === "Bitcoin" && this.bridgeSelectedChannel) {
        const netInfo = walletOverview.networks?.[entry.network_name] || {};
        const chosenCh = (netInfo.individual_channels || []).find((c) => c.channel_id === this.bridgeSelectedChannel);
        if (chosenCh && chosenCh.assets && chosenCh.assets.BTC) {
          baseAvail = chosenCh.assets.BTC.free_local || 0;
        }
      }
    } else {
      baseAvail = entry.symbol === "BTC" ? 0.005 : entry.symbol === "ETH" ? 0.1 : 250;
    }

    const val = Number((baseAvail * pct).toFixed(dec));
    this.inpBridgeAmount.value = val > 0 ? val : 0;
  }

  attachInlineDrawerToActiveSlot() {
    if (!this.inlineTransferDrawer) return;
    const targetKey = this.bridgeSelectedAsset || "Ethereum:ETH";
    const slot =
      this.btcChannelContainer?.querySelector(`[data-drawer-slot="${targetKey}"]`) ||
      this.channelsContainer?.querySelector(`[data-drawer-slot="${targetKey}"]`);
    if (slot && this.inlineTransferDrawer.parentElement !== slot) {
      slot.appendChild(this.inlineTransferDrawer);
    }
  }

  selectBridgeQuickAction(direction, networkName, symbol, channelId = "", subtype = "") {
    this.bridgeDirection = direction;
    this.bridgeSelectedAsset = `${networkName}:${symbol}`;
    this.bridgeSelectedChannel = channelId;
    this.bridgeActionSubtype = subtype;

    if (this.inlineTransferDrawer) {
      this.attachInlineDrawerToActiveSlot();
      this.inlineTransferDrawer.style.display = "flex";
    }
    this.updateBridgeControls(this.lastSummary?.wallet_overview, true);
    if (this.inpBridgeAmount) {
      this.inpBridgeAmount.focus();
    }
  }

  async executeBridgeTransfer() {
    const [network, asset] = (this.bridgeSelectedAsset || "Ethereum:ETH").split(":");
    const rawAmount = parseFloat(this.inpBridgeAmount?.value || "0");
    const channelId = this.bridgeSelectedChannel || "";
    const actionSubtype =
      this.bridgeActionSubtype || (this.bridgeDirection === "offchain_to_onchain" && network === "Bitcoin" ? "close" : "");

    try {
      if (this.btnExecuteBridge) {
        this.btnExecuteBridge.disabled = true;
        this.btnExecuteBridge.textContent = "Working...";
      }

      const res = await fetch("/api/wallet/transfer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          direction: this.bridgeDirection,
          network,
          asset,
          amount: isNaN(rawAmount) ? 0 : rawAmount,
          channel_id: channelId || null,
          action_subtype: actionSubtype || null,
        }),
      });
      const data = await res.json();
      if (data.status === "SUCCESS") {
        this.showToast(data.message || `Completed ${data.action} on ${network} (${asset}).`, "✨", false);
        if (this.inlineTransferDrawer) this.inlineTransferDrawer.style.display = "none";
      } else {
        this.showToast(`${data.error || data.message || "Operation failed"}`, "⚠️", true);
      }
      await this.fetchAll();
    } catch (err) {
      this.showToast(`Transfer failed: ${err}`, "⚠️", true);
    } finally {
      if (this.btnExecuteBridge) {
        this.btnExecuteBridge.disabled = false;
      }
      this.updateBridgeControls(this.lastSummary?.wallet_overview, false);
    }
  }

  renderChannels(channels, walletOverview) {
    if (!this.channelsContainer || !this.btcChannelContainer) return;

    const wasDrawerVisible = !!(this.inlineTransferDrawer && this.inlineTransferDrawer.style.display !== "none");
    const wasInputFocused = document.activeElement === this.inpBridgeAmount;
    if (this.inlineTransferDrawer && this.inlineTransferDrawer.parentElement) {
      this.inlineTransferDrawer.parentElement.removeChild(this.inlineTransferDrawer);
    }

    this.btcChannelContainer.innerHTML = "";
    this.channelsContainer.innerHTML = "";

    if (!walletOverview || !walletOverview.networks) return;

    for (const netName of ["Bitcoin", "Ethereum", "Arbitrum"]) {
      const netData = walletOverview.networks[netName];
      if (!netData) continue;

      const card = document.createElement("div");
      card.className = netName === "Bitcoin" ? "channel-card btc-top-card" : "channel-card";
      const isBtc = netName === "Bitcoin";
      const hasLithiumChannel = !isBtc && !!netData.primary_channel_id;
      const activeChannels = (netData.individual_channels || []).filter((c) => !c.is_closing);

      let headerRightHtml = "";
      if (!isBtc && !hasLithiumChannel) {
        const defaultSym =
          (netData.assets || []).find((a) => (a.onchain_confirmed || 0) > 0)?.symbol ||
          (netName === "Arbitrum" ? "USDC" : "ETH");
        headerRightHtml = `
          <div class="btc-header-meta">
            <button type="button" class="asset-quick-btn btn-inbound-quick" data-qdir="rent_inbound" data-qnet="${netName}" data-qsym="${defaultSym}" title="Open ${netName} channel &amp; rent inbound ${defaultSym} (fee paid off-chain from existing channels)">
              + Rent Inbound to Open
            </button>
            <button type="button" class="btn btn-primary btn-xs" data-qdir="onchain_to_offchain" data-qnet="${netName}" data-qsym="${defaultSym}" data-qsub="open">
              &#x26A1; Initiate ${netName} Channel
            </button>
          </div>
        `;
      } else if (isBtc) {
        headerRightHtml = `
          <div class="btc-header-meta">
            <span class="badge-mini ${activeChannels.length >= 4 ? "badge-gold-border" : "badge-emerald"}">${activeChannels.length}/4 Hub Channels</span>
            <button type="button" class="asset-quick-btn" data-qdir="onchain_to_offchain" data-qnet="Bitcoin" data-qsym="BTC" data-qsub="open">
              + Open Channel
            </button>
            <button type="button" class="asset-quick-btn btn-inbound-quick" data-qdir="rent_inbound" data-qnet="Bitcoin" data-qsym="BTC" title="Rent inbound BTC Lightning capacity">
              + Inbound
            </button>
          </div>
        `;
      } else {
        headerRightHtml = `<span class="badge-mini badge-emerald">CHANNEL ACTIVE</span>`;
      }

      let assetsHtml = "";
      (netData.assets || []).forEach((a) => {
        const sym = a.symbol;
        const dec = sym === "BTC" ? 5 : sym === "ETH" ? 4 : 2;
        const sendVal = a.offchain_send || 0;
        const recvVal = a.offchain_recv || 0;
        const totalOff = sendVal + recvVal;
        const sendPct = totalOff > 0 ? (sendVal / totalOff) * 100 : 0;
        const recvPct = totalOff > 0 ? (recvVal / totalOff) * 100 : 0;

        const oncConf = a.onchain_confirmed || 0;
        const closingVal = a.offchain_closing || 0;

        // Hide completely empty secondary rows (0 on-chain and 0 off-chain) unless it's the primary asset or channel is active
        if (oncConf <= 0 && totalOff <= 0 && closingVal <= 0 && sym === "HDN") {
          return;
        }

        // Highlight Mint/Melt arrows when BTC channel is constrained and Cashu Vault is initialized
        if (isBtc && sym === "BTC") {
          const isCashuInit = !!(this.lastSummary?.cashu?.initialized);
          if (this.btnManualSweep) {
            this.btnManualSweep.classList.toggle("arrow-pulse", isCashuInit && !!a.recv_constrained);
          }
          if (this.btnManualRefuel) {
            this.btnManualRefuel.classList.toggle("arrow-pulse", isCashuInit && !!a.send_constrained);
          }
        }

        const withdrawingVal = a.withdrawing_local || 0;
        const isUpdatingAsset = !!a.is_updating;
        let closingPill = "";
        if (closingVal > 0) {
          closingPill = `<span class="badge-mini badge-gold-border">Settling: ${closingVal.toFixed(dec)} ${sym}</span>`;
        } else if (isUpdatingAsset) {
          if (a.settling_label) {
            closingPill = `<span class="badge-mini badge-gold-border" title="On-chain channel update confirming on ${netName}">${a.settling_label}</span>`;
          } else {
            const wdLabel = withdrawingVal > 0 ? `: ${withdrawingVal.toFixed(dec)} ${sym}` : "...";
            closingPill = `<span class="badge-mini badge-gold-border" title="On-chain channel update confirming on ${netName}">Settling${wdLabel}</span>`;
          }
        }

        let actionsHtml = "";
        if (isBtc) {
          actionsHtml = `
            ${oncConf > 0 ? `<button type="button" class="asset-quick-btn" data-qdir="onchain_to_offchain" data-qnet="Bitcoin" data-qsym="BTC" data-qsub="open">+ To Channel</button>` : ""}
            ${sendVal > 0 ? `<button type="button" class="asset-quick-btn btn-withdraw-quick" data-qdir="offchain_to_onchain" data-qnet="Bitcoin" data-qsym="BTC" data-qsub="close">Close to Wallet</button>` : ""}
          `;
        } else if (hasLithiumChannel) {
          actionsHtml = `
            <button type="button" class="asset-quick-btn" data-qdir="onchain_to_offchain" data-qnet="${netName}" data-qsym="${sym}" data-qcid="${netData.primary_channel_id}">+ Deposit</button>
            ${(sendVal > 0 || (a.max_withdrawable || 0) > 0) ? `<button type="button" class="asset-quick-btn btn-withdraw-quick" data-qdir="offchain_to_onchain" data-qnet="${netName}" data-qsym="${sym}" data-qcid="${netData.primary_channel_id}">Withdraw</button>` : ""}
            <button type="button" class="asset-quick-btn btn-inbound-quick" data-qdir="rent_inbound" data-qnet="${netName}" data-qsym="${sym}" data-qcid="${netData.primary_channel_id}" title="Rent inbound ${sym} receive capacity">+ Inbound</button>
          `;
        } else {
          actionsHtml = `
            ${oncConf > 0 ? `<button type="button" class="asset-quick-btn" data-qdir="onchain_to_offchain" data-qnet="${netName}" data-qsym="${sym}" data-qsub="open">+ Open Channel</button>` : ""}
            <button type="button" class="asset-quick-btn btn-inbound-quick" data-qdir="rent_inbound" data-qnet="${netName}" data-qsym="${sym}" title="Open ${netName} channel &amp; rent inbound ${sym} capacity (fee paid off-chain from existing channels)">+ Inbound</button>
          `;
        }

        const sendConstrained = !!a.send_constrained;
        const recvConstrained = !!a.recv_constrained;

        const sendLabelHtml = sendConstrained
          ? `<span class="cap-constrained" title="${(a.send_reasons || []).join(", ") || "Limiting 15 rungs"}">&#x26A0;&#xFE0F; Send: ${sendVal.toFixed(dec)} ${sym}</span>`
          : `<span class="text-cyan">Send: ${sendVal.toFixed(dec)} ${sym}</span>`;

        const recvLabelHtml = recvConstrained
          ? `<span class="cap-constrained" title="${(a.recv_reasons || []).join(", ") || "Limiting 15 rungs"}">&#x26A0;&#xFE0F; Receive: ${recvVal.toFixed(dec)} ${sym}</span>`
          : `<span class="text-purple">Receive: ${recvVal.toFixed(dec)} ${sym}</span>`;

        const advMsgs = a.advisor_messages || [];
        const advisorHtml =
          advMsgs.length > 0
            ? `<div class="channel-advisor-msg">
                 <span class="advisor-icon">&#x26A0;&#xFE0F;</span>
                 <span class="advisor-body">${advMsgs.join(" &bull; ")}</span>
               </div>`
            : "";

        assetsHtml += `
          <div class="channel-asset-row">
            <div class="asset-title-row">
              <div class="asset-left-meta">
                <strong class="asset-sym-badge">${sym}</strong>
                <span class="wallet-pill">Wallet: <strong class="text-emerald">${oncConf.toFixed(dec)}</strong> ($${Math.round(a.onchain_usd || 0)})</span>
                ${closingPill}
              </div>
              <div class="asset-quick-actions">
                ${actionsHtml}
              </div>
            </div>
            <div class="asset-drawer-slot" data-drawer-slot="${netName}:${sym}"></div>
            <div class="cap-bar-label">
              ${sendLabelHtml}
              ${recvLabelHtml}
            </div>
            <div class="progress-track">
              <div class="progress-fill-send" style="width: ${sendPct}%;"></div>
              <div class="progress-fill-recv" style="width: ${recvPct}%;"></div>
            </div>
            ${advisorHtml}
          </div>
        `;
      });

      // For Bitcoin (which can have up to 4 individual Lightning channels), show compact channel pills if > 1 channel
      let btcChannelsHtml = "";
      if (isBtc && activeChannels.length > 1) {
        const pills = activeChannels
          .map((ch) => {
            const loc = (ch.assets?.BTC?.free_local || 0).toFixed(5);
            const rem = (ch.assets?.BTC?.free_remote || 0).toFixed(5);
            return `
              <div class="indiv-channel-item">
                <span>${ch.short_id}: <strong class="text-cyan">${loc}</strong> / <strong class="text-purple">${rem}</strong> BTC</span>
                <button type="button" class="asset-quick-btn btn-withdraw-quick" data-qdir="offchain_to_onchain" data-qnet="Bitcoin" data-qsym="BTC" data-qcid="${ch.channel_id}" data-qsub="close">Close</button>
              </div>
            `;
          })
          .join("");
        btcChannelsHtml = `<div class="indiv-channels-strip">${pills}</div>`;
      }

      card.innerHTML = `
        <div class="channel-card-header">
          <span>${isBtc ? "Bitcoin Lightning" : netName}</span>
          ${headerRightHtml}
        </div>
        ${assetsHtml}
        ${btcChannelsHtml}
      `;

      if (isBtc) {
        this.btcChannelContainer.appendChild(card);
      } else {
        this.channelsContainer.appendChild(card);
      }
    }

    if (wasDrawerVisible && this.inlineTransferDrawer) {
      this.attachInlineDrawerToActiveSlot();
      this.inlineTransferDrawer.style.display = "flex";
      if (wasInputFocused && this.inpBridgeAmount) {
        this.inpBridgeAmount.focus({ preventScroll: true });
      }
    }

    const bindQuickBtns = (root) => {
      if (!root) return;
      root.querySelectorAll("[data-qdir]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const qdir = btn.getAttribute("data-qdir");
          const qnet = btn.getAttribute("data-qnet");
          const qsym = btn.getAttribute("data-qsym");
          const qcid = btn.getAttribute("data-qcid") || "";
          const qsub = btn.getAttribute("data-qsub") || "";
          this.selectBridgeQuickAction(qdir, qnet, qsym, qcid, qsub);
        });
      });
    };
    bindQuickBtns(this.btcChannelContainer);
    bindQuickBtns(this.channelsContainer);
  }

  renderChannelManagementLog(walletOverview, cashuData) {
    if (!this.channelMgmtLogBody) return;

    const rawLogs = (walletOverview && walletOverview.channel_management_log) || [];
    const entries = rawLogs.map((l) => ({
      id: l.id,
      ts: Number(l.timestamp || 0),
      timeStr: l.time_str || "--:--:--",
      mode: (l.mode || "MANUAL").toUpperCase(),
      network: l.network || "",
      asset: l.asset || "",
      actionType: l.action_type || "",
      status: l.status || "COMPLETED",
      message: l.message || "",
      advice: l.advice_acted_on || null,
    }));

    // Include historical Cashu events if not already logged in channel_management_log
    if (entries.length === 0 && cashuData && Array.isArray(cashuData.recent_events)) {
      cashuData.recent_events.forEach((ev) => {
        const isSweep = ev.type === "SWEEP";
        const tsNum = Number(ev.epoch || Date.parse(ev.timestamp || "") / 1000 || 0);
        const timeStr = tsNum > 0 ? new Date(tsNum * 1000).toLocaleTimeString() : "--:--:--";
        const sats = Number(ev.amount_sats || ev.melted_sats || 0);
        const isManual = !!ev.manual;
        entries.push({
          id: ev.id,
          ts: tsNum,
          timeStr,
          mode: isManual ? "MANUAL" : "AUTO",
          network: "Bitcoin",
          asset: "BTC",
          actionType: isSweep ? "CASHU_MINT" : "CASHU_MELT",
          status: ev.status || "COMPLETED",
          message: isSweep
            ? `Minted ${sats.toLocaleString()} sats (~$${Number(ev.amount_usd || 0).toFixed(2)}) into Cashu Vault to restore BTC inbound.`
            : `Melted ${sats.toLocaleString()} sats (~$${Number(ev.amount_usd || 0).toFixed(2)}) from Cashu Vault into BTC channel.`,
          advice: isManual
            ? null
            : isSweep
            ? "BTC Receive capacity low — Advise minting sats to Cashu Vault to restore inbound capacity."
            : "BTC Send capacity low — Advise melting sats from Cashu Vault into BTC channel.",
        });
      });
    }

    if (this.cmLogCountBadge) {
      this.cmLogCountBadge.textContent = `${entries.length} ${entries.length === 1 ? "Action" : "Actions"}`;
    }

    if (entries.length === 0) {
      this.channelMgmtLogBody.innerHTML = `
        <div class="cm-log-empty">
          No channel actions logged yet. Manual and automated Mint/Melt, Deposit, Withdraw, and Inbound actions will appear here.
        </div>
      `;
      return;
    }

    this.channelMgmtLogBody.innerHTML = entries
      .slice(0, 35)
      .map((item) => {
        const isAuto = item.mode === "AUTO";
        const isFailed = item.status === "FAILED";
        const modeClass = isAuto ? "mode-badge-auto" : "mode-badge-manual";
        const statusDot = isFailed
          ? `<span class="log-status-failed" title="Failed">&#x26A0;&#xFE0F;</span>`
          : `<span class="log-status-ok" title="Completed">&#x2713;</span>`;
        const adviceHtml =
          isAuto && item.advice
            ? `<div class="cm-log-advice">Acted on advice: &ldquo;${item.advice}&rdquo;</div>`
            : "";

        return `
          <div class="cm-log-item ${isFailed ? "cm-log-item-failed" : ""}">
            <div class="cm-log-top">
              <div class="cm-log-meta">
                <span class="mono-dim">${item.timeStr}</span>
                <span class="cm-mode-badge ${modeClass}">[${item.mode}]</span>
                <strong class="cm-action-tag">${item.actionType.replace(/_/g, " ")}</strong>
              </div>
              ${statusDot}
            </div>
            <div class="cm-log-msg">${item.message}</div>
            ${adviceHtml}
          </div>
        `;
      })
      .join("");
  }

  // ==================== TRADE LOG (EXECUTED MAKER FILLS & REBATES) ====================

  renderTradeLog(passiveIncome) {
    if (!this.tradeLogBody) return;

    const recentFills = (passiveIncome && passiveIncome.recent_fills) || [];
    const fillsCount = (passiveIncome && passiveIncome.maker_fills_count) || recentFills.length || 0;
    const totalVolUsd = (passiveIncome && passiveIncome.maker_volume_usd) || 0;
    const rebatesUsd = (passiveIncome && passiveIncome.maker_rebates_usd) || 0;
    const spreadUsd = (passiveIncome && passiveIncome.spread_capture_usd) || 0;

    if (this.tradeLogSummaryBadge) {
      const volText = totalVolUsd > 0 ? ` • $${totalVolUsd.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} Vol` : "";
      this.tradeLogSummaryBadge.textContent = `${fillsCount} Trades${volText} • +$${(rebatesUsd + spreadUsd).toFixed(4)} Yield`;
    }

    const entries = [];
    recentFills.forEach((f) => {
      const isBuy = (f.side || "").toUpperCase() === "BUY";
      const pair = f.pair || this.selectedPair;
      const baseSym = pair.split("/")[0] || "";
      const price = Number(f.price || 0);
      const prec = price > 0 && price < 1.0 ? 6 : 2;
      const prefix = pair.endsWith("/BTC") ? "₿" : "$";
      const timeStr = f.time_str || (f.timestamp ? new Date(f.timestamp * 1000).toLocaleTimeString() : "--:--:--");
      const rungBadge = f.rung !== undefined && f.rung !== null ? `#${f.rung}` : "";

      const amtDecimals = baseSym === "BTC" ? 7 : baseSym === "ETH" ? 5 : 2;
      const amtStr = Number(f.amount || 0).toFixed(amtDecimals).replace(/\.?0+$/, "") || "0";

      entries.push({
        ts: Number(f.timestamp || 0),
        rowClass: isBuy ? "trade-row-buy" : "trade-row-sell",
        html: `
          <span class="mono-dim">${timeStr}</span>
          <span class="trade-pair">${pair}</span>
          <span>
            <span class="trade-side-pill ${isBuy ? "pill-buy" : "pill-sell"}">${isBuy ? "BUY" : "SELL"} ${rungBadge}</span>
          </span>
          <span class="mono">${prefix}${price.toLocaleString("en-US", { minimumFractionDigits: prec, maximumFractionDigits: prec })}</span>
          <span class="mono">${amtStr} ${baseSym}</span>
          <span class="mono">$${Number(f.value_usd || 0).toFixed(2)}</span>
          <span class="text-right text-emerald mono">+Rebate $${Number(f.rebate_usd || 0).toFixed(4)} &bull; +Spread $${Number(f.spread_usd || 0).toFixed(4)}</span>
        `,
      });
    });

    entries.sort((a, b) => b.ts - a.ts);

    if (entries.length === 0) {
      this.tradeLogBody.innerHTML = `
        <div class="trade-log-empty">
          No trades executed yet &mdash; 15-rung Maker orders are resting on the DEX.
        </div>
      `;
      return;
    }

    this.tradeLogBody.innerHTML = entries
      .slice(0, 50)
      .map((item) => `<div class="trade-log-row ${item.rowClass}">${item.html}</div>`)
      .join("");
  }

  // ==================== INTERACTIVE ACTIONS ====================

  async runSimulation(scenario) {
    try {
      const res = await fetch("/api/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario, pair: this.selectedPair }),
      });
      const data = await res.json();
      if (data.message) {
        const icon = scenario === "bid_wave" ? "⚡" : scenario === "ask_wave" ? "🔥" : scenario === "reset" ? "↺" : "💰";
        this.showToast(data.message, icon, false);
      }
      await this.fetchAll();
    } catch (err) {
      this.showToast(`Simulation error: ${err}`, "⚠️", true);
    }
  }

  async verifyLiveCashuMint() {
    try {
      this.btnVerifyMint.disabled = true;
      this.btnVerifyMint.textContent = "Testing Mint...";
      const res = await fetch("/api/cashu/test-mint", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ test_sats: 30000 }),
      });
      const data = await res.json();
      if (data.hypothesis_verified) {
        this.showToast(
          `[CASHU VERIFIED] ${data.mint_note} | Denominations: [${(data.denominations || []).join("+")}] sats | Quote: ${data.bolt11_preview}`,
          "🧪",
          false
        );
      } else {
        this.showToast(`Cashu test result: ${data.error || "Failed"}`, "⚠️", true);
      }
    } catch (err) {
      this.showToast(`Error testing Cashu mint: ${err}`, "⚠️", true);
    } finally {
      this.btnVerifyMint.disabled = false;
      this.btnVerifyMint.innerHTML = "&#x1F9EA; Test Live Cashu Mint";
    }
  }

  async updateCurveSigma(sigma) {
    try {
      await fetch("/api/curve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sigma }),
      });
      await this.fetchAll();
      this.showToast(`Updated Gaussian Bell Curve concentration to σ = ${sigma.toFixed(2)}`, "📈", false);
    } catch (err) {
      console.error("Error updating curve sigma:", err);
    }
  }

  async switchNetworkRail(testnet, mintUrl = undefined, startLocalMint = undefined) {
    try {
      const payload = { testnet };
      if (mintUrl !== undefined) payload.mint_url = mintUrl;
      if (startLocalMint !== undefined) payload.start_local_mint = startLocalMint;

      const res = await fetch("/api/network", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      await this.fetchAll();
      const railLabel = data.testnet
        ? "TESTNET (Bitcoin Signet 0a03cf40 + Arbitrum Sepolia 421614)"
        : "MAINNET (Bitcoin f9beb4d9 + Arbitrum 42161)";
      this.showToast(`Switched to ${railLabel} • Active Mint: ${data.mint_url}`, "🌐", false);
    } catch (err) {
      this.showToast(`Failed to switch network rail: ${err}`, "⚠️", true);
    }
  }

  async toggleLocalSignetMint() {
    const isRunning =
      this.lastSummary &&
      this.lastSummary.cashu &&
      this.lastSummary.cashu.local_signet_mint &&
      this.lastSummary.cashu.local_signet_mint.running;
    const nextState = !isRunning;
    await this.switchNetworkRail(
      nextState ? true : this.lastSummary?.testnet,
      nextState ? "http://127.0.0.1:3338" : undefined,
      nextState
    );
  }

  async togglePause() {
    const action = this.isPaused ? "resume" : "pause";
    try {
      await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      await this.fetchSummary();
      this.renderCurrentState();
    } catch (err) {
      console.error("Error toggling pause:", err);
    }
  }

  async forceTick() {
    try {
      this.btnTick.disabled = true;
      this.btnTick.textContent = "Refreshing...";
      await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "tick" }),
      });
      await this.fetchAll();
    } catch (err) {
      console.error("Error running tick:", err);
    } finally {
      this.btnTick.disabled = false;
      this.btnTick.textContent = "Refresh";
    }
  }

  async toggleChannelAuto(field, enabled) {
    try {
      this.lastAutoToggleTs = Date.now();
      this.optimisticAutomation = {
        ...(this.lastSummary?.wallet_overview?.automation || {}),
        ...(this.optimisticAutomation || {}),
        [field]: enabled,
      };
      if (this.lastSummary?.wallet_overview) {
        this.lastSummary.wallet_overview.automation = { ...this.optimisticAutomation };
      }
      const res = await fetch("/api/channel-management/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: enabled }),
      });
      if (res.ok) {
        const data = await res.json();
        if (data.automation) {
          this.optimisticAutomation = { ...data.automation };
          if (this.lastSummary?.wallet_overview) {
            this.lastSummary.wallet_overview.automation = { ...data.automation };
          }
        }
      }
      const labels = {
        auto_cashu: "Cashu Vault Automation (Mint/Melt)",
        auto_btc_channels: "BTC Channels Automation",
        auto_lithium_channels: "Lithium Channels Automation",
      };
      this.showToast(`${labels[field] || field} ${enabled ? "Enabled" : "Disabled"}`, enabled ? "🤖" : "🔒", false);
      await this.fetchAll();
    } catch (err) {
      console.error(`Error toggling ${field}:`, err);
    }
  }

  async executeManualSweep() {
    try {
      this.btnManualSweep.disabled = true;
      this.btnManualSweep.innerHTML = `<span class="arrow-label">MINTING...</span>`;
      const res = await fetch("/api/cashu/sweep", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount_usd: 25.0 }),
      });
      const data = await res.json();
      if (data.status === "SUCCESS" || data.status === "COMPLETED") {
        this.showToast(
          `Minted ${(data.amount_sats || 0).toLocaleString()} sats into Cashu Vault & restored BTC Inbound capacity.`,
          "⚡"
        );
      } else {
        this.showToast(`Mint result: ${data.message || data.error || "No action taken"}`, "⚠️", true);
      }
      await this.fetchAll();
    } catch (err) {
      this.showToast(`Error during Cashu Mint: ${err}`, "⚠️", true);
    } finally {
      this.btnManualSweep.disabled = false;
      this.btnManualSweep.innerHTML = `<span class="arrow-label">MINT</span><span class="arrow-glyph">&rarr;</span>`;
    }
  }

  async executeManualRefuel() {
    try {
      this.btnManualRefuel.disabled = true;
      this.btnManualRefuel.innerHTML = `<span class="arrow-label">MELTING...</span>`;
      const res = await fetch("/api/cashu/refuel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount_usd: 25.0 }),
      });
      const data = await res.json();
      if (data.status === "SUCCESS" || data.status === "COMPLETED") {
        this.showToast(
          `Melted ${(data.melted_sats || data.amount_sats || 0).toLocaleString()} sats from Cashu Vault into BTC Send capacity.`,
          "🔥"
        );
      } else {
        this.showToast(`Melt result: ${data.message || data.error || "No action taken"}`, "⚠️", true);
      }
      await this.fetchAll();
    } catch (err) {
      this.showToast(`Error during Cashu Melt: ${err}`, "⚠️", true);
    } finally {
      this.btnManualRefuel.disabled = false;
      this.btnManualRefuel.innerHTML = `<span class="arrow-glyph">&larr;</span><span class="arrow-label">MELT</span>`;
    }
  }

  openCashuInitModal() {
    if (this.cashuInitModal) {
      this.cashuInitModal.classList.add("show");
    }
  }

  closeCashuInitModal() {
    if (this.cashuInitModal) {
      this.cashuInitModal.classList.remove("show");
    }
  }

  async confirmInitializeCashuVault() {
    try {
      if (this.btnConfirmCashuInit) {
        this.btnConfirmCashuInit.disabled = true;
        this.btnConfirmCashuInit.textContent = "Initializing...";
      }
      const res = await fetch("/api/cashu/initialize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (data.status === "SUCCESS") {
        this.closeCashuInitModal();
        this.showToast("Cashu Ecash Vault initialized and connected to community Mint.", "⚡", false);
        await this.fetchAll();
      } else {
        this.showToast(data.error || "Failed to initialize Cashu Vault", "⚠️", true);
      }
    } catch (err) {
      this.showToast(`Error initializing Cashu Vault: ${err}`, "⚠️", true);
    } finally {
      if (this.btnConfirmCashuInit) {
        this.btnConfirmCashuInit.disabled = false;
        this.btnConfirmCashuInit.innerHTML = "I Understand &mdash; Initialize Vault";
      }
    }
  }

  async openExportModal() {
    this.exportModal.classList.add("show");
    this.txtTokenString.value = "Generating token...";
    this.modalTokenAmount.textContent = "Loading...";

    try {
      const res = await fetch("/api/cashu/export", { method: "POST" });
      const data = await res.json();
      if (data.token) {
        this.txtTokenString.value = data.token;
        this.modalTokenAmount.textContent = `${(data.amount_sats || 0).toLocaleString()} sats`;
      } else {
        this.txtTokenString.value = data.error || "Vault is currently empty (0 sats).";
        this.modalTokenAmount.textContent = "0 sats";
      }
    } catch (err) {
      this.txtTokenString.value = `Error generating token: ${err}`;
      this.modalTokenAmount.textContent = "0 sats";
    }
  }

  closeExportModal() {
    this.exportModal.classList.remove("show");
  }

  copyTokenToClipboard() {
    const text = this.txtTokenString.value;
    if (!text || text.startsWith("Vault") || text.startsWith("Error")) return;

    navigator.clipboard.writeText(text).then(() => {
      const prev = this.btnCopyToken.textContent;
      this.btnCopyToken.textContent = "Copied! ✓";
      this.btnCopyToken.style.background = "var(--green)";
      setTimeout(() => {
        this.btnCopyToken.textContent = prev;
        this.btnCopyToken.style.background = "";
      }, 2000);
    });
  }
}

// Instantiate on DOM content loaded
document.addEventListener("DOMContentLoaded", () => {
  window.dlmmApp = new DLMMApp();
});
