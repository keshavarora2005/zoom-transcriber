"""
zoom_transcriber.py - Combined Zoom Meeting Recorder and Transcriber
======================================================================
Records Zoom meeting audio and transcribes it to PDF in one integrated workflow.

INSTALL
-------
    pip install playwright moviepy requests reportlab python-dotenv
    playwright install chromium
    
    # Optional (for wav/mp3 output):
    #   Windows : winget install ffmpeg
    #   Linux   : sudo apt install ffmpeg

USAGE
-----
    python zoom_transcriber.py "https://us04web.zoom.us/j/XXXX?pwd=YYY"
    python zoom_transcriber.py "https://..." --no-headless   # see the browser
    python zoom_transcriber.py "https://..." --debug         # save screenshots
    python zoom_transcriber.py "https://..." --help

ENVIRONMENT
-----------
Create a .env file with:
    API_KEY=your_assemblyai_api_key
"""

import argparse
import asyncio
import base64
import datetime
import logging
import re
import shutil
import signal
import subprocess
import sys
import time
import tempfile
import os
import requests
from pathlib import Path
from typing import Union, Callable, List
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, BrowserContext, async_playwright
# from moviepy.editor import VideoFileClip  # Not needed for audio-only processing
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from dotenv import load_dotenv

load_dotenv()
os.environ["API_KEY"] = os.getenv("API_KEY")

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zoom_transcriber")

# ── AssemblyAI Configuration ─────────────────────────────────────────────────
base_url = "https://api.assemblyai.com/v2"

# ─────────────────────────────────────────────────────────────────────────────
# JS hook - injected via add_init_script() so it runs BEFORE any page JS.
# Strategy: Record Zoom meeting audio by hooking RTCPeerConnection
# ─────────────────────────────────────────────────────────────────────────────

INIT_SCRIPT = r"""
(function() {
    // ── shared state ──────────────────────────────────────────────────────
    window.__ZR = window.__ZR || {
        recorder:    null,
        destStream:  null,
        ctx:         null,
        dest:        null,
        connected:   new WeakSet(),
        trackCount:  0,
    };
    const ZR = window.__ZR;

    // ── lazy-initialise AudioContext + MediaRecorder ───────────────────────
    function ensureRecorder() {
        if (ZR.recorder && ZR.recorder.state !== 'inactive') return;

        ZR.ctx  = new AudioContext({ sampleRate: 44100 });
        ZR.dest = ZR.ctx.createMediaStreamDestination();

        const mime = ['audio/webm;codecs=opus','audio/webm','audio/ogg']
                        .find(m => MediaRecorder.isTypeSupported(m)) || '';

        ZR.recorder = new MediaRecorder(ZR.dest.stream, mime ? {mimeType: mime} : {});
        ZR.recorder.ondataavailable = async (e) => {
            if (!e.data || e.data.size === 0) return;
            const ab  = await e.data.arrayBuffer();
            const u8  = new Uint8Array(ab);
            // btoa in chunks to avoid stack overflow on large buffers
            let bin = '';
            for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
            console.log('AUDIO_CHUNK::' + btoa(bin));
        };
        ZR.recorder.onstart  = () => console.log('ZOOM_REC::recorder_started mime=' + (mime || 'default'));
        ZR.recorder.onerror  = (e) => console.log('ZOOM_REC::recorder_error ' + e.error);
        ZR.recorder.start(1000);
    }

    // ── Suppress beep/notification sounds ─────────────────────────────────────
    const NativeOscillator = window.OscillatorNode;
    const NativeAudioBufferSource = window.AudioBufferSourceNode;
    const NativeGain = window.GainNode;

    // Patch createOscillator — kills "ding" tones
    const _createOscillator = AudioContext.prototype.createOscillator;
    AudioContext.prototype.createOscillator = function() {
        const osc = _createOscillator.apply(this, arguments);
        osc.frequency.value = 0;
        const origConnect = osc.connect.bind(osc);
        osc.connect = function(target) {
            if (target === window.__ZR && target === window.__ZR.dest) {
                return origConnect(target);
            }
            return origConnect(target);
        };
        return osc;
    };

    const _createBufferSource = AudioContext.prototype.createBufferSource;
    AudioContext.prototype.createBufferSource = function() {
        const src = _createBufferSource.apply(this, arguments);
        const origStart = src.start.bind(src);
        src.start = function(when, offset, duration) {
            if (src.buffer && src.buffer.duration < 2.0) {
                src.disconnect();
                console.log('ZOOM_REC::beep_suppressed duration=' + (src.buffer.duration).toFixed(3));
                return;
            }
            return origStart(when, offset, duration);
        };
        return src;
    };

    // ── connect a MediaStreamTrack to the recorder ─────────────────────────
    function connectTrack(track, label) {
        if (ZR.connected.has(track)) return;
        ZR.connected.add(track);
        if (track.kind !== 'audio') return;

        ensureRecorder();

        const ms  = new MediaStream([track]);
        const src = ZR.ctx.createMediaStreamSource(ms);
        src.connect(ZR.dest);
        ZR.trackCount++;
        console.log('ZOOM_REC::track_connected label=' + label + ' total=' + ZR.trackCount);
    }

    // ── mute any <audio>/<video> DOM elements Zoom might use ──────────────
    function silenceElement(el) {
        el.muted  = true;
        el.volume = 0;
        if (el.srcObject) el.srcObject.getAudioTracks().forEach(t => connectTrack(t, 'elem_muted_' + t.id));
    }

    // ── hook RTCPeerConnection (the main path for Zoom WebRTC audio) ───────
    const NativePeerConn = window.RTCPeerConnection;
    window.RTCPeerConnection = function(...args) {
        const pc = new NativePeerConn(...args);
        pc.addEventListener('track', (e) => {
            const track = e.track;
            connectTrack(track, 'rtc_ontrack_' + track.id);
            track.addEventListener('unmute', () => {
                connectTrack(track, 'rtc_unmute_' + track.id);
            });
        });
        return pc;
    };
    window.RTCPeerConnection.prototype = NativePeerConn.prototype;
    Object.getOwnPropertyNames(NativePeerConn).forEach(k => {
        try { window.RTCPeerConnection[k] = NativePeerConn[k]; } catch(_) {}
    });

    // ── also hook getUserMedia (local fallback path) ───────────────────────
    const origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (constraints) => {
        const stream = await origGUM(constraints);
        stream.getAudioTracks().forEach(t => connectTrack(t, 'gum_' + t.id));
        return stream;
    };

    // ── hook <audio>/<video> elements as a fallback ────────────────────────
    function tryConnectElement(el) {
        if (ZR.connected.has(el)) return;
        ZR.connected.add(el);
        try { silenceElement(el); } catch(e) {}
    }

    const mo = new MutationObserver(muts => {
        for (const m of muts)
            for (const n of m.addedNodes) {
                if (n.nodeName === 'AUDIO' || n.nodeName === 'VIDEO') tryConnectElement(n);
                else if (n.querySelectorAll) n.querySelectorAll('audio,video').forEach(tryConnectElement);
            }
    });
    if (document.body) {
        mo.observe(document.body, { childList: true, subtree: true });
    } else {
        document.addEventListener('DOMContentLoaded', () => {
            mo.observe(document.body, { childList: true, subtree: true });
        });
    }

    console.log('ZOOM_REC::hook_installed');
})();
"""

STOP_JS = """
(function(){
    const r = window.__ZR && window.__ZR.recorder;
    if (r && r.state !== 'inactive') { r.stop(); return 'stopped'; }
    return 'already_inactive';
})();
"""

# ─────────────────────────────────────────────────────────────────────────────
# URL parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_zoom_link(url: str) -> tuple[str, Union[str, None]]:
    url = url.strip()
    if re.fullmatch(r"[\d\s\-]+", url):
        return re.sub(r"\D", "", url), None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if parsed.scheme == "zoommtg":
        mid = qs.get("confno", [None])[0]
        if not mid:
            raise ValueError("Cannot parse meeting ID from zoommtg:// link")
        return mid, qs.get("pwd", [None])[0]
    m = re.search(r"/j/(\d+)", parsed.path)
    if not m:
        raise ValueError(f"No meeting ID found in: {url}")
    return m.group(1), qs.get("pwd", [None])[0]

def build_join_url(meeting_id: str, passcode: Union[str, None]) -> str:
    url = f"https://app.zoom.us/wc/join/{meeting_id}"
    return url + (f"?pwd={passcode}" if passcode else "")

# ─────────────────────────────────────────────────────────────────────────────
# Page state machine
# ─────────────────────────────────────────────────────────────────────────────

STATES = {
    "name_input":     "input#inputname, input[placeholder*='Your Name' i], input[placeholder*='name' i], input[placeholder*='Enter your name' i], input[type='text']",
    "passcode_input": "input#inputpasscode, input[placeholder*='passcode' i], input[placeholder*='password' i]",
    "waiting_room":   "[class*='waitingRoom'], [class*='waiting-room'], div:has-text('Waiting for host')",
    "audio_dialog":   "[class*='join-audio-by-voip'], button:has-text('Join with Computer Audio'), button:has-text('Join Audio'), button:has-text('Test Speaker')",
    "in_meeting":     "#wc-footer, [class*='footer-container'], [class*='meeting-app'], div[role='main']",
    "meeting_ended":  "[class*='meeting-ended'], div:has-text('meeting has been ended by the host'), div:has-text('This meeting has been ended')",
    "error":          "[class*='error-container'], div:has-text('invalid meeting'), div:has-text('This meeting ID is not valid')",
}

async def get_state(page: Page) -> str:
    for name, sel in STATES.items():
        try:
            if await page.query_selector(sel):
                return name
        except Exception:
            pass
    return "unknown"

async def wait_state(
    page: Page,
    want: List[str],
    timeout_sec: int = 60,
    poll: float = 2.0,
    debug_dir: Union[Path, None] = None,
    tag: str = "",
) -> str:
    deadline = time.time() + timeout_sec
    last     = "unknown"
    n        = 0
    while time.time() < deadline:
        s = await get_state(page)
        if s != last:
            log.info(f"  State {last!r} → {s!r}  {tag}")
            last = s
            if debug_dir:
                n += 1
                try:
                    await page.screenshot(path=str(debug_dir / f"{n:02d}_{s}.png"))
                except Exception:
                    pass
        if s in want:
            return s
        if s == "error":
            return "error"
        await asyncio.sleep(poll)
    log.warning(f"Timeout waiting for {want}  last={last!r}")
    if debug_dir:
        try:
            await page.screenshot(path=str(debug_dir / "timeout.png"))
        except Exception:
            pass
    return "timeout"

# ─────────────────────────────────────────────────────────────────────────────
# Chunk writer
# ─────────────────────────────────────────────────────────────────────────────

class ChunkWriter:
    """Receives base64 audio/webm chunks from the browser and appends to file."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path   = path
        self._fh    = open(path, "wb")
        self.chunks = 0
        self.bytes  = 0

    def write(self, b64: str):
        try:
            data = base64.b64decode(b64)
            self._fh.write(data)
            self._fh.flush()
            self.chunks += 1
            self.bytes  += len(data)
        except Exception as e:
            log.warning(f"Chunk error: {e}")

    def close(self) -> Path:
        self._fh.close()
        mb = self.path.stat().st_size / 1_048_576
        log.info(f"Closed: {self.path}  ({self.chunks} chunks, {mb:.2f} MB)")
        return self.path

    @property
    def duration_est(self) -> float:
        return float(self.chunks)   # ≈ seconds (1 chunk/s)

# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg conversions
# ─────────────────────────────────────────────────────────────────────────────

def _run_ffmpeg(args: List[str]) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    r = subprocess.run(["ffmpeg", "-y"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        log.warning(f"ffmpeg failed:\n{r.stderr[-400:]}")
        return False
    return True

def to_wav(src: Path) -> Union[Path, None]:
    out = src.with_suffix(".wav")
    ok  = _run_ffmpeg(["-i", str(src), "-ar", "44100", "-ac", "2", str(out)])
    if ok:
        log.info(f"WAV: {out}  ({out.stat().st_size/1_048_576:.1f} MB)")
        return out
    return None

def to_mp3(src: Path, bitrate: str = "128k") -> Union[Path, None]:
    out = src.with_suffix(".mp3")
    ok  = _run_ffmpeg(["-i", str(src), "-codec:a", "libmp3lame", "-b:a", bitrate, str(out)])
    if ok:
        log.info(f"MP3: {out}  ({out.stat().st_size/1_048_576:.1f} MB)")
        return out
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Transcription functions
# ─────────────────────────────────────────────────────────────────────────────

def upload_audio_file(file_path):
    """Upload local audio file to AssemblyAI"""
    headers = {"authorization": os.getenv("API_KEY")}
    
    with open(file_path, "rb") as f:
        response = requests.post(base_url + "/upload", 
                               headers=headers, 
                               files={"file": f})
    
    if response.status_code == 200:
        audio_url = response.json()["upload_url"]
        log.info(f"📤 Upload successful: {audio_url}")
        return audio_url
    else:
        raise RuntimeError(f"❌ Upload failed: {response.status_code} - {response.text}")

def transcribe_audio(audio_url):
    """Transcribe audio using AssemblyAI API with SPEAKER LABELS"""
    headers = {"authorization": os.getenv("API_KEY"), "content-type": "application/json"}
    data = {
        "audio_url": audio_url,
        "speech_models": ["universal"],
        "language_detection": True,
        "punctuate": True,
        "format_text": True,
        "speaker_labels": True,
    }
    
    log.info("🔄 Submitting transcription request with speaker labels...")
    response = requests.post(base_url + "/transcript", json=data, headers=headers)
    
    if response.status_code != 200:
        raise RuntimeError(f"❌ Transcription request failed: {response.status_code} - {response.text}")
    
    response_data = response.json()
    if 'id' not in response_data:
        raise RuntimeError(f"❌ Invalid response from AssemblyAI: {response_data}")
    
    transcript_id = response_data['id']
    log.info(f"📄 Transcription ID: {transcript_id}")
    
    polling_endpoint = base_url + f"/transcript/{transcript_id}"
    while True:
        result = requests.get(polling_endpoint, headers=headers).json()
        
        if result['status'] == 'completed':
            log.info("✅ Transcription complete with speakers!")
            return result
        elif result['status'] == 'error':
            raise RuntimeError(f"❌ Transcription failed: {result['error']}")
        else:
            log.info(f"⏳ Processing... ({result['status']})")
            time.sleep(3)

def format_speaker_transcript(result):
    """Convert AssemblyAI result to speaker-formatted text"""
    if 'utterances' not in result:
        log.warning("⚠️ No speaker data found - falling back to plain text")
        return result.get('text', 'No transcript available')
    
    # Debug: Log the number of speakers detected
    speakers = set()
    for utterance in result['utterances']:
        speakers.add(utterance.get('speaker', 'Unknown'))
    log.info(f"🎯 Detected {len(speakers)} speakers: {sorted(speakers)}")
    
    formatted_lines = []
    for utterance in result['utterances']:
        speaker = utterance.get('speaker', 'Unknown')
        text = utterance.get('text', '').strip()
        if text:
            formatted_lines.append(f"Speaker {speaker}: {text}")
    
    return '\n\n'.join(formatted_lines)

def save_transcript_as_pdf(result, output_pdf="meeting_transcript.pdf"):
    """Save SPEAKER transcript as formatted PDF"""
    log.info("📄 Creating PDF with speaker labels...")
    
    doc = SimpleDocTemplate(output_pdf, pagesize=letter)
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Title'],
        fontSize=24,
        spaceAfter=30,
        alignment=1
    )
    
    story = []
    story.append(Paragraph("Meeting Transcript with Speaker Labels", title_style))
    story.append(Spacer(1, 20))
    
    speaker_formatted_text = format_speaker_transcript(result)
    
    content_style = ParagraphStyle(
        'SpeakerContent',
        parent=styles['Normal'],
        fontSize=11,
        spaceAfter=8,
        leftIndent=20
    )
    
    paragraphs = speaker_formatted_text.split('\n\n')
    for para in paragraphs:
        if para.strip():
            clean_para = para.strip()
            clean_para = ''.join(char for char in clean_para if char.isprintable() or char in '\n\t')
            clean_para = clean_para.replace('\x00', '').replace('\ufffd', '')
            
            story.append(Paragraph(clean_para, content_style))
    
    doc.build(story)
    log.info(f"✅ Speaker PDF saved: {output_pdf}")

# ─────────────────────────────────────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────────────────────────────────────

async def run_bot(
    meeting_id:   str,
    passcode:     Union[str, None],
    display_name: str,
    writer:       ChunkWriter,
    headless:     bool,
    max_min:      int,
    debug_dir:    Union[Path, None],
    manual_stop_check: Union[Callable, None] = None,
):
    join_url = build_join_url(meeting_id, passcode)
    log.info(f"Join URL: {join_url}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--mute-audio",
                "--use-fake-device-for-media-stream",
            ],
        )

        ctx: BrowserContext = await browser.new_context(
            permissions=["microphone", "camera"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

        await ctx.add_init_script(INIT_SCRIPT)
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )

        page = await ctx.new_page()

        track_count = {"n": 0}

        def on_console(msg):
            text = msg.text
            if text.startswith("AUDIO_CHUNK::"):
                writer.write(text[13:])
            elif text.startswith("ZOOM_REC::"):
                info = text[10:]
                log.info(f"  [js] {info}")
                if "track_connected" in info:
                    track_count["n"] += 1
            elif debug_dir:
                log.debug(f"  [browser {msg.type}] {text[:200]}")

        page.on("console",   on_console)
        page.on("pageerror", lambda e: log.debug(f"  [page error] {e}"))

        log.info("Opening Zoom Web Client …")
        try:
            await page.goto(join_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            log.error(f"Navigation error: {e}")
            await browser.close()
            return

        await asyncio.sleep(3)
        log.info(f"URL after load: {page.url}")
        
        # Debug: print page title and look for any forms
        try:
            title = await page.title()
            log.info(f"Page title: {title}")
            
            # Look for any input fields
            inputs = await page.query_selector_all("input")
            log.info(f"Found {len(inputs)} input fields")
            for i, inp in enumerate(inputs[:3]):  # Show first 3
                placeholder = await inp.get_attribute("placeholder") or "no placeholder"
                input_type = await inp.get_attribute("type") or "no type"
                log.info(f"  Input {i+1}: type='{input_type}', placeholder='{placeholder}'")
            
            # Look for any buttons
            buttons = await page.query_selector_all("button")
            log.info(f"Found {len(buttons)} buttons")
            for i, btn in enumerate(buttons[:5]):  # Show first 5
                text = await btn.text_content() or ""
                log.info(f"  Button {i+1}: '{text}'")
                
        except Exception as e:
            log.warning(f"Debug info failed: {e}")

        if debug_dir:
            await page.screenshot(path=str(debug_dir / "00_initial.png"))

        # Dismiss consent / cookie banners
        for sel in ['button:has-text("I Agree")', 'button:has-text("Accept")']:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    await asyncio.sleep(1)
                    log.info(f"  Consent dismissed")
                    break
            except Exception:
                pass

        # Check if already in meeting first
        current_state = await get_state(page)
        log.info(f"Current state: {current_state}")
        
        if current_state in ["in_meeting", "audio_dialog"]:
            log.info("Already in meeting, skipping name input")
        else:
            # Enter name
            log.info(f"Waiting for name input … (will enter '{display_name}')")
            ns = await wait_state(page, ["name_input"], 20, debug_dir=debug_dir, tag="name")

            if ns == "name_input":
                for sel in [
                    "input#inputname",
                    "input[placeholder*='Your Name' i]",
                    "input[placeholder*='name' i]",
                    "input[placeholder*='Enter your name' i]",
                    "input[type='text']",
                ]:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        await el.fill("")  # Clear any existing content
                        await el.type(display_name, delay=80)
                        log.info(f"  Name entered ({sel})")
                        break

                await asyncio.sleep(0.5)
                for sel in [
                    "button#joinBtn", 
                    "button:has-text('Join')", 
                    "button[type='submit']",
                    "button[aria-label*='Join' i]",
                    ".join-btn",
                    "button:has-text('Join Meeting')",
                ]:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        log.info(f"  Join clicked ({sel})")
                        break
                await asyncio.sleep(2)
            else:
                log.warning(f"Name field not shown ({ns}) — Zoom may have auto-joined.")
                if debug_dir:
                    await page.screenshot(path=str(debug_dir / "no_name.png"))
                
                # Fallback: try to find any text input and enter name
                log.info("Trying fallback approach to find name input...")
                try:
                    # Look for any input field
                    inputs = await page.query_selector_all("input[type='text'], input:not([type])")
                    for input_el in inputs:
                        placeholder = await input_el.get_attribute("placeholder") or ""
                        if "name" in placeholder.lower() or "enter" in placeholder.lower() or not placeholder:
                            await input_el.click()
                            await input_el.fill("")  # Clear any existing content
                            await input_el.type(display_name, delay=80)
                            log.info(f"  Name entered using fallback (placeholder: {placeholder})")
                            break
                    
                    # Look for any join button
                    await asyncio.sleep(0.5)
                    buttons = await page.query_selector_all("button")
                    for btn in buttons:
                        text = await btn.text_content() or ""
                        if "join" in text.lower():
                            await btn.click()
                            log.info(f"  Join clicked using fallback (text: {text})")
                            break
                    await asyncio.sleep(2)
                except Exception as e:
                    log.warning(f"Fallback approach failed: {e}")

        # Passcode
        if await get_state(page) == "passcode_input":
            log.info("Passcode prompt …")
            if passcode:
                await page.fill("input#inputpasscode", passcode)
                await page.click("button:has-text('Join'), button[type='submit']")
                await asyncio.sleep(2)
            else:
                log.error("Passcode required but none provided!")

        # Wait for admission
        log.info("Waiting for admission / meeting start …")
        s = await wait_state(
            page,
            ["waiting_room", "audio_dialog", "in_meeting", "meeting_ended"],
            timeout_sec=max_min * 60,
            poll=3,
            debug_dir=debug_dir,
            tag="admission",
        )

        if s == "waiting_room":
            log.info("In waiting room …")
            s = await wait_state(
                page,
                ["audio_dialog", "in_meeting", "meeting_ended"],
                timeout_sec=max_min * 60,
                poll=5,
                debug_dir=debug_dir,
                tag="waiting_room",
            )

        if s in ("timeout", "error", "meeting_ended"):
            log.error(f"Cannot proceed — state: {s!r}")
            await browser.close()
            return

        # Join computer audio
        log.info("Joining with computer audio …")
        for _ in range(3):
            try:
                for sel in [
                    "[class*='join-audio-by-voip'] button",
                    "button:has-text('Join with Computer Audio')",
                    "button:has-text('Computer Audio')",
                    "[aria-label*='join audio' i]",
                    "button:has-text('Join Audio')",
                ]:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        log.info(f"  Audio clicked ({sel})")
                        await asyncio.sleep(1.5)
                        break
                for sel in [
                    "button:has-text('Join with Computer Audio')",
                    "button:has-text('Computer Audio')",
                ]:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(1)
                        break
                break
            except Exception as e:
                log.warning(f"  Audio join error: {e}")
                await asyncio.sleep(2)

        if debug_dir:
            await page.screenshot(path=str(debug_dir / "after_audio_join.png"))

        # Confirm in meeting
        s = await wait_state(page, ["in_meeting", "meeting_ended"], 20, debug_dir=debug_dir)
        log.info(f"  In-meeting state: {s!r}")

        # Wait for first track
        log.info("Waiting for audio tracks to connect (up to 30s) …")
        track_deadline = time.time() + 30
        while time.time() < track_deadline:
            if track_count["n"] > 0:
                log.info(f"  Got {track_count['n']} audio track(s) — recording active.")
                break
            await asyncio.sleep(1)
        else:
            log.warning(
                "No audio tracks captured after 30s.\n"
                "  Possible causes:\n"
                "  1. Meeting has no active speakers (everyone muted).\n"
                "  2. Zoom routed audio differently — check --debug screenshots.\n"
                "  3. The meeting host hasn't started yet.\n"
                "  Recording will continue — tracks may connect later."
            )

        log.info(f"Recording in progress … (Ctrl-C to stop, max {max_min} min)")
        deadline = time.time() + max_min * 60
        last_log = time.time()

        # Main wait loop
        while time.time() < deadline:
            # Check for manual stop request
            if manual_stop_check and manual_stop_check():
                log.info("🛑 Manual stop request received, finishing recording...")
                break
                
            s = await get_state(page)
            if s == "meeting_ended":
                log.info("Meeting ended signal detected.")
                break
            if "zoom.us" not in page.url and "app.zoom" not in page.url:
                log.info(f"Browser navigated away: {page.url}")
                break
            if time.time() - last_log >= 60:
                log.info(
                    f"  Still recording … "
                    f"{writer.duration_est/60:.1f} min, "
                    f"{writer.bytes/1_048_576:.1f} MB, "
                    f"{track_count['n']} track(s)"
                )
                last_log = time.time()
            await asyncio.sleep(5)
        else:
            log.warning(f"Max wait ({max_min} min) reached.")

        # Stop
        log.info("Stopping recorder …")
        try:
            r = await page.evaluate(STOP_JS)
            log.info(f"  Recorder: {r}")
            await asyncio.sleep(1.5)
        except Exception as e:
            log.warning(f"  Stop error: {e}")

        if debug_dir:
            try:
                await page.screenshot(path=str(debug_dir / "final.png"))
            except Exception:
                pass

        await browser.close()
        log.info("Done.")

# ─────────────────────────────────────────────────────────────────────────────
# Main workflow
# ─────────────────────────────────────────────────────────────────────────────

def process_recording_to_pdf(webm_path: Path, output_dir: Path, meeting_id: str) -> Path:
    """Process recorded audio to transcript PDF"""
    log.info("🔄 Starting transcription process...")
    
    # Convert to MP3 for better compatibility
    mp3_path = to_mp3(webm_path)
    if not mp3_path:
        log.error("Failed to convert to MP3, using original WebM file")
        mp3_path = webm_path
    
    try:
        # Upload to AssemblyAI
        audio_url = upload_audio_file(mp3_path)
        
        # Transcribe with speaker labels
        result = transcribe_audio(audio_url)
        
        # Generate PDF filename
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        pdf_path = output_dir / f"zoom_transcript_{meeting_id}_{timestamp}.pdf"
        
        # Save as PDF
        save_transcript_as_pdf(result, str(pdf_path))
        
        return pdf_path
        
    except Exception as e:
        log.error(f"Transcription failed: {e}")
        raise

def _finish(writer: ChunkWriter, args: argparse.Namespace):
    webm = writer.close()

    if not webm.exists() or webm.stat().st_size < 512:
        log.error(
            f"Output file missing or too small ({webm.stat().st_size if webm.exists() else 0} bytes).\n"
            "  → Run with --no-headless --debug to see what happened.\n"
            "  → Check that someone was actually speaking in the meeting."
        )
        return

    outputs = [str(webm)]

    if not args.no_wav:
        w = to_wav(webm)
        if w:
            outputs.append(str(w))
        elif not shutil.which("ffmpeg"):
            log.info("  (install ffmpeg to get .wav/.mp3 output)")

    if not args.no_mp3:
        m = to_mp3(webm)
        if m:
            outputs.append(str(m))

    print("\n✓ Recording complete:")
    for o in outputs:
        print(f"  {o}")

    # Process to PDF if transcription is enabled
    if not args.no_transcript:
        try:
            output_dir = Path(args.out)
            pdf_path = process_recording_to_pdf(webm, output_dir, args.meeting_id)
            print(f"\n🎉 Transcript PDF created: {pdf_path}")
        except Exception as e:
            log.error(f"Failed to create transcript: {e}")

def main():
    p = argparse.ArgumentParser(
        description="Record Zoom meeting audio and transcribe to PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("zoom_link",     nargs="?",             help="Zoom invite URL")
    p.add_argument("--name",        default="Recorder",    help="Display name in meeting")
    p.add_argument("--out",         default="./recordings",help="Output directory")
    p.add_argument("--no-headless", action="store_true",   help="Show the browser window")
    p.add_argument("--max-wait",    type=int, default=180, help="Max minutes to record (default 180)")
    p.add_argument("--no-mp3",      action="store_true",   help="Skip mp3 conversion")
    p.add_argument("--no-wav",      action="store_true",   help="Skip wav conversion")
    p.add_argument("--no-transcript", action="store_true", help="Skip PDF transcription")
    p.add_argument("--debug",       action="store_true",   help="Save screenshots at each step")
    args = p.parse_args()

    # Check for API key
    if not args.no_transcript and not os.getenv("API_KEY"):
        sys.exit("❌ API_KEY not found in environment variables. Set it in .env file or export it.")

    if not args.zoom_link:
        args.zoom_link = input("Zoom link: ").strip()
        if not args.zoom_link:
            sys.exit("No link provided.")

    try:
        meeting_id, passcode = parse_zoom_link(args.zoom_link)
        args.meeting_id = meeting_id
    except ValueError as e:
        sys.exit(f"Error: {e}")

    log.info(f"Meeting : {meeting_id}")
    log.info(f"Passcode: {'(none)' if not passcode else '***'}")

    ts        = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir   = Path(args.out)
    webm_path = out_dir / f"zoom_{meeting_id}_{ts}.webm"
    debug_dir = (out_dir / f"debug_{ts}") if args.debug else None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"Debug dir: {debug_dir}")

    log.info(f"Output  : {webm_path}")

    writer = ChunkWriter(webm_path)

    def _sigint(sig, frame):
        log.info("\nCtrl-C — saving …")
        _finish(writer, args)
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)

    try:
        asyncio.run(run_bot(
            meeting_id=meeting_id,
            passcode=passcode,
            display_name=args.name,
            writer=writer,
            headless=not args.no_headless,
            max_min=args.max_wait,
            debug_dir=debug_dir,
        ))
    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)

    _finish(writer, args)

if __name__ == "__main__":
    main()
