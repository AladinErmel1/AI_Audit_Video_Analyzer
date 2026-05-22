# AI Audit Video Analyzer

Upload a video walkthrough, site inspection, process recording, or screen recording. The app extracts representative frames with FFmpeg, reads them with OpenCV, and uses a vision model to produce internal-audit findings.

The output is an audit report, not a narration script. It highlights visible or reasonably inferable risks, obstacles, safety issues, compliance concerns, security/privacy weaknesses, process gaps, impacts, and remediation recommendations. There is no three-minute script limit.

## Requirements

- Python 3.10+
- FFmpeg and FFprobe on PATH
- Python packages from `requirements.txt`
- `OPENAI_API_KEY` for semantic audit analysis

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run:

```powershell
$env:OPENAI_API_KEY="your_api_key_here"
npm run dev
```

Open [http://localhost:5174](http://localhost:5174).
