# AI Audit Video Analyzer

Upload a video walkthrough, site inspection, process recording, or screen recording. The app extracts representative frames with FFmpeg, reads them with OpenCV, and uses a vision model to produce internal-audit findings.

The output is an audit report, not a narration script. It highlights visible or reasonably inferable risks, obstacles, safety issues, compliance concerns, security/privacy weaknesses, process gaps, impacts, and remediation recommendations. There is no three-minute script limit.

## Requirements

- Python 3.10+
- FFmpeg and FFprobe on PATH
- Python packages from `requirements.txt`
- An OpenAI API key entered in the app, or `OPENAI_API_KEY` configured on the server

Install dependencies (e.g. in your terminal/powershell):

```powershell
pip install -r requirements.txt
```

Run in your terminal (e.g. powershell):

```powershell
npm run dev
```

Open [http://localhost:5174](http://localhost:5174).

## OpenAI API Key

The app supports two options:

1. Enter the OpenAI API key directly in the app before running an analysis. The key is sent to the backend only with that analysis request and is not stored by the app.
2. Configure `OPENAI_API_KEY` as an environment variable on the server. This is useful for deployments such as Railway where you want the app to run without users entering their own key.

For Railway, add an environment variable:

```text
OPENAI_API_KEY=your_api_key_here
```

If `OPENAI_API_KEY` is configured on the server, the in-app key field can be left blank. If a user enters a key in the app, that key is used for that request instead of the server key.
