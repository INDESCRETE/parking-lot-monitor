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
};

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
const dialog = document.querySelector("#spaceDialog");
const spaceForm = document.querySelector("#spaceForm");
const spaceLabel = document.querySelector("#spaceLabel");
const cancelSpaceButton = document.querySelector("#cancelSpaceButton");

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
  if (dialog.open) {
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
    drawPolygon(space, space.id === state.hoveredSpaceId);
  }
  for (const detection of state.observations.detections) {
    drawDetection(detection, detection.id === state.hoveredDetectionId);
  }
  if (state.draftPoints.length > 0) {
    drawDraft();
  }
}

function drawPolygon(space, isHovered) {
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

  if (!isHovered) {
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
  renderSpaces();
  draw();
  setStatus("Space deleted");
}

markSpaceButton.addEventListener("click", () => {
  if (!state.selectedImage) {
    setStatus("Select or upload an image first");
    return;
  }
  state.mode = state.mode === "marking" ? "idle" : "marking";
  state.draftPoints = [];
  state.hoveredDetectionId = null;
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
  if (state.mode !== "marking" || !state.imageElement) {
    return;
  }
  if (state.draftPoints.length >= 4) {
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
});

canvas.addEventListener("mousemove", (event) => {
  if (!state.imageElement || state.mode === "marking") {
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
  canvas.style.cursor = hoveredSpace || hoveredDetection ? "pointer" : "crosshair";
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
});

loadInitialData().catch((error) => {
  console.error(error);
  setStatus(error.message);
});
