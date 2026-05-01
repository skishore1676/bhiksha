const state = {
  bundle: null,
  chart: null,
  candleSeries: null,
  daySeparatorSeries: [],
  holdWindowSeries: null,
  selectedTradeId: null,
  current: null,
  timeLabelByTime: new Map(),
};

const els = {
  sourceSummary: document.getElementById("sourceSummary"),
  symbolSelect: document.getElementById("symbolSelect"),
  strategySelect: document.getElementById("strategySelect"),
  outcomeSelect: document.getElementById("outcomeSelect"),
  barWindowSelect: document.getElementById("barWindowSelect"),
  collapseGapsToggle: document.getElementById("collapseGapsToggle"),
  chartTitle: document.getElementById("chartTitle"),
  chartSubtitle: document.getElementById("chartSubtitle"),
  warningBadge: document.getElementById("warningBadge"),
  chart: document.getElementById("chart"),
  detailsPanel: document.getElementById("detailsPanel"),
  tradeCount: document.getElementById("tradeCount"),
  tradeList: document.getElementById("tradeList"),
};

init();

async function init() {
  const response = await fetch("./data.json", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Could not load data.json: ${response.status}`);
  }
  state.bundle = await response.json();
  buildControls();
  createChart();
  updateView();
  window.addEventListener("resize", resizeChart);
}

function buildControls() {
  const symbols = Object.keys(state.bundle.symbols).sort();
  els.symbolSelect.innerHTML = symbols.map((symbol) => `<option value="${symbol}">${symbol}</option>`).join("");
  els.symbolSelect.value = defaultSymbol(symbols);

  const strategies = new Set(["all"]);
  for (const symbol of symbols) {
    for (const trade of state.bundle.symbols[symbol].trades) {
      strategies.add(strategyName(trade.deploymentId));
    }
  }
  els.strategySelect.innerHTML = Array.from(strategies)
    .map((strategy) => `<option value="${strategy}">${strategy === "all" ? "All" : strategy}</option>`)
    .join("");

  els.sourceSummary.textContent = `Trades: ${state.bundle.metadata.tradeSource} · Candles: ${state.bundle.metadata.candleSource} · Generated ${formatDateTime(state.bundle.metadata.generatedAt)}`;

  els.symbolSelect.addEventListener("change", updateView);
  els.strategySelect.addEventListener("change", updateView);
  els.outcomeSelect.addEventListener("change", updateView);
  els.collapseGapsToggle.addEventListener("change", updateView);
  els.barWindowSelect.addEventListener("change", () => {
    if (state.selectedTradeId) selectTrade(state.selectedTradeId, true);
  });
}

function createChart() {
  state.chart = LightweightCharts.createChart(els.chart, {
    localization: {
      timeFormatter: (time) => formatChartTime(time, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
    },
    layout: {
      background: { color: "#0f1115" },
      textColor: "#c8d0dc",
    },
    grid: {
      vertLines: { color: "#202630" },
      horzLines: { color: "#202630" },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
    },
    rightPriceScale: {
      borderColor: "#2d3440",
    },
    timeScale: {
      borderColor: "#2d3440",
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: (time) => formatChartTime(time, { hour: "numeric", minute: "2-digit" }),
    },
  });

  state.candleSeries = state.chart.addCandlestickSeries({
    upColor: "#34c985",
    downColor: "#f05d5d",
    borderUpColor: "#34c985",
    borderDownColor: "#f05d5d",
    wickUpColor: "#34c985",
    wickDownColor: "#f05d5d",
  });
  state.holdWindowSeries = state.chart.addLineSeries({
    color: "#f5c84c",
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
  resizeChart();
}

function updateView() {
  const symbol = els.symbolSelect.value;
  const symbolData = state.bundle.symbols[symbol];
  const trades = filteredTrades(symbolData.trades);
  state.current = chartState(symbolData, trades);

  state.candleSeries.setData(state.current.candles);
  state.candleSeries.setMarkers(markersForTrades(state.current.trades));
  renderDaySeparators(state.current.candles);
  state.holdWindowSeries.setData([]);
  state.chart.timeScale().fitContent();

  els.chartTitle.textContent = symbol;
  els.chartSubtitle.textContent = `${symbolData.candles.length} candles · ${trades.length} visible trades · axis CT${els.collapseGapsToggle.checked ? " · gaps collapsed" : ""}`;
  setWarning(symbolData.warnings || []);
  renderTradeList(state.current.trades);

  if (state.current.trades.length > 0) {
    selectTrade(state.current.trades[state.current.trades.length - 1].tradeId, true);
  } else {
    state.selectedTradeId = null;
    renderDetails(null);
  }
}

function chartState(symbolData, trades) {
  const collapse = els.collapseGapsToggle.checked;
  const baseTime = symbolData.candles[0]?.time || 0;
  const actualToPlotTime = new Map();
  const indexByTime = new Map();
  const timeLabelByTime = new Map();

  const candles = symbolData.candles.map((candle, index) => {
    const plotTime = collapse ? baseTime + index * 60 : candle.time;
    actualToPlotTime.set(candle.time, plotTime);
    indexByTime.set(plotTime, index);
    timeLabelByTime.set(plotTime, candle.iso);
    const sessionDate = centralDate(candle.iso);
    return {
      ...candle,
      time: plotTime,
      actualTime: candle.time,
      sessionDate,
    };
  });

  state.timeLabelByTime = timeLabelByTime;

  const plottedTrades = trades.map((trade) => ({
    ...trade,
    entryPlotTime: actualToPlotTime.get(trade.entryMarkerTime) || trade.entryMarkerTime || trade.entryTime,
    exitPlotTime: trade.exitMarkerTime ? actualToPlotTime.get(trade.exitMarkerTime) || trade.exitMarkerTime : null,
  }));

  return {
    candles,
    trades: plottedTrades,
    indexByTime,
  };
}

function renderDaySeparators(candles) {
  for (const series of state.daySeparatorSeries) {
    state.chart.removeSeries(series);
  }
  state.daySeparatorSeries = [];

  for (const separator of daySeparatorData(candles)) {
    const series = state.chart.addLineSeries({
      color: "rgba(120, 169, 255, 0.48)",
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    series.setData([
      { time: separator.time, value: separator.low },
      { time: separator.time + 1, value: separator.high },
    ]);
    state.daySeparatorSeries.push(series);
  }
}

function filteredTrades(trades) {
  const strategy = els.strategySelect.value;
  const outcome = els.outcomeSelect.value;
  return trades.filter((trade) => {
    const strategyOk = strategy === "all" || strategyName(trade.deploymentId) === strategy;
    const outcomeOk =
      outcome === "all" ||
      (outcome === "winner" && trade.optionPnl !== null && trade.optionPnl > 0) ||
      (outcome === "loser" && trade.optionPnl !== null && trade.optionPnl < 0) ||
      (outcome === "open" && trade.exitTime === null);
    return strategyOk && outcomeOk;
  });
}

function markersForTrades(trades) {
  const markers = [];
  for (const trade of trades) {
    const isShort = trade.direction === "short";
    const contract = trade.contractType || "OPT";
    const labelId = shortId(trade.tradeId);
    markers.push({
      time: trade.entryPlotTime,
      position: isShort ? "aboveBar" : "belowBar",
      color: isShort ? "#f05d5d" : "#34c985",
      shape: isShort ? "arrowDown" : "arrowUp",
      text: `B ${contract} ${labelId}`,
    });
    if (trade.exitTime) {
      markers.push({
        time: trade.exitPlotTime || trade.exitTime,
        position: isShort ? "belowBar" : "aboveBar",
        color: "#f5c84c",
        shape: "circle",
        text: `S ${contract} ${labelId}`,
      });
    }
  }
  return markers;
}

function renderTradeList(trades) {
  els.tradeCount.textContent = `${trades.length}`;
  if (trades.length === 0) {
    els.tradeList.innerHTML = '<div class="empty-state">No trades match the current filters.</div>';
    return;
  }

  els.tradeList.innerHTML = trades
    .map((trade) => {
      const pnlClass = trade.optionPnl > 0 ? "pnl-good" : trade.optionPnl < 0 ? "pnl-bad" : "";
      const id = shortId(trade.tradeId);
      const contract = trade.contractType || "OPTION";
      return `
        <button class="trade-card ${trade.tradeId === state.selectedTradeId ? "active" : ""}" data-trade-id="${trade.tradeId}">
          <div class="trade-card-top">
            <span class="trade-card-title">${id} · BUY ${contract}</span>
            <span class="${pnlClass}">${trade.optionPnl === null ? "open" : money(trade.optionPnl)}</span>
          </div>
          <div class="trade-card-time">${formatCentralDateTime(trade.entryIso)} → ${trade.exitIso ? formatCentralDateTime(trade.exitIso) : "open"}</div>
          <div class="trade-card-meta">${trade.direction.toUpperCase()} ${trade.symbol} · ${trade.optionSymbol || "no option"} x${trade.quantity}</div>
          <div class="trade-card-meta">${strategyName(trade.deploymentId)}</div>
        </button>
      `;
    })
    .join("");

  for (const card of els.tradeList.querySelectorAll(".trade-card")) {
    card.addEventListener("click", () => selectTrade(card.dataset.tradeId, true));
  }
}

function selectTrade(tradeId, zoom) {
  state.selectedTradeId = tradeId;
  const symbolData = state.bundle.symbols[els.symbolSelect.value];
  const trade = symbolData.trades.find((item) => item.tradeId === tradeId);
  renderDetails(trade);
  renderHoldWindow(trade);
  for (const card of els.tradeList.querySelectorAll(".trade-card")) {
    card.classList.toggle("active", card.dataset.tradeId === tradeId);
  }

  if (zoom && trade) {
    zoomToTrade(trade);
  }
}

function renderHoldWindow(trade) {
  if (!trade || !state.current?.candles.length) {
    state.holdWindowSeries.setData([]);
    return;
  }
  const entryIndex = state.current.indexByTime.get(trade.entryPlotTime);
  const exitIndex = trade.exitPlotTime ? state.current.indexByTime.get(trade.exitPlotTime) : entryIndex;
  if (entryIndex === undefined || exitIndex === undefined) {
    state.holdWindowSeries.setData([]);
    return;
  }
  const fromIndex = Math.min(entryIndex, exitIndex);
  const toIndex = Math.max(entryIndex, exitIndex);
  const low = Math.min(...state.current.candles.slice(fromIndex, toIndex + 1).map((candle) => candle.low));
  const y = low * 0.998;
  state.holdWindowSeries.setData([
    { time: state.current.candles[fromIndex].time, value: y },
    { time: state.current.candles[toIndex].time, value: y },
  ]);
}

function zoomToTrade(trade) {
  const windowValue = els.barWindowSelect.value;
  if (windowValue === "all" || !state.current?.candles.length) {
    state.chart.timeScale().fitContent();
    return;
  }

  const entryIndex = state.current.indexByTime.get(trade.entryPlotTime);
  const exitIndex = trade.exitPlotTime ? state.current.indexByTime.get(trade.exitPlotTime) : entryIndex;
  if (entryIndex === undefined) {
    return;
  }

  const minIndex = Math.min(entryIndex, exitIndex ?? entryIndex);
  const maxIndex = Math.max(entryIndex, exitIndex ?? entryIndex);
  const requestedBars = Number(windowValue);
  const selectedSpan = maxIndex - minIndex + 1;
  const targetBars = Math.max(requestedBars, selectedSpan + 20);
  const center = Math.round((minIndex + maxIndex) / 2);
  const fromIndex = Math.max(0, center - Math.floor(targetBars / 2));
  const toIndex = Math.min(state.current.candles.length - 1, fromIndex + targetBars - 1);
  const from = state.current.candles[fromIndex].time;
  const to = state.current.candles[toIndex].time;
  state.chart.timeScale().setVisibleLogicalRange({ from: fromIndex, to: toIndex });
}

function renderDetails(trade) {
  if (!trade) {
    els.detailsPanel.innerHTML = '<div class="empty-state">Select a trade to inspect its context.</div>';
    return;
  }
  const pnlClass = trade.optionPnl > 0 ? "pnl-good" : trade.optionPnl < 0 ? "pnl-bad" : "";
  els.detailsPanel.innerHTML = `
    <div class="metric"><span>Trade</span><strong>${shortId(trade.tradeId)} · ${trade.direction.toUpperCase()} ${trade.symbol}</strong></div>
    <div class="metric"><span>Entry</span><strong>${trade.entryAction || "BUY"} ${trade.contractType || "OPTION"} · ${formatCentralDateTime(trade.entryIso)} · ${price(trade.underlyingEntryPrice)}</strong></div>
    <div class="metric"><span>Exit</span><strong>${trade.exitIso ? `${trade.exitAction || "SELL"} ${trade.contractType || "OPTION"} · ${formatCentralDateTime(trade.exitIso)} · ${price(trade.underlyingExitApprox)}` : "open"}</strong></div>
    <div class="metric"><span>Option</span><strong>${trade.optionSymbol || "n/a"} x${trade.quantity}</strong></div>
    <div class="metric"><span>Option P/L</span><strong class="${pnlClass}">${trade.optionPnl === null ? "n/a" : money(trade.optionPnl)}</strong></div>
    <div class="metric"><span>Entry Premium</span><strong>${price(trade.entryPrice)}</strong></div>
    <div class="metric"><span>Exit Premium</span><strong>${price(trade.exitPrice)}</strong></div>
    <div class="metric"><span>Exit Mode</span><strong>${trade.exitMode || trade.status}</strong></div>
    <div class="metric"><span>Deployment</span><strong title="${trade.deploymentId}">${strategyName(trade.deploymentId)}</strong></div>
  `;
}

function daySeparatorData(candles) {
  const separators = [];
  let previousDate = null;
  const lows = candles.map((candle) => candle.low);
  const highs = candles.map((candle) => candle.high);
  const low = Math.min(...lows);
  const high = Math.max(...highs);
  for (const candle of candles) {
    if (previousDate !== null && candle.sessionDate !== previousDate) {
      separators.push({ time: candle.time, low, high });
    }
    previousDate = candle.sessionDate;
  }
  return separators;
}

function setWarning(warnings) {
  const missing = [...(state.bundle.metadata.warnings || []), ...warnings];
  if (missing.length === 0) {
    els.warningBadge.hidden = true;
    els.warningBadge.textContent = "";
    return;
  }
  els.warningBadge.hidden = false;
  els.warningBadge.textContent = missing[0];
}

function resizeChart() {
  if (!state.chart) return;
  state.chart.applyOptions({
    width: els.chart.clientWidth,
    height: els.chart.clientHeight,
  });
}

function strategyName(deploymentId) {
  return deploymentId
    .replace(/^strategy_/, "")
    .replace(/_live_row_\d+$/, "")
    .replace(/_shadow_row_\d+$/, "")
    .replace(/_[a-f0-9]{12}$/, "");
}

function defaultSymbol(symbols) {
  let bestSymbol = symbols[0];
  let bestTime = 0;
  for (const symbol of symbols) {
    const trades = state.bundle.symbols[symbol].trades;
    const latest = trades.reduce((maxTime, trade) => Math.max(maxTime, trade.entryTime || 0), 0);
    if (latest > bestTime) {
      bestTime = latest;
      bestSymbol = symbol;
    }
  }
  return bestSymbol;
}

function shortId(value) {
  return String(value).slice(0, 4);
}

function money(value) {
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

function price(value) {
  if (value === null || value === undefined) return "n/a";
  return Number(value).toFixed(2);
}

function formatDateTime(value) {
  if (!value) return "n/a";
  return new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatCentralDateTime(value) {
  if (!value) return "n/a";
  return new Date(value).toLocaleString(undefined, {
    timeZone: "America/Chicago",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

function formatChartTime(time, options) {
  const timestamp = typeof time === "number" ? time : time.timestamp;
  if (!timestamp) return "";
  const iso = state.timeLabelByTime.get(timestamp) || nearestVisibleIso(timestamp);
  return new Date(iso || timestamp * 1000).toLocaleString(undefined, {
    timeZone: "America/Chicago",
    ...options,
  });
}

function nearestVisibleIso(timestamp) {
  if (!state.current?.candles.length) return null;
  let closest = state.current.candles[0];
  let closestDistance = Math.abs(closest.time - timestamp);
  for (const candle of state.current.candles) {
    const distance = Math.abs(candle.time - timestamp);
    if (distance < closestDistance) {
      closest = candle;
      closestDistance = distance;
    }
  }
  return closest.iso;
}

function centralDate(value) {
  return new Date(value).toLocaleDateString("en-CA", {
    timeZone: "America/Chicago",
  });
}
