const state = {
  cameras: [],
  images: [],
  spaces: [],
  cameraId: "camera_1",
  selectedImage: null,
  imageElement: null,
  observations: { detections: [], occupancy: [] },
  mode: "idle",
  draftPoints: [],
  hoveredSpaceId: null,
  hoveredDetectionId: null,
  selectedSpaceId: null,
  draggingHandle: null,
  suppressNextClick: false,
  statusSort: "label",
};

const HANDLE_RADIUS = 9;

const cameraSelect = document.querySelector("#cameraSelect");
const imageList = document.querySelector("#imageList");
const spaceList = document.querySelector("#spaceList");
const canvas = document.querySelector("#annotationCanvas");
const ctx = canvas.getContext("2d");
const emptyState = document.querySelector("#emptyState");
const markSpaceButton = document.querySelector("#markSpaceButton");
const undoPointButton = document.querySelector("#undoPointButton");
const clearDraftButton = document.querySelector("#clearDraftButton");
const configLink = document.querySelector("#configLink");
const statusEl = document.querySelector("#status");
const uploadForm = document.querySelector("#uploadForm");
const uploadInput = document.querySelector("#uploadInput");
const videoUploadForm = document.querySelector("#videoUploadForm");
const videoUploadInput = document.querySelector("#videoUploadInput");
const videoIntervalInput = document.querySelector("#videoIntervalInput");
const videoEndInput = document.querySelector("#videoEndInput");
const videoSubmitButton = videoUploadForm.querySelector("button");
const dialog = document.querySelector("#spaceDialog");
const spaceForm = document.querySelector("#spaceForm");
const spaceLabel = document.querySelector("#spaceLabel");
const cancelSpaceButton = document.querySelector("#cancelSpaceButton");
const statusList = document.querySelector("#statusList");
const statusSortSelect = document.querySelector("#statusSortSelect");
const historyDialog = document.querySelector("#historyDialog");
const historyTitle = document.querySelector("#historyTitle");
const historyList = document.querySelector("#historyList");
const closeHistoryButton = document.querySelector("#closeHistoryButton");

function setStatus(message) {
  statusEl.textContent = message;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

async function loadInitialData() {
  const payload = await fetchJson("/api/cameras");
  state.cameras = payload.cameras;
  state.cameraId = state.cameras[0]?.id || "camera_1";
  renderCameras();
  await loadCameraData();
}

async function loadCameraData() {
  configLink.href = `/api/config/${state.cameraId}`;
  const [imagesPayload, spacesPayload] = await Promise.all([
    fetchJson(`/api/images?camera_id=${encodeURIComponent(state.cameraId)}`),
    fetchJson(`/api/spaces?camera_id=${encodeURIComponent(state.cameraId)}`),
  ]);
  state.images = imagesPayload.images;
  state.spaces = spacesPayload.spaces;
  state.selectedImage = state.images[0] || null;
  renderImages();
  renderSpaces();
  renderStatusList();
  await loadSelectedImage();
}

function renderCameras() {
  cameraSelect.innerHTML = "";
  for (const camera of state.cameras) {
    const option = document.createElement("option");
    option.value = camera.id;
    option.textContent = camera.name;
    cameraSelect.append(option);
  }
  cameraSelect.value = state.cameraId;
}

function renderImages() {
  imageList.innerHTML = "";
  if (state.images.length === 0) {
    imageList.textContent = "No images yet.";
    return;
  }
  for (const image of state.images) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `image-row ${state.selectedImage?.id === image.id ? "active" : ""}`;
    row.innerHTML = `
      <span class="row-title">${escapeHtml(image.filename)}</span>
      <span class="row-meta">${escapeHtml(image.captured_at)}</span>
    `;
    row.addEventListener("click", () => selectImage(image));
    imageList.append(row);
  }
}

async function selectImage(image, options = {}) {
  if (!image || state.selectedImage?.id === image.id) {
    return;
  }
  state.selectedImage = image;
  state.draftPoints = [];
  state.hoveredSpaceId = null;
  state.hoveredDetectionId = null;
  state.selectedSpaceId = null;
  renderImages();
  updateDraftControls();
  if (options.scrollIntoView !== false) {
    scrollSelectedImageIntoView();
  }
  await loadSelectedImage();
}

function scrollSelectedImageIntoView() {
  const activeRow = imageList.querySelector(".image-row.active");
  activeRow?.scrollIntoView({ block: "nearest" });
}

function selectedImageIndex() {
  return state.images.findIndex((image) => image.id === state.selectedImage?.id);
}

function shouldIgnoreImageNavigation(event) {
  if (dialog.open || historyDialog.open) {
    return true;
  }
  if (event.target?.classList?.contains("image-row")) {
    return false;
  }
  const tagName = event.target?.tagName;
  return ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(tagName);
}

async function navigateImages(direction) {
  if (state.images.length === 0) {
    return;
  }
  const currentIndex = selectedImageIndex();
  const fallbackIndex = direction > 0 ? 0 : state.images.length - 1;
  const nextIndex =
    currentIndex === -1
      ? fallbackIndex
      : Math.min(Math.max(currentIndex + direction, 0), state.images.length - 1);
  await selectImage(state.images[nextIndex]);
}

function renderSpaces() {
  spaceList.innerHTML = "";
  if (state.spaces.length === 0) {
    spaceList.textContent = "No spaces marked yet.";
    return;
  }
  for (const space of state.spaces) {
    const row = document.createElement("div");
    row.className = "space-row";
    row.innerHTML = `
      <div>
        <div class="row-title">${escapeHtml(space.label)}</div>
        <div class="row-meta">${space.polygon.length} points</div>
      </div>
    `;
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "delete-space";
    deleteButton.textContent = "Delete";
    deleteButton.addEventListener("click", () => deleteSpace(space.id));
    row.append(deleteButton);
    spaceList.append(row);
  }
}

async function loadSelectedImage() {
  if (!state.selectedImage) {
    state.imageElement = null;
    state.observations = { detections: [], occupancy: [] };
    state.hoveredDetectionId = null;
    canvas.style.display = "none";
    emptyState.style.display = "block";
    draw();
    return;
  }

  state.observations = await fetchImageObservations(state.selectedImage.id);
  const img = new Image();
  img.onload = () => {
    state.imageElement = img;
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.style.display = "block";
    emptyState.style.display = "none";
    draw();
    const observedCount = state.observations.occupancy.length;
    const occupiedCount = state.observations.occupancy.filter((item) => item.occupied).length;
    setStatus(
      observedCount > 0
        ? `${occupiedCount}/${observedCount} observed occupied`
        : `${state.spaces.length} spaces configured`,
    );
  };
  img.src = state.selectedImage.url;
}

async function fetchImageObservations(imageId) {
  try {
    return await fetchJson(`/api/observations?image_id=${encodeURIComponent(imageId)}`);
  } catch (error) {
    console.warn(error);
    return { detections: [], occupancy: [] };
  }
}

function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!state.imageElement) {
    return;
  }

  ctx.drawImage(state.imageElement, 0, 0);
  for (const space of state.spaces) {
    drawPolygon(space, space.id === state.hoveredSpaceId, space.id === state.selectedSpaceId);
  }
  const selectedSpace = state.spaces.find((space) => space.id === state.selectedSpaceId);
  if (selectedSpace) {
    drawSpaceHandles(selectedSpace);
  }
  for (const detection of state.observations.detections) {
    drawDetection(detection, detection.id === state.hoveredDetectionId);
  }
  if (state.draftPoints.length > 0) {
    drawDraft();
  }
}

function drawPolygon(space, isHovered, isSelected) {
  const points = space.polygon;
  const observation = observationForSpace(space.id);
  const occupied = observation?.occupied;
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) {
      ctx.moveTo(point.x, point.y);
    } else {
      ctx.lineTo(point.x, point.y);
    }
  });
  ctx.closePath();
  if (occupied === true) {
    ctx.fillStyle = isHovered ? "rgba(220, 38, 38, 0.28)" : "rgba(220, 38, 38, 0.16)";
    ctx.strokeStyle = "#dc2626";
  } else if (occupied === false) {
    ctx.fillStyle = isHovered ? "rgba(38, 166, 154, 0.28)" : "rgba(38, 166, 154, 0.14)";
    ctx.strokeStyle = "#26a69a";
  } else {
    ctx.fillStyle = isHovered ? "rgba(38, 166, 154, 0.3)" : "rgba(38, 166, 154, 0.18)";
    ctx.strokeStyle = "#26a69a";
  }
  ctx.fill();
  ctx.lineWidth = isHovered ? 4 : 3;
  ctx.stroke();

  if (isSelected) {
    ctx.save();
    ctx.setLineDash([8, 5]);
    ctx.lineWidth = 3;
    ctx.strokeStyle = "#f59e0b";
    ctx.stroke();
    ctx.restore();
  }

  if (observation && observation.since) {
    drawDurationBadge(points, observation);
  }

  if (!isHovered && !isSelected) {
    return;
  }

  const center = centroid(points);
  ctx.fillStyle = "rgba(0, 0, 0, 0.42)";
  ctx.fillRect(center.x - 28, center.y - 12, 56, 24);
  ctx.fillStyle = "rgba(255, 255, 255, 0.82)";
  ctx.font = "14px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(space.label, center.x, center.y);
}

function drawDurationBadge(points, observation) {
  const label = `${observation.occupied ? "Occupied" : "Vacant"} ${formatDuration(observation.duration_seconds)}`;
  const center = centroid(points);
  const top = points.reduce((topmost, point) => (point.y < topmost.y ? point : topmost), points[0]);

  ctx.font = "11px system-ui, sans-serif";
  const paddingX = 6;
  const textWidth = ctx.measureText(label).width;
  const boxWidth = textWidth + paddingX * 2;
  const boxHeight = 17;
  const x = center.x - boxWidth / 2;
  const y = Math.max(2, top.y - boxHeight - 4);

  ctx.fillStyle = observation.occupied ? "rgba(220, 38, 38, 0.88)" : "rgba(14, 93, 86, 0.88)";
  ctx.fillRect(x, y, boxWidth, boxHeight);
  ctx.fillStyle = "#fff";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, x + boxWidth / 2, y + boxHeight / 2);
}

function drawSpaceHandles(space) {
  for (const point of space.polygon) {
    ctx.beginPath();
    ctx.arc(point.x, point.y, HANDLE_RADIUS, 0, Math.PI * 2);
    ctx.fillStyle = "#f59e0b";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
  }
}

function drawDetection(detection, isHovered) {
  const { x1, y1, x2, y2 } = detection.bbox;
  const width = x2 - x1;
  const height = y2 - y1;
  ctx.lineWidth = isHovered ? 3 : 2;
  ctx.strokeStyle = isHovered ? "rgba(14, 93, 86, 1)" : "rgba(14, 93, 86, 0.82)";
  ctx.strokeRect(x1, y1, width, height);

  if (!isHovered) {
    return;
  }

  ctx.fillStyle = "rgba(14, 93, 86, 0.75)";
  const label = `${detection.class_name} ${Math.round(detection.confidence * 100)}%`;
  ctx.font = "12px system-ui, sans-serif";
  const labelWidth = Math.max(72, ctx.measureText(label).width + 12);
  ctx.fillRect(x1, Math.max(0, y1 - 22), labelWidth, 22);
  ctx.fillStyle = "#fff";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(label, x1 + 6, Math.max(11, y1 - 11));
}

function observationForSpace(spaceId) {
  return state.observations.occupancy.find((item) => item.space_id === spaceId);
}

function drawDraft() {
  ctx.lineWidth = 3;
  ctx.strokeStyle = "#f59e0b";
  ctx.fillStyle = "rgba(245, 158, 11, 0.2)";
  ctx.beginPath();
  state.draftPoints.forEach((point, index) => {
    if (index === 0) {
      ctx.moveTo(point.x, point.y);
    } else {
      ctx.lineTo(point.x, point.y);
    }
  });
  if (state.draftPoints.length === 4) {
    ctx.closePath();
    ctx.fill();
  }
  ctx.stroke();

  for (const point of state.draftPoints) {
    ctx.beginPath();
    ctx.arc(point.x, point.y, 6, 0, Math.PI * 2);
    ctx.fillStyle = "#f59e0b";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
  }
}

function centroid(points) {
  const total = points.reduce(
    (acc, point) => ({ x: acc.x + point.x, y: acc.y + point.y }),
    { x: 0, y: 0 },
  );
  return { x: total.x / points.length, y: total.y / points.length };
}

function canvasPoint(event) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: Math.round((event.clientX - rect.left) * scaleX),
    y: Math.round((event.clientY - rect.top) * scaleY),
  };
}

function spaceAtPoint(point) {
  for (let index = state.spaces.length - 1; index >= 0; index -= 1) {
    const space = state.spaces[index];
    if (pointInPolygon(point, space.polygon)) {
      return space;
    }
  }
  return null;
}

function handleIndexAtPoint(space, point) {
  if (!space) {
    return -1;
  }
  for (let index = 0; index < space.polygon.length; index += 1) {
    const handle = space.polygon[index];
    const dx = handle.x - point.x;
    const dy = handle.y - point.y;
    if (Math.sqrt(dx * dx + dy * dy) <= HANDLE_RADIUS + 3) {
      return index;
    }
  }
  return -1;
}

function detectionAtPoint(point) {
  for (const detection of state.observations.detections) {
    const { x1, y1, x2, y2 } = detection.bbox;
    if (point.x >= x1 && point.x <= x2 && point.y >= y1 && point.y <= y2) {
      return detection;
    }
  }
  return null;
}

function pointInPolygon(point, polygon) {
  let inside = false;
  for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index++) {
    const currentPoint = polygon[index];
    const previousPoint = polygon[previous];
    const intersects =
      currentPoint.y > point.y !== previousPoint.y > point.y &&
      point.x <
        ((previousPoint.x - currentPoint.x) * (point.y - currentPoint.y)) /
          (previousPoint.y - currentPoint.y) +
          currentPoint.x;
    if (intersects) {
      inside = !inside;
    }
  }
  return inside;
}

function updateDraftControls() {
  markSpaceButton.classList.toggle("active", state.mode === "marking");
  undoPointButton.disabled = state.draftPoints.length === 0;
  clearDraftButton.disabled = state.draftPoints.length === 0;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds) || seconds < 0) {
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

function formatTimestamp(value) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function currentElapsedSeconds(space) {
  if (!space.current_since) {
    return null;
  }
  const started = Date.parse(space.current_since);
  if (Number.isNaN(started)) {
    return null;
  }
  return Math.max(0, (Date.now() - started) / 1000);
}

function renderStatusList() {
  statusList.innerHTML = "";
  if (state.spaces.length === 0) {
    statusList.textContent = "No spaces marked yet.";
    return;
  }
  const rows = [...state.spaces].sort((a, b) => {
    if (state.statusSort === "duration") {
      const bElapsed = currentElapsedSeconds(b);
      const aElapsed = currentElapsedSeconds(a);
      return (bElapsed ?? -1) - (aElapsed ?? -1);
    }
    return a.label.localeCompare(b.label, undefined, { numeric: true });
  });
  for (const space of rows) {
    const known = space.current_occupied !== null && space.current_occupied !== undefined;
    const occupied = space.current_occupied;
    const badgeClass = !known ? "badge-unknown" : occupied ? "badge-occupied" : "badge-vacant";
    const badgeText = !known ? "No data" : occupied ? "Occupied" : "Vacant";
    const durationText = known ? formatDuration(currentElapsedSeconds(space)) : "—";

    const metaText = known ? `${durationText} ${occupied ? "occupied" : "vacant"}` : "No data yet";
    const row = document.createElement("div");
    row.className = "status-row";
    row.innerHTML = `
      <div>
        <div class="status-row-title">
          <span class="status-badge ${badgeClass}">${badgeText}</span>
          <span class="row-title">${escapeHtml(space.label)}</span>
        </div>
        <div class="row-meta">${escapeHtml(metaText)}</div>
      </div>
    `;
    const historyButton = document.createElement("button");
    historyButton.type = "button";
    historyButton.className = "history-button";
    historyButton.textContent = "History";
    historyButton.addEventListener("click", () => openHistory(space));
    row.append(historyButton);
    statusList.append(row);
  }
}

async function openHistory(space) {
  historyTitle.textContent = `${space.label} — History`;
  historyList.textContent = "Loading…";
  historyDialog.showModal();
  try {
    const payload = await fetchJson(`/api/spaces/${space.id}/intervals?limit=50`);
    renderHistoryList(payload.intervals);
  } catch (error) {
    historyList.textContent = error.message;
  }
}

function renderHistoryList(intervals) {
  historyList.innerHTML = "";
  if (!intervals || intervals.length === 0) {
    historyList.textContent = "No history yet — run detection to populate.";
    return;
  }
  for (const interval of intervals) {
    const badgeClass = interval.occupied ? "badge-occupied" : "badge-vacant";
    const badgeText = interval.occupied ? "Occupied" : "Vacant";
    const endText = interval.is_current ? "now" : formatTimestamp(interval.end_captured_at);
    const durationSeconds = interval.is_current
      ? Math.max(0, (Date.now() - Date.parse(interval.start_captured_at)) / 1000)
      : interval.duration_seconds;
    const currentTag = interval.is_current ? '<span class="current-tag">current</span>' : "";

    const row = document.createElement("div");
    row.className = "history-row";
    row.innerHTML = `
      <span class="status-badge ${badgeClass}">${badgeText}</span>
      <span class="history-range">
        ${escapeHtml(formatTimestamp(interval.start_captured_at))} → ${escapeHtml(endText)} ${currentTag}
      </span>
      <span class="row-meta">${formatDuration(durationSeconds)}</span>
    `;
    historyList.append(row);
  }
}

closeHistoryButton.addEventListener("click", () => historyDialog.close());

statusSortSelect.addEventListener("change", () => {
  state.statusSort = statusSortSelect.value;
  renderStatusList();
});

setInterval(() => {
  if (!historyDialog.open) {
    renderStatusList();
  }
}, 30000);

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

async function saveDraftSpace(label) {
  const payload = await fetchJson("/api/spaces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      camera_id: state.cameraId,
      label,
      polygon: state.draftPoints,
    }),
  });
  state.spaces.push(payload.space);
  state.draftPoints = [];
  state.mode = "idle";
  renderSpaces();
  renderStatusList();
  updateDraftControls();
  draw();
  setStatus(`Saved ${payload.space.label}`);
}

async function deleteSpace(spaceId) {
  await fetchJson(`/api/spaces/${spaceId}`, { method: "DELETE" });
  state.spaces = state.spaces.filter((space) => space.id !== spaceId);
  if (state.hoveredSpaceId === spaceId) {
    state.hoveredSpaceId = null;
  }
  if (state.selectedSpaceId === spaceId) {
    state.selectedSpaceId = null;
  }
  renderSpaces();
  renderStatusList();
  draw();
  setStatus("Space deleted");
}

async function saveSpacePolygon(spaceId, polygon) {
  try {
    const payload = await fetchJson(`/api/spaces/${spaceId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polygon }),
    });
    const index = state.spaces.findIndex((space) => space.id === spaceId);
    if (index !== -1) {
      // Merge rather than replace: a polygon PATCH response doesn't carry
      // current_* occupancy fields (those only come from list_spaces), so
      // preserve whatever the Live Status panel already knew.
      state.spaces[index] = { ...state.spaces[index], ...payload.space };
    }
    renderStatusList();
    setStatus(`Updated ${payload.space.label}`);
  } catch (error) {
    setStatus(error.message);
    await loadCameraData();
  }
}

markSpaceButton.addEventListener("click", () => {
  if (!state.selectedImage) {
    setStatus("Select or upload an image first");
    return;
  }
  state.mode = state.mode === "marking" ? "idle" : "marking";
  state.draftPoints = [];
  state.hoveredDetectionId = null;
  state.selectedSpaceId = null;
  updateDraftControls();
  draw();
  setStatus(state.mode === "marking" ? "Click four corners of a space" : "");
});

undoPointButton.addEventListener("click", () => {
  state.draftPoints.pop();
  updateDraftControls();
  draw();
});

clearDraftButton.addEventListener("click", () => {
  state.draftPoints = [];
  updateDraftControls();
  draw();
});

canvas.addEventListener("click", (event) => {
  if (state.suppressNextClick) {
    state.suppressNextClick = false;
    return;
  }
  if (state.mode === "marking") {
    if (!state.imageElement || state.draftPoints.length >= 4) {
      return;
    }
    state.draftPoints.push(canvasPoint(event));
    updateDraftControls();
    draw();
    if (state.draftPoints.length === 4) {
      spaceLabel.value = `Space ${state.spaces.length + 1}`;
      dialog.showModal();
      spaceLabel.focus();
      spaceLabel.select();
    }
    return;
  }

  if (!state.imageElement) {
    return;
  }
  const point = canvasPoint(event);
  const clickedSpace = spaceAtPoint(point);
  const nextSelectedId = clickedSpace?.id || null;
  if (state.selectedSpaceId !== nextSelectedId) {
    state.selectedSpaceId = nextSelectedId;
    draw();
  }
});

canvas.addEventListener("mousedown", (event) => {
  if (!state.imageElement || state.mode === "marking" || !state.selectedSpaceId) {
    return;
  }
  const space = state.spaces.find((item) => item.id === state.selectedSpaceId);
  if (!space) {
    return;
  }
  const point = canvasPoint(event);
  const handleIndex = handleIndexAtPoint(space, point);
  if (handleIndex === -1) {
    return;
  }
  event.preventDefault();
  state.draggingHandle = { spaceId: space.id, pointIndex: handleIndex };
});

canvas.addEventListener("mousemove", (event) => {
  if (!state.imageElement) {
    return;
  }
  if (state.draggingHandle) {
    const space = state.spaces.find((item) => item.id === state.draggingHandle.spaceId);
    if (space) {
      const point = canvasPoint(event);
      const clampedPoint = {
        x: Math.min(Math.max(point.x, 0), canvas.width),
        y: Math.min(Math.max(point.y, 0), canvas.height),
      };
      space.polygon = space.polygon.map((existing, index) =>
        index === state.draggingHandle.pointIndex ? clampedPoint : existing,
      );
      draw();
    }
    return;
  }
  if (state.mode === "marking") {
    return;
  }
  const point = canvasPoint(event);
  const hoveredSpace = spaceAtPoint(point);
  const hoveredDetection = detectionAtPoint(point);
  const hoveredSpaceId = hoveredSpace?.id || null;
  const hoveredDetectionId = hoveredDetection?.id || null;
  if (
    state.hoveredSpaceId !== hoveredSpaceId ||
    state.hoveredDetectionId !== hoveredDetectionId
  ) {
    state.hoveredSpaceId = hoveredSpaceId;
    state.hoveredDetectionId = hoveredDetectionId;
    draw();
  }
  const selectedSpace = state.spaces.find((item) => item.id === state.selectedSpaceId);
  const onHandle = handleIndexAtPoint(selectedSpace, point) !== -1;
  canvas.style.cursor = onHandle
    ? "grab"
    : hoveredSpace || hoveredDetection
      ? "pointer"
      : "crosshair";
});

canvas.addEventListener("mouseup", async (event) => {
  if (!state.draggingHandle) {
    return;
  }
  const { spaceId } = state.draggingHandle;
  state.draggingHandle = null;
  state.suppressNextClick = true;
  const space = state.spaces.find((item) => item.id === spaceId);
  if (space) {
    await saveSpacePolygon(spaceId, space.polygon);
    draw();
  }
});

canvas.addEventListener("mouseleave", () => {
  if (state.hoveredSpaceId !== null || state.hoveredDetectionId !== null) {
    state.hoveredSpaceId = null;
    state.hoveredDetectionId = null;
    draw();
  }
  canvas.style.cursor = "default";
});

spaceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const label = spaceLabel.value.trim();
  if (label) {
    await saveDraftSpace(label);
  }
  dialog.close();
});

cancelSpaceButton.addEventListener("click", () => {
  state.draftPoints = [];
  updateDraftControls();
  draw();
  dialog.close();
});

cameraSelect.addEventListener("change", async () => {
  state.cameraId = cameraSelect.value;
  state.draftPoints = [];
  state.hoveredSpaceId = null;
  state.hoveredDetectionId = null;
  state.selectedSpaceId = null;
  state.mode = "idle";
  await loadCameraData();
  updateDraftControls();
});

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!uploadInput.files[0]) {
    return;
  }
  const body = new FormData();
  body.append("camera_id", state.cameraId);
  body.append("image", uploadInput.files[0]);
  const payload = await fetchJson("/api/images", { method: "POST", body });
  state.images.push(payload.image);
  uploadInput.value = "";
  await selectImage(payload.image);
});

function parseTimeToSeconds(value) {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  const parts = trimmed.split(":").map((part) => Number(part));
  if (parts.length === 0 || parts.some((part) => Number.isNaN(part) || part < 0)) {
    throw new Error(`Couldn't understand "${value}" — use seconds or mm:ss.`);
  }
  return parts.reduce((total, part) => total * 60 + part, 0);
}

videoUploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!videoUploadInput.files[0]) {
    return;
  }

  let endSeconds;
  try {
    endSeconds = parseTimeToSeconds(videoEndInput.value);
  } catch (error) {
    setStatus(error.message);
    return;
  }

  const interval = Number(videoIntervalInput.value) || 5;
  const body = new FormData();
  body.append("camera_id", state.cameraId);
  body.append("video", videoUploadInput.files[0]);
  body.append("interval_seconds", String(interval));
  if (endSeconds !== null) {
    body.append("end_seconds", String(endSeconds));
  }

  videoSubmitButton.disabled = true;
  setStatus("Extracting frames… this can take a while for longer clips.");
  try {
    const payload = await fetchJson("/api/videos", { method: "POST", body });
    setStatus(`Extracted ${payload.frames_extracted} frame(s) from ${payload.video}`);
    videoUploadInput.value = "";
    videoEndInput.value = "";
    await loadCameraData();
  } catch (error) {
    setStatus(error.message);
  } finally {
    videoSubmitButton.disabled = false;
  }
});

document.addEventListener("keydown", async (event) => {
  if (shouldIgnoreImageNavigation(event)) {
    return;
  }
  if (event.key === "ArrowDown") {
    event.preventDefault();
    await navigateImages(1);
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    await navigateImages(-1);
  }
  if (event.key.toLowerCase() === "m") {
    event.preventDefault();
    markSpaceButton.click();
  }
});

loadInitialData().catch((error) => {
  console.error(error);
  setStatus(error.message);
});
