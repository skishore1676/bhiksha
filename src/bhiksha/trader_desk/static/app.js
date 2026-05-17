const state = {
  consultation: null,
  intent: null,
  preview: null,
  ticket: null,
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
  const stamp = new Date().toLocaleTimeString();
  const detail = payload ? `\n${JSON.stringify(payload, null, 2)}` : "";
  $("eventLog").textContent = `[${stamp}] ${message}${detail}\n\n${$("eventLog").textContent}`;
}

function tile(label, ok, detail) {
  const cls = ok === true ? "ok" : ok === false ? "bad" : "warn";
  return `<div class="tile ${cls}"><strong>${label}</strong><span>${detail || ""}</span></div>`;
}

function artifactRows(latest) {
  return Object.entries(latest)
    .map(([key, value]) => {
      const status = value.status || "none";
      return `<div><strong>${key}</strong>: ${status}</div>`;
    })
    .join("");
}

function renderStatus(payload) {
  const preflight = payload.preflight || {};
  const playbook = (payload.playbooks || [])[0] || {};
  $("playbookTitle").textContent = playbook.title || playbook.id || "No playbook";
  $("playbookMeta").textContent = `${playbook.id || ""} v${playbook.version || ""} | ${playbook.runtime_mode || ""}`;
  $("playbookPolicies").innerHTML = (playbook.management_policy_ids || [])
    .map((policy) => `<div class="tile"><strong>${policy}</strong><span>Allowed management policy</span></div>`)
    .join("");
  $("nextStep").textContent = payload.next_step || "";
  $("latestArtifacts").innerHTML = artifactRows(payload.latest || {});
  $("systemTiles").innerHTML = [
    tile("Packet", preflight.executable, preflight.eligibility || "unknown"),
    tile("Runtime", true, preflight.runtime_mode || "unknown"),
    tile("Live Boundary", true, payload.safety_boundary || "guarded"),
  ].join("");
  if (payload.health) {
    $("systemTiles").innerHTML += (payload.health.providers || [])
      .map((provider) => tile(provider.name, provider.ok, provider.detail))
      .join("");
  }
  setPolicyOptions(playbook.management_policy_ids || []);
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
    `status: ${result.status}`,
    `verdict: ${result.verdict || "unknown"}`,
    `policy: ${result.policy || "unknown"}`,
    `selected_exit: ${result.selected_exit || "none"}`,
    `artifact: ${result.artifact_md}`,
  ].join("\n");
}

function summarizeDecision(result) {
  return [
    `status: ${result.status}`,
    `decision: ${result.decision}`,
    `execution_ready: ${result.execution_ready}`,
    `policy: ${result.selected_management_policy_id || "none"}`,
    `warnings: ${(result.warning_reasons || []).join(", ") || "none"}`,
  ].join("\n");
}

function summarizePreview(result) {
  return [
    `status: ${result.status}`,
    `option: ${result.option_symbol || "none"}`,
    `quantity: ${result.quantity}`,
    `entry: ${result.estimated_entry_price}`,
    `stop: ${result.underlying_stop_price}`,
    `blocks: ${(result.block_reasons || []).join(", ") || "none"}`,
  ].join("\n");
}

function summarizeTicket(result) {
  return [
    `status: ${result.status}`,
    `order_submission_allowed: ${result.order_submission_allowed}`,
    `option: ${result.option_symbol || "none"}`,
    `quantity: ${result.quantity}`,
    `limit: ${result.limit_price}`,
    `blocks: ${(result.block_reasons || []).join(", ") || "none"}`,
  ].join("\n");
}

async function refreshStatus(includeHealth = false) {
  const payload = await api(`/api/status${includeHealth ? "?health=1" : ""}`);
  renderStatus(payload);
  log(includeHealth ? "Health refreshed" : "Status refreshed");
}

async function consult() {
  const result = await api("/api/consult", {
    method: "POST",
    body: JSON.stringify({
      symbol: $("symbol").value,
      direction: $("direction").value,
      timestamp: $("timestamp").value,
      chart_read: $("chartRead").value,
    }),
  });
  state.consultation = result;
  $("consultationSummary").classList.remove("empty");
  $("consultationSummary").textContent = summarizeConsultation(result);
  setPolicyOptions(result.allowed_management_policy_ids || []);
  log("Consultation complete", result);
  await refreshStatus();
}

async function decide(decision) {
  if (!state.consultation) {
    throw new Error("Consultation is required first.");
  }
  const result = await api("/api/decision", {
    method: "POST",
    body: JSON.stringify({
      consultation_artifact: state.consultation.artifact_json,
      decision,
      selected_management_policy_id: decision === "take" ? $("managementPolicy").value : "",
      operator_note: $("operatorNote").value || `${decision} from Trader Desk`,
    }),
  });
  state.intent = result;
  $("consultationSummary").textContent = `${summarizeConsultation(state.consultation)}\n\n${summarizeDecision(result)}`;
  log(`Decision recorded: ${decision}`, result);
  await refreshStatus();
}

async function previewOption() {
  if (!state.intent) {
    throw new Error("Take decision is required first.");
  }
  const result = await api("/api/option-preview", {
    method: "POST",
    body: JSON.stringify({
      intent_artifact: state.intent.artifact_json,
      underlying_price: $("underlyingPrice").value,
      underlying_stop_price: $("underlyingStop").value,
    }),
  });
  state.preview = result;
  $("previewSummary").classList.remove("empty");
  $("previewSummary").textContent = summarizePreview(result);
  log("Option preview complete", result);
  await refreshStatus();
}

async function liveTicket(decision) {
  if (!state.preview) {
    throw new Error("Option preview is required first.");
  }
  const result = await api("/api/live-ticket", {
    method: "POST",
    body: JSON.stringify({
      option_preview_artifact: state.preview.artifact_json,
      decision,
      operator: "Suman",
      operator_note: $("ticketNote").value || `${decision} from Trader Desk`,
      approval_phrase: $("approvalPhrase").value,
    }),
  });
  state.ticket = result;
  $("ticketSummary").classList.remove("empty");
  $("ticketSummary").textContent = summarizeTicket(result);
  log(`Live ticket ${decision}`, result);
  await refreshStatus();
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

bind("refreshStatus", () => refreshStatus(false));
bind("refreshHealth", () => refreshStatus(true));
bind("consultBtn", consult);
bind("takeBtn", () => decide("take"));
bind("watchBtn", () => decide("watch"));
bind("passBtn", () => decide("pass"));
bind("previewBtn", previewOption);
bind("approveTicketBtn", () => liveTicket("approve"));
bind("rejectTicketBtn", () => liveTicket("reject"));

refreshStatus(false).catch((error) => log("Initial status failed", { error: error.message }));
