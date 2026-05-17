const state = {
  context: null,
  consultation: null,
  intent: null,
  preview: null,
  submitted: null,
  submitHoldTimer: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || response.statusText);
  }
  return payload;
}

function log(message, payload) {
  const detail = payload ? `\n${JSON.stringify(payload, null, 2)}` : "";
  $("eventLog").textContent = `[${new Date().toLocaleTimeString()}] ${message}${detail}\n\n${$("eventLog").textContent}`;
}

function pill(label, ok, detail = "") {
  const cls = ok === true ? "ok" : ok === false ? "bad" : "warn";
  return `<div class="pill ${cls}"><strong>${label}</strong><span>${detail}</span></div>`;
}

function renderContext(payload) {
  state.context = payload;
  const preflight = payload.preflight || {};
  const health = payload.health || {};
  const providers = health.providers || [];
  const quote = payload.quote || {};
  $("clock").textContent = `${payload.market_timestamp || ""} ${payload.rth_open ? "RTH" : "Closed"}`;
  $("quoteBadge").textContent = quote.ok ? `${quote.symbol} ${Number(quote.price).toFixed(2)}` : "No quote";
  $("readiness").innerHTML = [
    pill("Packet", preflight.executable, preflight.eligibility || "unknown"),
    pill("Runtime", true, preflight.runtime_mode || "unknown"),
    pill("Market", payload.rth_open, payload.rth_open ? "open" : "closed"),
    ...providers.map((provider) => pill(provider.name, provider.ok, provider.detail || "")),
  ].join("");
  const policies = preflight.management_policy_ids || [];
  setPolicyOptions(policies);
}

function setPolicyOptions(policies) {
  const current = $("managementPolicy").value;
  $("managementPolicy").innerHTML = policies
    .map((policy) => `<option value="${policy}">${policy}</option>`)
    .join("");
  if (policies.includes(current)) {
    $("managementPolicy").value = current;
  }
}

function summarizeConsultation(result) {
  return [
    `verdict: ${result.verdict || "unknown"}`,
    `policy: ${result.policy || "unknown"}`,
    `exit: ${result.selected_exit || "none"}`,
    `time: ${result.timestamp}`,
  ].join("\n");
}

function summarizePreview(result) {
  return [
    `option: ${result.option_symbol || "none"}`,
    `quantity: ${result.quantity}`,
    `entry: ${result.estimated_entry_price}`,
    `underlying: ${result.underlying_entry_price}`,
    `stop: ${result.underlying_stop_price}`,
    `blocks: ${(result.block_reasons || []).join(", ") || "none"}`,
  ].join("\n");
}

function renderOrderSummary() {
  if (!state.preview || state.preview.status !== "option_preview_ready") {
    $("orderSummary").classList.add("empty");
    $("orderSummary").textContent = "Preview a trade first.";
    $("approveSubmitBtn").disabled = true;
    return;
  }
  $("orderSummary").classList.remove("empty");
  $("orderSummary").innerHTML = [
    `<strong>${state.preview.option_symbol}</strong>`,
    `<span>Qty ${state.preview.quantity} @ ${state.preview.estimated_entry_price}</span>`,
    `<span>Stop ${state.preview.underlying_stop_price} | Policy ${state.preview.selected_management_policy_id}</span>`,
  ].join("");
  $("approveSubmitBtn").disabled = false;
}

function summarizeSubmit(result) {
  const lifecycle = result.lifecycle || {};
  return [
    `status: ${result.status}`,
    `trade_state: ${lifecycle.trade_state || ""}`,
    `entry_order: ${lifecycle.entry_order_id || ""}`,
    `stop_order: ${lifecycle.stop_order_id || ""}`,
    `target_order: ${lifecycle.target_order_id || ""}`,
    `blocks: ${(result.block_reasons || []).join(", ") || "none"}`,
  ].join("\n");
}

async function refreshContext() {
  const symbol = $("symbol").value || "QQQ";
  const payload = await api(`/api/live-context?symbol=${encodeURIComponent(symbol)}`);
  renderContext(payload);
  return payload;
}

async function consult() {
  const body = {
    symbol: $("symbol").value,
    direction: $("direction").value,
    chart_read: $("chartRead").value,
  };
  const override = $("timestamp").value.trim();
  if (override) {
    body.timestamp = override;
  }
  const result = await api("/api/consult", { method: "POST", body: JSON.stringify(body) });
  state.consultation = result;
  $("consultationSummary").classList.remove("empty");
  $("consultationSummary").textContent = summarizeConsultation(result);
  setPolicyOptions(result.allowed_management_policy_ids || []);
  log("Consulted Mala", { verdict: result.verdict, timestamp: result.timestamp });
}

async function takeAndPreview() {
  if (!state.consultation) {
    await consult();
  }
  const decision = await api("/api/decision", {
    method: "POST",
    body: JSON.stringify({
      consultation_artifact: state.consultation.artifact_json,
      decision: "take",
      selected_management_policy_id: $("managementPolicy").value,
      operator_note: "Take from Trader Desk cockpit",
    }),
  });
  state.intent = decision;
  const body = {
    intent_artifact: decision.artifact_json,
    preview_mode: $("previewMode").value,
    symbol: decision.symbol,
    direction: decision.direction,
    underlying_stop_price: $("underlyingStop").value,
  };
  if ($("underlyingPrice").value) {
    body.underlying_price = $("underlyingPrice").value;
  }
  const preview = await api("/api/option-preview", { method: "POST", body: JSON.stringify(body) });
  state.preview = preview;
  $("previewSummary").classList.remove("empty");
  $("previewSummary").textContent = summarizePreview(preview);
  renderOrderSummary();
  log("Option preview ready", { status: preview.status, option: preview.option_symbol });
}

async function approveSubmit() {
  if (!state.preview || state.preview.status !== "option_preview_ready") {
    throw new Error("Ready option preview is required first.");
  }
  const result = await api("/api/approve-submit", {
    method: "POST",
    body: JSON.stringify({
      option_preview_artifact: state.preview.artifact_json,
      approval_confirmed: true,
      operator: "Suman",
      operator_note: $("submitNote").value || "Approved from Trader Desk cockpit",
    }),
  });
  state.submitted = result;
  $("submitSummary").classList.remove("empty");
  $("submitSummary").textContent = summarizeSubmit(result);
  await refreshLiveState();
  log("Approve+Submit completed", { status: result.status });
}

async function refreshLiveState() {
  const payload = await api("/api/live-management/status");
  $("liveState").classList.remove("empty");
  $("liveState").textContent = [
    `status: ${payload.status}`,
    `trade_state: ${payload.trade_state || "none"}`,
    `critical: ${payload.critical}`,
  ].join("\n");
}

function bind(id, fn) {
  $(id).addEventListener("click", async () => {
    try {
      await fn();
    } catch (error) {
      log("Action blocked", { error: error.message });
      alert(error.message);
    }
  });
}

function bindHoldToSubmit() {
  const button = $("approveSubmitBtn");
  const start = () => {
    if (button.disabled) return;
    button.classList.add("holding");
    button.textContent = "Keep Holding...";
    state.submitHoldTimer = setTimeout(async () => {
      try {
        button.textContent = "Submitting...";
        await approveSubmit();
      } catch (error) {
        log("Submit blocked", { error: error.message });
        alert(error.message);
      } finally {
        button.classList.remove("holding");
        button.textContent = "Hold To Submit Live Order";
      }
    }, 900);
  };
  const cancel = () => {
    clearTimeout(state.submitHoldTimer);
    if (!button.classList.contains("holding")) return;
    button.classList.remove("holding");
    button.textContent = "Hold To Submit Live Order";
  };
  button.addEventListener("mousedown", start);
  button.addEventListener("touchstart", start);
  button.addEventListener("mouseup", cancel);
  button.addEventListener("mouseleave", cancel);
  button.addEventListener("touchend", cancel);
}

bind("refreshBtn", refreshContext);
bind("consultBtn", consult);
bind("takePreviewBtn", takeAndPreview);
bind("liveStateBtn", refreshLiveState);
bindHoldToSubmit();

$("symbol").addEventListener("change", () => refreshContext().catch((error) => log("Context refresh failed", { error: error.message })));

refreshContext()
  .then(refreshLiveState)
  .catch((error) => log("Initial load failed", { error: error.message }));
