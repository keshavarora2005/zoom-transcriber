"""
zoom_transcriber.py - Combined Zoom Meeting Recorder and Transcriber
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
import os
import requests
from pathlib import Path
from typing import Union, Callable, List
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, BrowserContext, async_playwright
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zoom_transcriber")

base_url = "https://api.assemblyai.com/v2"

# ─────────────────────────────────────────────────────────────────────────────
# JS hook
# ─────────────────────────────────────────────────────────────────────────────

INIT_SCRIPT = r"""
(function() {
    window.__ZR = window.__ZR || {
        recorder:    null,
        destStream:  null,
        ctx:         null,
        dest:        null,
        connected:   new WeakSet(),
        trackCount:  0,
    };
    const ZR = window.__ZR;

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
            let bin = '';
            for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
            console.log('AUDIO_CHUNK::' + btoa(bin));
        };
        ZR.recorder.onstart  = () => console.log('ZOOM_REC::recorder_started mime=' + (mime || 'default'));
        ZR.recorder.onerror  = (e) => console.log('ZOOM_REC::recorder_error ' + e.error);
        ZR.recorder.start(1000);
    }

    // ── Aggressively suppress ALL notification / beep sounds ─────────────────
    const _createOscillator = AudioContext.prototype.createOscillator;
    AudioContext.prototype.createOscillator = function() {
        const osc = _createOscillator.apply(this, arguments);
        try { osc.frequency.value = 0; } catch(_) {}
        return osc;
    };

    const _createBufferSource = AudioContext.prototype.createBufferSource;
    AudioContext.prototype.createBufferSource = function() {
        const src = _createBufferSource.apply(this, arguments);
        const origStart = src.start.bind(src);
        src.start = function(when, offset, duration) {
            if (src.buffer && src.buffer.duration < 3.0) {
                try { src.disconnect(); } catch(_) {}
                console.log('ZOOM_REC::beep_suppressed duration=' + (src.buffer ? src.buffer.duration.toFixed(3) : 'unknown'));
                return;
            }
            return origStart(when, offset, duration);
        };
        return src;
    };

    const _createGain = AudioContext.prototype.createGain;
    AudioContext.prototype.createGain = function() {
        const gain = _createGain.apply(this, arguments);
        const origConnect = gain.connect.bind(gain);
        gain.connect = function(target) {
            if (target && target === this.context && target === this.context.destination) {
                gain.gain.value = 0;
            }
            return origConnect(target);
        };
        return gain;
    };

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

    function silenceElement(el) {
        el.muted  = true;
        el.volume = 0;
        if (el.srcObject) el.srcObject.getAudioTracks().forEach(t => connectTrack(t, 'elem_muted_' + t.id));
    }

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

    const origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (constraints) => {
        const stream = await origGUM(constraints);
        stream.getAudioTracks().forEach(t => connectTrack(t, 'gum_' + t.id));
        return stream;
    };

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
    "joining":        "div:has-text('Joining Meeting'), [class*='join-meeting'], div:has-text('Joining')",
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
    return "timeout"

# ─────────────────────────────────────────────────────────────────────────────
# Chunk writer
# ─────────────────────────────────────────────────────────────────────────────

class ChunkWriter:
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
        return float(self.chunks)

# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run_ffmpeg(args: List[str]) -> bool:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        log.warning("ffmpeg not found in PATH — skipping conversion")
        return False
    r = subprocess.run([ffmpeg_path, "-y"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        log.warning(f"ffmpeg failed:\n{r.stderr[-400:]}")
        return False
    return True

def to_mp3(src: Path, bitrate: str = "128k") -> Union[Path, None]:
    out = src.with_suffix(".mp3")
    ok = _run_ffmpeg(["-i", str(src), "-vn", "-codec:a", "libmp3lame", "-b:a", bitrate, str(out)])
    if ok and out.exists() and out.stat().st_size > 0:
        log.info(f"MP3: {out}  ({out.stat().st_size/1_048_576:.1f} MB)")
        return out
    log.warning("MP3 conversion failed or produced empty file — will upload webm directly")
    return None

# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: Validate audio has real content before transcription
# ─────────────────────────────────────────────────────────────────────────────

MIN_AUDIO_SIZE_BYTES = 512 * 1024  # 512 KB — anything smaller is likely silence

def validate_audio(path: Path):
    """Raise RuntimeError if the audio file is too small to contain real speech."""
    if not path.exists():
        raise RuntimeError(f"Audio file does not exist: {path}")
    size = path.stat().st_size
    if size < MIN_AUDIO_SIZE_BYTES:
        raise RuntimeError(
            f"Audio file is only {size / 1024:.1f} KB — likely silence or failed recording. "
            f"The bot may not have successfully joined the meeting."
        )
    log.info(f"Audio size OK: {size / 1_048_576:.2f} MB")

# ─────────────────────────────────────────────────────────────────────────────
# Transcription
# ─────────────────────────────────────────────────────────────────────────────

def upload_audio_file(file_path):
    headers = {"authorization": os.getenv("API_KEY")}
    log.info(f"Uploading {Path(file_path).name} ({Path(file_path).stat().st_size / 1_048_576:.1f} MB)...")
    with open(file_path, "rb") as f:
        response = requests.post(
            base_url + "/upload",
            headers=headers,
            data=f,
            timeout=300,
        )
    if response.status_code == 200:
        audio_url = response.json()["upload_url"]
        log.info(f"Upload successful")
        return audio_url
    else:
        raise RuntimeError(f"Upload failed: {response.status_code} - {response.text}")

def transcribe_audio(audio_url):
    headers = {"authorization": os.getenv("API_KEY"), "content-type": "application/json"}

    # FIX 2: Disable language_detection and hardcode English to prevent
    # "language_detection cannot be performed on files with no spoken audio" error.
    # If you record non-English meetings, set language_code accordingly.
    data = {
        "audio_url":          audio_url,
        "speech_model":       "universal",
        "language_code":      "en",    # ← hardcoded; avoids auto-detect on near-silent files
        "language_detection": False,   # ← disabled; was causing failures on short/silent audio
        "punctuate":          True,
        "format_text":        True,
        "speaker_labels":     True,
    }

    log.info("Submitting transcription request...")
    response = requests.post(base_url + "/transcript", json=data, headers=headers)

    if response.status_code != 200:
        raise RuntimeError(f"Transcription request failed: {response.status_code} - {response.text}")

    response_data = response.json()
    if 'id' not in response_data:
        raise RuntimeError(f"Invalid response from AssemblyAI: {response_data}")

    transcript_id = response_data['id']
    log.info(f"Transcription ID: {transcript_id}")

    polling_endpoint = base_url + f"/transcript/{transcript_id}"
    while True:
        result = requests.get(polling_endpoint, headers=headers).json()
        if result['status'] == 'completed':
            log.info("Transcription complete!")
            return result
        elif result['status'] == 'error':
            raise RuntimeError(f"Transcription failed: {result.get('error', 'unknown error')}")
        else:
            log.info(f"Processing... ({result['status']})")
            time.sleep(3)

def format_speaker_transcript(result):
    if 'utterances' not in result or not result['utterances']:
        log.warning("No speaker data — falling back to plain text")
        return result.get('text', 'No transcript available')

    speakers = set(u.get('speaker', 'Unknown') for u in result['utterances'])
    log.info(f"Detected {len(speakers)} speaker(s): {sorted(speakers)}")

    lines = []
    for u in result['utterances']:
        speaker = u.get('speaker', 'Unknown')
        text    = u.get('text', '').strip()
        if text:
            lines.append(f"Speaker {speaker}: {text}")

    return '\n\n'.join(lines)

def save_transcript_as_pdf(result, output_pdf="meeting_transcript.pdf"):
    log.info("Creating PDF...")
    doc    = SimpleDocTemplate(output_pdf, pagesize=letter)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'CustomTitle', parent=styles['Title'],
        fontSize=24, spaceAfter=30, alignment=1
    )
    content_style = ParagraphStyle(
        'SpeakerContent', parent=styles['Normal'],
        fontSize=11, spaceAfter=8, leftIndent=20
    )

    story = [
        Paragraph("Meeting Transcript with Speaker Labels", title_style),
        Spacer(1, 20),
    ]

    text = format_speaker_transcript(result)
    for para in text.split('\n\n'):
        para = para.strip()
        if para:
            para = ''.join(c for c in para if c.isprintable() or c in '\n\t')
            para = para.replace('\x00', '').replace('\ufffd', '')
            story.append(Paragraph(para, content_style))

    doc.build(story)
    log.info(f"PDF saved: {output_pdf}")

# ─────────────────────────────────────────────────────────────────────────────
# Bot
# ─────────────────────────────────────────────────────────────────────────────

async def run_bot(
    meeting_id:        str,
    passcode:          Union[str, None],
    display_name:      str,
    writer:            ChunkWriter,
    headless:          bool,
    max_min:           int,
    debug_dir:         Union[Path, None],
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

        log.info("Opening Zoom Web Client...")
        try:
            await page.goto(join_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            log.error(f"Navigation error: {e}")
            await browser.close()
            return

        await asyncio.sleep(3)
        log.info(f"URL after load: {page.url}")

        try:
            title  = await page.title()
            inputs = await page.query_selector_all("input")
            log.info(f"Page title: {title}")
            log.info(f"Found {len(inputs)} input fields")
            for i, inp in enumerate(inputs[:3]):
                ph  = await inp.get_attribute("placeholder") or "no placeholder"
                typ = await inp.get_attribute("type") or "no type"
                log.info(f"  Input {i+1}: type='{typ}', placeholder='{ph}'")
            buttons = await page.query_selector_all("button")
            log.info(f"Found {len(buttons)} buttons")
            for i, btn in enumerate(buttons[:5]):
                txt = await btn.text_content() or ""
                log.info(f"  Button {i+1}: '{txt}'")
        except Exception as e:
            log.warning(f"Debug info failed: {e}")

        # Consent banners
        for sel in ['button:has-text("I Agree")', 'button:has-text("Accept")']:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    await asyncio.sleep(1)
                    break
            except Exception:
                pass

        current_state = await get_state(page)
        log.info(f"Current state: {current_state}")

        if current_state not in ["in_meeting", "audio_dialog"]:
            log.info(f"Waiting for name input (will enter '{display_name}')...")
            ns = await wait_state(page, ["name_input"], 20, debug_dir=debug_dir, tag="name")

            if ns == "name_input":
                # ── Step 1: Wait for input to be truly ready ──────────────────
                await asyncio.sleep(2)

                # ── Step 2: Dump all clickable elements for one-time diagnosis ─
                try:
                    all_btns = await page.query_selector_all("button, [role='button'], [class*='btn'], [class*='join']")
                    log.info(f"  Found {len(all_btns)} clickable element(s) on page:")
                    for i, b in enumerate(all_btns[:10]):
                        txt  = (await b.text_content() or "").strip()
                        cls  = (await b.get_attribute("class") or "")[:60]
                        role = (await b.get_attribute("role") or "")
                        eid  = (await b.get_attribute("id") or "")
                        log.info(f"    [{i}] tag=? id={eid!r} role={role!r} class={cls!r} text={txt!r}")
                    all_inputs = await page.query_selector_all("input")
                    log.info(f"  Found {len(all_inputs)} input(s):")
                    for i, inp in enumerate(all_inputs[:5]):
                        typ = (await inp.get_attribute("type") or "")
                        ph  = (await inp.get_attribute("placeholder") or "")
                        eid = (await inp.get_attribute("id") or "")
                        log.info(f"    [{i}] type={typ!r} id={eid!r} placeholder={ph!r}")
                except Exception as de:
                    log.warning(f"  DOM dump failed: {de}")

                # ── Step 3: Enter display name ────────────────────────────────
                name_entered = False
                name_selectors = [
                    "input#inputname",
                    "input[placeholder*='Your Name' i]",
                    "input[placeholder*='Enter your name' i]",
                    "input[placeholder*='name' i]",
                    "input[type='text']",
                    "input:not([type='hidden'])",
                ]
                for sel in name_selectors:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        await el.press("Control+a")
                        await el.press("Backspace")
                        await asyncio.sleep(0.3)
                        await el.type(display_name, delay=80)
                        log.info(f"  Name entered ({sel})")
                        name_entered = True
                        break

                if not name_entered:
                    log.warning("  Could not find name input via any selector — trying JS inject")
                    try:
                        await page.evaluate(f"""
                            const inp = document.querySelector('input');
                            if (inp) {{
                                inp.value = '';
                                inp.focus();
                                document.execCommand('selectAll');
                                document.execCommand('insertText', false, '{display_name}');
                            }}
                        """)
                        log.info("  Name injected via JS execCommand")
                        name_entered = True
                    except Exception as je:
                        log.warning(f"  JS name inject failed: {je}")

                await asyncio.sleep(1)

                # ── Step 4: Click Join — buttons AND div/role="button" elements ─
                async def try_click_join() -> bool:
                    # 4a: standard <button> selectors
                    for sel in [
                        "button#joinBtn",
                        "button[type='submit']",
                        "button:has-text('Join')",
                        "button:has-text('Join Meeting')",
                        "button[aria-label*='Join' i]",
                    ]:
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                await el.click()
                                log.info(f"  Join clicked via button selector ({sel})")
                                return True
                        except Exception:
                            pass

                    # 4b: div / span / any element acting as a button (Zoom often does this)
                    for sel in [
                        "[role='button']:has-text('Join')",
                        "div:has-text('Join Meeting')",
                        "[class*='join-btn']",
                        "[class*='joinBtn']",
                        "[class*='join_btn']",
                        "[id*='join']",
                    ]:
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                await el.click()
                                log.info(f"  Join clicked via role/div selector ({sel})")
                                return True
                        except Exception:
                            pass

                    # 4c: scan ALL elements whose text contains "join" (but exclude "joining")
                    try:
                        candidates = await page.query_selector_all("button, [role='button'], div, span, a")
                        for el in candidates:
                            txt = (await el.text_content() or "").strip().lower()
                            # Only click actual join buttons, not status indicators
                            if txt in ("join", "join meeting", "join now") and "joining" not in txt:
                                await el.click()
                                log.info(f"  Join clicked via text scan: {txt!r}")
                                return True
                    except Exception as se:
                        log.warning(f"  Text scan failed: {se}")

                    return False

                join_clicked = await try_click_join()

                if not join_clicked:
                    log.warning("  No Join element found — pressing Enter as last resort")
                    await page.keyboard.press("Enter")

                # ── Step 5: Wait and retry once if still stuck ────────────────
                await asyncio.sleep(5)
                recheck = await get_state(page)
                log.info(f"  State after join attempt: {recheck!r}")

                if recheck == "name_input":
                    log.warning("  Still on name_input — second join attempt...")
                    await try_click_join()
                    await asyncio.sleep(5)
                elif recheck == "joining":
                    log.info("  Joining in progress — waiting for transition...")
                    # Wait for joining to complete (can take 10-15 seconds)
                    # But also check for manual stop during this phase
                    join_start = time.time()
                    join_timeout = 30  # Maximum 30 seconds for joining
                    
                    while time.time() - join_start < join_timeout:
                        # Check manual stop while joining
                        if manual_stop_check and manual_stop_check():
                            log.info("Manual stop received during joining — aborting...")
                            await browser.close()
                            return
                        
                        join_result = await get_state(page)
                        if join_result in ["audio_dialog", "in_meeting", "waiting_room"]:
                            log.info(f"  Join completed: {join_result!r}")
                            break
                        await asyncio.sleep(1)
                    else:
                        log.warning("  Joining timed out - trying manual refresh...")
                        if manual_stop_check and manual_stop_check():
                            log.info("Manual stop received during joining timeout — aborting...")
                            await browser.close()
                            return
                        await page.reload(wait_until="domcontentloaded")
                        await asyncio.sleep(3)

            else:
                log.info(f"  Join successful - moved to state: {recheck!r}")
                # Add a small delay to let the page settle after joining
                await asyncio.sleep(2)
                try:
                    inputs = await page.query_selector_all("input[type='text'], input:not([type])")
                    for inp in inputs:
                        ph = await inp.get_attribute("placeholder") or ""
                        if "name" in ph.lower() or "enter" in ph.lower() or not ph:
                            await inp.click()
                            await inp.press("Control+a")
                            await inp.press("Backspace")
                            await asyncio.sleep(0.3)
                            await inp.type(display_name, delay=80)
                            log.info(f"  Name entered via fallback")
                            break
                    await asyncio.sleep(1)
                    # Try both button and div-based join elements in fallback too
                    for sel in [
                        "button:has-text('Join')", "button[type='submit']",
                        "[role='button']:has-text('Join')", "div:has-text('Join Meeting')",
                    ]:
                        btn = await page.query_selector(sel)
                        if btn:
                            await btn.click()
                            log.info(f"  Join clicked via fallback ({sel})")
                            break
                    await asyncio.sleep(5)
                except Exception as e:
                    log.warning(f"Fallback failed: {e}")

        if await get_state(page) == "passcode_input":
            if passcode:
                await page.fill("input#inputpasscode", passcode)
                await page.click("button:has-text('Join'), button[type='submit']")
                await asyncio.sleep(2)
            else:
                log.error("Passcode required but none provided!")

        log.info("Waiting for admission...")
        s = await wait_state(
            page,
            ["waiting_room", "audio_dialog", "in_meeting", "meeting_ended"],
            timeout_sec=max_min * 60, poll=3, debug_dir=debug_dir, tag="admission",
        )

        if s == "waiting_room":
            log.info("In waiting room...")
            s = await wait_state(
                page,
                ["audio_dialog", "in_meeting", "meeting_ended"],
                timeout_sec=max_min * 60, poll=5, debug_dir=debug_dir, tag="waiting_room",
            )

        if s in ("timeout", "error", "meeting_ended"):
            log.error(f"Cannot proceed — state: {s!r}")
            await browser.close()
            return

        log.info("Joining with computer audio...")
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
                break
            except Exception as e:
                log.warning(f"  Audio join error: {e}")
                await asyncio.sleep(2)

        s = await wait_state(page, ["in_meeting", "meeting_ended"], 20, debug_dir=debug_dir)
        log.info(f"In-meeting state: {s!r}")

        log.info("Waiting for audio tracks (up to 30s)...")
        track_deadline = time.time() + 30
        while time.time() < track_deadline:
            if track_count["n"] > 0:
                log.info(f"Got {track_count['n']} audio track(s) — recording active.")
                break
            await asyncio.sleep(1)
        else:
            log.warning("No audio tracks after 30s — recording continues, tracks may connect later.")

        log.info(f"Recording in progress (max {max_min} min)...")
        deadline  = time.time() + max_min * 60
        last_log  = time.time()

        while time.time() < deadline:
            # Check manual stop more frequently (every 1 second instead of 5)
            if manual_stop_check and manual_stop_check():
                log.info("Manual stop received — finishing recording...")
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
                    f"Still recording: {writer.duration_est/60:.1f} min, "
                    f"{writer.bytes/1_048_576:.1f} MB, {track_count['n']} track(s)"
                )
                last_log = time.time()
            # Reduced sleep time for faster stop response
            await asyncio.sleep(1)
        else:
            log.warning(f"Max recording time ({max_min} min) reached.")

        log.info("Stopping recorder...")
        try:
            r = await page.evaluate(STOP_JS)
            log.info(f"  Recorder stop result: {r}")
            await asyncio.sleep(1.5)
        except Exception as e:
            log.warning(f"  Stop error: {e}")

        try:
            await page.goto("about:blank", timeout=5_000)
        except Exception:
            pass

        await browser.close()
        log.info("Browser closed.")

# ─────────────────────────────────────────────────────────────────────────────
# Main recording → PDF workflow
# ─────────────────────────────────────────────────────────────────────────────

def process_recording_to_pdf(webm_path: Path, output_dir: Path, meeting_id: str) -> Path:
    log.info("Starting transcription process...")

    audio_path = to_mp3(webm_path)
    if audio_path is None:
        log.info("Using original WebM for upload (ffmpeg not available or failed)")
        audio_path = webm_path

    # FIX 8: Validate audio size BEFORE uploading to prevent AssemblyAI silent-file errors
    validate_audio(audio_path)

    audio_url = upload_audio_file(audio_path)
    result    = transcribe_audio(audio_url)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    pdf_path  = output_dir / f"zoom_transcript_{meeting_id}_{timestamp}.pdf"
    save_transcript_as_pdf(result, str(pdf_path))

    return pdf_path


def _finish(writer, args):
    webm = writer.close()
    if not webm.exists() or webm.stat().st_size < 512:
        log.error("Output file missing or too small.")
        return
    if not args.no_transcript:
        try:
            pdf = process_recording_to_pdf(webm, Path(args.out), args.meeting_id)
            print(f"\n🎉 Transcript PDF: {pdf}")
        except Exception as e:
            log.error(f"Failed to create transcript: {e}")


def main():
    p = argparse.ArgumentParser(description="Record Zoom meeting audio and transcribe to PDF.")
    p.add_argument("zoom_link",       nargs="?")
    p.add_argument("--name",          default="Recorder")
    p.add_argument("--out",           default="./recordings")
    p.add_argument("--no-headless",   action="store_true")
    p.add_argument("--max-wait",      type=int, default=180)
    p.add_argument("--no-mp3",        action="store_true")
    p.add_argument("--no-transcript", action="store_true")
    p.add_argument("--debug",         action="store_true")
    args = p.parse_args()

    if not args.no_transcript and not os.getenv("API_KEY"):
        sys.exit("API_KEY not found. Set it in your .env file.")

    if not args.zoom_link:
        args.zoom_link = input("Zoom link: ").strip()
        if not args.zoom_link:
            sys.exit("No link provided.")

    try:
        meeting_id, passcode = parse_zoom_link(args.zoom_link)
        args.meeting_id = meeting_id
    except ValueError as e:
        sys.exit(f"Error: {e}")

    ts        = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir   = Path(args.out)
    webm_path = out_dir / f"zoom_{meeting_id}_{ts}.webm"
    debug_dir = (out_dir / f"debug_{ts}") if args.debug else None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)

    writer = ChunkWriter(webm_path)

    def _sigint(sig, frame):
        log.info("Ctrl-C — saving...")
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