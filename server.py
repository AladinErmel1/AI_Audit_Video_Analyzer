from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import base64
import cgi
import json
import math
import mimetypes
import os
import shutil
import subprocess
import sys
import time
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
TARGET_SCRIPT_WORDS = 390
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_VISION_MODEL = "gpt-4.1-mini"


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "VideoInterpreter/1.0"

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
                "vision": bool(os.environ.get("OPENAI_API_KEY")),
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
            self.log_error("analysis failed: %s", exc)
            self.respond_json({"error": "Video analysis failed. Check the terminal for details."}, HTTPStatus.INTERNAL_SERVER_ERROR)

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

    form = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers, environ={
        "REQUEST_METHOD": "POST",
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(content_length),
    })

    if "video" not in form:
        raise UserFacingError("Upload field 'video' is required.")

    file_item = form["video"]
    if not getattr(file_item, "filename", None):
        raise UserFacingError("Choose a video file before analyzing.")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    extension = Path(file_item.filename).suffix.lower() or ".mp4"
    upload_path = UPLOAD_DIR / f"{job_id}{extension}"
    frame_path = FRAME_DIR / job_id
    frame_path.mkdir(parents=True, exist_ok=True)

    with upload_path.open("wb") as target:
        shutil.copyfileobj(file_item.file, target)

    metadata = probe_video(upload_path)
    frames = extract_frames(upload_path, frame_path, metadata["duration"])
    if not frames:
        raise UserFacingError("No frames could be extracted from the video.")

    frame_analysis = analyze_frames(frames)
    semantic_analysis = analyze_frame_content(frames, metadata)
    interpretation = interpret_video(metadata, frame_analysis, semantic_analysis)
    script = generate_script(metadata, frame_analysis, interpretation)

    return {
        "jobId": job_id,
        "fileName": Path(file_item.filename).name,
        "metadata": metadata,
        "framesAnalyzed": len(frames),
        "contentFramesAnalyzed": len(semantic_analysis.get("frames", [])),
        "semanticAnalysis": semantic_analysis,
        "interpretation": interpretation,
        "timeline": frame_analysis["timeline"],
        "script": script,
    }


def probe_video(video_path):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
        "-of", "json",
        str(video_path),
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(completed.stdout)
    stream = data.get("streams", [{}])[0]
    duration = float(data.get("format", {}).get("duration") or 0)
    fps = parse_fps(stream.get("avg_frame_rate", "0/1"))
    return {
        "duration": round(duration, 2),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(fps, 2),
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
    target_frames = 30
    seconds_per_frame = max(1, math.ceil(max(duration, 1) / target_frames))
    pattern = output_dir / "frame_%04d.jpg"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(video_path),
        "-vf", f"fps=1/{seconds_per_frame},scale=640:-1",
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
    color_names = []

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
        dominant = dominant_color_name(image)
        motion = 0.0
        if previous_gray is not None:
            motion = float(np.mean(cv2.absdiff(gray, previous_gray)))
        previous_gray = gray

        brightness_values.append(brightness)
        contrast_values.append(contrast)
        motion_values.append(motion)
        color_names.append(dominant)
        timeline.append({
            "frame": index,
            "brightness": round(brightness, 1),
            "contrast": round(contrast, 1),
            "saturation": round(saturation, 1),
            "motion": round(motion, 1),
            "edgeDensity": round(edge_density, 3),
            "dominantColor": dominant,
            "description": describe_frame(brightness, contrast, saturation, motion, edge_density, dominant),
        })

    most_common_color = max(set(color_names), key=color_names.count) if color_names else "neutral"
    return {
        "timeline": timeline,
        "summary": {
            "avgBrightness": round(float(np.mean(brightness_values)), 1),
            "avgContrast": round(float(np.mean(contrast_values)), 1),
            "avgMotion": round(float(np.mean(motion_values)), 1),
            "peakMotion": round(float(np.max(motion_values)), 1),
            "dominantColor": most_common_color,
            "visualPace": classify_pace(float(np.mean(motion_values)), float(np.max(motion_values))),
            "lighting": classify_lighting(float(np.mean(brightness_values))),
            "detailLevel": classify_detail(float(np.mean(contrast_values))),
        },
    }


def analyze_frame_content(frames, metadata):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise UserFacingError("Set OPENAI_API_KEY before analyzing video content. FFmpeg and OpenCV extract the frames; the vision model identifies what is actually in them.")

    selected_frames = select_semantic_frames(frames, max_frames=8)
    content = [{
        "type": "input_text",
        "text": (
            "Analyze these extracted video frames in chronological order. Identify the real visible content, "
            "main subjects, actions, setting, any readable text, mood, and how the story changes over time. "
            "This is for a screen-presenter style voiceover. The final script should sound like the person "
            "is actually presenting the screen in real time, explaining what the audience can see, what each "
            "visible feature is used for, and why choices on screen matter. Use first person naturally where useful, "
            "for example 'Here I can choose...' or 'In this example I use...'. Do not use timestamps, scene labels, "
            "or generic phrases like 'let me walk you through this video'. "
            "Return strict JSON with this shape: "
            "{\"summary\":\"...\",\"setting\":\"...\",\"mainSubjects\":[\"...\"],"
            "\"actions\":[\"...\"],\"visibleText\":[\"...\"],\"storyArc\":\"...\","
            "\"presentationScript\":\"a natural first-person spoken screen presentation under 390 words\","
            "\"frames\":[{\"index\":1,\"timestamp\":\"00:00\",\"description\":\"...\","
            "\"subjects\":[\"...\"],\"actions\":[\"...\"],\"importance\":\"...\"}]}"
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
        "input": [{
            "role": "user",
            "content": content,
        }],
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 1600,
    }

    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise UserFacingError(f"Vision analysis request failed: {detail}") from exc
    except urllib.error.URLError as exc:
        raise UserFacingError(f"Could not reach the vision model API: {exc.reason}") from exc

    text = extract_response_text(data)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {
            "summary": text,
            "setting": "Unknown",
            "mainSubjects": [],
            "actions": [],
            "visibleText": [],
            "storyArc": text,
            "frames": [],
        }

    parsed.setdefault("frames", [])
    parsed.setdefault("summary", "")
    parsed.setdefault("setting", "")
    parsed.setdefault("mainSubjects", [])
    parsed.setdefault("actions", [])
    parsed.setdefault("visibleText", [])
    parsed.setdefault("storyArc", "")
    parsed.setdefault("presentationScript", "")
    return parsed


def select_semantic_frames(frames, max_frames):
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
    raise UserFacingError("The vision model returned no readable text.")


def dominant_color_name(image):
    average = np.mean(image.reshape(-1, 3), axis=0)
    b, g, r = average
    colors = {
        "red/warm": np.array([60, 70, 170]),
        "green/natural": np.array([70, 150, 70]),
        "blue/cool": np.array([170, 100, 70]),
        "yellow/bright": np.array([70, 180, 190]),
        "neutral gray": np.array([125, 125, 125]),
        "dark": np.array([45, 45, 45]),
        "light": np.array([210, 210, 210]),
    }
    sample = np.array([b, g, r])
    return min(colors, key=lambda name: np.linalg.norm(sample - colors[name]))


def describe_frame(brightness, contrast, saturation, motion, edge_density, color):
    light = classify_lighting(brightness)
    pace = "little motion" if motion < 7 else "noticeable motion" if motion < 18 else "strong motion"
    detail = "simple composition" if edge_density < 0.05 else "moderate detail" if edge_density < 0.11 else "dense visual detail"
    mood = "muted" if saturation < 60 else "balanced" if saturation < 110 else "vivid"
    return f"{light} lighting, {pace}, {detail}, {mood} color, with a {color} cast"


def classify_lighting(value):
    if value < 75:
        return "low-key"
    if value > 170:
        return "bright"
    return "balanced"


def classify_pace(avg_motion, peak_motion):
    if avg_motion > 16 or peak_motion > 35:
        return "fast and energetic"
    if avg_motion > 8 or peak_motion > 20:
        return "moderate and active"
    return "calm and steady"


def classify_detail(value):
    if value > 62:
        return "high contrast"
    if value > 38:
        return "moderate contrast"
    return "soft contrast"


def interpret_video(metadata, analysis, semantic):
    summary = analysis["summary"]
    duration = metadata["duration"]
    pacing = summary["visualPace"]
    lighting = summary["lighting"]
    detail = summary["detailLevel"]
    color = summary["dominantColor"]
    structure = infer_structure(analysis["timeline"])
    content_summary = semantic.get("summary") or "The semantic content could not be summarized."
    story_arc = semantic.get("storyArc") or structure
    return {
        "overview": f"The video runs for {format_duration(duration)}. Content analysis: {content_summary}",
        "visualTone": infer_tone(summary),
        "structure": story_arc,
        "setting": semantic.get("setting", ""),
        "mainSubjects": semantic.get("mainSubjects", []),
        "actions": semantic.get("actions", []),
        "visibleText": semantic.get("visibleText", []),
        "frames": semantic.get("frames", []),
        "presentationScript": semantic.get("presentationScript", ""),
        "productionNotes": [
            f"Visual rhythm: {pacing}; lighting: {lighting}; contrast: {detail}; color impression: {color}.",
            f"Resolution: {metadata['width']} x {metadata['height']} at about {metadata['fps']} fps.",
            f"Average motion score: {summary['avgMotion']} with a peak of {summary['peakMotion']}.",
            f"Average brightness: {summary['avgBrightness']} and average contrast: {summary['avgContrast']}.",
        ],
    }


def infer_tone(summary):
    if summary["visualPace"] == "fast and energetic" and summary["lighting"] == "bright":
        return "energetic, direct, and attention-grabbing"
    if summary["lighting"] == "low-key":
        return "quiet, serious, and atmospheric"
    if summary["detailLevel"] == "high contrast":
        return "crisp, dramatic, and visually defined"
    return "clear, observational, and measured"


def infer_structure(timeline):
    if len(timeline) < 3:
        return "The clip is short, so it reads as a single visual moment."
    first = timeline[0]["description"]
    middle = timeline[len(timeline) // 2]["description"]
    last = timeline[-1]["description"]
    return f"It opens with {first}; develops into {middle}; and closes with {last}."


def generate_script(metadata, analysis, interpretation):
    model_script = clean_model_script(interpretation.get("presentationScript", ""))
    if model_script:
        return build_script_response(model_script)

    summary = analysis["summary"]
    subjects = join_items(interpretation.get("mainSubjects", []), "the main visible subjects")
    actions = join_items(interpretation.get("actions", []), "the visible actions")
    visible_text = join_items(interpretation.get("visibleText", []), "no clearly readable text")
    frame_beats = build_presenter_frame_beats(interpretation.get("frames", []))

    script = f"""
Here you can see {subjects} in {interpretation.get('setting') or 'the interface shown on screen'}. The main thing I am showing is {actions}.

{frame_beats}

The important part is what these visible elements allow me to do. I can use the controls on screen to make choices, compare options, and show how the workflow changes depending on the selected settings. If there are tradeoffs, I would explain them directly while the relevant part of the screen is visible, so the audience understands both the feature and the consequence of using it.

Overall, this screen is showing the following flow: {interpretation['structure']}

The visible text I can refer to is: {visible_text}. By the end, the audience should understand what is on the screen, what I am selecting or demonstrating, and why those choices matter.
""".strip()

    return build_script_response(script)


def build_script_response(script):
    words = script.split()
    if len(words) > TARGET_SCRIPT_WORDS:
        script = " ".join(words[:TARGET_SCRIPT_WORDS]).rsplit(".", 1)[0] + "."
    return {
        "title": "Generated Screen Presentation Script",
        "estimatedReadTimeSeconds": estimate_read_time(script),
        "wordCount": len(script.split()),
        "text": script,
    }


def clean_model_script(script):
    text = str(script or "").strip()
    banned_openers = (
        "let me walk you through",
        "in this video, we will",
        "this video shows",
    )
    if any(text.lower().startswith(opener) for opener in banned_openers):
        return ""
    return text


def build_presenter_frame_beats(frames):
    cleaned = [frame for frame in frames if frame.get("description")]
    if not cleaned:
        return "On the screen, I would focus on the visible controls, labels, and user actions, explaining each part as it appears."

    selected = cleaned[:4]
    phrases = []
    starters = [
        "At the start, I focus on",
        "Then I point out",
        "Next I explain",
        "Finally I bring attention to",
    ]
    for index, frame in enumerate(selected):
        description = normalize_frame_description(frame.get("description", ""))
        phrases.append(f"{starters[index]} {description}.")
    return " ".join(phrases)


def normalize_frame_description(description):
    text = str(description).strip().rstrip(".")
    if not text:
        return "the visible action continues"
    lowered = text.lower()
    if lowered.startswith("presenter "):
        return f"the {text}"
    for prefix in ("a ", "an ", "the "):
        if lowered.startswith(prefix):
            return text
    return text


def join_items(items, fallback):
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return fallback
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def select_timeline_points(timeline):
    if not timeline:
        return []
    indexes = sorted(set([0, len(timeline) // 2, len(timeline) - 1]))
    return [timeline[index] for index in indexes]


def estimate_read_time(text):
    return min(180, math.ceil(len(text.split()) / 2.5))


def format_duration(seconds):
    seconds = int(round(seconds))
    minutes, remaining = divmod(seconds, 60)
    if minutes:
        return f"{minutes} minute{'s' if minutes != 1 else ''} and {remaining} second{'s' if remaining != 1 else ''}"
    return f"{remaining} second{'s' if remaining != 1 else ''}"


def main():
    DATA_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", "5173"))
    os.chdir(PUBLIC_DIR)
    server = ThreadingHTTPServer(("127.0.0.1", port), AppHandler)
    print(f"Video Interpreter running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
