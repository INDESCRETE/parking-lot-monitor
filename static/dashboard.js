/* Internal analytics dashboard: client/lot picker + date range, backed by
 * /api/reports/metrics (the same aggregation the client report export
 * uses — see app/analytics.py — so the numbers here and in a downloaded
 * report can never disagree). Charts are hand-built SVG, matching the
 * project's "no charting library" convention and the report's own
 * visual language (same colors, same math) so the two feel like one
 * product. */

const ACCENT = "#147c72";
const ACCENT_STRONG = "#0e5d56";
const OCCUPIED = "#dc2626";
const MUTED = "#6f7782";
const LINE = "#d8dde5";

const state = {
  clients: [],
  lots: [],
  cameras: [],
  clientId: null,
  lotId: null,
  cameraId: "",
  rangeMode: "7",
};

const clientSelect = document.querySelector("#clientSelect");
const lotSelect = document.querySelector("#lotSelect");
const cameraFilterSelect = document.querySelector("#cameraFilterSelect");
const rangeSelect = document.querySelector("#rangeSelect");
const customStartWrap = document.querySelector("#customStartWrap");
const customEndWrap = document.querySelector("#customEndWrap");
const startDateInput = document.querySelector("#startDateInput");
const endDateInput = document.querySelector("#endDateInput");
const downloadReportButton = document.querySelector("#downloadReportButton");
const dashboardStatus = document.querySelector("#dashboardStatus");
const dashboardContent = document.querySelector("#dashboardContent");

const statOccupancy = document.querySelector("#statOccupancy");
const statArrivals = document.querySelector("#statArrivals");
const statDwell = document.querySelector("#statDwell");
const statPeak = document.querySelector("#statPeak");
const statPeakSub = document.querySelector("#statPeakSub");

const occupancyChart = document.querySelector("#occupancyChart");
const turnoverChart = document.querySelector("#turnoverChart");
const hourChart = document.querySelector("#hourChart");
const dwellChart = document.querySelector("#dwellChart");

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

function formatPct(rate) {
  if (rate === null || rate === undefined) {
    return "—";
  }
  return `${(rate * 100).toFixed(1)}%`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) {
    return "—";
  }
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days > 0) {
    return `${days}d ${hours}h`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${secs}s`;
  }
  return `${secs}s`;
}

function formatDateLabel(isoDate) {
  const [year, month, day] = isoDate.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

function formatHourLabel(hour) {
  if (hour === 0) return "12am";
  if (hour < 12) return `${hour}am`;
  if (hour === 12) return "12pm";
  return `${hour - 12}pm`;
}

function todayUtcIso() {
  return new Date().toISOString().slice(0, 10);
}

function addDaysIso(isoDate, delta) {
  const [year, month, day] = isoDate.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  date.setUTCDate(date.getUTCDate() + delta);
  return date.toISOString().slice(0, 10);
}

/** Vertical bar chart. `values` entries may be null (no data), rendered as
 * a gap rather than a zero-height bar, mirroring app/reports.py's
 * `_bar_chart` exactly so the dashboard and the printed report never
 * look inconsistent for the same data. */
function barChartSvg(labels, values, { color, valueFormatter, width = 760, height = 220 }) {
  if (labels.length === 0) {
    return '<p class="empty-note">No data in this range.</p>';
  }
  const paddingLeft = 36;
  const paddingBottom = 28;
  const paddingTop = 12;
  const plotWidth = width - paddingLeft - 10;
  const plotHeight = height - paddingBottom - paddingTop;
  const n = labels.length;
  const barWidth = Math.max(2.0, (plotWidth / n) * 0.65);
  const gap = plotWidth / n;
  const numeric = values.filter((v) => v !== null && v !== undefined);
  const maxValue = Math.max(numeric.length ? Math.max(...numeric) : 1.0, 1e-9);

  const bars = [];
  const ticks = [];
  const labelStride = Math.max(1, Math.floor(n / 12));
  let lastX = 0;
  labels.forEach((label, i) => {
    const value = values[i];
    const x = paddingLeft + i * gap + (gap - barWidth) / 2;
    lastX = x;
    if (value !== null && value !== undefined) {
      const barHeight = (value / maxValue) * plotHeight;
      const y = paddingTop + (plotHeight - barHeight);
      const title = `${label}: ${valueFormatter(value)}`;
      bars.push(
        `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" fill="${color}" rx="1.5"><title>${escapeHtml(title)}</title></rect>`
      );
    }
    if (i % labelStride === 0 || i === n - 1) {
      const tickX = x + barWidth / 2;
      ticks.push(
        `<text x="${tickX.toFixed(1)}" y="${height - 8}" font-size="10" fill="${MUTED}" text-anchor="middle">${escapeHtml(label)}</text>`
      );
    }
  });
  const axisY = paddingTop + plotHeight;
  return (
    `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" xmlns="http://www.w3.org/2000/svg" role="img">` +
    `<line x1="${paddingLeft}" y1="${axisY}" x2="${width - 10}" y2="${axisY}" stroke="${LINE}" stroke-width="1"/>` +
    bars.join("") +
    ticks.join("") +
    "</svg>"
  );
}

/** Horizontal bars, one per space, busiest first — mirrors
 * app/reports.py's `_dwell_bars`. */
function dwellBarsSvg(dwellBySpace, { width = 760 } = {}) {
  if (dwellBySpace.length === 0) {
    return '<p class="empty-note">No spaces marked for this lot.</p>';
  }
  const rows = [...dwellBySpace].sort((a, b) => {
    const aNull = a.average_seconds === null;
    const bNull = b.average_seconds === null;
    if (aNull !== bNull) return aNull ? 1 : -1;
    return (b.average_seconds || 0) - (a.average_seconds || 0);
  });
  const numeric = rows.map((r) => r.average_seconds).filter((v) => v !== null && v !== undefined);
  const maxValue = numeric.length ? Math.max(...numeric) : 1.0;
  const rowHeight = 22;
  const labelWidth = 130;
  const barArea = width - labelWidth - 90;
  const height = rowHeight * rows.length + 10;
  const bars = [];
  rows.forEach((row, i) => {
    const y = 8 + i * rowHeight;
    const value = row.average_seconds;
    const barW = value ? (value / maxValue) * barArea : 0;
    const color = value ? ACCENT : "#c7ccd3";
    const valueText = value !== null && value !== undefined ? formatDuration(value) : "no activity";
    bars.push(
      `<text x="${labelWidth - 8}" y="${y + 13}" font-size="11" fill="#20242a" text-anchor="end">${escapeHtml(row.label)}</text>` +
        `<rect x="${labelWidth}" y="${y + 3}" width="${Math.max(barW, 1.5).toFixed(1)}" height="14" fill="${color}" rx="2"/>` +
        `<text x="${(labelWidth + barW + 6).toFixed(1)}" y="${y + 13}" font-size="10" fill="${MUTED}">${escapeHtml(valueText)}</text>`
    );
  });
  return (
    `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" xmlns="http://www.w3.org/2000/svg" role="img">` +
    bars.join("") +
    "</svg>"
  );
}

function setStatus(message, { error = false } = {}) {
  dashboardStatus.textContent = message;
  dashboardStatus.hidden = !message;
  dashboardStatus.classList.toggle("dashboard-status-error", error);
}

function currentRange() {
  if (state.rangeMode === "custom") {
    const start = startDateInput.value;
    const end = endDateInput.value;
    if (!start || !end) {
      return null;
    }
    // Bare calendar dates: the server treats a bare `end` date as inclusive
    // of that whole day, which is what someone picking specific days on a
    // calendar expects.
    return { start, end };
  }
  // Presets ("Last N days") send precise timestamps rather than bare dates,
  // so this is an exact trailing N-day window ending now — matching
  // app/server.py's own `parse_report_range` default (no bare-date
  // inclusive bump, which would otherwise silently turn "last 7 days"
  // into an 8-calendar-day window).
  const days = Number(state.rangeMode);
  const end = new Date();
  const start = new Date(end.getTime() - days * 86400000);
  return { start: start.toISOString(), end: end.toISOString() };
}

function reportExportUrl() {
  const range = currentRange();
  if (!state.lotId || !range) {
    return null;
  }
  const params = new URLSearchParams({ lot_id: state.lotId, start: range.start, end: range.end });
  if (state.cameraId) {
    params.set("camera_id", state.cameraId);
  }
  return `/api/reports/export?${params.toString()}`;
}

function updateDownloadButton() {
  const url = reportExportUrl();
  downloadReportButton.disabled = !url;
}

async function loadClients() {
  try {
    const payload = await fetchJson("/api/clients");
    state.clients = payload.clients;
    clientSelect.innerHTML = "";
    if (state.clients.length === 0) {
      clientSelect.innerHTML = '<option value="">No clients yet</option>';
      setStatus("No clients yet. Assign a camera to a lot to get started.");
      return;
    }
    for (const client of state.clients) {
      const option = document.createElement("option");
      option.value = client.id;
      option.textContent = client.name;
      clientSelect.append(option);
    }
    state.clientId = state.clients[0].id;
    clientSelect.value = state.clientId;
    await loadLots();
  } catch (err) {
    setStatus(`Couldn't load clients: ${err.message}`, { error: true });
  }
}

async function loadLots() {
  lotSelect.disabled = true;
  lotSelect.innerHTML = '<option value="">Loading…</option>';
  try {
    const payload = await fetchJson(`/api/lots?client_id=${encodeURIComponent(state.clientId)}`);
    state.lots = payload.lots;
    lotSelect.innerHTML = "";
    if (state.lots.length === 0) {
      lotSelect.innerHTML = '<option value="">No lots for this client</option>';
      state.lotId = null;
      state.cameras = [];
      state.cameraId = "";
      cameraFilterSelect.innerHTML = '<option value="">All cameras</option>';
      cameraFilterSelect.disabled = true;
      dashboardContent.hidden = true;
      updateDownloadButton();
      setStatus("This client has no lots yet.");
      return;
    }
    for (const lot of state.lots) {
      const option = document.createElement("option");
      option.value = lot.id;
      option.textContent = lot.name;
      lotSelect.append(option);
    }
    lotSelect.disabled = false;
    state.lotId = state.lots[0].id;
    lotSelect.value = state.lotId;
    await loadCameras();
  } catch (err) {
    setStatus(`Couldn't load lots: ${err.message}`, { error: true });
  }
}

/** Populates the Camera filter with the cameras assigned to the selected
 * lot. A lot can be covered by several camera angles that all combine into
 * one report by default ("All cameras"); picking one here narrows every
 * chart and the downloaded report down to just that camera's spaces. */
async function loadCameras() {
  cameraFilterSelect.disabled = true;
  cameraFilterSelect.innerHTML = '<option value="">Loading…</option>';
  try {
    const payload = await fetchJson(`/api/cameras?lot_id=${encodeURIComponent(state.lotId)}`);
    state.cameras = payload.cameras;
    cameraFilterSelect.innerHTML = "";
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "All cameras";
    cameraFilterSelect.append(allOption);
    for (const camera of state.cameras) {
      const option = document.createElement("option");
      option.value = camera.id;
      option.textContent = camera.name;
      cameraFilterSelect.append(option);
    }
    state.cameraId = "";
    cameraFilterSelect.value = "";
    cameraFilterSelect.disabled = state.cameras.length === 0;
    await loadReport();
  } catch (err) {
    setStatus(`Couldn't load cameras: ${err.message}`, { error: true });
  }
}

async function loadReport() {
  const range = currentRange();
  if (!state.lotId || !range) {
    updateDownloadButton();
    return;
  }
  updateDownloadButton();
  setStatus("Loading analytics…");
  dashboardContent.hidden = true;
  try {
    const params = new URLSearchParams({ lot_id: state.lotId, start: range.start, end: range.end });
    if (state.cameraId) {
      params.set("camera_id", state.cameraId);
    }
    const report = await fetchJson(`/api/reports/metrics?${params.toString()}`);
    renderReport(report);
    setStatus("");
    dashboardContent.hidden = false;
  } catch (err) {
    setStatus(`Couldn't load analytics: ${err.message}`, { error: true });
  }
}

function renderReport(report) {
  const summary = report.summary;

  statOccupancy.textContent = formatPct(summary.overall_occupancy_rate);
  statArrivals.textContent = summary.total_arrivals;
  statDwell.textContent = formatDuration(summary.overall_average_dwell_seconds);
  const peakLabel = summary.peak_hour !== null ? formatHourLabel(summary.peak_hour) : "—";
  const offPeakLabel = summary.off_peak_hour !== null ? formatHourLabel(summary.off_peak_hour) : "—";
  statPeak.textContent = `${peakLabel} / ${offPeakLabel}`;
  statPeakSub.textContent = `${formatPct(summary.peak_hour_rate)} / ${formatPct(summary.off_peak_hour_rate)} occupied`;

  const occupancyLabels = report.occupancy_by_day.map((d) => formatDateLabel(d.date));
  const occupancyValues = report.occupancy_by_day.map((d) => d.rate);
  occupancyChart.innerHTML = barChartSvg(occupancyLabels, occupancyValues, {
    color: ACCENT,
    valueFormatter: formatPct,
  });

  const turnoverLabels = report.turnover_by_day.map((d) => formatDateLabel(d.date));
  const turnoverValues = report.turnover_by_day.map((d) => d.arrivals);
  turnoverChart.innerHTML = barChartSvg(turnoverLabels, turnoverValues, {
    color: ACCENT_STRONG,
    valueFormatter: (v) => `${Math.round(v)} arrivals`,
  });

  const hourLabels = report.peak_hours.map((h) => formatHourLabel(h.hour));
  const hourValues = report.peak_hours.map((h) => h.rate);
  hourChart.innerHTML = barChartSvg(hourLabels, hourValues, {
    color: OCCUPIED,
    valueFormatter: formatPct,
  });

  dwellChart.innerHTML = dwellBarsSvg(report.dwell_by_space);
}

function initCustomRangeDefaults() {
  const end = todayUtcIso();
  const start = addDaysIso(end, -7);
  if (!startDateInput.value) startDateInput.value = start;
  if (!endDateInput.value) endDateInput.value = end;
}

clientSelect.addEventListener("change", async () => {
  state.clientId = clientSelect.value;
  await loadLots();
});

lotSelect.addEventListener("change", async () => {
  state.lotId = lotSelect.value;
  await loadCameras();
});

cameraFilterSelect.addEventListener("change", async () => {
  state.cameraId = cameraFilterSelect.value;
  await loadReport();
});

rangeSelect.addEventListener("change", async () => {
  state.rangeMode = rangeSelect.value;
  const isCustom = state.rangeMode === "custom";
  customStartWrap.hidden = !isCustom;
  customEndWrap.hidden = !isCustom;
  if (isCustom) {
    initCustomRangeDefaults();
  }
  await loadReport();
});

startDateInput.addEventListener("change", loadReport);
endDateInput.addEventListener("change", loadReport);

downloadReportButton.addEventListener("click", () => {
  const url = reportExportUrl();
  if (url) {
    window.open(url, "_blank");
  }
});

loadClients();
