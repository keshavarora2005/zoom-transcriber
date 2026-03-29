# 🎙️ Zoom Meeting Transcriber

A headless bot that **joins Zoom meetings**, records audio via the browser's Web Audio API, and produces a **speaker-labelled PDF transcript** — all through Zoom's web client, no desktop app or Zoom SDK required.

---

## ✨ Features

- **Fully automated** — paste a Zoom link and walk away
- **No Zoom SDK** — uses Playwright to drive the browser web client
- **Speaker diarisation** — powered by [AssemblyAI](https://www.assemblyai.com/)
- **PDF output** — clean, formatted transcript with speaker labels
- **FastAPI web UI** — start/stop jobs, stream live logs, download transcripts
- **Persistent jobs** — job state survives server restarts (Render disk-backed)

---

## 🗂️ Project Structure

```
.
├── zoom_transcriber.py   # Core bot: joins Zoom, records audio, transcribes
├── main.py               # FastAPI web interface & job management API
├── templates/            # Jinja2 HTML templates
├── static/               # JS / CSS assets
├── requirements.txt      # Python dependencies
├── .gitignore
```

---

## 🚀 Quick Start

### 1. Prerequisites

| Tool | Version |
|------|---------|
| Python | 3.11+ |
| ffmpeg | any recent |
| Playwright Chromium | installed via `playwright install` |

### 2. Clone & install

```bash
git clone https://github.com/keshavarora2005/<repo-name>.git
cd <repo-name>

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure environment

Create a `.env` file in the project root:

```env
API_KEY=your_assemblyai_api_key_here
```

Get a free API key at [assemblyai.com](https://www.assemblyai.com/).

### 4. Run

**Command-line (single meeting):**

```bash
python zoom_transcriber.py "https://zoom.us/j/123456789?pwd=abc123"
```

**Web interface:**

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
# Open http://localhost:8000
```

---

## 🖥️ CLI Reference

```
python zoom_transcriber.py [zoom_link] [options]

Options:
  --name NAME           Display name shown in the meeting  (default: Recorder)
  --out DIR             Output directory for recordings    (default: ./recordings)
  --max-wait MINUTES    Maximum recording duration         (default: 180)
  --no-headless         Show the browser window (debug)
  --no-mp3              Skip WebM → MP3 conversion
  --no-transcript       Save audio only, skip transcription
  --debug               Save screenshots at each state change
```

---

## 🌐 Web API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/start` | Start a new recording job |
| `GET` | `/api/job/{id}` | Get job status & metadata |
| `GET` | `/api/job/{id}/logs` | Stream live logs (SSE) |
| `POST` | `/api/job/{id}/stop` | Request graceful stop |
| `GET` | `/api/job/{id}/download/pdf` | Download transcript PDF |
| `DELETE` | `/api/job/{id}` | Delete job & files |
| `GET` | `/api/jobs` | List all jobs |

**Start request body:**

```json
{
  "zoom_link": "https://zoom.us/j/123456789?pwd=abc",
  "display_name": "Recorder",
  "max_minutes": 180,
  "headless": true,
  "skip_transcript": false
}
```


## ⚙️ How It Works

```
Zoom link
   │
   ▼
Playwright (headless Chromium)
   │  injects JS Web Audio hook
   │  joins meeting as display_name
   │
   ▼
RTCPeerConnection tracks captured
   │  streamed as base64 chunks via console.log
   │
   ▼
ChunkWriter → zoom_<id>_<ts>.webm
   │
   ▼
ffmpeg → .mp3
   │
   ▼
AssemblyAI upload → transcribe (speaker diarisation)
   │
   ▼
ReportLab → PDF transcript
```

---

## 🔒 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `API_KEY` | ✅ Yes | AssemblyAI API key |
| `RECORDINGS_DIR` | No | Override recordings path (default `./recordings`) |
| `PORT` | No | Web server port (default `10000`) |

---

## 📋 Requirements

See [`requirements.txt`](requirements.txt). Key dependencies:

- `playwright` — browser automation
- `fastapi` + `uvicorn` — web interface
- `requests` — AssemblyAI API calls
- `reportlab` — PDF generation
- `python-dotenv` — environment config

---

## ⚠️ Limitations & Notes

- The bot joins as a **participant** — the meeting host must admit it if a waiting room is enabled.
- Recording quality depends on the browser's Web Audio capture; background noise from other tabs may bleed in.
- AssemblyAI's free tier has monthly minute limits; check your usage at [app.assemblyai.com](https://app.assemblyai.com/).
- This tool is intended for **meetings you are authorised to record**. Always comply with your organisation's recording policies and inform participants where required by law.

---

## 🤝 Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you'd like to change.

---

## 📄 License

MIT
