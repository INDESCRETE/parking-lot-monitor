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
  detectionPollTimer: null,
  liveCameraId: null,
  liveMode: "stream",
  liveStatusTimer: null,
  liveSnapshotTimer: null,
  statusPollTimer: null,
  liveFeedActive: false,
  liveFrameSource: false,
  liveFeedTimer: null,
  liveFeedIntervalSeconds: 3,
  liveFeedConfigured: false,
  notice: null,
  noCamera: false,
};

const HANDLE_RADIUS = 9;

const cameraSelect = document.querySelector("#cameraSelect");
const imageList = document.querySelector("#imageList");
const spaceList = document.querySelector("#spaceList");
const canvas = document.querySelector("#annotationCanvas");
const ctx = canvas.getContext("2d");
const emptyState = document.querySelector("#emptyState");
const emptyStatePath = document.querySelector("#emptyStatePath");
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
const addCameraButton = document.querySelector("#addCameraButton");
const cameraDialog = document.querySelector("#cameraDialog");
const cameraForm = document.querySelector("#cameraForm");
const cameraNameInput = document.querySelector("#cameraNameInput");
const cancelCameraButton = document.querySelector("#cancelCameraButton");
const runDetectionButton = document.querySelector("#runDetectionButton");
const detectionStatus = document.querySelector("#detectionStatus");
const minOccupiedInput = document.querySelector("#minOccupiedInput");
const minOccupiedStatus = document.querySelector("#minOccupiedStatus");
const liveViewButton = document.querySelector("#liveViewButton");
const liveFeedButton = document.querySelector("#liveFeedButton");
const liveFeedIndicator = document.querySelector("#liveFeedIndicator");
const liveDialog = document.querySelector("#liveDialog");
const liveTitle = document.querySelector("#liveTitle");
const liveImage = document.querySelector("#liveImage");
const liveMessage = document.querySelector("#liveMessage");
const liveStatusLine = document.querySelector("#liveStatusLine");
const closeLiveButton = document.querySelector("#closeLiveButton");
const deleteCameraButton = document.querySelector("#deleteCameraButton");
const emptyStateDefault = document.querySelector("#emptyStateDefault");
const emptyStateNoCamera = document.querySelector("#emptyStateNoCamera");
const confirmDialog = document.querySelector("#confirmDialog");
const confirmTitle = document.querySelector("#confirmTitle");
const confirmMessage = document.querySelector("#confirmMessage");
const confirmDetail = document.querySelector("#confirmDetail");
const confirmOkButton = document.querySelector("#confirmOkButton");
const confirmCancelButton = document.querySelector("#confirmCancelButton");

const LIVE_STATUS_POLL_MS = 1000;
const LIVE_SNAPSHOT_POLL_MS = 3000;
// How often the Live Status list re-checks the server for occupied/vacant
// changes while a camera is selected -- mainly for the continuous live
// detector, which updates a space's status in the background on its own
// schedule with nobody clicking anything.
const STATUS_POLL_MS = 5000;
// 1x1 transparent GIF. Pointing the <img> at this reliably cancels an open
// live video stream (which is what tells the server to stop ffmpeg).
const BLANK_IMAGE = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

function setStatus(message) {
  statusEl.textContent = message;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed: ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

const LAST_CAMERA_STORAGE_KEY = "parkingLotMonitor.lastCameraId";

// Remembers the selected camera across page reloads. Wrapped in try/catch:
// private browsing or a locked-down browser can make localStorage throw,
// and losing the remembered camera is a minor inconvenience, not worth
// breaking the page over.
function rememberCameraId(cameraId) {
  try {
    if (cameraId) {
      localStorage.setItem(LAST_CAMERA_STORAGE_KEY, cameraId);
    } else {
      localStorage.removeItem(LAST_CAMERA_STORAGE_KEY);
    }
  } catch (error) {
    console.warn("Couldn't remember the selected camera:", error);
  }
}

function getRememberedCameraId() {
  try {
    return localStorage.getItem(LAST_CAMERA_STORAGE_KEY);
  } catch (error) {
    return null;
  }
}

async function loadInitialData() {
  const payload = await fetchJson("/api/cameras");
  state.cameras = payload.cameras;
  const remembered = getRememberedCameraId();
  state.cameraId = state.cameras.some((camera) => camera.id === remembered)
    ? remembered
    : state.cameras[0]?.id || null;
  rememberCameraId(state.cameraId); // keep storage in sync if the remembered one was gone
  renderCameras();
  await loadCameraData();
  if (state.cameraId) {
    checkDetectionStatusOnce(state.cameraId);
  }
  refreshLiveAvailability();
}

// With no camera selected (e.g. the last one was just deleted) the page turns
// off everything that needs a camera instead of talking to one that isn't there.
function setCameraControlsEnabled(hasCamera) {
  // Only act when this actually changes, so ordinary reloads never re-enable a
  // button that something else (a running detection or import) turned off.
  if (hasCamera === !state.noCamera) {
    return;
  }
  state.noCamera = !hasCamera;
  for (const control of [
    deleteCameraButton,
    uploadInput,
    uploadForm.querySelector("button"),
    videoUploadInput,
    videoIntervalInput,
    videoEndInput,
    videoSubmitButton,
    runDetectionButton,
    markSpaceButton,
    minOccupiedInput,
  ]) {
    control.disabled = !hasCamera;
  }
  cameraSelect.disabled = !hasCamera;
}

async function loadCameraData() {
  if (!state.cameraId) {
    stopStatusPolling();
    exitLiveFrame();
    setCameraControlsEnabled(false);
    configLink.removeAttribute("href");
    renderMinOccupiedInput();
    state.images = [];
    state.spaces = [];
    state.selectedImage = null;
    renderImages();
    renderSpaces();
    renderStatusList();
    await loadSelectedImage();
    return;
  }
  setCameraControlsEnabled(true);
  configLink.href = `/api/config/${state.cameraId}`;
  renderMinOccupiedInput();
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
  startStatusPolling();
}

// Shows the currently-selected camera's own minimum-parked-time override (or
// blank, meaning "use the script's own default" -- the placeholder shows
// what that default is so it's never a mystery why the box looks empty).
function renderMinOccupiedInput() {
  minOccupiedStatus.textContent = "";
  const camera = state.cameras.find((c) => c.id === state.cameraId);
  const value = camera ? camera.min_occupied_seconds : null;
  minOccupiedInput.value = value === null || value === undefined ? "" : value;
}

function renderDetectionStatus(status) {
  detectionStatus.classList.remove("detecting", "detection-error");
  if (!status || status.status === "idle") {
    detectionStatus.textContent = "";
    return;
  }
  if (status.status === "error" && state.images.length === 0) {
    // Not actionable any more (see renderImages -- Run Detection is now
    // disabled for a camera with no images), so don't leave it on screen.
    detectionStatus.textContent = "";
    return;
  }
  if (status.status === "running") {
    const progress = status.progress;
    if (status.phase === "finalizing") {
      // All images are detected (progress would just be sitting at a stale
      // 100% here); the script is now rebuilding each space's occupied/
      // vacant timeline from the results, which has no per-item progress of
      // its own but can still take a few real seconds for a lot with many
      // spaces. Said explicitly so this doesn't read as finished or stuck.
      detectionStatus.textContent = "Finalizing (rebuilding occupancy timelines)…";
    } else if (progress && progress.total > 0) {
      const pct = Math.round((progress.done / progress.total) * 100);
      detectionStatus.textContent = `Detecting vehicles… ${pct}% (${progress.done}/${progress.total})`;
    } else {
      // No progress line seen yet -- e.g. the model is still loading, or
      // the very first image hasn't finished. Falls back to a plain
      // spinner-style message rather than showing "0%" and looking stuck.
      detectionStatus.textContent = "Detecting vehicles…";
    }
    detectionStatus.classList.add("detecting");
    return;
  }
  detectionStatus.textContent = `Detection failed: ${status.error || "unknown error"}`;
  detectionStatus.classList.add("detection-error");
}

function stopDetectionPolling() {
  if (state.detectionPollTimer) {
    clearTimeout(state.detectionPollTimer);
    state.detectionPollTimer = null;
  }
}

function stopStatusPolling() {
  if (state.statusPollTimer) {
    clearInterval(state.statusPollTimer);
    state.statusPollTimer = null;
  }
}

// Keeps the Live Status list (and the space list) in sync with the database
// without a manual page reload. Mainly for the continuous live detector,
// which changes a space's status in the background on its own schedule --
// but harmless (just a no-op re-render) for a camera that's only ever
// updated by a manual Run Detection.
function startStatusPolling() {
  stopStatusPolling();
  const cameraId = state.cameraId;
  if (!cameraId) {
    return;
  }
  state.statusPollTimer = setInterval(async () => {
    // Don't fetch while the user is mid-edit (drawing a new space or
    // dragging a handle) or looking at a space's history dialog -- a
    // fetch landing right then would either yank a polygon out from
    // under an active drag, or just be wasted work while the dialog
    // covers the list anyway.
    if (state.mode !== "idle" || state.draggingHandle || historyDialog.open) {
      return;
    }
    try {
      const payload = await fetchJson(`/api/spaces?camera_id=${encodeURIComponent(cameraId)}`);
      if (state.cameraId !== cameraId) {
        return; // switched cameras while the request was in flight
      }
      state.spaces = payload.spaces;
      renderSpaces();
      renderStatusList();
      if (state.liveFeedActive) {
        draw(); // refresh space colors/duration badges even between frame refreshes
      }
    } catch (error) {
      console.warn(error);
    }
  }, STATUS_POLL_MS);
}

// Polls a camera's background detection run to completion, then reloads its
// spaces/images/observations automatically -- the whole point being that
// nobody has to manually refresh the page (or run anything themselves) to
// see results once a video/image upload or the Run Detection button has
// kicked a run off server-side.
// 1s rather than a slower interval so a short run's progress (or the
// finalizing phase) actually has a chance to show up at least once before
// the run finishes, instead of jumping straight from "no data yet" to done.
const DETECTION_POLL_INTERVAL_MS = 1000;

async function pollDetectionUntilDone(cameraId) {
  stopDetectionPolling();
  const tick = async () => {
    let status;
    try {
      status = await fetchJson(`/api/cameras/${encodeURIComponent(cameraId)}/detection-status`);
    } catch (error) {
      state.detectionPollTimer = setTimeout(tick, DETECTION_POLL_INTERVAL_MS);
      return;
    }
    const viewingThisCamera = state.cameraId === cameraId;
    if (viewingThisCamera) {
      renderDetectionStatus(status);
    }
    if (status.status === "running") {
      state.detectionPollTimer = setTimeout(tick, DETECTION_POLL_INTERVAL_MS);
      return;
    }
    state.detectionPollTimer = null;
    if (viewingThisCamera) {
      await loadCameraData();
      renderDetectionStatus(status);
    }
  };
  await tick();
}

// Used on page load and whenever the camera picker changes: shows whatever
// is already true for that camera (e.g. a run kicked off before the page
// was last refreshed) without assuming a run just started.
async function checkDetectionStatusOnce(cameraId) {
  if (!cameraId) {
    return;
  }
  let status;
  try {
    status = await fetchJson(`/api/cameras/${encodeURIComponent(cameraId)}/detection-status`);
  } catch (error) {
    return;
  }
  if (state.cameraId !== cameraId) {
    return;
  }
  renderDetectionStatus(status);
  if (status.status === "running") {
    await pollDetectionUntilDone(cameraId);
  }
}

function renderCameras() {
  cameraSelect.innerHTML = "";
  if (state.cameras.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No cameras yet";
    cameraSelect.append(option);
    return;
  }
  for (const camera of state.cameras) {
    const option = document.createElement("option");
    option.value = camera.id;
    option.textContent = camera.name;
    cameraSelect.append(option);
  }
  cameraSelect.value = state.cameraId;
}

async function createCamera(name) {
  const payload = await fetchJson("/api/cameras", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const camerasPayload = await fetchJson("/api/cameras");
  state.cameras = camerasPayload.cameras;
  state.cameraId = payload.camera.id;
  rememberCameraId(state.cameraId);
  renderCameras();
  await loadCameraData();
  renderDetectionStatus(null);
  setStatus(`Created camera "${payload.camera.name}" — its own separate images and spaces.`);
}

function renderImages() {
  runDetectionButton.disabled = state.noCamera || state.images.length === 0;
  runDetectionButton.title = state.images.length === 0
    ? "No stored images to run batch detection on -- upload/import images, or check the live feed if this camera has one"
    : "";
  imageList.innerHTML = "";
  if (state.images.length === 0) {
    imageList.textContent = "No images yet.";
    return;
  }
  for (const image of state.images) {
    const item = document.createElement("div");
    item.className = "image-item";
    const row = document.createElement("button");
    row.type = "button";
    row.className = `image-row ${state.selectedImage?.id === image.id ? "active" : ""}`;
    row.innerHTML = `
      <span class="row-title">${escapeHtml(image.filename)}</span>
      <span class="row-meta">${escapeHtml(image.captured_at)}</span>
    `;
    row.addEventListener("click", () => selectImage(image));
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "delete-image";
    deleteButton.textContent = "\u00d7";
    deleteButton.title = `Delete ${image.filename}`;
    deleteButton.setAttribute("aria-label", `Delete ${image.filename}`);
    deleteButton.addEventListener("click", () => deleteImage(image));
    item.append(row, deleteButton);
    imageList.append(item);
  }
}

async function selectImage(image, options = {}) {
  if (!image || state.selectedImage?.id === image.id) {
    return;
  }
  exitLiveFrame(); // picking a specific photo means editing it, not watching the live feed
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
  if (dialog.open || historyDialog.open || cameraDialog.open || confirmDialog.open) {
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
    emptyStatePath.textContent = `data/images/${state.cameraId}`;
    emptyStateDefault.hidden = !state.cameraId;
    emptyStateNoCamera.hidden = Boolean(state.cameraId);
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
    // A one-time "Deleted ..." message wins over the usual summary, once.
    setStatus(
      state.notice ||
        (observedCount > 0
          ? `${occupiedCount}/${observedCount} observed occupied`
          : `${state.spaces.length} spaces configured`),
    );
    state.notice = null;
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
  // While the main canvas is showing the live camera feed, colour spaces by
  // their live current_occupied/current_since fields (kept fresh by
  // startStatusPolling) instead of the batch detection results tied to a
  // specific stored image -- there is no "selected image" in live mode.
  if (state.liveFrameSource) {
    const space = state.spaces.find((item) => item.id === spaceId);
    if (!space || space.current_occupied === null || space.current_occupied === undefined) {
      return null;
    }
    return {
      occupied: space.current_occupied,
      since: space.current_since,
      duration_seconds: currentElapsedSeconds(space),
    };
  }
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
  // intervals arrives newest-first. A gap between one interval's start and
  // the next-older interval's end means nothing was actually observed in
  // between (server restart, camera/network hiccup, etc.) -- the live
  // detector deliberately doesn't bridge that time (see app/live_detection.py),
  // so it shows up as two back-to-back records that can have the *same*
  // status, looking like a change that never really happened. Flag it
  // explicitly instead of leaving it looking like an unexplained status flip.
  for (let i = 0; i < intervals.length; i++) {
    const interval = intervals[i];
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

    const older = intervals[i + 1];
    if (older) {
      const gapSeconds = (Date.parse(interval.start_captured_at) - Date.parse(older.end_captured_at)) / 1000;
      if (gapSeconds > 1) {
        const gapRow = document.createElement("div");
        gapRow.className = "history-gap";
        gapRow.textContent = `no data for ${formatDuration(gapSeconds)} — monitoring was interrupted`;
        historyList.append(gapRow);
      }
    }
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
      // The actual pixel size of the photo these points were drawn on --
      // lets the server correctly rescale this space if it's ever checked
      // against a differently-sized photo later (e.g. the live camera's
      // full-resolution snapshots vs. a smaller reference photo).
      reference_width: state.imageElement?.naturalWidth,
      reference_height: state.imageElement?.naturalHeight,
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
      body: JSON.stringify({
        polygon,
        reference_width: state.imageElement?.naturalWidth,
        reference_height: state.imageElement?.naturalHeight,
      }),
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
  if (!state.imageElement) {
    setStatus("Select or upload an image first");
    return;
  }
  if (state.mode !== "marking" && state.liveFeedActive) {
    // Freeze on the current live frame so it doesn't refresh out from under
    // the four corner-clicks -- "Back to Live Feed" resumes it afterward.
    stopLiveFeed();
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
  if (state.liveFeedActive) {
    // Same reasoning as Mark Space above -- don't let the photo shift under
    // a corner mid-drag.
    stopLiveFeed();
  }
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

addCameraButton.addEventListener("click", () => {
  cameraNameInput.value = "";
  cameraDialog.showModal();
  cameraNameInput.focus();
});

cameraForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = cameraNameInput.value.trim();
  cameraDialog.close();
  if (!name) {
    return;
  }
  try {
    await createCamera(name);
  } catch (error) {
    setStatus(error.message);
  }
});

cancelCameraButton.addEventListener("click", () => {
  cameraDialog.close();
});

cameraSelect.addEventListener("change", async () => {
  stopDetectionPolling();
  exitLiveFrame();
  state.cameraId = cameraSelect.value;
  rememberCameraId(state.cameraId);
  state.draftPoints = [];
  state.hoveredSpaceId = null;
  state.hoveredDetectionId = null;
  state.selectedSpaceId = null;
  state.mode = "idle";
  await loadCameraData();
  updateDraftControls();
  checkDetectionStatusOnce(state.cameraId);
  refreshLiveAvailability();
});

minOccupiedInput.addEventListener("change", async () => {
  const raw = minOccupiedInput.value.trim();
  let minOccupiedSeconds = null; // blank -> clear override, fall back to script default
  if (raw !== "") {
    const parsed = Number(raw);
    if (Number.isNaN(parsed) || parsed < 0) {
      minOccupiedStatus.textContent = "Invalid";
      renderMinOccupiedInput();
      return;
    }
    minOccupiedSeconds = parsed;
  }
  try {
    const payload = await fetchJson(`/api/cameras/${encodeURIComponent(state.cameraId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ min_occupied_seconds: minOccupiedSeconds }),
    });
    const index = state.cameras.findIndex((c) => c.id === state.cameraId);
    if (index !== -1) {
      state.cameras[index] = payload.camera;
    }
    minOccupiedStatus.textContent = "Saved";
    setTimeout(() => {
      if (minOccupiedStatus.textContent === "Saved") {
        minOccupiedStatus.textContent = "";
      }
    }, 2000);
  } catch (error) {
    minOccupiedStatus.textContent = "";
    setStatus(error.message);
  }
});

// ---- Live view -----------------------------------------------------------
// Shows the camera's live video (a ~2 fps stream relayed by the server, which
// only runs while this window is open) with a status line about the photo
// grabber that feeds detection. If the stream can't start, it falls back to
// the newest grabbed photo, refreshed every few seconds.

// The button shows when this camera has live view set up -- or when the
// camera settings file is broken, so the problem is visible instead of the
// button silently missing.
// --- Main-canvas live feed -------------------------------------------------
// For a camera with live view set up, the main workspace shows the newest
// live photo (refreshed at the same interval the live detector checks
// frames) with spaces colored by their current occupied/vacant status,
// instead of requiring a stored image to be selected. Picking an image from
// the Images list (selectImage) exits this mode to edit that photo; the
// "Back to Live Feed" button re-enters it.

function updateLiveFeedControls() {
  liveFeedButton.hidden = !state.liveFeedConfigured || state.liveFeedActive;
  liveFeedIndicator.hidden = !state.liveFeedActive;
}

function exitLiveFrame() {
  stopLiveFeed();
  state.liveFrameSource = false;
}

function stopLiveFeed() {
  if (state.liveFeedTimer) {
    clearInterval(state.liveFeedTimer);
    state.liveFeedTimer = null;
  }
  if (state.liveFeedActive) {
    state.liveFeedActive = false;
    updateLiveFeedControls();
  }
}

function refreshLiveFeedImage() {
  if (!state.liveFeedActive) {
    return;
  }
  const cameraId = state.cameraId;
  const img = new Image();
  img.onload = () => {
    if (!state.liveFeedActive || state.cameraId !== cameraId) {
      return; // stopped or switched cameras while this photo was loading
    }
    state.imageElement = img;
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.style.display = "block";
    emptyState.style.display = "none";
    draw();
  };
  // On a load failure (e.g. no frame grabbed yet) just leave the last good
  // frame on screen -- the next tick tries again.
  img.src = `/api/cameras/${encodeURIComponent(cameraId)}/latest.jpg?t=${Date.now()}`;
}

function enterLiveFeed() {
  const cameraId = state.cameraId;
  if (!cameraId || !state.liveFeedConfigured) {
    return;
  }
  stopLiveFeed();
  state.liveFeedActive = true;
  state.liveFrameSource = true;
  state.selectedImage = null;
  state.mode = "idle";
  state.draftPoints = [];
  state.hoveredSpaceId = null;
  state.hoveredDetectionId = null;
  state.selectedSpaceId = null;
  state.observations = { detections: [], occupancy: [] };
  renderImages();
  updateDraftControls();
  updateLiveFeedControls();
  refreshLiveFeedImage();
  const intervalMs = Math.max(1, state.liveFeedIntervalSeconds) * 1000;
  state.liveFeedTimer = setInterval(refreshLiveFeedImage, intervalMs);
  setStatus("Showing the live camera feed");
}

async function refreshLiveAvailability() {
  const cameraId = state.cameraId;
  if (!cameraId) {
    liveViewButton.hidden = true;
    state.liveFeedConfigured = false;
    exitLiveFrame();
    updateLiveFeedControls();
    return;
  }
  try {
    const status = await fetchJson(`/api/cameras/${encodeURIComponent(cameraId)}/live-status`);
    if (cameraId !== state.cameraId) return; // switched cameras mid-request
    liveViewButton.hidden = !(status.configured || status.config_error);
    state.liveFeedConfigured = Boolean(status.configured);
    state.liveFeedIntervalSeconds = status.interval_seconds || 3;
    if (state.liveFeedConfigured) {
      enterLiveFeed();
    } else {
      exitLiveFrame();
      updateLiveFeedControls();
    }
  } catch (error) {
    liveViewButton.hidden = true;
    state.liveFeedConfigured = false;
    exitLiveFrame();
    updateLiveFeedControls();
  }
}

liveFeedButton.addEventListener("click", enterLiveFeed);

function secondsSince(isoString) {
  const then = Date.parse(isoString);
  return Number.isNaN(then) ? null : Math.max(0, Math.round((Date.now() - then) / 1000));
}

function renderLiveStatus(status) {
  liveStatusLine.classList.remove("live-error");
  if (!status.configured) {
    liveStatusLine.textContent = status.config_error
      ? `Camera setup problem: ${status.config_error}`
      : "Live view isn't set up for this camera yet.";
    liveStatusLine.classList.add("live-error");
    return;
  }
  if (status.last_error) {
    liveStatusLine.textContent = `Can't get a photo from the camera: ${status.last_error}`;
    liveStatusLine.classList.add("live-error");
  } else if (status.last_frame_at) {
    const ago = secondsSince(status.last_frame_at);
    liveStatusLine.textContent =
      `Camera connected · newest photo ${ago === null ? "just now" : `${ago}s ago`}` +
      ` · a new one every ${status.interval_seconds}s`;
  } else {
    liveStatusLine.textContent = "Connecting to the camera…";
  }
  if (state.liveMode === "snapshot") {
    liveMessage.replaceChildren();
    const headline = document.createElement("div");
    headline.textContent = "Live video isn't available right now, so this shows the newest photo instead (it updates every few seconds).";
    liveMessage.append(headline);
    if (status.stream_error) {
      const detail = document.createElement("div");
      detail.className = "live-message-detail";
      detail.textContent = status.stream_error;
      liveMessage.append(detail);
    }
    liveMessage.hidden = false;
  }
}

async function pollLiveStatus() {
  const cameraId = state.liveCameraId;
  try {
    const status = await fetchJson(`/api/cameras/${encodeURIComponent(cameraId)}/live-status`);
    if (!liveDialog.open || cameraId !== state.liveCameraId) return;
    renderLiveStatus(status);
    if (!status.configured) {
      stopLiveMedia();
      liveImage.hidden = true;
    }
  } catch (error) {
    if (liveDialog.open) {
      liveStatusLine.textContent = "Can't reach the app server.";
      liveStatusLine.classList.add("live-error");
    }
  }
}

function stopLiveMedia() {
  clearInterval(state.liveSnapshotTimer);
  state.liveSnapshotTimer = null;
  liveImage.onerror = null;
  liveImage.src = BLANK_IMAGE; // drops the video connection so the server stops ffmpeg
}

function stopLive() {
  clearInterval(state.liveStatusTimer);
  state.liveStatusTimer = null;
  stopLiveMedia();
  liveImage.removeAttribute("src");
  state.liveCameraId = null;
}

function startSnapshotFallback(cameraId) {
  state.liveMode = "snapshot";
  const refresh = () => {
    if (!liveDialog.open || cameraId !== state.liveCameraId) return;
    liveImage.hidden = false;
    liveImage.src = `/api/cameras/${encodeURIComponent(cameraId)}/latest.jpg?t=${Date.now()}`;
  };
  liveImage.onerror = () => {
    liveImage.hidden = true; // no photo grabbed yet; the status line explains why
  };
  refresh();
  state.liveSnapshotTimer = setInterval(refresh, LIVE_SNAPSHOT_POLL_MS);
  pollLiveStatus();
}

function openLive() {
  const cameraId = state.cameraId;
  const camera = state.cameras.find((c) => c.id === cameraId);
  state.liveCameraId = cameraId;
  state.liveMode = "stream";
  liveTitle.textContent = `Live View · ${camera ? camera.name : cameraId}`;
  liveMessage.hidden = true;
  liveStatusLine.textContent = "Connecting to the camera…";
  liveStatusLine.classList.remove("live-error");
  liveImage.hidden = false;
  liveDialog.showModal();
  liveImage.onerror = () => {
    if (state.liveCameraId !== cameraId) return;
    startSnapshotFallback(cameraId);
  };
  liveImage.src = `/api/cameras/${encodeURIComponent(cameraId)}/live.mjpg?t=${Date.now()}`;
  pollLiveStatus();
  state.liveStatusTimer = setInterval(pollLiveStatus, LIVE_STATUS_POLL_MS);
}

liveViewButton.addEventListener("click", openLive);
closeLiveButton.addEventListener("click", () => liveDialog.close());
liveDialog.addEventListener("close", stopLive);
window.addEventListener("pagehide", stopLive);

// Recovers the live feed if a back/forward-cache restore (e.g. clicking
// Back after visiting the dashboard) didn't resume its timer on its own.
window.addEventListener("pageshow", (event) => {
  if (event.persisted && state.liveFeedConfigured && !state.liveFeedActive) {
    enterLiveFeed();
  }
});

runDetectionButton.addEventListener("click", async () => {
  runDetectionButton.disabled = true;
  try {
    const status = await fetchJson(`/api/cameras/${encodeURIComponent(state.cameraId)}/detect`, {
      method: "POST",
    });
    renderDetectionStatus(status);
    await pollDetectionUntilDone(state.cameraId);
  } catch (error) {
    setStatus(error.message);
  } finally {
    runDetectionButton.disabled = false;
  }
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
  if (payload.detection_triggered) {
    pollDetectionUntilDone(payload.image.camera_id);
  }
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
    if (payload.detection_triggered) {
      pollDetectionUntilDone(payload.camera_id);
    }
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

// ---- Deleting images and cameras ----

// Shows a Cancel / Delete box and resolves true only if Delete is clicked.
// Cancel, Esc, or clicking away all mean "no".
let confirmResolve = null;

function confirmAction({ title, message, detail = "", confirmLabel = "Delete" }) {
  if (confirmResolve) {
    finishConfirm(false);
  }
  return new Promise((resolve) => {
    confirmTitle.textContent = title;
    confirmMessage.textContent = message;
    confirmDetail.textContent = detail;
    confirmDetail.hidden = !detail;
    confirmOkButton.textContent = confirmLabel;
    confirmResolve = resolve;
    confirmDialog.showModal();
    confirmCancelButton.focus(); // so a stray Enter cancels rather than deletes
  });
}

function finishConfirm(answer) {
  const resolve = confirmResolve;
  confirmResolve = null;
  if (confirmDialog.open) {
    confirmDialog.close();
  }
  if (resolve) {
    resolve(answer);
  }
}

confirmOkButton.addEventListener("click", () => finishConfirm(true));
confirmCancelButton.addEventListener("click", () => finishConfirm(false));
confirmDialog.addEventListener("close", () => finishConfirm(false));
confirmDialog.addEventListener("click", (event) => {
  if (event.target === confirmDialog) {
    finishConfirm(false);
  }
});

// A message that should survive the "N/M observed occupied" line the next
// image load would otherwise write over it. Only kept if an image is about
// to load; otherwise it would show up stale on some later image.
function setNotice(message) {
  setStatus(message);
  state.notice = state.selectedImage ? message : null;
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

async function deleteImage(image) {
  const confirmed = await confirmAction({
    title: "Delete this image?",
    message: image.filename,
    detail:
      "The photo and its detection results will be removed, and the parking history will be recalculated without it. This can't be undone.",
  });
  if (!confirmed) {
    return;
  }
  const cameraId = state.cameraId;
  try {
    await fetchJson(`/api/images/${encodeURIComponent(image.id)}`, { method: "DELETE" });
  } catch (error) {
    setStatus(error.message);
    if (error.status === 404) {
      await loadCameraData(); // already gone -- just show what's really there
    }
    return;
  }
  if (state.cameraId !== cameraId) {
    return; // switched cameras while the request ran
  }
  const index = state.images.findIndex((item) => item.id === image.id);
  const wasSelected = state.selectedImage?.id === image.id;
  state.images = state.images.filter((item) => item.id !== image.id);
  if (wasSelected) {
    state.selectedImage = state.images[Math.min(Math.max(index, 0), state.images.length - 1)] || null;
    state.draftPoints = [];
    state.hoveredSpaceId = null;
    state.hoveredDetectionId = null;
    state.selectedSpaceId = null;
  }
  try {
    // Removing a photo can change what each space's history says.
    const spacesPayload = await fetchJson(`/api/spaces?camera_id=${encodeURIComponent(cameraId)}`);
    state.spaces = spacesPayload.spaces;
  } catch (error) {
    console.warn(error);
  }
  renderImages();
  renderSpaces();
  renderStatusList();
  updateDraftControls();
  if (wasSelected) {
    scrollSelectedImageIntoView();
    await loadSelectedImage();
  } else {
    draw();
  }
  setNotice(`Deleted ${image.filename}`);
}

async function deleteCurrentCamera() {
  const camera = state.cameras.find((item) => item.id === state.cameraId);
  if (!camera) {
    return;
  }
  const confirmed = await confirmAction({
    title: "Delete this camera?",
    message: camera.name,
    detail:
      `This permanently removes the camera, its ${plural(state.images.length, "image")}, ` +
      `its ${plural(state.spaces.length, "marked space")}, and all their detection history. ` +
      "Your other cameras are not affected. This can't be undone.",
    confirmLabel: "Delete camera",
  });
  if (!confirmed) {
    return;
  }
  stopDetectionPolling();
  let result;
  try {
    result = await fetchJson(`/api/cameras/${encodeURIComponent(camera.id)}`, { method: "DELETE" });
  } catch (error) {
    setStatus(error.message);
    if (error.status === 404) {
      // Already gone (deleted somewhere else): fall through and refresh.
      result = { images_deleted: 0, spaces_deleted: 0, warnings: [] };
    } else {
      // Detection may still be running for it; keep watching.
      checkDetectionStatusOnce(state.cameraId);
      return;
    }
  }
  const camerasPayload = await fetchJson("/api/cameras");
  state.cameras = camerasPayload.cameras;
  state.cameraId = state.cameras[0]?.id || null;
  rememberCameraId(state.cameraId);
  state.draftPoints = [];
  state.hoveredSpaceId = null;
  state.hoveredDetectionId = null;
  state.selectedSpaceId = null;
  state.mode = "idle";
  exitLiveFrame();
  renderCameras();
  await loadCameraData();
  renderDetectionStatus(null);
  updateDraftControls();
  if (state.cameraId) {
    checkDetectionStatusOnce(state.cameraId);
  }
  refreshLiveAvailability();
  let message =
    `Deleted "${camera.name}" (${plural(result.images_deleted, "image")}, ` +
    `${plural(result.spaces_deleted, "space")} removed).`;
  if (result.warnings && result.warnings.length > 0) {
    message += ` Heads up: ${result.warnings.join(" ")}`;
  }
  setNotice(message);
}

deleteCameraButton.addEventListener("click", () => {
  deleteCurrentCamera().catch((error) => setStatus(error.message));
});

loadInitialData().catch((error) => {
  console.error(error);
  setStatus(error.message);
});
