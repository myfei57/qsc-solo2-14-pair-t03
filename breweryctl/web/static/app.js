const STATE = {
  batches: [],
  tanks: [],
};

async function apiGet(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  return parseResponse(response);
}

async function apiPost(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  return parseResponse(response);
}

async function parseResponse(response) {
  let body;
  try {
    body = await response.json();
  } catch (error) {
    body = { error: "invalid_response", message: String(error) };
  }
  if (!response.ok) {
    const message = body && body.message ? body.message : response.statusText;
    throw new Error(body.error + ": " + message);
  }
  return body;
}

function element(id) {
  return document.getElementById(id);
}

function write(id, value) {
  const target = element(id);
  if (target) {
    target.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }
}

function addLog(id, message, payload) {
  const target = element(id);
  if (!target) {
    return;
  }
  const entry = document.createElement("div");
  entry.className = "log-entry";
  const stamp = new Date().toLocaleTimeString();
  entry.textContent = "[" + stamp + "] " + message + (payload ? " " + JSON.stringify(payload) : "");
  target.prepend(entry);
}

function value(id) {
  const target = element(id);
  return target ? target.value.trim() : "";
}

function numberValue(id) {
  return Number(value(id));
}

async function run(action, id, onSuccess) {
  try {
    const payload = await action();
    if (onSuccess) {
      onSuccess(payload);
    }
    return payload;
  } catch (error) {
    addLog(id, "失败：" + error.message);
    write(id + "-result", { error: error.message });
    return null;
  }
}

async function loadBanner() {
  const overview = await apiGet("/api/state");
  const banner = overview.banner;
  write("banner-batches", banner.active_batches);
  write("banner-tanks", banner.busy_tanks);
  write("banner-alarms", banner.active_alarms);
  write("banner-latches", banner.latching_alarms);
  write("banner-store", overview.store.collections.batches || 0);
  return overview;
}

async function loadBatches(selectId) {
  const payload = await apiGet("/api/batches");
  STATE.batches = payload.batches;
  const select = element(selectId);
  if (select) {
    select.innerHTML = "";
    STATE.batches.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.code + " · " + item.stage;
      select.appendChild(option);
    });
  }
  return STATE.batches;
}

async function loadTanks(selectId) {
  const payload = await apiGet("/api/control/tanks");
  STATE.tanks = payload.tanks;
  const select = element(selectId);
  if (select) {
    select.innerHTML = "";
    STATE.tanks.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.code + " · " + item.stage;
      select.appendChild(option);
    });
  }
  return STATE.tanks;
}

async function refreshBatch() {
  const batchId = value("batch-select");
  if (!batchId) {
    return;
  }
  const view = await apiGet("/api/batches/" + batchId);
  write("batch-view", view);
  return view;
}

async function refreshTankSnapshot() {
  const tankId = value("tank-select");
  if (!tankId) {
    return;
  }
  const snapshot = await apiGet("/api/control/tanks/" + tankId);
  write("tank-view", snapshot);
  return snapshot;
}

async function initMashPage() {
  const overview = await loadBanner();
  write("store-view", overview.store);
  await loadBatches("batch-select");
  await refreshBatch();
  const recipes = await apiGet("/api/recipes");
  const published = recipes.recipes.filter((item) => item.status === "published");
  const select = element("recipe-select");
  published.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name + " · v" + item.version;
    select.appendChild(option);
  });
  write("recipe-view", published);
}

async function initFermentPage() {
  await loadBanner();
  await loadBatches("batch-select");
  await loadTanks("tank-select");
  await refreshQualifiedCultures();
  await refreshBatch();
  await refreshTankSnapshot();
}

async function refreshQualifiedCultures() {
  const payload = await apiGet("/api/yeast/cultures?status=qualified");
  const select = element("pitch-culture");
  if (!select) {
    return payload;
  }
  select.innerHTML = "";
  payload.cultures.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent =
      item.code + " · " + item.strain + " · 第" + item.generation + "代 · " + item.viability_pct + "%";
    select.appendChild(option);
  });
  if (!payload.cultures.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "无合格在库酵母，请先扩培检测";
    select.appendChild(option);
  }
  return payload;
}

async function initYeastPage() {
  const overview = await loadBanner();
  const yeast = overview.yeast || {};
  write("banner-yeast", yeast.qualified || 0);
  write("banner-yeast-quarantine", yeast.quarantined || 0);
  await loadYeastSummary();
  await loadCultures();
}

async function loadYeastSummary() {
  const payload = await apiGet("/api/yeast/summary");
  write("yeast-summary", payload.summary);
  return payload;
}

function fillCultureSelect(selectId, cultures, withEmpty) {
  const select = element(selectId);
  if (!select) {
    return;
  }
  select.innerHTML = "";
  if (withEmpty) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "（无母罐）";
    select.appendChild(empty);
  }
  cultures.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent =
      item.code + " · " + item.strain + " · 第" + item.generation + "代 · " + item.status;
    select.appendChild(option);
  });
}

async function loadCultures() {
  const payload = await apiGet("/api/yeast/cultures");
  STATE.cultures = payload.cultures;
  fillCultureSelect("culture-select", payload.cultures, false);
  const consumed = payload.cultures.filter((item) => item.status === "consumed");
  fillCultureSelect("reg-parent", consumed, true);
  renderCultureRows(payload.cultures);
  await loadCultureDetail();
  return payload;
}

function renderCultureRows(cultures) {
  const tbody = element("culture-rows");
  if (!tbody) {
    return;
  }
  tbody.innerHTML = "";
  cultures.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML =
      "<td>" + item.code + "</td>" +
      "<td>" + item.strain + "</td>" +
      "<td>" + item.source + "</td>" +
      "<td>第" + item.generation + "代</td>" +
      "<td>" + item.status + "</td>" +
      "<td>" + (item.viability_pct == null ? "-" : item.viability_pct) + "</td>" +
      "<td>" + item.volume_l + "</td>" +
      "<td>" + (item.consumed_batch_id || "-") + "</td>" +
      "<td></td>";
    const actions = row.lastElementChild;
    const pick = document.createElement("button");
    pick.textContent = "选择";
    pick.className = "secondary";
    pick.onclick = () => {
      element("culture-select").value = item.id;
      loadCultureDetail();
    };
    actions.appendChild(pick);
    tbody.appendChild(row);
  });
}

async function loadCultureDetail() {
  const cultureId = value("culture-select");
  if (!cultureId) {
    return null;
  }
  const payload = await apiGet("/api/yeast/cultures/" + cultureId);
  write("culture-detail", payload);
  return payload;
}

async function registerCulture() {
  const source = value("reg-source");
  const cells = value("reg-cells");
  const brewery = value("reg-brewery");
  let breweryId = brewery;
  if (!breweryId) {
    const overview = await apiGet("/api/state");
    breweryId = overview.namespace && overview.namespace.default_brewery_id;
  }
  const payload = await apiPost("/api/yeast/cultures", {
    brewery_id: breweryId,
    strain: value("reg-strain"),
    source: source,
    volume_l: numberValue("reg-volume"),
    cell_count_m_ml: cells ? Number(cells) : null,
    parent_id: source === "cropped" ? value("reg-parent") : null,
    operator: value("reg-operator") || "console",
  });
  addLog("yeast-log", "已登记 " + payload.culture.code, payload);
  return payload;
}

async function submitAssay() {
  const cultureId = value("culture-select");
  const cells = value("assay-cells");
  const payload = await apiPost("/api/yeast/cultures/" + cultureId + "/assay", {
    viability_pct: numberValue("assay-viability"),
    cell_count_m_ml: cells ? Number(cells) : null,
    operator: value("assay-operator") || "console",
  });
  addLog("assay-log", "检测完成：" + payload.culture.status, payload);
  return payload;
}

async function discardCulture() {
  const cultureId = value("culture-select");
  const payload = await apiPost("/api/yeast/cultures/" + cultureId + "/discard", {
    reason: value("discard-reason") || "人工废弃",
    operator: value("assay-operator") || "console",
  });
  addLog("assay-log", "已废弃 " + payload.culture.code, payload);
  return payload;
}

async function loadLineage() {
  const cultureId = value("culture-select");
  const payload = await apiGet("/api/yeast/cultures/" + cultureId + "/lineage");
  write("lineage-view", payload);
  return payload;
}

async function initCipPage() {
  await loadBanner();
  await loadTanks("tank-select");
  await refreshCertificate();
}

async function initAlarmsPage() {
  await loadBanner();
  await loadAlarms();
  await loadBatches("audit-batch-select");
}

async function loadAlarms() {
  const status = value("alarm-status") || "active";
  const payload = await apiGet("/api/alarms?status=" + encodeURIComponent(status));
  const tbody = element("alarm-rows");
  tbody.innerHTML = "";
  payload.alarms.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML =
      "<td>" +
      item.severity +
      "</td><td>" +
      item.source +
      "</td><td>" +
      item.message +
      "</td><td>" +
      item.status +
      "</td><td>" +
      (item.latching ? "是" : "否") +
      "</td>";
    const actions = document.createElement("td");
    const ack = document.createElement("button");
    ack.textContent = "确认";
    ack.onclick = () =>
      run(
        () => apiPost("/api/alarms/" + item.id + "/ack", { operator: value("operator") || "console" }),
        "alarm-log",
        loadAlarms
      );
    const resolve = document.createElement("button");
    resolve.textContent = "解除";
    resolve.className = "secondary";
    resolve.onclick = () =>
      run(
        () =>
          apiPost("/api/alarms/" + item.id + "/resolve", {
            operator: value("operator") || "console",
            note: "控制台手动解除",
          }),
        "alarm-log",
        loadAlarms
      );
    actions.appendChild(ack);
    actions.appendChild(resolve);
    row.appendChild(actions);
    tbody.appendChild(row);
  });
  write("alarm-summary", payload.summary);
  return payload;
}

async function refreshCertificate() {
  const tankId = value("tank-select");
  if (!tankId) {
    return;
  }
  const report = await apiGet("/api/maintenance/tanks/" + tankId + "/certificate");
  write("certificate-view", report);
  return report;
}

async function loadAudit() {
  const batchId = value("audit-batch-select");
  const payload = await apiGet("/api/audit?batch_id=" + encodeURIComponent(batchId));
  write("audit-view", payload);
  return payload;
}

async function submitReading() {
  const batchId = value("ferment-batch-select") || value("batch-select");
  const payload = await apiPost("/api/telemetry/readings", {
    probe_id: value("probe-select"),
    value_c: numberValue("reading-value"),
    batch_id: batchId || null,
    actor: value("operator") || "console",
  });
  write("reading-result", payload);
  await loadTrend();
  return payload;
}

async function loadTrend() {
  const batchId = value("batch-select") || value("ferment-batch-select");
  if (!batchId) {
    return null;
  }
  const trend = await apiGet("/api/telemetry/batches/" + batchId + "/trend");
  write("trend-view", trend);
  return trend;
}

document.addEventListener("DOMContentLoaded", () => {
  const operator = element("operator");
  if (operator && !operator.value) {
    operator.value = "console";
  }
});
