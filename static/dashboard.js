/* Internal analytics dashboard: client/lot picker + date range, backed by
 * /api/reports/metrics (the same aggregation the client report export
 * uses — see app/analytics.py — so the numbers here and in a downloaded
 * report can never disagree). Charts are hand-built SVG, matching the
 * project's "no charting library" convention and the report's own
 * visual language (same colors, same math) so the two feel like one
 * product. */

// Fallback values only -- these match the light theme in styles.css, but
// the charts read the CURRENT theme's colors at draw time via cssVar()
// below, so they redraw correctly after a dark-mode toggle too.
const ACCENT = "#147c72";
const ACCENT_STRONG = "#0e5d56";
const OCCUPIED = "#dc2626";
const MUTED = "#6f7782";
const LINE = "#d8dde5";

function cssVar(name, fallback) {
  try {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  } catch (error) {
    return fallback;
  }
}

const state = {
  clients: [],
  lots: [],
  cameras: [],
  clientId: null,
  lotId: null,
  cameraId: "",
  rangeMode: "7",
  // Single-day "Hour by Hour" chart: which local calendar day it shows,
  // independent of the Range dropdown (which only drives the averaged
  // charts). dayFollowsToday keeps it on "today" across midnight if the
  // tab is left open.
  dayIso: null,
  dayFollowsToday: true,
  dayReport: null,
  dayRequestId: 0,
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
const dayHourChart = document.querySelector("#dayHourChart");
const dayChartTitle = document.querySelector("#dayChartTitle");
const dayPrevButton = document.querySelector("#dayPrevButton");
const dayNextButton = document.querySelector("#dayNextButton");
const dwellChart = document.querySelector("#dwellChart");
const flowPanel = document.querySelector("#flowPanel");
const flowSummary = document.querySelector("#flowSummary");
const flowHourChart = document.querySelector("#flowHourChart");
const flowDayWrap = document.querySelector("#flowDayWrap");
const flowDayChart = document.querySelector("#flowDayChart");
const accumulationSummary = document.querySelector("#accumulationSummary");
const accumulationChart = document.querySelector("#accumulationChart");

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

function todayLocalIso() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
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
function niceNumber(value, round) {
  const exponent = Math.floor(Math.log10(value));
  const fraction = value / 10 ** exponent;
  let niceFraction;
  if (round) {
    if (fraction < 1.5) niceFraction = 1;
    else if (fraction < 3) niceFraction = 2;
    else if (fraction < 7) niceFraction = 5;
    else niceFraction = 10;
  } else {
    if (fraction <= 1) niceFraction = 1;
    else if (fraction <= 2) niceFraction = 2;
    else if (fraction <= 5) niceFraction = 5;
    else niceFraction = 10;
  }
  return niceFraction * 10 ** exponent;
}

/** Picks a "nice" y-axis ceiling and evenly-spaced tick values below it
 * (the standard graph-labeling algorithm) -- e.g. a data max of 0.42 becomes
 * ticks at 0/0.1/0.2/0.3/0.4/0.5, not an arbitrary scale tied to whatever
 * the data's raw max happens to be. targetCount is how many gridlines to
 * aim for (the actual count can be one more or less once rounded). */
function niceTicks(maxValue, targetCount = 4) {
  if (!(maxValue > 0)) {
    return { niceMax: 1, ticks: [0, 1] };
  }
  const niceRange = niceNumber(maxValue, false);
  const step = niceNumber(niceRange / Math.max(1, targetCount - 1), true);
  const niceMax = Math.ceil(maxValue / step) * step;
  const ticks = [];
  for (let v = 0; v <= niceMax + step / 1e6; v += step) {
    ticks.push(Math.round(v / step) * step);
  }
  return { niceMax, ticks };
}

function barChartSvg(labels, values, { color, valueFormatter, axisFormatter, nowIndex = null, showZero = false, width = 760, height = 220 }) {
  if (labels.length === 0) {
    return '<p class="empty-note">No data in this range.</p>';
  }
  const mutedColor = cssVar("--muted", MUTED);
  const lineColor = cssVar("--line", LINE);
  const formatAxisValue = axisFormatter || valueFormatter;
  const paddingLeft = 42;
  const paddingBottom = 28;
  const paddingTop = 12;
  const plotWidth = width - paddingLeft - 10;
  const plotHeight = height - paddingBottom - paddingTop;
  const n = labels.length;
  const barWidth = Math.max(2.0, (plotWidth / n) * 0.65);
  const gap = plotWidth / n;
  const numeric = values.filter((v) => v !== null && v !== undefined);
  const dataMax = Math.max(numeric.length ? Math.max(...numeric) : 1.0, 1e-9);
  const { niceMax, ticks: axisTicks } = niceTicks(dataMax);

  const bars = [];
  const ticks = [];
  const labelStride = Math.max(1, Math.floor(n / 12));
  let lastX = 0;
  labels.forEach((label, i) => {
    const value = values[i];
    const x = paddingLeft + i * gap + (gap - barWidth) / 2;
    lastX = x;
    if (showZero && value === 0) {
      // A measured 0% (the lot was watched and nothing was parked) gets a
      // thin stub on the baseline, so it reads differently from a gap
      // (no data at all for that hour).
      bars.push(
        `<rect x="${x.toFixed(1)}" y="${(paddingTop + plotHeight - 2).toFixed(1)}" width="${barWidth.toFixed(1)}" height="2" fill="${mutedColor}" rx="1"><title>${escapeHtml(`${label}: ${valueFormatter(0)}`)}</title></rect>`
      );
    } else if (value !== null && value !== undefined) {
      const barHeight = (value / niceMax) * plotHeight;
      const y = paddingTop + (plotHeight - barHeight);
      const title = `${label}: ${valueFormatter(value)}`;
      bars.push(
        `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" fill="${color}" rx="1.5"><title>${escapeHtml(title)}</title></rect>`
      );
    }
    if (i % labelStride === 0 || i === n - 1) {
      const tickX = x + barWidth / 2;
      ticks.push(
        `<text x="${tickX.toFixed(1)}" y="${height - 8}" font-size="10" fill="${mutedColor}" text-anchor="middle">${escapeHtml(label)}</text>`
      );
    }
  });
  // Y-axis: a hairline gridline per "nice" tick value, with its value
  // labeled at the left -- recessive (drawn first, under the bars), same
  // line color/weight as the old single baseline this replaces.
  const yGridlines = [];
  const yLabels = [];
  for (const tick of axisTicks) {
    const y = paddingTop + plotHeight - (tick / niceMax) * plotHeight;
    yGridlines.push(
      `<line x1="${paddingLeft}" y1="${y.toFixed(1)}" x2="${width - 10}" y2="${y.toFixed(1)}" stroke="${lineColor}" stroke-width="1"/>`
    );
    yLabels.push(
      `<text x="${(paddingLeft - 6).toFixed(1)}" y="${y.toFixed(1)}" font-size="10" fill="${mutedColor}" text-anchor="end" dominant-baseline="middle">${escapeHtml(formatAxisValue(tick))}</text>`
    );
  }
  // A "now" reference line -- shown only when the caller knows the current
  // moment falls within this chart's x-axis (e.g. the peak/off-peak-hours
  // chart when today is part of the selected range) -- marks where "so
  // far today" ends, so hours after it reading empty is self-explanatory
  // instead of looking like missing data. Dashed (never solid, so it never
  // reads as a gridline) and drawn on top of the bars.
  let nowMarker = "";
  if (nowIndex !== null && nowIndex >= 0 && nowIndex < n) {
    const nowX = paddingLeft + nowIndex * gap + gap / 2;
    nowMarker =
      `<line x1="${nowX.toFixed(1)}" y1="${paddingTop}" x2="${nowX.toFixed(1)}" y2="${(paddingTop + plotHeight).toFixed(1)}" ` +
      `stroke="${mutedColor}" stroke-width="1" stroke-dasharray="3,3"/>` +
      `<text x="${(nowX + 4).toFixed(1)}" y="${(paddingTop + 8).toFixed(1)}" font-size="9" fill="${mutedColor}">now</text>`;
  }
  return (
    `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" xmlns="http://www.w3.org/2000/svg" role="img">` +
    yGridlines.join("") +
    yLabels.join("") +
    bars.join("") +
    ticks.join("") +
    nowMarker +
    "</svg>"
  );
}

/** Two bars per category (vehicles in / out), side by side with a 2px gap.
 * Same axis, gridlines and label style as barChartSvg; mirrored in
 * app/reports.py's `_grouped_bar_chart`. `null` values leave a gap (hour not
 * watched enough to count). */
function groupedBarChartSvg(labels, seriesA, seriesB, { colors, names, valueFormatter, integerAxis = false, emptyNote = "No data in this range.", width = 760, height = 220 }) {
  const hasValue = [...seriesA, ...seriesB].some((v) => v !== null && v !== undefined);
  if (labels.length === 0 || !hasValue) {
    return `<p class="empty-note">${escapeHtml(emptyNote)}</p>`;
  }
  const mutedColor = cssVar("--muted", MUTED);
  const lineColor = cssVar("--line", LINE);
  const paddingLeft = 42;
  const paddingBottom = 28;
  const paddingTop = 12;
  const plotWidth = width - paddingLeft - 10;
  const plotHeight = height - paddingBottom - paddingTop;
  const n = labels.length;
  const slot = plotWidth / n;
  const groupWidth = slot * 0.72;
  const barWidth = Math.max(1.5, (groupWidth - 2) / 2);
  const numeric = [...seriesA, ...seriesB].filter((v) => v !== null && v !== undefined);
  const dataMax = Math.max(numeric.length ? Math.max(...numeric) : 1, 1e-9);
  const { niceMax, ticks: allTicks } = niceTicks(integerAxis ? Math.max(dataMax, 4) : dataMax);
  // Whole-number counts get whole-number gridlines only.
  const axisTicks = integerAxis ? allTicks.filter((t) => Number.isInteger(t)) : allTicks;
  const parts = [];
  for (const tick of axisTicks) {
    const y = paddingTop + plotHeight - (tick / niceMax) * plotHeight;
    parts.push(`<line x1="${paddingLeft}" y1="${y.toFixed(1)}" x2="${width - 10}" y2="${y.toFixed(1)}" stroke="${lineColor}" stroke-width="1"/>`);
    parts.push(`<text x="${paddingLeft - 6}" y="${y.toFixed(1)}" font-size="10" fill="${mutedColor}" text-anchor="end" dominant-baseline="middle">${escapeHtml(String(Math.round(tick * 10) / 10))}</text>`);
  }
  const labelStride = Math.max(1, Math.floor(n / 12));
  labels.forEach((label, i) => {
    const groupX = paddingLeft + i * slot + (slot - groupWidth) / 2;
    [seriesA[i], seriesB[i]].forEach((value, k) => {
      if (value === null || value === undefined) return;
      const x = groupX + k * (barWidth + 2);
      const h = Math.max(value > 0 ? 1.5 : 0, (value / niceMax) * plotHeight);
      const y = paddingTop + plotHeight - h;
      const title = `${label} · ${names[k]}: ${valueFormatter(value)}`;
      parts.push(`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${h.toFixed(1)}" fill="${colors[k]}" rx="1.5"><title>${escapeHtml(title)}</title></rect>`);
    });
    // The last label only if it won't collide with the previous one.
    const lastFits = i === n - 1 && i % labelStride >= labelStride / 2;
    if (i % labelStride === 0 || lastFits) {
      parts.push(`<text x="${(groupX + groupWidth / 2).toFixed(1)}" y="${height - 8}" font-size="10" fill="${mutedColor}" text-anchor="middle">${escapeHtml(label)}</text>`);
    }
  });
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" xmlns="http://www.w3.org/2000/svg" role="img">${parts.join("")}</svg>`;
}

// A chart with its legend above it (no legend over an empty-state note).
function withFlowLegend(chartHtml, inColor, outColor) {
  return chartHtml.startsWith("<svg") ? flowLegend(inColor, outColor) + chartHtml : chartHtml;
}

function flowLegend(inColor, outColor) {
  return `<div class="flow-legend"><span><i style="background:${inColor}"></i>Entering</span><span><i style="background:${outColor}"></i>Leaving</span></div>`;
}

/** Entries & exits (lines marked as lot entrance/exit on the lot's traffic
 * cameras). Hidden when the lot has none. */
function renderFlow(flow) {
  flowPanel.hidden = !flow;
  if (!flow) return;
  const inColor = cssVar("--flow-in", "#2563eb");
  const outColor = cssVar("--flow-out", "#c2410c");
  const watchedPct = flow.range_seconds ? Math.round((flow.monitored_seconds / flow.range_seconds) * 100) : 0;
  const watchedText = `${formatDuration(flow.monitored_seconds)} (${watchedPct}%)`;
  const busiest = flow.busiest_hour
    ? ` Busiest hour on average: ${formatHourLabel(flow.busiest_hour.hour)} (${flow.busiest_hour.in} in, ${flow.busiest_hour.out} out).`
    : "";
  const lines = flow.lines.map((l) => l.name).join(", ");
  flowSummary.innerHTML =
    `<strong>${flow.totals.in}</strong> entered and <strong>${flow.totals.out}</strong> left.${escapeHtml(busiest)} ` +
    `<span class="flow-muted">From entrance/exit line${flow.lines.length === 1 ? "" : "s"}: ${escapeHtml(lines)}. ` +
    `Entrances/exits watched ${escapeHtml(watchedText)} of this range${watchedPct < 95 ? "; vehicles that passed while they weren't watched aren't included" : ""}.</span>`;
  const fmt = (v) => `${Math.round(v * 10) / 10} vehicles`;
  flowHourChart.innerHTML = withFlowLegend(
    groupedBarChartSvg(
      flow.by_hour.map((h) => formatHourLabel(h.hour)),
      flow.by_hour.map((h) => h.in),
      flow.by_hour.map((h) => h.out),
      {
        colors: [inColor, outColor],
        names: ["Entering", "Leaving"],
        valueFormatter: (v) => `${fmt(v)} on average`,
        emptyNote: "No hour has been watched long enough yet (at least half of it) to show an average.",
      },
    ),
    inColor,
    outColor,
  );
  renderAccumulation(flow);
  const multiDay = flow.by_day.length > 1;
  flowDayWrap.hidden = !multiDay;
  if (multiDay) {
    flowDayChart.innerHTML = withFlowLegend(
      groupedBarChartSvg(
        flow.by_day.map((d) => formatDateLabel(d.date)),
        flow.by_day.map((d) => (d.monitored_seconds > 0 ? d.in : null)),
        flow.by_day.map((d) => (d.monitored_seconds > 0 ? d.out : null)),
        { colors: [inColor, outColor], names: ["Entering", "Leaving"], valueFormatter: (v) => `${Math.round(v)} vehicles`, integerAxis: true },
      ),
      inColor,
      outColor,
    );
  }
}

/** Vehicles inside the lot hour by hour (the most at any moment in each
 * hour), from the starting count set on the Traffic page. */
function renderAccumulation(flow) {
  const acc = flow.accumulation;
  const occ = flow.occupancy;
  const parts = [];
  if (occ && occ.started) {
    const since = new Date(occ.set_at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    parts.push(`<strong>${occ.current}</strong> inside right now (counting from ${occ.starting_count} on ${escapeHtml(since)}).`);
    if (occ.gaps && occ.gaps.length) {
      parts.push(`<span class="flow-muted">Entrances/exits weren't watched for ${escapeHtml(formatDuration(occ.missed_seconds))} since then, so it may be off.</span>`);
    }
  }
  if (acc) {
    const peakAt = new Date(acc.peak.start);
    const peakLabel = `${peakAt.toLocaleDateString([], { month: "short", day: "numeric" })}, ${formatHourLabel(peakAt.getHours())}`;
    parts.push(`Most in this range: <strong>${acc.peak.count}</strong> (${escapeHtml(peakLabel)}).`);
    if (acc.went_negative) {
      parts.push('<span class="flow-muted">The count went below zero at some point, so the starting count was too low or exits were double counted.</span>');
    }
  }
  if (!acc && !(occ && occ.started)) {
    accumulationSummary.innerHTML = '<span class="flow-muted">Set a starting count on the Traffic page ("Vehicles in the lot" → Set count) to track how many vehicles are inside.</span>';
    accumulationChart.innerHTML = "";
    return;
  }
  accumulationSummary.innerHTML = parts.join(" ");
  if (!acc) {
    accumulationChart.innerHTML = "";
    return;
  }
  // One day: hour by hour. Several days: the most inside on each day
  // (hourly bars across a week are too thin to read).
  const days = new Map();
  for (const h of acc.hours) {
    const day = h.start.slice(0, 10); // local date (the server sends local times)
    const peaks = days.get(day) || [];
    if (h.peak !== null) peaks.push(h.peak);
    days.set(day, peaks);
  }
  let labels;
  let values;
  if (days.size > 1) {
    labels = [...days.keys()].map((d) => formatDateLabel(d));
    values = [...days.values()].map((peaks) => (peaks.length ? Math.max(0, ...peaks) : null));
  } else {
    labels = acc.hours.map((h) => formatHourLabel(new Date(h.start).getHours()));
    values = acc.hours.map((h) => (h.peak === null ? null : Math.max(0, h.peak)));
  }
  accumulationChart.innerHTML = barChartSvg(labels, values, {
    color: cssVar("--accent", ACCENT),
    valueFormatter: (v) => `${Math.round(v)} inside at most`,
    axisFormatter: (v) => `${Math.round(v)}`,
    showZero: true,
  });
}

/** Horizontal bars, one per space, busiest first — mirrors
 * app/reports.py's `_dwell_bars`. */
function dwellBarsSvg(dwellBySpace, { width = 760 } = {}) {
  if (dwellBySpace.length === 0) {
    return '<p class="empty-note">No spaces marked for this lot.</p>';
  }
  const mutedColor = cssVar("--muted", MUTED);
  const textColor = cssVar("--text", "#20242a");
  const emptyBarColor = cssVar("--chart-empty", "#c7ccd3");
  const accentColor = cssVar("--accent", ACCENT);
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
    const color = value ? accentColor : emptyBarColor;
    const valueText = value !== null && value !== undefined ? formatDuration(value) : "no activity";
    bars.push(
      `<text x="${labelWidth - 8}" y="${y + 13}" font-size="11" fill="${textColor}" text-anchor="end">${escapeHtml(row.label)}</text>` +
        `<rect x="${labelWidth}" y="${y + 3}" width="${Math.max(barW, 1.5).toFixed(1)}" height="14" fill="${color}" rx="2"/>` +
        `<text x="${(labelWidth + barW + 6).toFixed(1)}" y="${y + 13}" font-size="10" fill="${mutedColor}">${escapeHtml(valueText)}</text>`
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
    // Traffic cameras count cars crossing lines; they have no parking spaces.
    state.cameras = payload.cameras.filter((camera) => camera.kind !== "traffic");
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
    loadDayChart();
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
    renderDayChart();
  } catch (err) {
    setStatus(`Couldn't load analytics: ${err.message}`, { error: true });
  }
}

function renderReport(report) {
  state.lastReport = report; // so a dark-mode toggle can redraw with the new colors
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
    color: cssVar("--accent", ACCENT),
    valueFormatter: formatPct,
    axisFormatter: (v) => `${Math.round(v * 100)}%`,
  });

  const turnoverLabels = report.turnover_by_day.map((d) => formatDateLabel(d.date));
  const turnoverValues = report.turnover_by_day.map((d) => d.arrivals);
  turnoverChart.innerHTML = barChartSvg(turnoverLabels, turnoverValues, {
    color: cssVar("--accent-strong", ACCENT_STRONG),
    valueFormatter: (v) => `${Math.round(v)} arrivals`,
    axisFormatter: (v) => `${Math.round(v)}`,
  });

  const hourLabels = report.peak_hours.map((h) => formatHourLabel(h.hour));
  const hourValues = report.peak_hours.map((h) => h.rate);
  // An average across many days -- no "now" line here; the single-day
  // "Hour by Hour" chart is where "what's happening today" lives.
  hourChart.innerHTML = barChartSvg(hourLabels, hourValues, {
    color: cssVar("--occupied", OCCUPIED),
    valueFormatter: (v) => `${formatPct(v)} average`,
    axisFormatter: (v) => `${Math.round(v * 100)}%`,
  });

  dwellChart.innerHTML = dwellBarsSvg(report.dwell_by_space);
  renderFlow(report.flow);
}

/** Start/end timestamps for one local calendar day. For today the end is
 * "now", so hours that haven't happened yet correctly stay empty. Sent as
 * precise timestamps (not bare dates) so the server's inclusive-end-date
 * rule doesn't stretch the window to two days. */
function localDayBounds(isoDate) {
  const [year, month, day] = isoDate.split("-").map(Number);
  const start = new Date(year, month - 1, day);
  let end = new Date(year, month - 1, day + 1);
  const now = new Date();
  if (end > now) end = now;
  return { start: start.toISOString(), end: end.toISOString() };
}

function formatDayTitle(isoDate) {
  const today = todayLocalIso();
  const [year, month, day] = isoDate.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  const short = date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (isoDate === today) return `Today, ${short}`;
  if (isoDate === addDaysIso(today, -1)) return `Yesterday, ${short}`;
  return date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function renderDayChart() {
  const today = todayLocalIso();
  if (state.dayFollowsToday) state.dayIso = today;
  if (!state.dayIso) state.dayIso = today;
  const isToday = state.dayIso === today;
  dayChartTitle.textContent = formatDayTitle(state.dayIso);
  dayNextButton.disabled = state.dayIso >= today;

  const report = state.dayReport;
  if (!report) {
    dayHourChart.innerHTML = '<p class="empty-note">Loading…</p>';
    return;
  }
  const values = report.peak_hours.map((h) => h.rate);
  if (values.every((v) => v === null || v === undefined)) {
    dayHourChart.innerHTML = '<p class="empty-note">No monitoring data for this day.</p>';
    return;
  }
  dayHourChart.innerHTML = barChartSvg(
    report.peak_hours.map((h) => formatHourLabel(h.hour)),
    values,
    {
      color: cssVar("--occupied", OCCUPIED),
      valueFormatter: (v) => `${formatPct(v)} occupied`,
      axisFormatter: (v) => `${Math.round(v * 100)}%`,
      showZero: true,
      nowIndex: isToday ? new Date().getHours() : null,
    }
  );
}

/** Fetches one day's numbers from the same /api/reports/metrics endpoint
 * the rest of the dashboard uses -- for a single day, its hour-of-day
 * breakdown IS that day's hour-by-hour occupancy. A request counter drops
 * any response that arrives after the user already moved on (switched
 * day, lot or camera). */
async function loadDayChart({ quiet = false } = {}) {
  if (!state.lotId) return;
  if (state.dayFollowsToday || !state.dayIso) state.dayIso = todayLocalIso();
  const requestId = ++state.dayRequestId;
  if (!quiet) {
    state.dayReport = null;
    renderDayChart();
  }
  try {
    const { start, end } = localDayBounds(state.dayIso);
    const params = new URLSearchParams({ lot_id: state.lotId, start, end });
    if (state.cameraId) params.set("camera_id", state.cameraId);
    const report = await fetchJson(`/api/reports/metrics?${params.toString()}`);
    if (requestId !== state.dayRequestId) return;
    state.dayReport = report;
    renderDayChart();
  } catch (err) {
    if (requestId !== state.dayRequestId) return;
    dayHourChart.innerHTML = `<p class="empty-note">Couldn't load this day: ${escapeHtml(err.message)}</p>`;
  }
}

function changeDay(isoDate) {
  const today = todayLocalIso();
  state.dayIso = isoDate > today ? today : isoDate;
  state.dayFollowsToday = state.dayIso === today;
  loadDayChart();
}

function initCustomRangeDefaults() {
  const end = todayLocalIso();
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
  loadDayChart();
  await loadReport();
});

dayPrevButton.addEventListener("click", () => changeDay(addDaysIso(state.dayIso || todayLocalIso(), -1)));
dayNextButton.addEventListener("click", () => changeDay(addDaysIso(state.dayIso || todayLocalIso(), 1)));

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

window.addEventListener("themechange", () => {
  // Redraw with whatever's already loaded -- no need to hit the server again.
  if (state.lastReport) {
    renderReport(state.lastReport);
  }
  renderDayChart();
});

// The Peak/Off-Peak Hours chart's "now" line is computed at draw time from
// the browser's own clock (see renderReport's hourChart block), but nothing
// else here re-draws on a timer -- loadReport() only re-runs when a filter
// changes. Left alone, a tab open for a while would show that line frozen
// at whatever time it happened to load, silently going stale and pointing
// at the wrong hour. Redrawing from the already-loaded report every minute
// keeps it honest without hitting the server again.
setInterval(() => {
  if (state.lastReport) {
    renderReport(state.lastReport);
  }
  // The single-day chart, when it's showing today, re-fetches so the
  // current hour's bar (and the "now" line) keep up with the live camera.
  // Past days never change, so they're just redrawn.
  if (state.dayFollowsToday) {
    loadDayChart({ quiet: true });
  } else {
    renderDayChart();
  }
}, 60000);

loadClients();
