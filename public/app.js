const form = document.querySelector("#uploadForm");
const input = document.querySelector("#videoInput");
const analyzeButton = document.querySelector("#analyzeButton");
const fileMeta = document.querySelector("#fileMeta");
const message = document.querySelector("#message");
const progress = document.querySelector("#progress");
const health = document.querySelector("#health");
const emptyState = document.querySelector("#emptyState");
const results = document.querySelector("#results");
const metrics = document.querySelector("#metrics");
const scriptTitle = document.querySelector("#scriptTitle");
const scriptStats = document.querySelector("#scriptStats");
const scriptText = document.querySelector("#scriptText");
const overview = document.querySelector("#overview");
const structure = document.querySelector("#structure");
const timeline = document.querySelector("#timeline");
const copyButton = document.querySelector("#copyButton");

let selectedFile = null;

checkHealth();

input.addEventListener("change", () => {
  selectedFile = input.files[0] || null;
  updateSelectedFile();
});

for (const eventName of ["dragenter", "dragover"]) {
  form.addEventListener(eventName, (event) => {
    event.preventDefault();
    form.classList.add("dragging");
  });
}

for (const eventName of ["dragleave", "drop"]) {
  form.addEventListener(eventName, (event) => {
    event.preventDefault();
    form.classList.remove("dragging");
  });
}

form.addEventListener("drop", (event) => {
  const [file] = event.dataTransfer.files;
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  input.files = transfer.files;
  selectedFile = file;
  updateSelectedFile();
});

analyzeButton.addEventListener("click", async () => {
  if (!selectedFile) return;

  const body = new FormData();
  body.append("video", selectedFile);
  setBusy(true);
  setMessage("Extracting frames and interpreting the real video content...", false);

  try {
    const response = await fetch("/api/analyze", { method: "POST", body });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || "Analysis failed.");
    }
    renderResults(payload);
    setMessage(`Analyzed ${payload.contentFramesAnalyzed} content frames from ${payload.fileName}.`, false);
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    setBusy(false);
  }
});

copyButton.addEventListener("click", async () => {
  await navigator.clipboard.writeText(scriptText.value);
  copyButton.textContent = "Copied";
  setTimeout(() => {
    copyButton.textContent = "Copy";
  }, 1200);
});

async function checkHealth() {
  try {
    const response = await fetch("/api/health");
    const payload = await response.json();
    const ready = payload.ok && payload.opencv && payload.ffmpeg && payload.vision;
    health.classList.toggle("ok", ready);
    health.classList.toggle("warn", !ready);
    health.textContent = ready ? "Semantic analysis ready" : "Setup needed";
  } catch {
    health.classList.add("warn");
    health.textContent = "Server offline";
  }
}

function updateSelectedFile() {
  analyzeButton.disabled = !selectedFile;
  if (!selectedFile) {
    fileMeta.textContent = "No file selected";
    return;
  }
  fileMeta.textContent = `${selectedFile.name} - ${formatBytes(selectedFile.size)}`;
}

function setBusy(isBusy) {
  analyzeButton.disabled = isBusy || !selectedFile;
  analyzeButton.textContent = isBusy ? "Analyzing..." : "Analyze video";
  progress.hidden = !isBusy;
}

function setMessage(text, isError) {
  message.textContent = text;
  message.classList.toggle("error", isError);
}

function renderResults(payload) {
  emptyState.hidden = true;
  results.hidden = false;
  copyButton.disabled = false;

  const metadata = payload.metadata;
  const script = payload.script;
  const interpretation = payload.interpretation;

  metrics.innerHTML = "";
  [
    ["Duration", formatDuration(metadata.duration)],
    ["Frames", payload.framesAnalyzed],
    ["Content frames", payload.contentFramesAnalyzed],
    ["Read time", `${script.estimatedReadTimeSeconds}s`],
  ].forEach(([label, value]) => {
    const item = document.createElement("div");
    item.className = "metric";
    item.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
    metrics.appendChild(item);
  });

  scriptTitle.textContent = script.title;
  scriptStats.textContent = `${script.wordCount} words - estimated ${script.estimatedReadTimeSeconds} seconds aloud`;
  scriptText.value = script.text;
  overview.textContent = interpretation.overview;
  structure.textContent = interpretation.structure;

  timeline.innerHTML = "";
  const contentTimeline = payload.semanticAnalysis?.frames?.length ? payload.semanticAnalysis.frames : payload.timeline;
  contentTimeline.forEach((item) => {
    const node = document.createElement("div");
    node.className = "timeline-item";
    const label = item.timestamp ? `Frame ${item.index} - ${item.timestamp}` : `Frame ${item.frame}`;
    const text = item.description || item.importance || "";
    node.innerHTML = `<strong>${label}</strong><br>${text}`;
    timeline.appendChild(node);
  });
}

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatDuration(seconds) {
  const rounded = Math.round(seconds || 0);
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}
