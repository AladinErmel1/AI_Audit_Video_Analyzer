# Video Interpreter

Upload a video, extract representative frames with FFmpeg, analyze them with OpenCV and a vision model, then generate a spoken audience guide designed to stay under three minutes when read aloud.

## Requirements

- Python 3.10+
- FFmpeg and FFprobe on `PATH`
- Python packages from `requirements.txt`

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

Set your OpenAI API key in your terminal (e.g. powershell) so the app can identify the real content inside the extracted frames:

```powershell
$env:OPENAI_API_KEY="your_api_key_here"
```

Optional:

```powershell
$env:OPENAI_MODEL="gpt-4.1-mini"
```

Run the app:

```powershell
npm run dev
```

Open [http://localhost:5173](http://localhost:5173).
