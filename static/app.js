const state = {
  dataset: null,
  images: [],
  filtered: [],
  current: null,
  imageData: null,
  selectedId: null,
  activeCategory: 1,
  hiddenCategories: new Set(),
  filter: "all",
  search: "",
  showBoxes: true,
  showMasks: true,
  drawMode: false,
  scale: 1,
  zoom: 1,
  panX: 0,
  panY: 0,
  panMode: false,
  spaceDown: false,
  maskCache: null,
  maskCacheKey: "",
  drag: null,
  loadToken: 0,
  nextNewId: 1,
  activeToolJob: null,
  inspectIslandCount: null,
  inspectInstances: [],
  inspectSelected: null,
  inspectPage: 0,
  inspectMaskStrokes: [],
  inspectComponents: [],
  inspectSelectedIslands: new Set(),
  inspectSplitMode: false,
  inspectSplitCategory: 1,
  inspectHistoryBefore: null,
  inspectEditMode: false,
  inspectEditOperation: "add",
  inspectBrushSize: 12,
  maskEdits: readStorage("coco-mask-edits", {}),
  inspectImageData: null,
  reviews: readStorage("coco-reviews", {}),
  edits: readStorage("coco-edits", {}),
  created: readStorage("coco-created", []),
};

const elements = {
  search: document.querySelector("#search"),
  resultCount: document.querySelector("#result-count"),
  selectionPosition: document.querySelector("#selection-position"),
  imageList: document.querySelector("#image-list"),
  categoryList: document.querySelector("#category-list"),
  activeCategory: document.querySelector("#active-category"),
  allCategories: document.querySelector("#all-categories"),
  imageName: document.querySelector("#image-name"),
  imageMeta: document.querySelector("#image-meta"),
  previous: document.querySelector("#previous"),
  next: document.querySelector("#next"),
  zoomOut: document.querySelector("#zoom-out"),
  zoomIn: document.querySelector("#zoom-in"),
  zoomReset: document.querySelector("#zoom-reset"),
  fitImage: document.querySelector("#fit-image"),
  panMode: document.querySelector("#pan-mode"),
  showBoxes: document.querySelector("#show-boxes"),
  showMasks: document.querySelector("#show-masks"),
  drawMode: document.querySelector("#draw-mode"),
  canvasArea: document.querySelector("#canvas-area"),
  stage: document.querySelector("#stage"),
  image: document.querySelector("#image"),
  overlay: document.querySelector("#overlay"),
  loading: document.querySelector("#loading"),
  objectPanel: document.querySelector("#object-panel"),
  objectList: document.querySelector("#object-list"),
  annotationStatus: document.querySelector("#annotation-status"),
  objectStatus: document.querySelector("#object-status"),
  saveState: document.querySelector("#save-state"),
  undo: document.querySelector("#undo"),
  redo: document.querySelector("#redo"),
  historySelect: document.querySelector("#history-select"),
  markKeep: document.querySelector("#mark-keep"),
  markFix: document.querySelector("#mark-fix"),
  markRemove: document.querySelector("#mark-remove"),
  clearReview: document.querySelector("#clear-review"),
  exportAll: document.querySelector("#export-all"),
  exportCurrent: document.querySelector("#export-current"),
  islandFrequencyTool: document.querySelector("#island-frequency-tool"),
  toolProgress: document.querySelector("#tool-progress"),
  toolProgressLabel: document.querySelector("#tool-progress-label"),
  toolProgressBar: document.querySelector("#tool-progress-bar"),
  toolProgressCount: document.querySelector("#tool-progress-count"),
  toolModal: document.querySelector("#tool-modal"),
  closeToolModal: document.querySelector("#close-tool-modal"),
  islandSummary: document.querySelector("#island-summary"),
  frequencyChart: document.querySelector("#frequency-chart"),
  frequencyTotal: document.querySelector("#frequency-total"),
  categoryFrequencyTable: document.querySelector("#category-frequency-table"),
  islandInspectionOptions: document.querySelector("#island-inspection-options"),
  inspectModal: document.querySelector("#inspect-modal"),
  closeInspectModal: document.querySelector("#close-inspect-modal"),
  inspectModalSubtitle: document.querySelector("#inspect-modal-subtitle"),
  inspectObjectList: document.querySelector("#inspect-object-list"),
  inspectInstanceCount: document.querySelector("#inspect-instance-count"),
  inspectLoading: document.querySelector("#inspect-loading"),
  inspectStage: document.querySelector("#inspect-stage"),
  inspectImage: document.querySelector("#inspect-image"),
  inspectOverlay: document.querySelector("#inspect-overlay"),
  inspectSelection: document.querySelector("#inspect-selection"),
  inspectEditToggle: document.querySelector("#inspect-edit-toggle"),
  inspectMaskAdd: document.querySelector("#inspect-mask-add"),
  inspectMaskRemove: document.querySelector("#inspect-mask-remove"),
  inspectBrushSize: document.querySelector("#inspect-brush-size"),
  inspectBrushValue: document.querySelector("#inspect-brush-value"),
  inspectMaskReset: document.querySelector("#inspect-mask-reset"),
  inspectSplitToggle: document.querySelector("#inspect-split-toggle"),
  inspectSplitCategory: document.querySelector("#inspect-split-category"),
  inspectSplitApply: document.querySelector("#inspect-split-apply"),
  inspectDeleteInstance: document.querySelector("#inspect-delete-instance"),
  inspectEditStatus: document.querySelector("#inspect-edit-status"),
  toast: document.querySelector("#toast"),
};

const toolRegistry = {
  islandFrequency: {
    label: "Island Frequency",
    run: runIslandFrequency,
  },
};

const context = elements.overlay.getContext("2d");
let toastTimer = null;
let saveTimer = null;
const inspectImageCache = new Map();

function readStorage(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value ? JSON.parse(value) : fallback;
  } catch {
    return fallback;
  }
}

function persist() {
  clearTimeout(saveTimer);
  elements.saveState.textContent = "Saving locally…";
  saveTimer = setTimeout(() => {
    try {
      localStorage.setItem("coco-reviews", JSON.stringify(state.reviews));
      localStorage.setItem("coco-edits", JSON.stringify(state.edits));
      localStorage.setItem("coco-created", JSON.stringify(state.created));
      localStorage.setItem("coco-mask-edits", JSON.stringify(state.maskEdits));
      elements.saveState.textContent = "Local edits enabled";
    } catch {
      elements.saveState.textContent = "Local storage full";
      showToast("Browser storage is full. Export before closing this tab.");
    }
  }, 200);
}

function captureHistoryState() {
  return {
    reviews: JSON.parse(JSON.stringify(state.reviews)),
    edits: JSON.parse(JSON.stringify(state.edits)),
    created: JSON.parse(JSON.stringify(state.created)),
    maskEdits: JSON.parse(JSON.stringify(state.maskEdits)),
    nextNewId: state.nextNewId,
  };
}

function applyHistoryState(snapshot) {
  state.reviews = JSON.parse(JSON.stringify(snapshot.reviews));
  state.edits = JSON.parse(JSON.stringify(snapshot.edits));
  state.created = JSON.parse(JSON.stringify(snapshot.created));
  state.maskEdits = JSON.parse(JSON.stringify(snapshot.maskEdits));
  state.nextNewId = snapshot.nextNewId;
  refreshCurrentImageState();
  persist();
  renderImageList();
  renderObjects();
  draw();
}

function historySignature(snapshot) {
  return JSON.stringify(snapshot);
}

const editHistory = [];
let historyIndex = -1;
const historyLimit = 100;

function initializeHistory() {
  editHistory.length = 0;
  editHistory.push({ label: "Session start", state: captureHistoryState() });
  historyIndex = 0;
  renderHistoryToolbar();
}

function commitHistory(label, before) {
  if (!before) return;
  const after = captureHistoryState();
  if (historySignature(before) === historySignature(after)) return;
  editHistory.splice(historyIndex + 1);
  editHistory.push({ label, state: after });
  if (editHistory.length > historyLimit) editHistory.shift();
  historyIndex = editHistory.length - 1;
  renderHistoryToolbar();
}

function restoreHistory(index) {
  if (index < 0 || index >= editHistory.length) return;
  historyIndex = index;
  applyHistoryState(editHistory[index].state);
  renderHistoryToolbar();
}

function undoHistory() {
  if (historyIndex <= 0) return;
  restoreHistory(historyIndex - 1);
  showToast(`Undo: ${editHistory[historyIndex + 1].label}`);
}

function redoHistory() {
  if (historyIndex >= editHistory.length - 1) return;
  restoreHistory(historyIndex + 1);
  showToast(`Redo: ${editHistory[historyIndex].label}`);
}

function renderHistoryToolbar() {
  elements.undo.disabled = historyIndex <= 0;
  elements.redo.disabled = historyIndex >= editHistory.length - 1;
  elements.historySelect.replaceChildren();
  editHistory.forEach((entry, index) => {
    const option = element("option", "", entry.label);
    option.value = String(index);
    option.disabled = index > historyIndex;
    option.selected = index === historyIndex;
    elements.historySelect.append(option);
  });
}

function refreshCurrentImageState() {
  if (!state.current || !state.imageData) return;
  const categoryCounts = {};
  let annotationCount = 0;
  for (const annotation of state.imageData.annotations) {
    if (annotation.image_id !== state.current.id || state.reviews[String(annotation.id)] === "remove") continue;
    const effective = effectiveAnnotation(annotation);
    annotationCount += 1;
    categoryCounts[String(effective.category_id)] = (categoryCounts[String(effective.category_id)] || 0) + 1;
  }
  for (const annotation of state.created.filter((item) => item.image_id === state.current.id)) {
    annotationCount += 1;
    categoryCounts[String(annotation.category_id)] = (categoryCounts[String(annotation.category_id)] || 0) + 1;
  }
  state.current.annotation_count = annotationCount;
  state.current.category_counts = categoryCounts;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => elements.toast.classList.remove("visible"), 3200);
}

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `Request failed: ${response.status}`);
  }
  return response;
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(value);
}

function updateToolProgress(label, job) {
  elements.toolProgress.hidden = false;
  elements.toolProgressLabel.textContent = job.workers ? `${label} · ${job.workers} workers` : label;
  elements.toolProgressBar.value = job.progress || 0;
  elements.toolProgressCount.textContent = `${formatNumber(job.processed || 0)} / ${formatNumber(job.total || 0)}`;
}

async function runIslandFrequency() {
  if (state.activeToolJob) {
    showToast("Island Frequency is already running");
    return;
  }
  const tool = toolRegistry.islandFrequency;
  elements.islandFrequencyTool.closest("details").open = false;
  elements.toolProgressLabel.textContent = tool.label;
  elements.toolProgressBar.value = 0;
  elements.toolProgressCount.textContent = "Starting…";
  elements.toolProgress.hidden = false;
  try {
    const response = await request("/api/tools/island-frequency/start", { method: "POST" });
    const { job_id: jobId } = await response.json();
    state.activeToolJob = jobId;
    while (state.activeToolJob === jobId) {
      const statusResponse = await request(`/api/tools/island-frequency/status?job_id=${encodeURIComponent(jobId)}`);
      const job = await statusResponse.json();
      updateToolProgress(tool.label, job);
      if (job.status === "completed") {
        renderIslandResults(job.result);
        elements.toolProgress.hidden = true;
        showToast("Island Frequency analysis complete");
        break;
      }
      if (job.status === "error") throw new Error(job.error || "Island Frequency analysis failed");
      await wait(600);
    }
  } catch (error) {
    elements.toolProgress.hidden = true;
    showToast(error.message);
  } finally {
    state.activeToolJob = null;
  }
}

function renderIslandResults(result) {
  const summary = result.summary;
  const cards = [
    ["Instances", summary.instances],
    ["Total islands", summary.total_islands],
    ["Mean / instance", summary.mean_islands],
    ["Median / instance", summary.median_islands],
    ["Mode", `${summary.mode_islands} islands`],
    ["Range", `${summary.minimum_islands}–${summary.maximum_islands}`],
    ["Single-island", summary.single_island_instances],
    ["Multi-island", summary.multi_island_instances],
  ];
  elements.islandSummary.replaceChildren();
  for (const [label, value] of cards) {
    const card = element("div", "summary-card");
    card.append(element("span", "", label), element("strong", "", formatNumber(value)));
    elements.islandSummary.append(card);
  }
  elements.frequencyTotal.textContent = `${formatNumber(summary.instances)} instances · ${summary.elapsed_seconds.toFixed(2)} seconds`;
  drawFrequencyChart(summary.distribution);
  renderCategoryFrequency(result.categories);
  renderInspectionOptions(result.inspection_groups || []);
  elements.toolModal.hidden = false;
}

function drawFrequencyChart(distribution) {
  const canvas = elements.frequencyChart;
  const chart = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  chart.clearRect(0, 0, width, height);
  let entries = distribution.map((item) => ({ label: String(item.islands), value: item.instances }));
  if (entries.length > 30) {
    const tail = entries.slice(24).reduce((sum, item) => sum + item.value, 0);
    entries = [...entries.slice(0, 24), { label: `${distribution[distribution.length - 1].islands}+`, value: tail }];
  }
  const margin = { top: 25, right: 24, bottom: 58, left: 70 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maximum = Math.max(1, ...entries.map((entry) => entry.value));
  chart.strokeStyle = "#2b3443";
  chart.lineWidth = 1;
  chart.font = "11px system-ui";
  chart.fillStyle = "#8f9bad";
  chart.textAlign = "right";
  for (let step = 0; step <= 4; step += 1) {
    const y = margin.top + plotHeight * step / 4;
    const value = Math.round(maximum * (1 - step / 4));
    chart.beginPath();
    chart.moveTo(margin.left, y);
    chart.lineTo(width - margin.right, y);
    chart.stroke();
    chart.fillText(formatNumber(value), margin.left - 10, y + 4);
  }
  const slotWidth = plotWidth / Math.max(1, entries.length);
  const barWidth = Math.max(3, slotWidth * 0.72);
  entries.forEach((entry, index) => {
    const barHeight = entry.value / maximum * plotHeight;
    const x = margin.left + index * slotWidth + (slotWidth - barWidth) / 2;
    const y = margin.top + plotHeight - barHeight;
    const gradient = chart.createLinearGradient(0, y, 0, margin.top + plotHeight);
    gradient.addColorStop(0, "#a78bfa");
    gradient.addColorStop(1, "#5b3df5");
    chart.fillStyle = gradient;
    chart.fillRect(x, y, barWidth, barHeight);
    if (entries.length <= 30 || index % 2 === 0 || index === entries.length - 1) {
      chart.save();
      chart.translate(x + barWidth / 2, margin.top + plotHeight + 12);
      chart.rotate(-0.45);
      chart.textAlign = "right";
      chart.fillStyle = "#aab4c2";
      chart.fillText(entry.label, 0, 0);
      chart.restore();
    }
  });
  chart.fillStyle = "#cbd3df";
  chart.textAlign = "center";
  chart.fillText("Islands per detection instance", margin.left + plotWidth / 2, height - 10);
}

function renderCategoryFrequency(categories) {
  elements.categoryFrequencyTable.replaceChildren();
  const header = element("div", "category-table-row header");
  header.append(element("span", "", "Category"), element("span", "", "Instances"), element("span", "", "Islands"), element("span", "", "Mean"), element("span", "", "Median"), element("span", "", "Multi"));
  elements.categoryFrequencyTable.append(header);
  for (const category of categories) {
    const row = element("div", "category-table-row");
    row.append(
      element("span", "", category.name),
      element("span", "", formatNumber(category.summary.instances)),
      element("span", "", formatNumber(category.summary.total_islands)),
      element("span", "", formatNumber(category.summary.mean_islands)),
      element("span", "", formatNumber(category.summary.median_islands)),
      element("span", "", formatNumber(category.summary.multi_island_instances)),
    );
    elements.categoryFrequencyTable.append(row);
  }
}

function renderInspectionOptions(groups) {
  elements.islandInspectionOptions.replaceChildren();
  for (const group of groups) {
    const row = element("div", "inspection-option");
    row.append(element("strong", "", `${group.islands} islands`), element("span", "", `${formatNumber(group.instances.length)} instances`));
    const button = element("button", "button secondary", "Inspect");
    button.type = "button";
    button.addEventListener("click", () => openIslandInspector(group.islands, group.instances));
    row.append(button);
    elements.islandInspectionOptions.append(row);
  }
}

function openIslandInspector(islandCount, instances) {
  state.inspectIslandCount = islandCount;
  state.inspectInstances = instances;
  state.inspectSelected = null;
  state.inspectMaskStrokes = [];
  state.inspectComponents = [];
  state.inspectSelectedIslands = new Set();
  state.inspectSplitMode = false;
  state.inspectSplitCategory = state.activeCategory;
  elements.inspectSplitCategory.value = String(state.inspectSplitCategory);
  state.inspectEditMode = false;
  state.inspectImageData = null;
  state.inspectPage = 0;
  elements.inspectModalSubtitle.textContent = `${islandCount} islands per instance · select an instance to inspect its colored mask regions`;
  elements.inspectInstanceCount.textContent = `${formatNumber(instances.length)} instances`;
  elements.inspectLoading.replaceChildren(element("div", "spinner"), element("strong", "", "Select an instance…"));
  elements.inspectLoading.hidden = false;
  elements.inspectStage.hidden = true;
  elements.inspectSelection.replaceChildren();
  renderInspectObjectList();
  elements.inspectModal.hidden = false;
  if (instances.length) selectInspectInstance(instances[0]);
}

function renderInspectObjectList() {
  const pageSize = 500;
  const pageCount = Math.max(1, Math.ceil(state.inspectInstances.length / pageSize));
  state.inspectPage = Math.max(0, Math.min(pageCount - 1, state.inspectPage || 0));
  elements.inspectObjectList.replaceChildren();
  const page = state.inspectInstances.slice(state.inspectPage * pageSize, (state.inspectPage + 1) * pageSize);
  for (const reference of page) {
    const button = element("button", "inspect-object-row");
    button.type = "button";
    if (reference.annotation_id === state.inspectSelected?.annotation_id) button.classList.add("active");
    const color = element("span", "object-color");
    color.style.background = categoryColor(reference.category_id);
    button.append(color, element("span", "object-name", `#${reference.annotation_id} · ${categoryName(reference.category_id)}`), element("span", "object-score", reference.score?.toFixed(2) || ""));
    button.addEventListener("click", () => selectInspectInstance(reference));
    elements.inspectObjectList.append(button);
  }
  if (!state.inspectInstances.length) elements.inspectObjectList.append(element("div", "object-empty", "No instances found"));
  const pagination = element("div", "inspect-pagination");
  const previous = element("button", "button secondary", "Previous");
  previous.type = "button";
  previous.disabled = state.inspectPage === 0;
  previous.addEventListener("click", () => {
    state.inspectPage -= 1;
    renderInspectObjectList();
  });
  const next = element("button", "button secondary", "Next");
  next.type = "button";
  next.disabled = state.inspectPage >= pageCount - 1;
  next.addEventListener("click", () => {
    state.inspectPage += 1;
    renderInspectObjectList();
  });
  pagination.append(previous, element("span", "", `${state.inspectPage + 1} / ${pageCount}`), next);
  elements.inspectObjectList.append(pagination);
}

function updateInspectEditControls() {
  const enabled = Boolean(state.inspectSelected);
  elements.inspectEditToggle.classList.toggle("active", state.inspectEditMode);
  elements.inspectEditToggle.textContent = state.inspectEditMode ? "Editing mask" : "Edit mask";
  elements.inspectMaskAdd.classList.toggle("active", state.inspectEditOperation === "add");
  elements.inspectMaskRemove.classList.toggle("active", state.inspectEditOperation === "remove");
  elements.inspectMaskAdd.disabled = !state.inspectEditMode || !enabled;
  elements.inspectMaskRemove.disabled = !state.inspectEditMode || !enabled;
  elements.inspectBrushSize.disabled = !state.inspectEditMode || !enabled;
  elements.inspectMaskReset.disabled = !state.inspectMaskStrokes.length;
  elements.inspectSplitToggle.classList.toggle("active", state.inspectSplitMode);
  elements.inspectSplitToggle.textContent = state.inspectSplitMode ? "Selecting islands" : "Split instance";
  elements.inspectSplitCategory.disabled = !state.inspectSplitMode || !enabled;
  elements.inspectSplitApply.disabled = !state.inspectSplitMode || !state.inspectSelectedIslands.size || !enabled;
  elements.inspectDeleteInstance.disabled = !enabled || state.inspectSplitMode;
  elements.inspectEditStatus.textContent = state.inspectSplitMode
    ? `Select islands: ${state.inspectSelectedIslands.size} selected`
    : state.inspectEditMode
      ? `Paint ${state.inspectEditOperation === "add" ? "add" : "erase"} on the focused instance`
      : enabled ? "Edit mask to add or erase pixels" : "Select an instance to edit its mask";
  elements.inspectOverlay.style.cursor = state.inspectEditMode ? "crosshair" : "default";
}

function saveInspectMaskEdits() {
  if (!state.inspectSelected) return;
  if (state.inspectMaskStrokes.length) state.maskEdits[String(state.inspectSelected.annotation_id)] = state.inspectMaskStrokes;
  else delete state.maskEdits[String(state.inspectSelected.annotation_id)];
  persist();
  updateInspectEditControls();
}

function deleteInspectInstance() {
  if (!state.inspectSelected) return;
  const before = captureHistoryState();
  const annotationId = String(state.inspectSelected.annotation_id);
  state.reviews[annotationId] = "remove";
  delete state.maskEdits[annotationId];
  state.inspectInstances = state.inspectInstances.filter((reference) => String(reference.annotation_id) !== annotationId);
  state.inspectSelected = null;
  state.inspectMaskStrokes = [];
  persist();
  commitHistory("Delete instance", before);
  renderInspectObjectList();
  updateInspectEditControls();
  if (state.inspectInstances.length) selectInspectInstance(state.inspectInstances[0]);
  else {
    elements.inspectImage.removeAttribute("src");
    elements.inspectStage.hidden = true;
    elements.inspectLoading.replaceChildren(element("strong", "", "No instances remain in this group"));
  }
  showToast("Instance marked for removal · Ctrl/Cmd+Z to undo");
}

function resetInspectMask() {
  const before = captureHistoryState();
  state.inspectMaskStrokes = [];
  saveInspectMaskEdits();
  commitHistory("Reset mask", before);
  drawInspectMask();
}

function inspectPointerPosition(event) {
  const rect = elements.inspectOverlay.getBoundingClientRect();
  const image = state.inspectImageData.image;
  return {
    x: Math.max(0, Math.min(image.width, (event.clientX - rect.left) * image.width / rect.width)),
    y: Math.max(0, Math.min(image.height, (event.clientY - rect.top) * image.height / rect.height)),
  };
}

function componentAtInspectPoint(point) {
  return state.inspectComponents.find((component) => component.intervals.some((interval) => interval.column === Math.floor(point.x) && point.y >= interval.start && point.y < interval.end));
}

function toggleInspectIsland(point) {
  const component = componentAtInspectPoint(point);
  if (!component) return;
  if (state.inspectSelectedIslands.has(component.id)) state.inspectSelectedIslands.delete(component.id);
  else state.inspectSelectedIslands.add(component.id);
  updateInspectEditControls();
  drawInspectMask();
}

function encodeSelectedIslands(components, height, width) {
  const mask = new Uint8Array(height * width);
  for (const component of components) {
    for (const interval of component.intervals) {
      const start = interval.column * height + interval.start;
      const end = interval.column * height + interval.end;
      mask.fill(1, start, end);
    }
  }
  const counts = [];
  let current = 0;
  let run = 0;
  let area = 0;
  let minX = width;
  let minY = height;
  let maxX = 0;
  let maxY = 0;
  for (let column = 0; column < width; column += 1) {
    for (let row = 0; row < height; row += 1) {
      const value = mask[column * height + row];
      if (value) {
        area += 1;
        minX = Math.min(minX, column);
        minY = Math.min(minY, row);
        maxX = Math.max(maxX, column + 1);
        maxY = Math.max(maxY, row + 1);
      }
      if (value === current) run += 1;
      else {
        counts.push(run);
        current = 1 - current;
        run = 1;
      }
    }
  }
  counts.push(run);
  return { segmentation: { size: [height, width], counts }, area, bbox: [minX, minY, maxX - minX, maxY - minY] };
}

function applySplitInstance() {
  if (!state.inspectImageData || !state.inspectSelectedIslands.size) return;
  const original = state.inspectImageData.annotation;
  const selectedComponents = state.inspectComponents.filter((component) => state.inspectSelectedIslands.has(component.id));
  if (!selectedComponents.length || selectedComponents.some((component) => !component.intervals.length)) {
    showToast("Split mode requires RLE mask islands");
    return;
  }
  const height = original.segmentation.size?.[0] || state.inspectImageData.image.height;
  const width = original.segmentation.size?.[1] || state.inspectImageData.image.width;
  const encoded = encodeSelectedIslands(selectedComponents, height, width);
  if (!encoded.area) return;
  const before = captureHistoryState();
  const newAnnotation = {
    id: state.nextNewId++,
    image_id: original.image_id,
    category_id: Number(elements.inspectSplitCategory.value),
    segmentation: encoded.segmentation,
    area: encoded.area,
    bbox: encoded.bbox,
    iscrowd: 0,
    score: 1,
  };
  state.created.push(newAnnotation);
  state.reviews[String(original.id)] = "remove";
  delete state.maskEdits[String(original.id)];
  refreshCurrentImageState();
  persist();
  commitHistory("Split instance", before);
  elements.inspectModal.hidden = true;
  showToast(`Created split instance #${newAnnotation.id} · original marked removed`);
}

function onInspectPointerDown(event) {
  if (!state.inspectImageData) return;
  if (state.inspectSplitMode) {
    event.preventDefault();
    toggleInspectIsland(inspectPointerPosition(event));
    return;
  }
  if (!state.inspectEditMode) return;
  event.preventDefault();
  state.inspectHistoryBefore = captureHistoryState();
  const point = inspectPointerPosition(event);
  state.inspectMaskStrokes.push({ operation: state.inspectEditOperation, radius: state.inspectBrushSize, points: [point] });
  elements.inspectOverlay.setPointerCapture(event.pointerId);
  drawInspectMask();
  updateInspectEditControls();
}

function onInspectPointerMove(event) {
  if (!state.inspectEditMode || !state.inspectImageData || !elements.inspectOverlay.hasPointerCapture(event.pointerId)) return;
  const stroke = state.inspectMaskStrokes[state.inspectMaskStrokes.length - 1];
  if (!stroke) return;
  const point = inspectPointerPosition(event);
  const previous = stroke.points[stroke.points.length - 1];
  if (Math.hypot(point.x - previous.x, point.y - previous.y) < Math.max(2, stroke.radius / 3)) return;
  stroke.points.push(point);
  drawInspectMask();
}

function onInspectPointerUp(event) {
  if (elements.inspectOverlay.hasPointerCapture(event.pointerId)) elements.inspectOverlay.releasePointerCapture(event.pointerId);
  const before = state.inspectHistoryBefore;
  state.inspectHistoryBefore = null;
  saveInspectMaskEdits();
  commitHistory("Edit mask", before);
}

function navigateInspectSelection(direction) {
  if (!state.inspectInstances.length) return;
  const currentIndex = state.inspectInstances.findIndex((instance) => instance.annotation_id === state.inspectSelected?.annotation_id);
  const nextIndex = (currentIndex + direction + state.inspectInstances.length) % state.inspectInstances.length;
  const next = state.inspectInstances[nextIndex];
  state.inspectPage = Math.floor(nextIndex / 500);
  selectInspectInstance(next);
  requestAnimationFrame(() => {
    const selected = elements.inspectObjectList.querySelector(".inspect-object-row.active");
    selected?.scrollIntoView({ block: "nearest" });
    selected?.focus({ preventScroll: true });
  });
}

async function selectInspectInstance(reference) {
  state.inspectSelected = reference;
  state.inspectMaskStrokes = (state.maskEdits[String(reference.annotation_id)] || []).map((stroke) => ({ ...stroke, points: stroke.points.map((point) => ({ ...point })) }));
  state.inspectComponents = [];
  state.inspectSelectedIslands = new Set();
  state.inspectSplitMode = false;
  updateInspectEditControls();
  renderInspectObjectList();
  elements.inspectLoading.hidden = false;
  elements.inspectStage.hidden = true;
  elements.inspectLoading.replaceChildren(element("div", "spinner"), element("strong", "", "Loading instance…"));
  try {
    let data = inspectImageCache.get(reference.image_id);
    if (!data) {
      const response = await request(`/api/image/${reference.image_id}`);
      data = await response.json();
      inspectImageCache.set(reference.image_id, data);
    }
    const annotation = data.annotations.find((item) => item.id === reference.annotation_id);
    if (!annotation) throw new Error("Instance annotation was not found");
    state.inspectImageData = { image: data.image, annotation };
    elements.inspectLoading.hidden = true;
    elements.inspectStage.hidden = false;
    elements.inspectOverlay.width = elements.inspectImage.naturalWidth || data.image.width;
    elements.inspectOverlay.height = elements.inspectImage.naturalHeight || data.image.height;
    elements.inspectImage.onload = () => {
      elements.inspectOverlay.width = elements.inspectImage.naturalWidth;
      elements.inspectOverlay.height = elements.inspectImage.naturalHeight;
      fitInspectStage();
      drawInspectMask();
    };
    elements.inspectImage.src = `${data.image_url}?inspect=${reference.annotation_id}`;
    elements.inspectSelection.replaceChildren(
      element("span", "", `Instance #${reference.annotation_id}`),
      element("strong", "", `${state.inspectIslandCount} islands · ${categoryName(annotation.category_id)}`),
    );
  } catch (error) {
    elements.inspectLoading.replaceChildren(element("strong", "", "Could not load instance"), element("span", "", error.message));
    showToast(error.message);
  }
}

function fitInspectStage() {
  if (!state.inspectImageData || elements.inspectStage.hidden) return;
  const image = state.inspectImageData.image;
  const bbox = state.inspectImageData.annotation.bbox;
  const width = Math.max(100, elements.inspectStage.parentElement.clientWidth - 40);
  const height = Math.max(100, elements.inspectStage.parentElement.clientHeight - 70);
  const scale = Math.max(0.5, Math.min(4, Math.min((width - 70) / Math.max(bbox[2], 1), (height - 70) / Math.max(bbox[3], 1))));
  const displayWidth = Math.round(image.width * scale);
  const displayHeight = Math.round(image.height * scale);
  const panX = displayWidth / 2 - (bbox[0] + bbox[2] / 2) * scale;
  const panY = displayHeight / 2 - (bbox[1] + bbox[3] / 2) * scale;
  elements.inspectStage.style.width = `${displayWidth}px`;
  elements.inspectStage.style.height = `${displayHeight}px`;
  elements.inspectStage.style.transform = `translate(-50%, -50%) translate(${panX}px, ${panY}px)`;
  elements.inspectImage.style.width = `${displayWidth}px`;
  elements.inspectImage.style.height = `${displayHeight}px`;
  elements.inspectOverlay.style.width = `${displayWidth}px`;
  elements.inspectOverlay.style.height = `${displayHeight}px`;
  drawInspectMask();
}

function drawInspectMask() {
  if (!state.inspectImageData || elements.inspectStage.hidden) return;
  const image = state.inspectImageData.image;
  const annotation = state.inspectImageData.annotation;
  const canvas = elements.inspectOverlay;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  state.inspectComponents = drawIslandOverlay(context, annotation.segmentation, image.width, image.height);
  context.save();
  for (const stroke of state.inspectMaskStrokes) {
    context.globalCompositeOperation = stroke.operation === "remove" ? "destination-out" : "source-over";
    context.fillStyle = "#facc15";
    context.globalAlpha = stroke.operation === "remove" ? 1 : 0.4;
    for (let index = 0; index < stroke.points.length; index += 1) {
      const point = stroke.points[index];
      if (index > 0) {
        const previous = stroke.points[index - 1];
        const distance = Math.hypot(point.x - previous.x, point.y - previous.y);
        const steps = Math.max(1, Math.ceil(distance / Math.max(2, stroke.radius / 2)));
        for (let step = 1; step <= steps; step += 1) {
          const t = step / steps;
          context.beginPath();
          context.arc(previous.x + (point.x - previous.x) * t, previous.y + (point.y - previous.y) * t, stroke.radius, 0, Math.PI * 2);
          context.fill();
        }
      } else {
        context.beginPath();
        context.arc(point.x, point.y, stroke.radius, 0, Math.PI * 2);
        context.fill();
      }
    }
  }
  context.restore();
  if (state.inspectSplitMode) {
    context.save();
    context.strokeStyle = "#ffffff";
    context.lineWidth = 4;
    for (const componentId of state.inspectSelectedIslands) {
      const component = state.inspectComponents[componentId];
      if (!component) continue;
      for (const interval of component.intervals) {
        context.strokeRect(interval.column, interval.start, 1, interval.end - interval.start);
      }
    }
    context.restore();
  }
  const [x, y, width, height] = annotation.bbox;
  context.strokeStyle = "#ffffff";
  context.lineWidth = 3;
  context.strokeRect(x, y, width, height);
}

function drawIslandOverlay(context, segmentation, imageWidth, imageHeight) {
  if (!segmentation) return [];
  const scale = Math.min(1, 1100 / imageWidth);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(imageWidth * scale));
  canvas.height = Math.max(1, Math.round(imageHeight * scale));
  const maskContext = canvas.getContext("2d");
  const palette = ["#f43f5e", "#22d3ee", "#a3e635", "#f59e0b", "#8b5cf6", "#ec4899", "#14b8a6", "#f97316", "#60a5fa", "#eab308", "#34d399", "#fb7185"];
  let components = [];
  if (Array.isArray(segmentation)) {
    segmentation.forEach((polygon, index) => {
      if (!Array.isArray(polygon) || polygon.length < 6) return;
      maskContext.fillStyle = palette[index % palette.length];
      maskContext.globalAlpha = 0.28;
      maskContext.beginPath();
      maskContext.moveTo(polygon[0] * scale, polygon[1] * scale);
      for (let offset = 2; offset < polygon.length; offset += 2) maskContext.lineTo(polygon[offset] * scale, polygon[offset + 1] * scale);
      maskContext.closePath();
      maskContext.fill();
      components.push({ id: index, polygon, intervals: [] });
    });
  } else {
    const counts = typeof segmentation.counts === "string" ? decodeCompressedCounts(segmentation.counts) : segmentation.counts;
    const height = segmentation.size?.[0] || imageHeight;
    const width = segmentation.size?.[1] || imageWidth;
    const columns = new Map();
    let position = 0;
    for (let runIndex = 0; runIndex < counts.length; runIndex += 1) {
      const count = counts[runIndex];
      if (runIndex % 2 === 0) {
        position += count;
        continue;
      }
      let remaining = count;
      while (remaining > 0 && position < height * width) {
        const column = Math.floor(position / height);
        const row = position % height;
        const length = Math.min(remaining, height - row);
        if (!columns.has(column)) columns.set(column, []);
        columns.get(column).push({ start: row, end: row + length });
        position += length;
        remaining -= length;
      }
      position += remaining;
    }
    const parent = [];
    const find = (node) => {
      while (parent[node] !== node) {
        parent[node] = parent[parent[node]];
        node = parent[node];
      }
      return node;
    };
    const union = (left, right) => {
      left = find(left);
      right = find(right);
      if (left !== right) parent[right] = left;
    };
    const componentIntervals = [];
    let previousColumn = null;
    let previous = [];
    for (const column of [...columns.keys()].sort((left, right) => left - right)) {
      const current = [];
      for (const interval of columns.get(column)) {
        const node = parent.length;
        parent.push(node);
        if (previousColumn === column - 1) {
          for (const previousInterval of previous) {
            if (interval.start < previousInterval.end && interval.end > previousInterval.start) union(node, previousInterval.node);
          }
        }
        current.push({ ...interval, node, column });
      }
      componentIntervals.push(...current);
      previousColumn = column;
      previous = current;
    }
    const componentMap = new Map();
    for (const interval of componentIntervals) {
      const root = find(interval.node);
      if (!componentMap.has(root)) componentMap.set(root, []);
      componentMap.get(root).push(interval);
    }
    components = [...componentMap.entries()].map(([root, intervals], id) => ({ id, root, intervals }));
    maskContext.globalAlpha = 0.28;
    for (const component of components) {
      maskContext.fillStyle = palette[component.id % palette.length];
      for (const interval of component.intervals) {
        const x = Math.floor(interval.column * scale);
        const y = Math.floor(interval.start * scale);
        const width = Math.max(1, Math.ceil((interval.column + 1) * scale) - x);
        const height = Math.max(1, Math.ceil(interval.end * scale) - y);
        maskContext.fillRect(x, y, width, height);
      }
    }
  }
  context.drawImage(canvas, 0, 0, imageWidth, imageHeight);
  return components;
}

async function loadDataset() {
  try {
    const response = await request("/api/dataset");
    const data = await response.json();
    state.dataset = data;
    state.images = data.images;
    if (data.categories.length) state.activeCategory = data.categories[0].id;
    renderCategories();
    applyFilter();
    const requested = Number(location.hash.slice(1));
    const initial = state.filtered.find((image) => image.id === requested) || state.filtered[0];
    if (initial) await selectImage(initial.id);
  } catch (error) {
    elements.loading.innerHTML = "";
    elements.loading.append(element("strong", "", "Could not load dataset"));
    elements.loading.append(element("span", "", error.message));
    showToast(error.message);
  }
}

function renderCategories() {
  elements.categoryList.replaceChildren();
  elements.activeCategory.replaceChildren();
  for (const category of state.dataset.categories) {
    const color = state.dataset.category_colors[category.id];
    const label = element("label", "category-row");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    checkbox.style.setProperty("--color", color);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.hiddenCategories.delete(category.id);
      else state.hiddenCategories.add(category.id);
      renderObjects();
      draw();
    });
    const dot = element("span", "object-color");
    dot.style.background = color;
    label.append(checkbox, dot, element("span", "", category.name), element("span", "category-count", ""));
    elements.categoryList.append(label);

    const option = document.createElement("option");
    option.value = category.id;
    option.textContent = category.name;
    option.selected = category.id === state.activeCategory;
    elements.activeCategory.append(option);
    const splitOption = document.createElement("option");
    splitOption.value = category.id;
    splitOption.textContent = category.name;
    splitOption.selected = category.id === state.inspectSplitCategory;
    elements.inspectSplitCategory.append(splitOption);
  }
}

function updateCategoryCounts() {
  const summary = state.current;
  if (!summary) return;
  for (const [index, category] of state.dataset.categories.entries()) {
    const count = elements.categoryList.children[index]?.querySelector(".category-count");
    if (count) count.textContent = summary.category_counts[String(category.id)] || 0;
  }
}

function applyFilter() {
  const query = state.search.toLowerCase();
  state.filtered = state.images.filter((image) => {
    if (query && !image.file_name.toLowerCase().includes(query)) return false;
    if (state.filter === "empty" && image.annotation_count) return false;
    if (state.filter === "reviewed" && !image.annotation_count) return false;
    if (state.filter === "reviewed" && !hasImageReview(image.id)) return false;
    if (state.filter === "flagged" && !hasFlaggedReview(image.id)) return false;
    return true;
  });
  elements.resultCount.textContent = state.filtered.length;
  renderImageList();
}

function hasImageReview(imageId) {
  return getOriginalAnnotations(imageId).some((id) => state.reviews[String(id)]);
}

function hasFlaggedReview(imageId) {
  return getOriginalAnnotations(imageId).some((id) => ["fix", "remove"].includes(state.reviews[String(id)]));
}

function getOriginalAnnotations(imageId) {
  const image = state.images.find((item) => item.id === Number(imageId));
  return image?.annotation_ids || [];
}

function renderImageList() {
  elements.imageList.replaceChildren();
  const fragment = document.createDocumentFragment();
  for (const image of state.filtered) {
    const button = element("button", "image-item");
    button.type = "button";
    button.dataset.imageId = image.id;
    if (image.id === state.current?.id) button.classList.add("active");
    const thumb = element("div", "thumb");
    const hue = image.id * 47 % 360;
    thumb.style.background = `linear-gradient(135deg, hsl(${hue} 35% 28%), hsl(${(hue + 55) % 360} 40% 15%))`;
    const frame = image.file_name.match(/frame_(\d+)/)?.[1] || image.id;
    thumb.append(element("span", "thumb-frame", frame));
    const copy = element("div", "item-copy");
    copy.append(element("span", "item-name", image.file_name));
    const meta = element("div", "item-meta");
    const dot = element("span", "status-dot");
    const review = getImageReview(image.id);
    if (review) dot.classList.add(review);
    meta.append(dot, element("span", "", `${image.annotation_count} objects`));
    copy.append(meta);
    const badge = element("span", "item-index", String(image.id));
    button.append(thumb, copy, badge);
    button.addEventListener("click", () => selectImage(image.id));
    fragment.append(button);
  }
  elements.imageList.append(fragment);
  updateSelectionPosition();
}

function getImageReview(imageId) {
  const image = state.images.find((item) => item.id === imageId);
  if (!image) return null;
  const ids = currentOriginalIds(imageId);
  const statuses = ids.map((id) => state.reviews[String(id)]).filter(Boolean);
  if (statuses.includes("fix") || statuses.includes("remove")) return "fix";
  if (statuses.length) return "keep";
  return null;
}

function currentOriginalIds(imageId) {
  return getOriginalAnnotations(imageId);
}

async function selectImage(imageId, updateHash = true) {
  const token = ++state.loadToken;
  state.current = state.images.find((image) => image.id === imageId) || null;
  state.zoom = 1;
  state.panX = 0;
  state.panY = 0;
  state.maskCache = null;
  state.maskCacheKey = "";
  setPanMode(false);
  state.selectedId = null;
  state.drag = null;
  elements.loading.hidden = false;
  elements.stage.hidden = true;
  elements.objectList.replaceChildren(element("div", "object-empty", "Loading objects…"));
  elements.loading.replaceChildren(element("div", "spinner"), element("strong", "", "Loading image…"), element("span", "", "Fetching annotations"));
  if (state.current) {
    elements.imageName.textContent = state.current.file_name;
    elements.imageMeta.textContent = `${state.current.width} × ${state.current.height}`;
    updateCategoryCounts();
    renderImageList();
  }
  if (updateHash) history.replaceState(null, "", `#${imageId}`);
  try {
    const response = await request(`/api/image/${imageId}`);
    const data = await response.json();
    if (token !== state.loadToken) return;
    state.imageData = data;
    state.nextNewId = Math.max(state.nextNewId, data.max_annotation_id + 1, ...state.created.map((annotation) => annotation.id + 1));
    state.selectedId = getAnnotations()[0]?.id ?? null;
    let attempt = 0;
    let timeoutId = null;
    const beginImageRequest = () => {
      if (token !== state.loadToken) return;
      clearTimeout(timeoutId);
      attempt += 1;
      elements.loading.replaceChildren(element("div", "spinner"), element("strong", "", attempt === 1 ? "Loading image…" : `Retrying image (${attempt}/3)…`), element("span", "", `${data.image.file_name} via local media proxy`));
      elements.image.src = `${data.image_url}?attempt=${attempt}`;
      timeoutId = setTimeout(() => retryImage(), 60000);
    };
    const retryImage = () => {
      if (token !== state.loadToken) return;
      if (attempt < 3) {
        beginImageRequest();
      } else {
        elements.loading.replaceChildren(element("strong", "", "Image failed after 3 attempts"), element("span", "", data.image.file_name));
        const retry = element("button", "button secondary", "Retry image");
        retry.type = "button";
        retry.addEventListener("click", () => {
          attempt = 0;
          beginImageRequest();
        });
        elements.loading.append(retry);
        showToast("The image proxy did not respond");
      }
    };
    elements.image.onload = () => {
      if (token !== state.loadToken) return;
      clearTimeout(timeoutId);
      elements.loading.hidden = true;
      elements.stage.hidden = false;
      elements.overlay.width = elements.image.naturalWidth;
      elements.overlay.height = elements.image.naturalHeight;
      elements.objectPanel.hidden = false;
      requestAnimationFrame(() => {
        fitStage();
        renderObjects();
      });
    };
    elements.image.onerror = retryImage;
    beginImageRequest();
  } catch (error) {
    if (token === state.loadToken) showToast(error.message);
  }
}

function fitStage(redraw = true) {
  if (!state.imageData || elements.stage.hidden) return;
  const image = state.imageData.image;
  const availableWidth = Math.max(200, elements.canvasArea.clientWidth - 50);
  const availableHeight = Math.max(200, elements.canvasArea.clientHeight - 50);
  const baseScale = Math.min(availableWidth / image.width, availableHeight / image.height, 1);
  state.scale = baseScale * state.zoom;
  const width = Math.max(1, Math.round(image.width * state.scale));
  const height = Math.max(1, Math.round(image.height * state.scale));
  elements.stage.style.width = `${width}px`;
  elements.stage.style.height = `${height}px`;
  elements.stage.style.transform = `translate(-50%, -50%) translate(${state.panX}px, ${state.panY}px)`;
  elements.image.style.width = `${width}px`;
  elements.image.style.height = `${height}px`;
  elements.overlay.style.width = `${width}px`;
  elements.overlay.style.height = `${height}px`;
  elements.zoomReset.textContent = `${Math.round(state.zoom * 100)}%`;
  if (redraw) draw();
}

function setZoom(nextZoom, clientX, clientY) {
  if (!state.imageData || elements.stage.hidden) return;
  const zoom = Math.max(0.25, Math.min(8, nextZoom));
  if (zoom < state.zoom) {
    state.zoom = zoom;
    state.panX = 0;
    state.panY = 0;
    fitStage();
    return;
  }
  const areaRect = elements.canvasArea.getBoundingClientRect();
  const focusX = clientX === undefined ? 0 : clientX - areaRect.left - areaRect.width / 2;
  const focusY = clientY === undefined ? 0 : clientY - areaRect.top - areaRect.height / 2;
  const sourceX = (focusX - state.panX) / state.scale;
  const sourceY = (focusY - state.panY) / state.scale;
  const baseScale = state.scale / state.zoom;
  const nextScale = baseScale * zoom;
  state.panX = focusX - sourceX * nextScale;
  state.panY = focusY - sourceY * nextScale;
  state.zoom = zoom;
  fitStage();
}

function focusAnnotation(annotation) {
  if (!state.imageData || elements.stage.hidden) return;
  const [x, y, width, height] = annotation.bbox;
  const image = state.imageData.image;
  const availableWidth = Math.max(200, elements.canvasArea.clientWidth - 100);
  const availableHeight = Math.max(200, elements.canvasArea.clientHeight - 100);
  const baseScale = Math.min((availableWidth / image.width), (availableHeight / image.height), 1);
  const desiredScale = Math.min(availableWidth / Math.max(width, 1), availableHeight / Math.max(height, 1));
  state.zoom = Math.max(1.5, Math.min(4, desiredScale / baseScale));
  state.panX = 0;
  state.panY = 0;
  fitStage(false);
  state.panX = image.width * state.scale / 2 - (x + width / 2) * state.scale;
  state.panY = image.height * state.scale / 2 - (y + height / 2) * state.scale;
  fitStage();
}

function resetView() {
  state.zoom = 1;
  state.panX = 0;
  state.panY = 0;
  fitStage();
}

function setPanMode(enabled) {
  state.panMode = enabled;
  elements.panMode.classList.toggle("active", enabled);
  elements.overlay.style.cursor = enabled ? "grab" : state.drawMode ? "crosshair" : "default";
}

function getAnnotations() {
  if (!state.imageData) return [];
  const created = state.created.filter((annotation) => annotation.image_id === state.imageData.image.id);
  return [...state.imageData.annotations, ...created].filter((annotation) => state.reviews[String(annotation.id)] !== "remove");
}

function effectiveAnnotation(annotation) {
  const edit = state.edits[String(annotation.id)];
  return edit ? { ...annotation, ...edit } : annotation;
}

function isCreated(annotation) {
  return state.created.some((item) => item.id === annotation.id);
}

function getSelected() {
  return getAnnotations().find((annotation) => annotation.id === state.selectedId) || null;
}

function categoryName(categoryId) {
  return state.dataset.categories.find((category) => category.id === categoryId)?.name || `Category ${categoryId}`;
}

function categoryColor(categoryId) {
  return state.dataset.category_colors[categoryId] || "#ffffff";
}

function draw() {
  if (!state.imageData || elements.stage.hidden) return;
  const image = state.imageData.image;
  context.clearRect(0, 0, elements.overlay.width, elements.overlay.height);
  const annotations = getAnnotations();
  const visible = annotations.filter((annotation) => !state.hiddenCategories.has(annotation.category_id));
  if (state.showMasks) drawMasks(visible);
  for (const original of visible) {
    const annotation = effectiveAnnotation(original);
    if (state.showBoxes || annotation.id === state.selectedId) drawBox(annotation, annotation.id === state.selectedId);
  }
  if (state.drag?.type === "new" && state.drag.box) {
    const box = normalizedBox(state.drag.start, state.drag.end);
    context.strokeStyle = categoryColor(state.activeCategory);
    context.lineWidth = 2.5 / state.scale;
    context.setLineDash([7 / state.scale, 5 / state.scale]);
    context.strokeRect(box.x, box.y, box.width, box.height);
    context.setLineDash([]);
  }
  updateStatus(annotations);
}

function drawMasks(annotations) {
  const image = state.imageData.image;
  const rleAnnotations = [];
  for (const original of annotations) {
    const annotation = effectiveAnnotation(original);
    if (annotation.segmentation?.counts) rleAnnotations.push(annotation);
    if (!Array.isArray(annotation.segmentation)) continue;
    context.save();
    context.fillStyle = categoryColor(annotation.category_id);
    context.globalAlpha = 0.2;
    for (const polygon of annotation.segmentation) {
      if (!Array.isArray(polygon) || polygon.length < 6) continue;
      context.beginPath();
      context.moveTo(polygon[0], polygon[1]);
      for (let index = 2; index < polygon.length; index += 2) context.lineTo(polygon[index], polygon[index + 1]);
      context.closePath();
      context.fill();
    }
    context.restore();
  }
  if (!rleAnnotations.length) return;
  const cacheKey = JSON.stringify([image.id, rleAnnotations.map((annotation) => [annotation.id, annotation.category_id])]);
  if (state.maskCacheKey === cacheKey && state.maskCache) {
    context.drawImage(state.maskCache, 0, 0, image.width, image.height);
    return;
  }
  const maskCanvas = document.createElement("canvas");
  const scale = Math.min(1, 960 / image.width);
  maskCanvas.width = Math.max(1, Math.round(image.width * scale));
  maskCanvas.height = Math.max(1, Math.round(image.height * scale));
  const maskContext = maskCanvas.getContext("2d");
  const imageData = maskContext.createImageData(maskCanvas.width, maskCanvas.height);
  const pixels = imageData.data;
  for (const annotation of rleAnnotations) {
    const segmentation = annotation.segmentation;
    const [red, green, blue] = hexToRgb(categoryColor(annotation.category_id));
    const counts = typeof segmentation.counts === "string" ? decodeCompressedCounts(segmentation.counts) : segmentation.counts;
    const maskHeight = segmentation.size?.[0] || image.height;
    const maskWidth = segmentation.size?.[1] || image.width;
    let position = 0;
    let foreground = false;
    for (const count of counts) {
      let remaining = count;
      while (foreground && remaining > 0 && position < maskHeight * maskWidth) {
        const column = Math.floor(position / maskHeight);
        const row = position % maskHeight;
        const length = Math.min(remaining, maskHeight - row);
        const xStart = Math.max(0, Math.min(maskCanvas.width - 1, Math.floor(column * scale)));
        const xEnd = Math.max(xStart + 1, Math.min(maskCanvas.width, Math.ceil((column + 1) * scale)));
        const yStart = Math.max(0, Math.min(maskCanvas.height - 1, Math.floor(row * scale)));
        const yEnd = Math.max(yStart + 1, Math.min(maskCanvas.height, Math.ceil((row + length) * scale)));
        for (let y = yStart; y < yEnd; y += 1) {
          for (let x = xStart; x < xEnd; x += 1) {
            const offset = (y * maskCanvas.width + x) * 4;
            if (pixels[offset + 3] === 0) {
              pixels[offset] = red;
              pixels[offset + 1] = green;
              pixels[offset + 2] = blue;
              pixels[offset + 3] = 64;
            }
          }
        }
        position += length;
        remaining -= length;
      }
      position += remaining;
      foreground = !foreground;
    }
  }
  maskContext.putImageData(imageData, 0, 0);
  state.maskCache = maskCanvas;
  state.maskCacheKey = cacheKey;
  context.drawImage(maskCanvas, 0, 0, image.width, image.height);
}

function hexToRgb(color) {
  const value = Number.parseInt(color.slice(1), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function decodeCompressedCounts(value) {
  const counts = [];
  let position = 0;
  while (position < value.length) {
    let number = 0;
    let shift = 0;
    let code = 0;
    let more = true;
    while (more && position < value.length) {
      code = value.charCodeAt(position++) - 48;
      number |= (code & 0x1f) << (5 * shift);
      more = Boolean(code & 0x20);
      shift += 1;
    }
    if (!more && (code & 0x10)) number |= -1 << (5 * shift);
    if (counts.length > 2) number += counts[counts.length - 2];
    counts.push(number);
  }
  return counts;
}

function drawBox(annotation, selected) {
  const [x, y, width, height] = annotation.bbox;
  const color = categoryColor(annotation.category_id);
  context.strokeStyle = selected ? "#ffffff" : color;
  context.lineWidth = (selected ? 3 : 2) / state.scale;
  context.strokeRect(x, y, width, height);
  const fontSize = 12 / state.scale;
  context.font = `600 ${fontSize}px system-ui`;
  const label = `${annotation.id} · ${categoryName(annotation.category_id)}`;
  const textWidth = context.measureText(label).width;
  const padding = 4 / state.scale;
  context.fillStyle = "rgba(5, 7, 10, 0.82)";
  context.fillRect(x, Math.max(0, y - fontSize - padding * 2), textWidth + padding * 2, fontSize + padding * 2);
  context.fillStyle = color;
  context.textBaseline = "bottom";
  context.fillText(label, x + padding, Math.max(fontSize, y - padding));
  if (selected) drawHandles(annotation.bbox);
}

function drawHandles(box) {
  const radius = 5 / state.scale;
  context.fillStyle = "#ffffff";
  context.strokeStyle = "#111827";
  context.lineWidth = 1.5 / state.scale;
  for (const [x, y] of handlePositions(box)) {
    context.beginPath();
    context.arc(x, y, radius, 0, Math.PI * 2);
    context.fill();
    context.stroke();
  }
}

function handlePositions(box) {
  const [x, y, width, height] = box;
  return [[x, y], [x + width / 2, y], [x + width, y], [x + width, y + height / 2], [x + width, y + height], [x + width / 2, y + height], [x, y + height], [x, y + height / 2]];
}

function updateStatus(annotations) {
  const selected = getSelected();
  const category = selected ? categoryColor(selected.category_id) : "transparent";
  elements.annotationStatus.innerHTML = selected
    ? `<span style="color:${category}">●</span> #${selected.id} ${categoryName(selected.category_id)} · [${selected.bbox.map((value) => Math.round(value)).join(", ")}]`
    : "No selection · draw mode creates a bbox";
  const visible = annotations.filter((annotation) => !state.hiddenCategories.has(annotation.category_id)).length;
  const review = selected ? state.reviews[String(selected.id)] : null;
  elements.objectStatus.textContent = `${annotations.length} objects · ${visible} visible${review ? ` · ${review}` : ""}`;
  for (const button of [elements.markKeep, elements.markFix, elements.markRemove]) button.classList.remove("active");
  if (review === "keep") elements.markKeep.classList.add("active");
  if (review === "fix") elements.markFix.classList.add("active");
  if (review === "remove") elements.markRemove.classList.add("active");
}

function getVisibleMenuAnnotations() {
  return getAnnotations().filter((original) => !state.hiddenCategories.has(effectiveAnnotation(original).category_id));
}

function navigateObjectSelection(direction) {
  const annotations = getVisibleMenuAnnotations();
  if (!annotations.length) return;
  const currentIndex = annotations.findIndex((annotation) => annotation.id === state.selectedId);
  const nextIndex = currentIndex < 0
    ? direction > 0 ? 0 : annotations.length - 1
    : (currentIndex + direction + annotations.length) % annotations.length;
  const annotation = effectiveAnnotation(annotations[nextIndex]);
  state.selectedId = annotation.id;
  renderObjects();
  focusAnnotation(annotation);
  requestAnimationFrame(() => {
    const selected = elements.objectList.querySelector(".object-row.active");
    selected?.scrollIntoView({ block: "nearest" });
    selected?.focus({ preventScroll: true });
  });
}

function renderObjects() {
  const scrollTop = elements.objectList.scrollTop;
  elements.objectList.replaceChildren();
  const visible = getVisibleMenuAnnotations();
  for (const original of visible) {
    const annotation = effectiveAnnotation(original);
    const button = element("button", "object-row");
    button.type = "button";
    if (annotation.id === state.selectedId) button.classList.add("active");
    const color = element("span", "object-color");
    color.style.background = categoryColor(annotation.category_id);
    const name = element("span", "object-name", `#${annotation.id} · ${categoryName(annotation.category_id)}`);
    const review = state.reviews[String(annotation.id)];
    if (review) name.style.textDecoration = review === "remove" ? "line-through" : "none";
    const score = element("span", "object-score", review ? review[0].toUpperCase() : annotation.score?.toFixed(2) || "new");
    button.append(color, name, score);
    button.addEventListener("click", () => {
      state.selectedId = annotation.id;
      renderObjects();
      focusAnnotation(annotation);
    });
    elements.objectList.append(button);
  }
  if (!visible.length) elements.objectList.append(element("div", "object-empty", "No objects in selected categories"));
  elements.objectList.scrollTop = scrollTop;
}

function pointerPosition(event) {
  const rect = elements.overlay.getBoundingClientRect();
  const image = state.imageData.image;
  return {
    x: Math.max(0, Math.min(image.width, (event.clientX - rect.left) * image.width / rect.width)),
    y: Math.max(0, Math.min(image.height, (event.clientY - rect.top) * image.height / rect.height)),
  };
}

function hitHandle(position) {
  const annotation = getSelected();
  if (!annotation || state.hiddenCategories.has(annotation.category_id)) return -1;
  const threshold = 9 / state.scale;
  return handlePositions(annotation.bbox).findIndex(([x, y]) => Math.hypot(position.x - x, position.y - y) <= threshold);
}

function hitAnnotation(position) {
  const annotations = getAnnotations().filter((annotation) => !state.hiddenCategories.has(annotation.category_id));
  for (let index = annotations.length - 1; index >= 0; index -= 1) {
    const annotation = effectiveAnnotation(annotations[index]);
    const [x, y, width, height] = annotation.bbox;
    if (position.x >= x && position.x <= x + width && position.y >= y && position.y <= y + height) return annotation;
  }
  return null;
}

function normalizedBox(start, end) {
  return {
    x: Math.min(start.x, end.x),
    y: Math.min(start.y, end.y),
    width: Math.abs(end.x - start.x),
    height: Math.abs(end.y - start.y),
  };
}

function clipBox(box) {
  const image = state.imageData.image;
  const x = Math.max(0, Math.min(image.width, box.x));
  const y = Math.max(0, Math.min(image.height, box.y));
  return {
    x,
    y,
    width: Math.max(0, Math.min(image.width - x, box.width)),
    height: Math.max(0, Math.min(image.height - y, box.height)),
  };
}

function onPointerDown(event) {
  if (!state.imageData || elements.stage.hidden) return;
  if (state.panMode || state.spaceDown || event.button === 1) {
    state.drag = { type: "pan", startX: event.clientX, startY: event.clientY, panX: state.panX, panY: state.panY };
    elements.overlay.style.cursor = "grabbing";
    elements.overlay.setPointerCapture(event.pointerId);
    event.preventDefault();
    return;
  }
  const position = pointerPosition(event);
  const handle = state.showBoxes ? hitHandle(position) : -1;
  if (handle >= 0) {
    const annotation = getSelected();
    state.drag = { type: "resize", handle, start: position, original: [...annotation.bbox], historyBefore: captureHistoryState() };
  } else if (state.drawMode || event.shiftKey) {
    state.drag = { type: "new", start: position, end: position, box: null, historyBefore: captureHistoryState() };
  } else {
    const annotation = hitAnnotation(position);
    state.selectedId = annotation?.id ?? null;
    if (annotation) {
      state.drag = { type: "move", start: position, original: [...annotation.bbox], historyBefore: captureHistoryState() };
      state.activeCategory = annotation.category_id;
      elements.activeCategory.value = String(annotation.category_id);
    }
  }
  elements.overlay.setPointerCapture(event.pointerId);
  renderObjects();
  draw();
}

function onPointerMove(event) {
  if (!state.drag) return;
  if (state.drag.type === "pan") {
    state.panX = state.drag.panX + event.clientX - state.drag.startX;
    state.panY = state.drag.panY + event.clientY - state.drag.startY;
    fitStage(false);
    return;
  }
  const position = pointerPosition(event);
  if (state.drag.type === "new") {
    state.drag.end = position;
    state.drag.box = normalizedBox(state.drag.start, position);
  } else {
    const annotation = getSelected();
    if (!annotation) return;
    const image = state.imageData.image;
    const [originalX, originalY, originalWidth, originalHeight] = state.drag.original;
    let box;
    if (state.drag.type === "move") {
      const x = Math.max(0, Math.min(image.width - originalWidth, originalX + position.x - state.drag.start.x));
      const y = Math.max(0, Math.min(image.height - originalHeight, originalY + position.y - state.drag.start.y));
      box = [x, y, originalWidth, originalHeight];
    } else {
      const left = originalX;
      const top = originalY;
      const right = originalX + originalWidth;
      const bottom = originalY + originalHeight;
      let x = left;
      let y = top;
      let width = originalWidth;
      let height = originalHeight;
      if ([0, 6, 7].includes(state.drag.handle)) {
        x = Math.min(right - 1, Math.max(0, position.x));
        width = right - x;
      }
      if ([2, 3, 4].includes(state.drag.handle)) {
        x = Math.max(left + 1, Math.min(image.width, position.x));
        width = x - left;
      }
      if ([0, 1, 2].includes(state.drag.handle)) {
        y = Math.min(bottom - 1, Math.max(0, position.y));
        height = bottom - y;
      }
      if ([4, 5, 6].includes(state.drag.handle)) {
        y = Math.max(top + 1, Math.min(image.height, position.y));
        height = y - top;
      }
      const clipped = clipBox({ x, y, width, height });
      box = [clipped.x, clipped.y, clipped.width, clipped.height];
    }
    setAnnotationBox(annotation, box);
  }
  draw();
}

function onPointerUp(event) {
  if (!state.drag) return;
  const wasPan = state.drag.type === "pan";
  const historyBefore = state.drag.historyBefore;
  if (state.drag.type === "new" && state.drag.box && state.drag.box.width > 3 / state.scale && state.drag.box.height > 3 / state.scale) {
    const box = clipBox(state.drag.box);
    const annotation = {
      id: state.nextNewId++,
      image_id: state.imageData.image.id,
      category_id: Number(elements.activeCategory.value),
      segmentation: [],
      area: Math.round(box.width * box.height),
      bbox: [box.x, box.y, box.width, box.height],
      iscrowd: 0,
      score: 1,
    };
    state.created.push(annotation);
    state.selectedId = annotation.id;
    state.current.annotation_count += 1;
    state.current.category_counts[String(annotation.category_id)] = (state.current.category_counts[String(annotation.category_id)] || 0) + 1;
    persist();
    applyFilter();
    commitHistory("Draw bounding box", historyBefore);
  }
  if (state.drag.type === "move") commitHistory("Move bounding box", historyBefore);
  if (state.drag.type === "resize") commitHistory("Resize bounding box", historyBefore);
  state.drag = null;
  if (elements.overlay.hasPointerCapture(event.pointerId)) elements.overlay.releasePointerCapture(event.pointerId);
  setPanMode(state.panMode);
  if (wasPan) return;
  renderObjects();
  draw();
}

function setAnnotationBox(annotation, box) {
  const edit = { bbox: box.map((value) => Math.round(value * 100) / 100), area: Math.round(box[2] * box[3] * 100) / 100 };
  if (isCreated(annotation)) {
    const target = state.created.find((item) => item.id === annotation.id);
    Object.assign(target, edit);
  } else {
    state.edits[String(annotation.id)] = { ...(state.edits[String(annotation.id)] || {}), ...edit };
  }
  persist();
}

function setReview(value) {
  const selected = getSelected();
  if (!selected) return;
  const before = captureHistoryState();
  const key = String(selected.id);
  if (!value) delete state.reviews[key];
  else state.reviews[key] = value;
  persist();
  commitHistory(value ? `Mark ${value}` : "Clear review", before);
  renderImageList();
  renderObjects();
  draw();
}

function navigate(offset) {
  if (!state.filtered.length) return;
  const index = state.filtered.findIndex((image) => image.id === state.current?.id);
  const next = Math.max(0, Math.min(state.filtered.length - 1, index + offset));
  if (state.filtered[next]) selectImage(state.filtered[next].id);
}

function updateSelectionPosition() {
  const index = state.filtered.findIndex((image) => image.id === state.current?.id);
  elements.selectionPosition.textContent = index >= 0 ? `${index + 1} / ${state.filtered.length}` : "";
}

async function downloadCurrent() {
  if (!state.imageData) return;
  try {
    const response = await request("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviews: state.reviews, edits: state.edits, mask_edits: state.maskEdits, created: state.created, image_ids: [state.imageData.image.id] }),
    });
    const blob = await response.blob();
    downloadBlob(blob, state.imageData.image.file_name.replace(/\.[^.]+$/, "-reviewed.json"));
    showToast("Current image exported");
  } catch (error) {
    showToast(error.message);
  }
}

async function exportAll() {
  elements.exportAll.disabled = true;
  elements.exportAll.textContent = "Building export…";
  try {
    const response = await request("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviews: state.reviews, edits: state.edits, mask_edits: state.maskEdits, created: state.created }),
    });
    const blob = await response.blob();
    downloadBlob(blob, "instances-reviewed.json");
    showToast(`Full COCO export created: ${state.dataset.annotation_count + state.created.length} annotations`);
  } catch (error) {
    showToast(error.message);
  } finally {
    elements.exportAll.disabled = false;
    elements.exportAll.textContent = "Export full COCO";
  }
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

elements.undo.addEventListener("click", undoHistory);
elements.redo.addEventListener("click", redoHistory);
elements.historySelect.addEventListener("change", (event) => restoreHistory(Number(event.target.value)));
elements.search.addEventListener("input", (event) => {
  state.search = event.target.value.trim();
  applyFilter();
});

for (const button of document.querySelectorAll(".filter-button")) {
  button.addEventListener("click", () => {
    document.querySelectorAll(".filter-button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    applyFilter();
  });
}

elements.activeCategory.addEventListener("change", () => {
  const selected = getSelected();
  if (!selected || String(selected.id).startsWith("new-")) {
    state.activeCategory = Number(elements.activeCategory.value);
    return;
  }
  const before = captureHistoryState();
  setAnnotationBox(selected, selected.bbox);
  const key = String(selected.id);
  state.edits[key] = { ...(state.edits[key] || {}), category_id: Number(elements.activeCategory.value) };
  state.current.category_counts[String(selected.category_id)] -= 1;
  state.current.category_counts[elements.activeCategory.value] = (state.current.category_counts[elements.activeCategory.value] || 0) + 1;
  state.activeCategory = Number(elements.activeCategory.value);
  persist();
  commitHistory("Change category", before);
  renderImageList();
  renderObjects();
  draw();
});

elements.allCategories.addEventListener("click", () => {
  if (state.hiddenCategories.size) {
    state.hiddenCategories.clear();
  } else {
    state.hiddenCategories = new Set(state.dataset.categories.map((category) => category.id));
  }
  for (const [index, category] of state.dataset.categories.entries()) {
    const input = elements.categoryList.children[index]?.querySelector("input");
    if (input) input.checked = !state.hiddenCategories.has(category.id);
  }
  renderObjects();
  draw();
});

elements.zoomOut.addEventListener("click", () => setZoom(state.zoom / 1.25));
elements.zoomIn.addEventListener("click", () => setZoom(state.zoom * 1.25));
elements.zoomReset.addEventListener("click", resetView);
elements.fitImage.addEventListener("click", resetView);
elements.panMode.addEventListener("click", () => setPanMode(!state.panMode));
elements.canvasArea.addEventListener("wheel", (event) => {
  if (!state.imageData || elements.stage.hidden) return;
  event.preventDefault();
  setZoom(state.zoom * Math.exp(-event.deltaY * 0.0015), event.clientX, event.clientY);
}, { passive: false });

elements.previous.addEventListener("click", () => navigate(-1));
elements.next.addEventListener("click", () => navigate(1));
elements.showBoxes.addEventListener("change", () => {
  state.showBoxes = elements.showBoxes.checked;
  draw();
});
elements.showMasks.addEventListener("change", () => {
  state.showMasks = elements.showMasks.checked;
  draw();
});
elements.drawMode.addEventListener("change", () => {
  state.drawMode = elements.drawMode.checked;
  setPanMode(state.panMode);
});
elements.markKeep.addEventListener("click", () => setReview("keep"));
elements.markFix.addEventListener("click", () => setReview("fix"));
elements.markRemove.addEventListener("click", () => setReview("remove"));
elements.clearReview.addEventListener("click", () => setReview(null));
elements.exportCurrent.addEventListener("click", downloadCurrent);
elements.exportAll.addEventListener("click", exportAll);
elements.islandFrequencyTool.addEventListener("click", () => toolRegistry.islandFrequency.run());
elements.inspectEditToggle.addEventListener("click", () => {
  state.inspectEditMode = !state.inspectEditMode;
  updateInspectEditControls();
});
elements.inspectMaskAdd.addEventListener("click", () => {
  state.inspectEditOperation = "add";
  updateInspectEditControls();
});
elements.inspectMaskRemove.addEventListener("click", () => {
  state.inspectEditOperation = "remove";
  updateInspectEditControls();
});
elements.inspectBrushSize.addEventListener("input", (event) => {
  state.inspectBrushSize = Number(event.target.value);
  elements.inspectBrushValue.textContent = `${state.inspectBrushSize} px`;
});
elements.inspectMaskReset.addEventListener("click", resetInspectMask);
elements.inspectSplitToggle.addEventListener("click", () => {
  state.inspectSplitMode = !state.inspectSplitMode;
  state.inspectSelectedIslands = new Set();
  updateInspectEditControls();
  drawInspectMask();
});
elements.inspectSplitCategory.addEventListener("change", (event) => {
  state.inspectSplitCategory = Number(event.target.value);
});
elements.inspectSplitApply.addEventListener("click", applySplitInstance);
elements.inspectDeleteInstance.addEventListener("click", deleteInspectInstance);
elements.inspectOverlay.addEventListener("pointerdown", onInspectPointerDown);
elements.inspectOverlay.addEventListener("pointermove", onInspectPointerMove);
elements.inspectOverlay.addEventListener("pointerup", onInspectPointerUp);
elements.inspectOverlay.addEventListener("pointercancel", onInspectPointerUp);
elements.closeInspectModal.addEventListener("click", () => {
  elements.inspectModal.hidden = true;
});
elements.inspectModal.addEventListener("click", (event) => {
  if (event.target === elements.inspectModal) elements.inspectModal.hidden = true;
});
elements.closeToolModal.addEventListener("click", () => {
  elements.toolModal.hidden = true;
});
elements.toolModal.addEventListener("click", (event) => {
  if (event.target === elements.toolModal) elements.toolModal.hidden = true;
});
elements.overlay.addEventListener("pointerdown", onPointerDown);
elements.overlay.addEventListener("pointermove", onPointerMove);
elements.overlay.addEventListener("pointerup", onPointerUp);
elements.overlay.addEventListener("pointercancel", onPointerUp);
new ResizeObserver(fitStage).observe(elements.canvasArea);

document.addEventListener("keydown", (event) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  if (event.code === "Space") {
    event.preventDefault();
    state.spaceDown = true;
    elements.overlay.style.cursor = "grab";
    return;
  }
  const key = event.key.toLowerCase();
  if ((event.ctrlKey || event.metaKey) && key === "z") {
    event.preventDefault();
    if (event.shiftKey) redoHistory();
    else undoHistory();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && key === "y") {
    event.preventDefault();
    redoHistory();
    return;
  }
  if (!elements.inspectModal.hidden && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
    event.preventDefault();
    navigateInspectSelection(event.key === "ArrowDown" ? 1 : -1);
    return;
  }
  if (event.key === "ArrowUp" || event.key === "ArrowDown") {
    event.preventDefault();
    navigateObjectSelection(event.key === "ArrowDown" ? 1 : -1);
    return;
  }
  if (key === "a" || event.key === "ArrowLeft") navigate(-1);
  if (key === "d" || event.key === "ArrowRight") navigate(1);
  if (key === "b") {
    state.drawMode = !state.drawMode;
    elements.drawMode.checked = state.drawMode;
    setPanMode(state.panMode);
  }
  if (key === "h") setPanMode(!state.panMode);
  if (key === "+" || key === "=") setZoom(state.zoom * 1.25);
  if (key === "-") setZoom(state.zoom / 1.25);
  if (key === "m") {
    state.showMasks = !state.showMasks;
    elements.showMasks.checked = state.showMasks;
    draw();
  }
  if (key === "delete" || key === "backspace") setReview("remove");
  if (key === "escape") {
    if (!elements.inspectModal.hidden) {
      elements.inspectModal.hidden = true;
      return;
    }
    if (!elements.toolModal.hidden) {
      elements.toolModal.hidden = true;
      return;
    }
    state.selectedId = null;
    state.drag = null;
    renderObjects();
    draw();
  }
  if (/^[1-8]$/.test(key)) {
    const category = state.dataset.categories[Number(key) - 1];
    if (category) {
      state.activeCategory = category.id;
      elements.activeCategory.value = String(category.id);
    }
  }
});

document.addEventListener("keyup", (event) => {
  if (event.code !== "Space") return;
  state.spaceDown = false;
  setPanMode(state.panMode);
});

initializeHistory();
loadDataset();
