// Traffic counting page: live picture from a traffic camera, the vehicles
// the tracker is following, counting lines (draw / move / rename / delete),
// live counts per direction and a CSV download of interval counts.
//
// Line coordinates are stored as fractions of the picture (0-1); everything
// on the canvas is drawn in the picture's own pixels, and the canvas is
// scaled to fit the window with CSS.

const state = {
  cameras: [],
  cameraId: null,
  lines: [],
  view: null, // what the server found in the last analysed picture
  image: null, // that picture
  frameSeq: null,
  frameSize: null,
  mode: "idle", // "idle" | "drawing"
  draftStart: null, // picture pixels
  pointer: null, // picture pixels, while drawing or dragging
  dragging: null, // { lineId, end: 1 | 2 }
  hoverHandle: null,
  editingLineId: null,
  pendingLine: null, // fractions, waiting for the name dialog
  counts: null,
  viewInFlight: false,
  lastFrameRequest: 0,
};

const VIEW_POLL_MS = 400;
const COUNTS_POLL_MS = 5000;
const IDLE_FRAME_REFRESH_MS = 2000; // before anything's been analysed
const HANDLE_RADIUS_SCREEN = 10;
const CAMERA_STORAGE_KEY = "parkingLotMonitor.lastTrafficCameraId";

const $ = (selector) => document.querySelector(selector);
const cameraSelect = $("#cameraSelect");
const connectionStatus = $("#connectionStatus");
const lineList = $("#lineList");
const crossingList = $("#crossingList");
const countsRangeLabel = $("#countsRangeLabel");
const peakHour = $("#peakHour");
const canvas = $("#trafficCanvas");
const ctx = canvas.getContext("2d");
const canvasWrap = document.querySelector(".canvas-wrap");
const emptyState = $("#emptyState");
const statusEl = $("#status");
const drawLineButton = $("#drawLineButton");
const cancelDrawButton = $("#cancelDrawButton");
const showBoxesInput = $("#showBoxesInput");
const trafficIndicator = $("#trafficIndicator");
const lineDialog = $("#lineDialog");
const lineForm = $("#lineForm");
const lineDialogTitle = $("#lineDialogTitle");
const lineNameInput = $("#lineNameInput");
const forwardLabelInput = $("#forwardLabelInput");
const reverseLabelInput = $("#reverseLabelInput");
const forwardArrow = $("#forwardArrow");
const reverseArrow = $("#reverseArrow");
const cameraDialog = $("#cameraDialog");
const cameraForm = $("#cameraForm");
const cameraNameInput = $("#cameraNameInput");
const deleteCameraButton = $("#deleteCameraButton");
const countDateInput = $("#countDateInput");
const binSelect = $("#binSelect");
const downloadButton = $("#downloadButton");
const confirmDialog = $("#confirmDialog");

// --- Small helpers -------------------------------------------------------------

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(payload?.error || `Request failed (${response.status})`);
  }
  return payload;
}

function jsonRequest(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function setStatus(message) {
  statusEl.textContent = message || "";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function localDateIso(date = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function formatClock(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function formatHour(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function secondsSince(iso) {
  return iso ? (Date.now() - new Date(iso).getTime()) / 1000 : Infinity;
}

// Which way a direction points on screen, as an arrow character.
function arrowFor(dx, dy) {
  const arrows = ["→", "↘", "↓", "↙", "←", "↖", "↑", "↗"];
  const angle = Math.atan2(dy, dx); // y grows downward
  const index = Math.round(angle / (Math.PI / 4));
  return arrows[(index + 8) % 8];
}

// Mirrors default_direction_labels in app/live_traffic.py. "Forward" is
// crossing from the left of point1->point2 to its right, i.e. moving along
// (-dy, dx) in picture coordinates.
function defaultLabels(dx, dy) {
  const nx = -dy;
  const ny = dx;
  if (Math.abs(nx) >= Math.abs(ny)) {
    return nx > 0 ? ["Left to right", "Right to left"] : ["Right to left", "Left to right"];
  }
  return ny > 0 ? ["Top to bottom", "Bottom to top"] : ["Bottom to top", "Top to bottom"];
}

// --- Geometry ------------------------------------------------------------------

function lineInPixels(line, override) {
  const [w, h] = state.frameSize;
  const x1 = override?.end === 1 ? override.point.x : line.x1 * w;
  const y1 = override?.end === 1 ? override.point.y : line.y1 * h;
  const x2 = override?.end === 2 ? override.point.x : line.x2 * w;
  const y2 = override?.end === 2 ? override.point.y : line.y2 * h;
  return { x1, y1, x2, y2 };
}

function forwardNormal(p) {
  const dx = p.x2 - p.x1;
  const dy = p.y2 - p.y1;
  const length = Math.hypot(dx, dy) || 1;
  return { x: -dy / length, y: dx / length };
}

function canvasPoint(event) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: ((event.clientX - rect.left) * canvas.width) / rect.width,
    y: ((event.clientY - rect.top) * canvas.height) / rect.height,
  };
}

function pixelsPerScreenPixel() {
  const rect = canvas.getBoundingClientRect();
  return rect.width ? canvas.width / rect.width : 1;
}

function handleAt(point) {
  if (!state.frameSize) return null;
  const radius = HANDLE_RADIUS_SCREEN * pixelsPerScreenPixel();
  for (const line of state.lines) {
    const p = lineInPixels(line);
    if (Math.hypot(point.x - p.x1, point.y - p.y1) <= radius) return { lineId: line.id, end: 1 };
    if (Math.hypot(point.x - p.x2, point.y - p.y2) <= radius) return { lineId: line.id, end: 2 };
  }
  return null;
}

// --- Drawing -------------------------------------------------------------------

function fitCanvas() {
  if (!state.frameSize) return;
  const [w, h] = state.frameSize;
  const style = getComputedStyle(canvasWrap);
  const availableWidth = canvasWrap.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - 2;
  const availableHeight = canvasWrap.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom) - 2;
  // Small streams are scaled up (to twice their size at most) so they're
  // easy to draw on; big ones are scaled down to fit without scrolling.
  const scale = Math.max(0.1, Math.min(availableWidth / w, availableHeight / h, 2));
  canvas.style.width = `${Math.floor(w * scale)}px`;
  canvas.style.height = `${Math.floor(h * scale)}px`;
}

function draw() {
  if (!state.image || !state.frameSize) {
    return;
  }
  const [w, h] = state.frameSize;
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
    fitCanvas();
  }
  ctx.clearRect(0, 0, w, h);
  ctx.drawImage(state.image, 0, 0, w, h);
  const unit = Math.max(1.5, w / 400); // stroke/label size that reads at any resolution
  if (showBoxesInput.checked && state.view) {
    drawVehicles(unit);
  }
  for (const line of state.lines) {
    const override = state.dragging?.lineId === line.id && state.pointer ? { end: state.dragging.end, point: state.pointer } : null;
    drawLine(line, lineInPixels(line, override), unit);
  }
  drawRecentCrossings(unit);
  if (state.mode === "drawing" && state.draftStart) {
    const end = state.pointer || state.draftStart;
    drawLine(null, { x1: state.draftStart.x, y1: state.draftStart.y, x2: end.x, y2: end.y }, unit);
  }
}

function labelBox(text, x, y, unit, color, align = "left") {
  ctx.font = `600 ${Math.round(6 * unit)}px Inter, system-ui, sans-serif`;
  const padding = 2 * unit;
  const width = ctx.measureText(text).width + padding * 2;
  const height = 9 * unit;
  let left = align === "center" ? x - width / 2 : x;
  left = Math.min(Math.max(0, left), canvas.width - width);
  const top = Math.min(Math.max(0, y - height / 2), canvas.height - height);
  ctx.fillStyle = "rgba(15, 18, 22, 0.78)";
  ctx.fillRect(left, top, width, height);
  ctx.fillStyle = color;
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillText(text, left + padding, top + height / 2 + unit * 0.3);
}

// A label just past an arrow's head, on the side it points to, so it never
// covers the arrowhead itself.
function labelBeyond(text, headX, headY, dirX, dirY, unit, color) {
  ctx.font = `600 ${Math.round(6 * unit)}px Inter, system-ui, sans-serif`;
  const width = ctx.measureText(text).width + 4 * unit;
  const height = 9 * unit;
  const gap = 3 * unit;
  // Centre of the box, pushed out until its edge clears the arrowhead.
  const push = Math.abs(dirX) * (width / 2) + Math.abs(dirY) * (height / 2) + gap;
  const cx = headX + dirX * push;
  const cy = headY + dirY * push;
  labelBox(text, cx, cy, unit, color, "center");
}

function arrow(fromX, fromY, toX, toY, unit, color) {
  const head = 5 * unit;
  const angle = Math.atan2(toY - fromY, toX - fromX);
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 1.6 * unit;
  ctx.beginPath();
  ctx.moveTo(fromX, fromY);
  ctx.lineTo(toX - Math.cos(angle) * head * 0.6, toY - Math.sin(angle) * head * 0.6);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(toX, toY);
  ctx.lineTo(toX - head * Math.cos(angle - 0.45), toY - head * Math.sin(angle - 0.45));
  ctx.lineTo(toX - head * Math.cos(angle + 0.45), toY - head * Math.sin(angle + 0.45));
  ctx.closePath();
  ctx.fill();
}

function drawLine(line, p, unit) {
  const color = cssVar("--line-color") || "#f59e0b";
  ctx.save();
  ctx.lineCap = "round";
  ctx.strokeStyle = "rgba(0, 0, 0, 0.55)";
  ctx.lineWidth = 4 * unit;
  ctx.beginPath();
  ctx.moveTo(p.x1, p.y1);
  ctx.lineTo(p.x2, p.y2);
  ctx.stroke();
  ctx.strokeStyle = color;
  ctx.lineWidth = 2.2 * unit;
  ctx.stroke();

  const length = Math.hypot(p.x2 - p.x1, p.y2 - p.y1);
  if (length > 8 * unit) {
    const n = forwardNormal(p);
    const along = { x: (p.x2 - p.x1) / length, y: (p.y2 - p.y1) / length };
    const mid = { x: (p.x1 + p.x2) / 2, y: (p.y1 + p.y2) / 2 };
    const reach = Math.min(22 * unit, Math.max(12 * unit, length * 0.2));
    const spread = Math.min(length * 0.18, 18 * unit);
    const labels = line ? [line.forward_label, line.reverse_label] : defaultLabels(p.x2 - p.x1, p.y2 - p.y1);
    const sides = [
      { sign: 1, offset: spread, color: cssVar("--dir-a"), label: labels[0] },
      { sign: -1, offset: -spread, color: cssVar("--dir-b"), label: labels[1] },
    ];
    for (const side of sides) {
      const cx = mid.x + along.x * side.offset;
      const cy = mid.y + along.y * side.offset;
      const fromX = cx - n.x * side.sign * reach;
      const fromY = cy - n.y * side.sign * reach;
      const toX = cx + n.x * side.sign * reach;
      const toY = cy + n.y * side.sign * reach;
      arrow(fromX, fromY, toX, toY, unit, side.color);
      labelBeyond(side.label, toX, toY, n.x * side.sign, n.y * side.sign, unit, side.color);
    }
  }

  // End handles (drag to move).
  for (const [x, y, end] of [[p.x1, p.y1, 1], [p.x2, p.y2, 2]]) {
    const hovered = line && state.hoverHandle?.lineId === line.id && state.hoverHandle.end === end;
    ctx.beginPath();
    ctx.arc(x, y, (hovered ? 5 : 3.5) * unit, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = unit;
    ctx.strokeStyle = "#111";
    ctx.stroke();
  }
  if (line) {
    labelBox(line.name, p.x1 + 5 * unit, p.y1 - 8 * unit, unit, color);
  }
  ctx.restore();
}

function drawVehicles(unit) {
  const accent = cssVar("--accent") || "#147c72";
  for (const track of state.view.tracks || []) {
    const [x1, y1, x2, y2] = track.box;
    ctx.save();
    const color = track.counted ? accent : "#e5e7eb";
    ctx.strokeStyle = color;
    ctx.lineWidth = (track.confirmed ? 1.6 : 1) * unit;
    if (!track.confirmed || !track.seen_now) ctx.setLineDash([4 * unit, 3 * unit]);
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    ctx.setLineDash([]);
    if (track.trail && track.trail.length > 1) {
      ctx.globalAlpha = 0.7;
      ctx.beginPath();
      ctx.moveTo(track.trail[0][0], track.trail[0][1]);
      for (const [x, y] of track.trail.slice(1)) ctx.lineTo(x, y);
      ctx.lineWidth = unit;
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
    if (track.confirmed) {
      labelBox(`#${track.id} ${track.class}`, x1, y1 - 5 * unit, unit, color);
    }
    ctx.restore();
  }
}

function drawRecentCrossings(unit) {
  const crossings = state.view?.recent_crossings || [];
  for (const crossing of crossings) {
    const age = secondsSince(crossing.crossed_at);
    if (age > 2.5 || !crossing.point) continue;
    const line = state.lines.find((l) => l.id === crossing.line_id);
    const color = crossing.direction === "forward" ? cssVar("--dir-a") : cssVar("--dir-b");
    ctx.save();
    ctx.globalAlpha = Math.max(0, 1 - age / 2.5);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2 * unit;
    ctx.beginPath();
    ctx.arc(crossing.point[0], crossing.point[1], (6 + age * 6) * unit, 0, Math.PI * 2);
    ctx.stroke();
    if (line) labelBox(`+1 ${crossing.direction_label}`, crossing.point[0] + 8 * unit, crossing.point[1], unit, color);
    ctx.restore();
  }
}

// --- Loading data ----------------------------------------------------------------

function selectedCamera() {
  return state.cameras.find((camera) => camera.id === state.cameraId) || null;
}

async function loadCameras(preferredId) {
  const payload = await fetchJson("/api/cameras");
  state.cameras = payload.cameras.filter((camera) => camera.kind === "traffic");
  let remembered = null;
  try {
    remembered = localStorage.getItem(CAMERA_STORAGE_KEY);
  } catch (error) {
    remembered = null;
  }
  const wanted = [preferredId, new URLSearchParams(location.search).get("camera"), remembered];
  state.cameraId = wanted.find((id) => id && state.cameras.some((c) => c.id === id)) || state.cameras[0]?.id || null;
  cameraSelect.innerHTML = "";
  if (state.cameras.length === 0) {
    cameraSelect.innerHTML = '<option value="">No traffic cameras yet</option>';
  }
  for (const camera of state.cameras) {
    const option = document.createElement("option");
    option.value = camera.id;
    option.textContent = camera.name;
    cameraSelect.append(option);
  }
  cameraSelect.value = state.cameraId || "";
  cameraSelect.disabled = state.cameras.length === 0;
  deleteCameraButton.disabled = !state.cameraId;
  drawLineButton.disabled = !state.cameraId;
  downloadButton.disabled = !state.cameraId;
  try {
    if (state.cameraId) localStorage.setItem(CAMERA_STORAGE_KEY, state.cameraId);
  } catch (error) {
    // storage blocked; nothing to remember
  }
  if (state.cameraId) {
    history.replaceState(null, "", `/traffic?camera=${encodeURIComponent(state.cameraId)}`);
  }
  await switchCamera();
}

async function switchCamera() {
  cancelDrawing();
  state.view = null;
  state.image = null;
  state.frameSeq = null;
  state.frameSize = null;
  state.counts = null;
  canvas.style.display = "none";
  crossingList.innerHTML = "";
  delete crossingList.dataset.key;
  delete crossingList.dataset.newest;
  if (!state.cameraId) {
    state.lines = [];
    renderLines();
    emptyState.style.display = "block";
    emptyState.textContent = 'No traffic cameras yet. Click "+ Add Camera" to create one.';
    connectionStatus.textContent = "";
    trafficIndicator.hidden = true;
    return;
  }
  emptyState.style.display = "block";
  emptyState.textContent = "Waiting for the first picture…";
  await loadLines();
  await Promise.all([pollView(), loadCounts()]);
}

async function loadLines() {
  const cameraId = state.cameraId;
  const payload = await fetchJson(`/api/lines?camera_id=${encodeURIComponent(cameraId)}`);
  if (cameraId !== state.cameraId) return;
  state.lines = payload.lines;
  renderLines();
  draw();
}

function todayRange() {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  return { start: start.toISOString(), end: end.toISOString() };
}

async function loadCounts() {
  const cameraId = state.cameraId;
  if (!cameraId) return;
  const { start, end } = todayRange();
  try {
    const report = await fetchJson(
      `/api/traffic/counts?camera_id=${encodeURIComponent(cameraId)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&bin_minutes=15`,
    );
    if (cameraId !== state.cameraId) return;
    state.counts = report;
    renderLines();
    renderPeakHour();
  } catch (error) {
    console.warn(error);
  }
}

async function pollView() {
  const cameraId = state.cameraId;
  if (!cameraId || state.viewInFlight) return;
  state.viewInFlight = true;
  try {
    const view = await fetchJson(`/api/cameras/${encodeURIComponent(cameraId)}/traffic-view`);
    if (cameraId !== state.cameraId) return;
    renderConnection(view);
    renderCrossings(view.recent_crossings || []);
    const seq = view.frame_seq ?? null;
    const nothingAnalysedYet = seq === null;
    const due = nothingAnalysedYet && Date.now() - state.lastFrameRequest > IDLE_FRAME_REFRESH_MS;
    if ((seq !== null && seq !== state.frameSeq) || due) {
      state.lastFrameRequest = Date.now();
      await loadFrame(cameraId, view, seq);
    } else {
      state.view = view;
      draw();
    }
  } catch (error) {
    connectionStatus.className = "traffic-connection problem";
    connectionStatus.textContent = `Can't reach the server: ${error.message}`;
  } finally {
    state.viewInFlight = false;
  }
}

function loadFrame(cameraId, view, seq) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      if (cameraId === state.cameraId) {
        state.image = img;
        state.view = view;
        state.frameSeq = seq;
        state.frameSize = view.frame_size || [img.naturalWidth, img.naturalHeight];
        canvas.style.display = "block";
        emptyState.style.display = "none";
        draw();
      }
      resolve();
    };
    img.onerror = () => resolve(); // no picture yet; the status line explains why
    img.src = `/api/cameras/${encodeURIComponent(cameraId)}/traffic-frame.jpg?seq=${seq ?? Date.now()}`;
  });
}

// --- Rendering the sidebar --------------------------------------------------------

function renderConnection(view) {
  const camera = view.camera || {};
  const status = view.status || {};
  let text = "";
  let kind = "";
  if (!camera.configured) {
    kind = "problem";
    text = camera.config_error
      ? `Camera settings problem: ${escapeHtml(camera.config_error)}`
      : `Not connected to a camera yet. Its settings go in <code>data/camera_sources.json</code> under <code>"${escapeHtml(state.cameraId)}"</code>, then restart the server.`;
  } else if (camera.last_error && camera.consecutive_failures > 0) {
    kind = "problem";
    text = `Camera problem: ${escapeHtml(camera.last_error)}`;
  } else if (status.model_loading) {
    text = "Connected. Loading the vehicle model (about half a minute the first time)…";
  } else if (!status.last_frame_at) {
    text = "Connected. Waiting for the first picture to be analysed…";
  } else if (secondsSince(status.last_frame_at) > 10) {
    kind = "problem";
    text = `No picture analysed for ${Math.round(secondsSince(status.last_frame_at))} s.${status.last_error ? " Last error: " + escapeHtml(status.last_error) : ""}`;
  } else {
    kind = "ok";
    const wanted = camera.interval_seconds ? 1 / camera.interval_seconds : null;
    const fps = status.processing_fps;
    text = `&#9679; Live · analysing ${fps ? fps.toFixed(1) : "…"} pictures/s`;
    if (fps && wanted && fps < wanted * 0.75) {
      text += ` (camera sends ${wanted.toFixed(1)}/s; this computer can't keep up with all of them, so fast cars may be missed)`;
    }
  }
  connectionStatus.className = `traffic-connection ${kind}`;
  connectionStatus.innerHTML = text;
  trafficIndicator.hidden = kind !== "ok";
}

function lineTotals(lineId) {
  const line = state.counts?.lines?.find((l) => l.id === lineId);
  return line ? line.totals : null;
}

function renderLines() {
  countsRangeLabel.textContent = "Today";
  if (!state.cameraId) {
    lineList.innerHTML = "";
    return;
  }
  if (state.lines.length === 0) {
    lineList.innerHTML =
      '<p class="line-empty">No lines yet. Click <strong>Draw Line</strong>, then click two points across the road or driveway. Each vehicle whose bottom edge crosses it is counted once.</p>';
    return;
  }
  lineList.innerHTML = "";
  for (const line of state.lines) {
    const totals = lineTotals(line.id);
    const p = state.frameSize ? lineInPixels(line) : { x1: line.x1, y1: line.y1, x2: line.x2, y2: line.y2 };
    const n = forwardNormal(p);
    const card = document.createElement("div");
    card.className = "line-card";
    card.innerHTML = `
      <div class="line-card-head">
        <span class="line-card-name" title="${escapeHtml(line.name)}">${escapeHtml(line.name)}</span>
        <button type="button" class="edit-line">Rename</button>
        <button type="button" class="delete-line">Delete</button>
      </div>
      <div class="direction-row dir-a"><span class="direction-arrow">${arrowFor(n.x, n.y)}</span><span class="direction-name">${escapeHtml(line.forward_label)}</span><span class="direction-count">${totals ? totals.forward : "–"}</span></div>
      <div class="direction-row dir-b"><span class="direction-arrow">${arrowFor(-n.x, -n.y)}</span><span class="direction-name">${escapeHtml(line.reverse_label)}</span><span class="direction-count">${totals ? totals.reverse : "–"}</span></div>
    `;
    card.querySelector(".edit-line").addEventListener("click", () => openLineDialog(line));
    card.querySelector(".delete-line").addEventListener("click", () => deleteLine(line));
    lineList.append(card);
  }
}

function renderPeakHour() {
  const peak = state.counts?.peak_hour;
  peakHour.textContent = peak
    ? `Busiest hour today: ${formatHour(peak.start)}–${formatHour(peak.end)} (${peak.volume} vehicles)`
    : state.counts?.total === 0
      ? "No vehicles counted today yet."
      : "";
}

function renderCrossings(crossings) {
  const key = crossings.map((c) => `${c.crossed_at}${c.track_id}`).join("|");
  if (crossingList.dataset.key === key) return;
  const newest = crossings[0]?.crossed_at;
  const previousNewest = crossingList.dataset.newest;
  crossingList.dataset.key = key;
  crossingList.dataset.newest = newest || "";
  if (crossings.length === 0) {
    crossingList.innerHTML = '<li class="traffic-muted">None yet since the server started.</li>';
    return;
  }
  crossingList.innerHTML = crossings
    .slice(0, 15)
    .map((c) => {
      const cls = c.direction === "forward" ? "dir-a" : "dir-b";
      return `<li><time>${formatClock(c.crossed_at)}</time><span><span class="${cls}">${escapeHtml(c.direction_label)}</span> · ${escapeHtml(c.vehicle_class)} · ${escapeHtml(c.line_name)}</span></li>`;
    })
    .join("");
  // A new crossing arrived: refresh the totals now instead of waiting.
  if (newest && newest !== previousNewest && previousNewest !== undefined) {
    loadCounts();
  }
}

// --- Drawing a line -----------------------------------------------------------------

function startDrawing() {
  if (!state.cameraId) return;
  if (!state.image) {
    setStatus("Wait for the first picture, then draw.");
    return;
  }
  state.mode = "drawing";
  state.draftStart = null;
  state.pointer = null;
  drawLineButton.classList.add("active");
  cancelDrawButton.hidden = false;
  canvas.classList.add("drawing");
  setStatus("Click where the line starts.");
}

function cancelDrawing() {
  state.mode = "idle";
  state.draftStart = null;
  state.pointer = null;
  state.pendingLine = null;
  drawLineButton.classList.remove("active");
  cancelDrawButton.hidden = true;
  canvas.classList.remove("drawing");
  setStatus("");
  draw();
}

function openLineDialog(line) {
  state.editingLineId = line ? line.id : null;
  let p;
  if (line) {
    lineDialogTitle.textContent = "Rename line";
    lineNameInput.value = line.name;
    forwardLabelInput.value = line.forward_label;
    reverseLabelInput.value = line.reverse_label;
    p = state.frameSize ? lineInPixels(line) : line;
  } else {
    const [w, h] = state.frameSize;
    p = { x1: state.pendingLine.x1 * w, y1: state.pendingLine.y1 * h, x2: state.pendingLine.x2 * w, y2: state.pendingLine.y2 * h };
    const [a, b] = defaultLabels(p.x2 - p.x1, p.y2 - p.y1);
    lineDialogTitle.textContent = "New counting line";
    lineNameInput.value = `Line ${state.lines.length + 1}`;
    forwardLabelInput.value = a;
    reverseLabelInput.value = b;
  }
  const n = forwardNormal(p);
  forwardArrow.textContent = arrowFor(n.x, n.y);
  forwardArrow.className = "direction-arrow dir-a";
  reverseArrow.textContent = arrowFor(-n.x, -n.y);
  reverseArrow.className = "direction-arrow dir-b";
  lineDialog.showModal();
  lineNameInput.select();
}

async function saveLineDialog() {
  const body = {
    name: lineNameInput.value.trim(),
    forward_label: forwardLabelInput.value.trim(),
    reverse_label: reverseLabelInput.value.trim(),
  };
  try {
    if (state.editingLineId !== null) {
      await fetchJson(`/api/lines/${state.editingLineId}`, jsonRequest("PATCH", body));
      setStatus(`Saved "${body.name}".`);
    } else {
      await fetchJson("/api/lines", jsonRequest("POST", { ...body, ...state.pendingLine, camera_id: state.cameraId }));
      setStatus(`Added "${body.name}". Vehicles crossing it are counted from now on.`);
    }
  } catch (error) {
    setStatus(error.message);
  }
  state.editingLineId = null;
  cancelDrawing();
  await loadLines();
  loadCounts();
}

async function moveLineEnd(lineId, end, point) {
  const [w, h] = state.frameSize;
  const fx = Math.min(1, Math.max(0, point.x / w));
  const fy = Math.min(1, Math.max(0, point.y / h));
  const body = end === 1 ? { x1: fx, y1: fy } : { x2: fx, y2: fy };
  try {
    await fetchJson(`/api/lines/${lineId}`, jsonRequest("PATCH", body));
    setStatus("Line moved.");
  } catch (error) {
    setStatus(error.message);
  }
  await loadLines();
}

async function deleteLine(line) {
  const totals = lineTotals(line.id);
  const today = totals ? totals.forward + totals.reverse : 0;
  const ok = await confirmAction(
    `Delete "${line.name}"?`,
    `Every crossing recorded on this line is deleted too${today ? ` (${today} today)` : ""}. This can't be undone. Download its counts first if you need them.`,
  );
  if (!ok) return;
  try {
    await fetchJson(`/api/lines/${line.id}`, { method: "DELETE" });
    setStatus(`Deleted "${line.name}".`);
  } catch (error) {
    setStatus(error.message);
  }
  await loadLines();
  loadCounts();
}

// --- Cameras -------------------------------------------------------------------------

async function createCamera(name) {
  try {
    const payload = await fetchJson("/api/cameras", jsonRequest("POST", { name, kind: "traffic" }));
    await loadCameras(payload.camera.id);
    setStatus(
      payload.camera.live_started
        ? `Created "${payload.camera.name}" and connected to its camera.`
        : `Created "${payload.camera.name}" (id "${payload.camera.id}").`,
    );
  } catch (error) {
    setStatus(error.message);
  }
}

async function deleteCamera() {
  const camera = selectedCamera();
  if (!camera) return;
  const ok = await confirmAction(
    `Delete camera "${camera.name}"?`,
    "Its counting lines and every crossing recorded on them are deleted. Its connection settings in camera_sources.json stay. This can't be undone.",
  );
  if (!ok) return;
  try {
    await fetchJson(`/api/cameras/${encodeURIComponent(camera.id)}`, { method: "DELETE" });
    setStatus(`Deleted "${camera.name}".`);
  } catch (error) {
    setStatus(error.message);
  }
  await loadCameras();
}

let confirmResolver = null;
function confirmAction(title, message) {
  $("#confirmTitle").textContent = title;
  $("#confirmMessage").textContent = message;
  confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmResolver = resolve;
  });
}

confirmDialog.addEventListener("close", () => {
  if (confirmResolver) {
    confirmResolver(confirmDialog.returnValue === "ok");
    confirmResolver = null;
  }
  confirmDialog.returnValue = "";
});
$("#confirmCancelButton").addEventListener("click", () => confirmDialog.close("cancel"));

// --- Events -----------------------------------------------------------------------

drawLineButton.addEventListener("click", () => (state.mode === "drawing" ? cancelDrawing() : startDrawing()));
cancelDrawButton.addEventListener("click", cancelDrawing);
showBoxesInput.addEventListener("change", draw);

canvas.addEventListener("mousedown", (event) => {
  if (state.mode !== "idle" || !state.frameSize) return;
  const handle = handleAt(canvasPoint(event));
  if (handle) {
    state.dragging = handle;
    state.pointer = canvasPoint(event);
    event.preventDefault();
  }
});

canvas.addEventListener("mousemove", (event) => {
  const point = canvasPoint(event);
  if (state.mode === "drawing") {
    state.pointer = point;
    draw();
    return;
  }
  if (state.dragging) {
    state.pointer = point;
    draw();
    return;
  }
  const handle = handleAt(point);
  const changed = JSON.stringify(handle) !== JSON.stringify(state.hoverHandle);
  state.hoverHandle = handle;
  canvas.style.cursor = handle ? "grab" : "";
  if (changed) draw();
});

window.addEventListener("mouseup", async (event) => {
  if (!state.dragging) return;
  const { lineId, end } = state.dragging;
  const point = state.pointer || canvasPoint(event);
  state.dragging = null;
  state.pointer = null;
  await moveLineEnd(lineId, end, point);
});

canvas.addEventListener("click", (event) => {
  if (state.mode !== "drawing") return;
  const point = canvasPoint(event);
  if (!state.draftStart) {
    state.draftStart = point;
    setStatus("Now click where the line ends. (Esc to cancel)");
    draw();
    return;
  }
  const [w, h] = state.frameSize;
  const minimum = Math.max(w, h) * 0.02;
  if (Math.hypot(point.x - state.draftStart.x, point.y - state.draftStart.y) < minimum) {
    setStatus("Too short. Click further away from the first point.");
    return;
  }
  state.pendingLine = { x1: state.draftStart.x / w, y1: state.draftStart.y / h, x2: point.x / w, y2: point.y / h };
  state.pointer = point;
  draw();
  openLineDialog(null);
});

lineForm.addEventListener("submit", (event) => {
  event.preventDefault();
  lineDialog.close();
  saveLineDialog();
});
$("#cancelLineButton").addEventListener("click", () => {
  lineDialog.close();
  state.editingLineId = null;
  cancelDrawing();
});
lineDialog.addEventListener("cancel", () => {
  state.editingLineId = null;
  cancelDrawing();
});

$("#addCameraButton").addEventListener("click", () => {
  cameraNameInput.value = "";
  cameraDialog.showModal();
});
$("#cancelCameraButton").addEventListener("click", () => cameraDialog.close());
cameraForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const name = cameraNameInput.value.trim();
  cameraDialog.close();
  if (name) createCamera(name);
});
deleteCameraButton.addEventListener("click", deleteCamera);

cameraSelect.addEventListener("change", async () => {
  state.cameraId = cameraSelect.value || null;
  try {
    localStorage.setItem(CAMERA_STORAGE_KEY, state.cameraId || "");
  } catch (error) {
    // ignore
  }
  history.replaceState(null, "", `/traffic?camera=${encodeURIComponent(state.cameraId || "")}`);
  await switchCamera();
});

downloadButton.addEventListener("click", () => {
  if (!state.cameraId) return;
  const day = countDateInput.value || localDateIso();
  const url = `/api/traffic/export.csv?camera_id=${encodeURIComponent(state.cameraId)}&start=${day}&end=${day}&bin_minutes=${binSelect.value}`;
  window.location.href = url;
});

document.addEventListener("keydown", (event) => {
  if (event.target.closest("input, select, textarea, dialog")) return;
  if (event.key === "l" || event.key === "L") {
    event.preventDefault();
    state.mode === "drawing" ? cancelDrawing() : startDrawing();
  } else if (event.key === "Escape" && state.mode === "drawing") {
    cancelDrawing();
  }
});

window.addEventListener("resize", fitCanvas);
// Redraw on theme change so line/arrow colours follow it.
new MutationObserver(draw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

// --- Start ---------------------------------------------------------------------

countDateInput.value = localDateIso();
countDateInput.max = localDateIso();
setInterval(pollView, VIEW_POLL_MS);
setInterval(loadCounts, COUNTS_POLL_MS);
loadCameras().catch((error) => setStatus(error.message));
