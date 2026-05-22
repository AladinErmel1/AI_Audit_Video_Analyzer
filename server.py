from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import base64
import email.message
import email.parser
import email.policy
import json
import math
import mimetypes
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid

try:
    import cv2
    import numpy as np
except ModuleNotFoundError as exc:
    cv2 = None
    np = None
    CV2_IMPORT_ERROR = exc
else:
    CV2_IMPORT_ERROR = None


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
FRAME_DIR = DATA_DIR / "frames"
MAX_UPLOAD_BYTES = 750 * 1024 * 1024
MIN_AUDIT_FRAMES = 4
MAX_AUDIT_FRAMES = 48
DEFAULT_AUDIT_FRAMES = 16
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_VISION_MODEL = "gpt-4.1-mini"


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "AIAuditVideoAnalyzer/1.0"

    def translate_path(self, path):
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("", "/"):
            path = "/index.html"
        safe_path = Path(path.lstrip("/"))
        return str((PUBLIC_DIR / safe_path).resolve())

    def do_GET(self):
        if self.path.startswith("/api/health"):
            self.respond_json({
                "ok": True,
                "opencv": cv2 is not None,
                "ffmpeg": shutil.which("ffmpeg") is not None,
                "serverKeyConfigured": bool(os.environ.get("OPENAI_API_KEY")),
                "clientKeySupported": True,
                "vision": True,
                "model": os.environ.get("OPENAI_MODEL", DEFAULT_VISION_MODEL),
            })
            return
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/analyze":
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return
        try:
            result = handle_upload(self)
            self.respond_json(result)
        except UserFacingError as exc:
            self.respond_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.log_error("audit analysis failed: %s", exc)
            self.respond_json({"error": "Audit video analysis failed. Check the terminal for details."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def respond_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UserFacingError(Exception):
    pass


def handle_upload(handler):
    if cv2 is None:
        raise UserFacingError(f"OpenCV is not installed for this Python environment: {CV2_IMPORT_ERROR}. Run: pip install -r requirements.txt")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise UserFacingError("FFmpeg and FFprobe must be installed and available on PATH.")

    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        raise UserFacingError("No upload received.")
    if content_length > MAX_UPLOAD_BYTES:
        raise UserFacingError("Video is too large. The current limit is 750 MB.")

    content_type = handler.headers.get("Content-Type")
    if not content_type:
        raise UserFacingError("Missing upload content type.")

    form = parse_multipart(handler.rfile, content_type, content_length)

    if "video" not in form:
        raise UserFacingError("Upload field 'video' is required.")

    file_item = form["video"]
    if not file_item.get("filename"):
        raise UserFacingError("Choose a video file before analyzing.")
    audit_frame_count = parse_audit_frame_count(form)
    api_key = parse_openai_api_key(form)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    extension = Path(file_item["filename"]).suffix.lower() or ".mp4"
    upload_path = UPLOAD_DIR / f"{job_id}{extension}"
    frame_path = FRAME_DIR / job_id
    frame_path.mkdir(parents=True, exist_ok=True)

    with upload_path.open("wb") as target:
        target.write(file_item["data"])

    metadata = probe_video(upload_path)
    frames = extract_frames(upload_path, frame_path, metadata["duration"])
    if not frames:
        raise UserFacingError("No frames could be extracted from the video.")

    visual_analysis = analyze_frames(frames)
    audit_report = analyze_audit_content(frames, metadata, visual_analysis, audit_frame_count, api_key)
    effective_audit_frames = audit_report.get("effectiveFramesReviewed", 0)

    return {
        "jobId": job_id,
        "fileName": Path(file_item["filename"]).name,
        "metadata": metadata,
        "framesAnalyzed": len(frames),
        "requestedAuditFrames": audit_frame_count,
        "auditFramesAnalyzed": effective_audit_frames,
        "effectiveAuditFrames": effective_audit_frames,
        "visualAnalysis": visual_analysis,
        "auditReport": audit_report,
    }


def parse_audit_frame_count(form):
    field = form.get("auditFrames")
    raw_value = field["data"].decode("utf-8") if field else str(DEFAULT_AUDIT_FRAMES)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise UserFacingError("Audit frame count must be a number.")
    return max(MIN_AUDIT_FRAMES, min(MAX_AUDIT_FRAMES, value))


def parse_openai_api_key(form):
    field = form.get("openaiApiKey")
    client_key = field["data"].decode("utf-8").strip() if field else ""
    api_key = client_key or os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise UserFacingError("Enter an OpenAI API key in the app or set OPENAI_API_KEY on the server.")
    return api_key


def parse_multipart(rfile, content_type, content_length):
    """Parse a multipart/form-data request body without the removed `cgi` module.

    Returns a dict mapping field name to a dict with keys:
      - "data": bytes  (raw field value)
      - "filename": str | None  (original filename for file fields, else None)
    """
    body = rfile.read(content_length)
    # Synthesise a minimal MIME message so the standard library can split parts.
    mime_message = f"Content-Type: {content_type}\r\n\r\n".encode("latin-1") + body
    parser = email.parser.BytesParser(policy=email.policy.compat32)
    msg = parser.parsebytes(mime_message)

    form = {}
    for part in msg.get_payload():
        if not isinstance(part, email.message.Message):
            continue
        disposition = part.get_param("name", header="content-disposition")
        if disposition is None:
            continue
        filename = part.get_filename()
        data = part.get_payload(decode=True)
        form[disposition] = {"data": data if data is not None else b"", "filename": filename}
    return form


def probe_video(video_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
        "-of", "json", str(video_path),
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(completed.stdout)
    stream = data.get("streams", [{}])[0]
    duration = float(data.get("format", {}).get("duration") or 0)
    return {
        "duration": round(duration, 2),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(parse_fps(stream.get("avg_frame_rate", "0/1")), 2),
    }


def parse_fps(value):
    try:
        numerator, denominator = value.split("/")
        denominator = float(denominator)
        return float(numerator) / denominator if denominator else 0
    except Exception:
        return 0


def extract_frames(video_path, output_dir, duration):
    output_dir.mkdir(parents=True, exist_ok=True)
    target_frames = 48
    seconds_per_frame = max(1, math.ceil(max(duration, 1) / target_frames))
    pattern = output_dir / "frame_%04d.jpg"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path),
        "-vf", f"fps=1/{seconds_per_frame},scale=768:-1",
        "-frames:v", str(target_frames),
        str(pattern),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return sorted(output_dir.glob("frame_*.jpg"))


def analyze_frames(frames):
    timeline = []
    previous_gray = None
    brightness_values = []
    contrast_values = []
    motion_values = []

    for index, frame in enumerate(frames, start=1):
        image = cv2.imread(str(frame))
        if image is None:
            continue
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        brightness = float(np.mean(gray))
        contrast = float(np.std(gray))
        saturation = float(np.mean(hsv[:, :, 1]))
        edge_density = float(np.mean(cv2.Canny(gray, 80, 160) > 0))
        motion = 0.0
        if previous_gray is not None:
            motion = float(np.mean(cv2.absdiff(gray, previous_gray)))
        previous_gray = gray
        brightness_values.append(brightness)
        contrast_values.append(contrast)
        motion_values.append(motion)
        timeline.append({
            "frame": index,
            "brightness": round(brightness, 1),
            "contrast": round(contrast, 1),
            "saturation": round(saturation, 1),
            "motion": round(motion, 1),
            "edgeDensity": round(edge_density, 3),
        })

    return {
        "timeline": timeline,
        "summary": {
            "avgBrightness": round(float(np.mean(brightness_values)), 1) if brightness_values else 0,
            "avgContrast": round(float(np.mean(contrast_values)), 1) if contrast_values else 0,
            "avgMotion": round(float(np.mean(motion_values)), 1) if motion_values else 0,
            "peakMotion": round(float(np.max(motion_values)), 1) if motion_values else 0,
        },
    }


def analyze_audit_content(frames, metadata, visual_analysis, audit_frame_count, api_key):
    selected_frames = select_audit_frames(frames, max_frames=audit_frame_count)
    content = [{
        "type": "input_text",
        "text": (
            "Act as a professional internal auditor reviewing a video walkthrough, site inspection, process recording, "
            "or screen recording. Analyze the extracted frames in chronological order. Look for visible or reasonably "
            "inferable risks, obstacles, control weaknesses, safety issues, compliance concerns, physical security gaps, "
            "data privacy risks, access-control problems, asset protection issues, housekeeping problems, blocked exits, "
            "trip hazards, unclear procedures, missing labels, unsafe behavior, exposed sensitive information, and process "
            "inefficiencies. Review every provided frame in detail before summarizing findings. For each frame, inspect: "
            "people and PPE, walkways and exits, tools/equipment, cables and obstructions, signage and labels, cleanliness, "
            "security controls, screens or documents that may expose sensitive data, access paths, process handoffs, storage "
            "conditions, emergency preparedness, and any missing or weak control evidence. Distinguish confirmed observations "
            "from plausible risks. Build a complete risk inventory before writing the final report. Every visible or reasonably "
            "inferable risk detected in any reviewed frame must be represented in the findings array. Do not omit lower-severity "
            "risks just because higher-severity risks exist. Do not limit the number of findings. If the same risk appears in "
            "multiple frames, consolidate it into one finding and cite the relevant frame numbers or timestamp range in the "
            "evidence. If several observations are related by the same root cause or control weakness, connect them in one "
            "finding and describe the relationship. If observations are unrelated, keep them as separate findings. "
            "Be evidence-based: do not invent findings that are not supported by the frames. Return strict JSON "
            "with this shape: {\"title\":\"...\",\"executiveSummary\":\"...\",\"auditScope\":\"...\","
            "\"overallRiskRating\":\"Low|Medium|High|Critical\",\"framesReviewed\":0,"
            "\"findings\":[{\"id\":\"F-001\",\"title\":\"...\",\"severity\":\"Low|Medium|High|Critical\","
            "\"category\":\"Safety|Operational|Compliance|Security|Privacy|Process|Housekeeping|Other\","
            "\"evidence\":\"detailed observations from one or more frames that support the finding\","
            "\"timestamp\":\"approximate timestamp or frame range if useful\","
            "\"risk\":\"why this matters\",\"impact\":\"potential consequence\","
            "\"recommendation\":\"practical remediation\",\"confidence\":\"Low|Medium|High\"}],"
            "\"positiveControls\":[\"...\"],\"limitations\":[\"...\"]}. "
            "The detailed frame analysis and all detected risks must be reflected inside finding evidence, risk, impact, "
            "and recommendation fields. "
            "Do not return a separate frame observation section. Findings should be specific, actionable, and supported by "
            "what is visible across one or more frames. The findings array is the complete list of detected risks. "
            "If no material risks are visible, return an empty findings array and explain the limitation."
        ),
    }]

    for index, frame in enumerate(selected_frames, start=1):
        content.append({
            "type": "input_text",
            "text": f"Frame {index}, approximate timestamp {frame_timestamp(index, len(selected_frames), metadata['duration'])}.",
        })
        content.append({
            "type": "input_image",
            "image_url": image_data_url(frame),
            "detail": "low",
        })

    payload = {
        "model": os.environ.get("OPENAI_MODEL", DEFAULT_VISION_MODEL),
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 6000,
    }

    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise UserFacingError(f"Vision audit request failed: {detail}") from exc
    except urllib.error.URLError as exc:
        raise UserFacingError(f"Could not reach the vision model API: {exc.reason}") from exc

    report = parse_audit_report(extract_response_text(data))
    report["modelReportedFramesReviewed"] = report.get("framesReviewed")
    report["framesReviewed"] = len(selected_frames)
    report["effectiveFramesReviewed"] = len(selected_frames)
    report["requestedFrames"] = audit_frame_count
    report["extractedFramesAvailable"] = len(frames)
    report["visualMetrics"] = visual_analysis.get("summary", {})
    return report


def parse_audit_report(text):
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        report = {
            "title": "AI Audit Video Review",
            "executiveSummary": text,
            "auditScope": "Video walkthrough review based on extracted frames.",
            "overallRiskRating": "Medium",
            "framesReviewed": 0,
            "findings": [],
            "positiveControls": [],
            "limitations": ["The model response was not valid JSON, so only the summary could be preserved."],
        }

    report.setdefault("title", "AI Audit Video Review")
    report.setdefault("executiveSummary", "")
    report.setdefault("auditScope", "Video walkthrough review based on extracted frames.")
    report.setdefault("overallRiskRating", "Medium")
    report.setdefault("framesReviewed", 0)
    report.setdefault("findings", [])
    report.setdefault("positiveControls", [])
    report.setdefault("limitations", [])

    normalized = []
    for index, finding in enumerate(report.get("findings") or [], start=1):
        normalized.append({
            "id": finding.get("id") or f"F-{index:03d}",
            "title": finding.get("title") or "Untitled finding",
            "severity": finding.get("severity") or "Medium",
            "category": finding.get("category") or "Other",
            "evidence": finding.get("evidence") or "Evidence not specified.",
            "timestamp": finding.get("timestamp") or "",
            "risk": finding.get("risk") or "Risk not specified.",
            "impact": finding.get("impact") or "Impact not specified.",
            "recommendation": finding.get("recommendation") or "Review and remediate as appropriate.",
            "confidence": finding.get("confidence") or "Medium",
        })
    report["findings"] = normalized
    return report


def select_audit_frames(frames, max_frames):
    if len(frames) <= max_frames:
        return frames
    indexes = np.linspace(0, len(frames) - 1, max_frames, dtype=int)
    return [frames[int(index)] for index in indexes]


def frame_timestamp(index, total, duration):
    seconds = 0 if total <= 1 else round(((index - 1) / (total - 1)) * duration)
    minutes, remaining = divmod(int(seconds), 60)
    return f"{minutes:02d}:{remaining:02d}"


def image_data_url(frame):
    mime_type = mimetypes.guess_type(frame.name)[0] or "image/jpeg"
    encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def extract_response_text(data):
    if data.get("output_text"):
        return data["output_text"]
    chunks = []
    for output in data.get("output", []):
        for content in output.get("content", []):
            if content.get("type") in ("output_text", "text") and content.get("text"):
                chunks.append(content["text"])
    if chunks:
        return "\n".join(chunks)
    raise UserFacingError("The vision model returned no readable audit report.")


def format_duration(seconds):
    seconds = int(round(seconds))
    minutes, remaining = divmod(seconds, 60)
    if minutes:
        return f"{minutes} minute{'s' if minutes != 1 else ''} and {remaining} second{'s' if remaining != 1 else ''}"
    return f"{remaining} second{'s' if remaining != 1 else ''}"


def main():
    DATA_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", "5174"))
    os.chdir(PUBLIC_DIR)
    server = ThreadingHTTPServer(("0.0.0.0", port), AppHandler)
    print(f"AI Audit Video Analyzer running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
