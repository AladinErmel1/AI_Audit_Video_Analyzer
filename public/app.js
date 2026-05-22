const form = document.querySelector("#uploadForm");
const input = document.querySelector("#videoInput");
const analyzeButton = document.querySelector("#analyzeButton");
const fileMeta = document.querySelector("#fileMeta");
const message = document.querySelector("#message");
const progress = document.querySelector("#progress");
const health = document.querySelector("#health");
const frameSlider = document.querySelector("#frameSlider");
const frameCountValue = document.querySelector("#frameCountValue");
const emptyState = document.querySelector("#emptyState");
const results = document.querySelector("#results");
const metrics = document.querySelector("#metrics");
const copyButton = document.querySelector("#copyButton");
const reportTitle = document.querySelector("#reportTitle");
const executiveSummary = document.querySelector("#executiveSummary");
const auditScope = document.querySelector("#auditScope");
const findingCount = document.querySelector("#findingCount");
const findings = document.querySelector("#findings");
const positiveControls = document.querySelector("#positiveControls");
const limitations = document.querySelector("#limitations");

let selectedFile = null;
let latestReportText = "";

checkHealth();

input.addEventListener("change", () => {
  selectedFile = input.files[0] || null;
  updateSelectedFile();
});

frameSlider.addEventListener("input", () => {
  frameCountValue.textContent = frameSlider.value;
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
  body.append("auditFrames", frameSlider.value);
  setBusy(true);
  setMessage(`Extracting frames and reviewing ${frameSlider.value} audit frames...`, false);

  try {
    const response = await fetch("/api/analyze", { method: "POST", body });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || "Audit analysis failed.");
    }
    renderResults(payload);
    setMessage(`Reviewed ${payload.auditFramesAnalyzed} audit frames from ${payload.fileName}.`, false);
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    setBusy(false);
  }
});

copyButton.addEventListener("click", async () => {
  await navigator.clipboard.writeText(latestReportText);
  copyButton.textContent = "Copied";
  setTimeout(() => {
    copyButton.textContent = "Copy report";
  }, 1200);
});

async function checkHealth() {
  try {
    const response = await fetch("/api/health");
    const payload = await response.json();
    const ready = payload.ok && payload.opencv && payload.ffmpeg && payload.vision;
    health.classList.toggle("ok", ready);
    health.classList.toggle("warn", !ready);
    health.textContent = ready ? "Audit AI ready" : "Setup needed";
  } catch {
    health.classList.add("warn");
    health.textContent = "Server offline";
  }
}

function updateSelectedFile() {
  analyzeButton.disabled = !selectedFile;
  fileMeta.textContent = selectedFile ? `${selectedFile.name} - ${formatBytes(selectedFile.size)}` : "No file selected";
}

function setBusy(isBusy) {
  analyzeButton.disabled = isBusy || !selectedFile;
  analyzeButton.textContent = isBusy ? "Analyzing..." : "Run audit analysis";
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

  const report = payload.auditReport;
  const metadata = payload.metadata;
  const visual = payload.visualAnalysis?.summary || {};

  metrics.innerHTML = "";
  const effectiveFrames = payload.effectiveAuditFrames || payload.auditFramesAnalyzed || report.framesReviewed || 0;
  const requestedFrames = payload.requestedAuditFrames || report.requestedFrames || Number(frameSlider.value) || effectiveFrames;
  const frameLabel = effectiveFrames === requestedFrames
    ? String(effectiveFrames)
    : `${effectiveFrames} of ${requestedFrames} requested`;
  [
    ["Overall risk", report.overallRiskRating || "Not rated"],
    ["Findings", report.findings.length],
    ["Frames reviewed", frameLabel],
    ["Duration", formatDuration(metadata.duration)],
  ].forEach(([label, value]) => {
    const item = document.createElement("div");
    item.className = "metric";
    item.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
    metrics.appendChild(item);
  });

  reportTitle.textContent = report.title || "AI Audit Video Review";
  executiveSummary.textContent = report.executiveSummary || "No executive summary returned.";
  auditScope.textContent = report.auditScope || "Scope: review of extracted video frames.";
  findingCount.textContent = `${report.findings.length} finding${report.findings.length === 1 ? "" : "s"}`;

  findings.innerHTML = "";
  if (!report.findings.length) {
    const empty = document.createElement("div");
    empty.className = "finding empty-finding";
    empty.textContent = "No material audit findings were visible in the reviewed frames.";
    findings.appendChild(empty);
  } else {
    report.findings.forEach((finding) => findings.appendChild(renderFinding(finding)));
  }

  renderList(positiveControls, report.positiveControls, "No positive controls were identified in the reviewed frames.");
  renderList(limitations, report.limitations, "No limitations were provided.");

  latestReportText = buildReportText(report, metadata, visual);
}

function renderFinding(finding) {
  const node = document.createElement("article");
  const severity = String(finding.severity || "Medium").toLowerCase();
  node.className = `finding severity-${severity}`;
  node.innerHTML = `
    <div class="finding-topline">
      <span class="finding-id">${escapeHtml(finding.id || "F-000")}</span>
      <span class="severity-pill">${escapeHtml(finding.severity || "Medium")}</span>
      <span class="category-pill">${escapeHtml(finding.category || "Other")}</span>
    </div>
    <h4>${escapeHtml(finding.title || "Untitled finding")}</h4>
    <dl>
      <dt>Evidence</dt><dd>${escapeHtml(withTimestamp(finding.evidence, finding.timestamp))}</dd>
      <dt>Risk</dt><dd>${escapeHtml(finding.risk || "Risk not specified.")}</dd>
      <dt>Impact</dt><dd>${escapeHtml(finding.impact || "Impact not specified.")}</dd>
      <dt>Recommendation</dt><dd>${escapeHtml(finding.recommendation || "Review and remediate as appropriate.")}</dd>
      <dt>Confidence</dt><dd>${escapeHtml(finding.confidence || "Medium")}</dd>
    </dl>
  `;
  return node;
}

function renderList(target, items, fallback) {
  target.innerHTML = "";
  const values = Array.isArray(items) && items.length ? items : [fallback];
  values.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item;
    target.appendChild(li);
  });
}

function buildReportText(report, metadata, visual) {
  const lines = [
    report.title || "AI Audit Video Review",
    "",
    `Overall risk: ${report.overallRiskRating || "Not rated"}`,
    `Video duration: ${formatDuration(metadata.duration)}`,
    `Frames reviewed: ${report.effectiveFramesReviewed || report.framesReviewed || "n/a"}`,
    `Requested frames: ${report.requestedFrames || "n/a"}`,
    "",
    "Executive summary:",
    report.executiveSummary || "No executive summary returned.",
    "",
    "Findings:",
  ];
  if (!report.findings.length) {
    lines.push("No material audit findings were visible in the reviewed frames.");
  } else {
    report.findings.forEach((finding) => {
      lines.push(`- ${finding.id} [${finding.severity}] ${finding.title}`);
      lines.push(`  Evidence: ${withTimestamp(finding.evidence, finding.timestamp)}`);
      lines.push(`  Risk: ${finding.risk}`);
      lines.push(`  Impact: ${finding.impact}`);
      lines.push(`  Recommendation: ${finding.recommendation}`);
    });
  }
  lines.push("", "Limitations:", ...(report.limitations || []));
  return lines.join("\n");
}

function withTimestamp(text, timestamp) {
  return timestamp ? `${text} (${timestamp})` : text;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  }[char]));
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
