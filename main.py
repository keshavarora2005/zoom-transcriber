"""
main.py - FastAPI Web Interface for Zoom Meeting Transcriber

Run with:
    python main.py
    
DO NOT use: uvicorn main:app directly on Windows — use python main.py instead.
"""

# ── Windows asyncio fix — MUST be first, before ANY other imports ─────────────
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ── Rest of imports ───────────────────────────────────────────────────────────
import datetime
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Union

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from zoom_transcriber import (
    parse_zoom_link,
    ChunkWriter,
    run_bot,
    process_recording_to_pdf,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("web_interface")

app = FastAPI(title="Zoom Meeting Transcriber", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

RECORDINGS_DIR = Path("./recordings")
RECORDINGS_DIR.mkdir(exist_ok=True)

jobs: dict = {}

class StartRequest(BaseModel):
    zoom_link: str
    display_name: str = "Recorder"
    max_minutes: int = 180
    headless: bool = True
    skip_transcript: bool = False

async def run_job(job_id: str, req: StartRequest):
    job = jobs[job_id]
    job["logs"] = []

    def log_job(msg: str):
        log.info(f"[{job_id[:8]}] {msg}")
        jobs[job_id].setdefault("logs", []).append(msg)

    try:
        log_job("🔗 Parsing Zoom link...")
        meeting_id, passcode = parse_zoom_link(req.zoom_link)
        job["meeting_id"] = meeting_id
        log_job(f"📋 Meeting ID: {meeting_id}")

        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_dir = RECORDINGS_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        webm_path = out_dir / f"zoom_{meeting_id}_{ts}.webm"
        job["webm_path"] = str(webm_path)
        job["out_dir"] = str(out_dir)

        if not req.skip_transcript and not os.getenv("API_KEY"):
            raise RuntimeError("API_KEY not found in environment. Add it to your .env file.")

        writer = ChunkWriter(webm_path)
        job["status"] = "recording"
        log_job("🎙️ Joining meeting and starting recording...")

        # Create a function to check for manual stop
        def should_stop():
            return jobs[job_id].get("manual_stop", False)

        await run_bot(
            meeting_id=meeting_id,
            passcode=passcode,
            display_name=req.display_name,
            writer=writer,
            headless=req.headless,
            max_min=req.max_minutes,
            debug_dir=None,
            manual_stop_check=should_stop,
        )

        webm = writer.close()
        log_job(f"💾 Recording saved: {webm.name} ({webm.stat().st_size / 1_048_576:.1f} MB)")

        if not webm.exists() or webm.stat().st_size < 512:
            raise RuntimeError("Recording file is missing or too small. No audio was captured.")

        job["webm_size_mb"] = round(webm.stat().st_size / 1_048_576, 2)

        if not req.skip_transcript:
            job["status"] = "transcribing"
            log_job("📤 Uploading audio to AssemblyAI...")
            pdf_path = process_recording_to_pdf(webm, out_dir, meeting_id)
            job["pdf_filename"] = pdf_path.name
            log_job(f"✅ Transcript PDF ready: {pdf_path.name}")
            job["status"] = "done"
            log_job("🎉 All done!")

        else:
            job["status"] = "done"
            log_job("🎉 Recording completed (transcript skipped)")

    except Exception as e:
        log.error(f"Job {job_id} failed: {e}", exc_info=True)
        job["status"] = "error"
        job.setdefault("logs", []).append(f"❌ Error: {e}")
        job["status"] = "failed"

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.post("/api/start")
async def start_job(req: StartRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "id": job_id,
        "status": "starting",
        "zoom_link": req.zoom_link,
        "display_name": req.display_name,
        "created_at": datetime.datetime.now().isoformat(),
        "logs": [],
    }
    background_tasks.add_task(run_job, job_id, req)
    return {"job_id": job_id, "status": "starting"}

@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]

@app.get("/api/job/{job_id}/logs")
async def stream_logs(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    async def event_generator():
        sent = 0
        while True:
            job = jobs.get(job_id, {})
            logs_list = job.get("logs", [])
            while sent < len(logs_list):
                line = logs_list[sent]
                yield f"data: {json.dumps({'log': line, 'status': job.get('status')})}\n\n"
                sent += 1
            if job.get("status") in ("done", "error"):
                yield f"data: {json.dumps({'done': True, 'status': job.get('status'), 'job': job})}\n\n"
                break
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/job/{job_id}/download/{file_type}")
async def download_file(job_id: str, file_type: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    out_dir = Path(job.get("out_dir", ""))
    filename_key = {"pdf": "pdf_filename"}.get(file_type)
    if not filename_key or filename_key not in job:
        raise HTTPException(404, f"No {file_type} file available")
    file_path = out_dir / job[filename_key]
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")
    return FileResponse(path=str(file_path), filename=job[filename_key], media_type="application/octet-stream")

@app.post("/api/job/{job_id}/stop")
async def stop_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    
    job = jobs[job_id]
    if job.get("status") != "recording":
        raise HTTPException(400, "Job is not currently recording")
    
    # Set a flag to indicate manual stop request
    job["manual_stop"] = True
    job["status"] = "stopping"
    
    return {"message": "Stop request sent", "job_id": job_id}

@app.delete("/api/job/{job_id}")
async def delete_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs.pop(job_id)
    out_dir = Path(job.get("out_dir", ""))
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    return {"deleted": job_id}

@app.get("/api/jobs")
async def list_jobs():
    return list(jobs.values())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),  # Use PORT env var
        reload=False,
        loop="none",
    )
