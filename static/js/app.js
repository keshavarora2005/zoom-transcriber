/* ── ZoomScribe Frontend ────────────────────────────────────────────── */

let currentJobId = null;
let waveInterval = null;
let eventSource  = null;

// ── Waveform ────────────────────────────────────────────────────────
const BAR_COUNT = 60;
let waveBars = null;

function initWaveform() {
  waveBars = document.getElementById('waveBars');
  if (!waveBars) return;
  
  waveBars.innerHTML = '';
  for (let i = 0; i < BAR_COUNT; i++) {
    const bar = document.createElement('div');
    bar.className = 'wave-bar';
    bar.style.height = '4px';
    waveBars.appendChild(bar);
  }
}

function startWaveAnimation(active = true) {
  stopWaveAnimation();
  if (!waveBars) return;
  
  const bars = waveBars.querySelectorAll('.wave-bar');
  if (!active) {
    bars.forEach(b => { b.style.height = '4px'; b.classList.remove('active'); });
    return;
  }
  bars.forEach(b => b.classList.add('active'));
  function animateFrame() {
    bars.forEach((bar, i) => {
      const t  = Date.now() / 1000;
      const h  = 6 + 20 * Math.abs(Math.sin(t * 3 + i * 0.4)) +
                    10 * Math.abs(Math.sin(t * 1.7 + i * 0.7)) +
                     6 * Math.random();
      bar.style.height = Math.min(h, 48) + 'px';
    });
  }
  waveInterval = setInterval(animateFrame, 80);
}

function stopWaveAnimation() {
  if (waveInterval) { clearInterval(waveInterval); waveInterval = null; }
}

// ── Header status ────────────────────────────────────────────────────
function setHeaderStatus(state) {
  const dot  = document.querySelector('.header-status .dot');
  const text = document.querySelector('.header-status .status-text');
  dot.className = 'dot ' + state;
  const labels = {
    idle: 'Ready', active: 'Recording', done: 'Complete', error: 'Error',
    transcribing: 'Transcribing',
  };
  text.textContent = labels[state] || state;
}

// ── Step tracker ─────────────────────────────────────────────────────
const STEP_MAP = {
  starting:     'joining',
  recording:    'recording',
  transcribing: 'transcribing',
  done:         'done',
  error:        null,
};

function setActiveStep(status) {
  const steps = ['joining','recording','transcribing','done'];
  const active = STEP_MAP[status];
  const activeIdx = steps.indexOf(active);

  steps.forEach((s, i) => {
    const el = document.getElementById('step-' + s);
    if (!el) return;
    el.className = 'step';
    if (active && i < activeIdx)      el.classList.add('done');
    else if (s === active)            el.classList.add('active');
  });

  if (status === 'error') {
    const el = document.getElementById('step-joining');
    if (el) el.classList.add('error');
  }
}

// ── Terminal log ──────────────────────────────────────────────────────
function appendLog(msg, type = '') {
  const body = document.getElementById('terminalBody');
  const line = document.createElement('div');
  line.className = 'log-line' + (type ? ' ' + type : '');

  // Auto-detect type from emoji prefix
  if (!type) {
    if (msg.includes('✅') || msg.includes('🎉') || msg.includes('🎵') || msg.includes('✓'))
      line.classList.add('success');
    if (msg.includes('❌') || msg.includes('Error') || msg.includes('failed'))
      line.classList.add('error');
  }

  // Prefix timestamp
  const ts = new Date().toLocaleTimeString('en-GB', { hour12: false });
  line.textContent = `[${ts}] ${msg}`;
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
}

// ── Start job ─────────────────────────────────────────────────────────
async function startJob() {
  console.log('startJob called');
  
  try {
    const zoomLink      = document.getElementById('zoomLink').value.trim();
    const displayName   = document.getElementById('displayName').value.trim() || 'Recorder';
    const maxMinutes    = parseInt(document.getElementById('maxMinutes').value) || 180;
    const skipTranscript = false; // Always generate transcript
    const showBrowser    = false; // Always headless mode

    console.log('Form values:', { zoomLink, displayName, maxMinutes });

    if (!zoomLink) {
      console.log('No zoom link provided');
      shakeInput('zoomLink');
      return;
    }

    const btn = document.getElementById('btnStart');
    console.log('Button found:', btn);
    btn.classList.add('loading');
    btn.querySelector('.btn-text').textContent = 'Starting…';

    console.log('Sending request to /api/start');
    const res = await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        zoom_link:       zoomLink,
        display_name:    displayName,
        max_minutes:     maxMinutes,
        skip_transcript: skipTranscript,
        headless:        !showBrowser,
      }),
    });

    console.log('Response status:', res.status);
    console.log('Response headers:', res.headers);
    
    if (!res.ok) {
      const errorText = await res.text();
      console.error('Error response:', errorText);
      const err = errorText ? JSON.parse(errorText) : {detail: 'Unknown error'};
      throw new Error(err.detail || 'Failed to start job');
    }

    const data = await res.json();
    console.log('Response data:', data);
    showJobPanel(data.job_id);
    streamLogs(data.job_id);

  } catch (e) {
    console.error('Error in startJob:', e);
    showToast('Error: ' + e.message, 'error');
    const btn = document.getElementById('btnStart');
    if (btn) {
      btn.classList.remove('loading');
      btn.querySelector('.btn-text').textContent = 'Start Recording Session';
    }
  }
}

// ── Show job panel ────────────────────────────────────────────────────
function showJobPanel(jobId) {
  currentJobId = jobId;

  document.getElementById('formSection').classList.add('hidden');
  document.getElementById('jobSection').classList.remove('hidden');
  document.getElementById('jobMeetingId').textContent = jobId.slice(0, 8).toUpperCase();

  // Reset state
  document.getElementById('terminalBody').innerHTML = '<div class="log-line init">$ zoomscribe session started</div>';
  document.getElementById('downloadsSection').classList.add('hidden');
  document.getElementById('errorBox').classList.add('hidden');
  document.getElementById('jobLabel').textContent = 'STARTING';

  initWaveform();
  setActiveStep('starting');
  setHeaderStatus('active');
}

// ── SSE log stream ────────────────────────────────────────────────────
function streamLogs(jobId) {
  if (eventSource) eventSource.close();

  eventSource = new EventSource(`/api/job/${jobId}/logs`);

  eventSource.onmessage = (e) => {
    const payload = JSON.parse(e.data);

    if (payload.log) {
      appendLog(payload.log);
    }

    const status = payload.status;
    if (status) {
      updateJobStatus(status, payload.job || null);
    }

    if (payload.done) {
      eventSource.close();
      finishJob(payload.job);
    }
  };

  eventSource.onerror = () => {
    eventSource.close();
    appendLog('⚠️ Log stream disconnected', 'error');
  };
}

// ── Stop recording ───────────────────────────────────────────────────────
async function stopRecording() {
  if (!currentJobId) return;
  
  const btn = document.getElementById('btnStop');
  btn.classList.add('loading');
  btn.querySelector('.btn-text').textContent = 'Stopping…';
  
  try {
    const res = await fetch(`/api/job/${currentJobId}/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Failed to stop recording');
    }
    
    appendLog('🛑 Stop request sent, finishing recording...');
    
  } catch (e) {
    showToast('Error: ' + e.message, 'error');
    btn.classList.remove('loading');
    btn.querySelector('.btn-text').textContent = 'Stop Recording';
  }
}

// ── Update status ──────────────────────────────────────────────────────
function updateJobStatus(status, job) {
  const labelEl = document.getElementById('jobLabel');
  const stopBtn = document.getElementById('btnStop');
  const labels  = {
    starting: 'STARTING', recording: 'RECORDING',
    transcribing: 'TRANSCRIBING', done: 'COMPLETE', error: 'ERROR',
    stopping: 'STOPPING',
  };
  labelEl.textContent = labels[status] || status.toUpperCase();

  // Show/hide stop button based on status
  if (status === 'recording') {
    stopBtn.classList.remove('hidden');
    stopBtn.classList.remove('loading');
    stopBtn.querySelector('.btn-text').textContent = 'Stop Recording';
  } else if (status === 'stopping') {
    stopBtn.classList.remove('hidden');
    stopBtn.classList.add('loading');
    stopBtn.querySelector('.btn-text').textContent = 'Stopping…';
  } else {
    stopBtn.classList.add('hidden');
  }

  setActiveStep(status);

  if (status === 'recording') {
    startWaveAnimation(true);
    setHeaderStatus('active');
  } else if (status === 'transcribing') {
    startWaveAnimation(false);
    setHeaderStatus('transcribing');
  } else if (status === 'done') {
    startWaveAnimation(false);
    setHeaderStatus('done');
  } else if (status === 'error') {
    startWaveAnimation(false);
    setHeaderStatus('error');
  }

  if (job && job.meeting_id) {
    document.getElementById('jobMeetingId').textContent = job.meeting_id;
  }
}

// ── Finish job ─────────────────────────────────────────────────────────
function finishJob(job) {
  if (!job) return;
  stopWaveAnimation();

  if (job.status === 'error') {
    const errBox = document.getElementById('errorBox');
    const errMsg = document.getElementById('errorMsg');
    const lastErr = (job.logs || []).filter(l => l.includes('❌')).pop() || 'Unknown error';
    errMsg.textContent = lastErr.replace('❌ Error: ', '');
    errBox.classList.remove('hidden');
    return;
  }

  // Show downloads
  const downloadsSection = document.getElementById('downloadsSection');
  const grid = document.getElementById('downloadsGrid');
  grid.innerHTML = '';

  const files = [
    { key: 'pdf_filename',  type: 'pdf',  label: 'Transcript',  icon: docIcon() },
  ];

  files.forEach(({ key, type, label, icon }) => {
    if (!job[key]) return;
    const a = document.createElement('a');
    a.className = `download-btn ${type}`;
    a.href = `/api/job/${job.id}/download/${type}`;
    a.download = job[key];
    a.innerHTML = `${icon} <span>${label}</span><span class="file-type">${type.toUpperCase()}</span>`;
    grid.appendChild(a);
  });

  if (grid.children.length > 0) {
    downloadsSection.classList.remove('hidden');
  }

  loadHistory();
}

// ── Cancel view ───────────────────────────────────────────────────────
function cancelView() {
  if (eventSource) eventSource.close();
  stopWaveAnimation();
  currentJobId = null;
  document.getElementById('jobSection').classList.add('hidden');
  document.getElementById('formSection').classList.remove('hidden');

  const btn = document.getElementById('btnStart');
  btn.classList.remove('loading');
  btn.querySelector('.btn-text').textContent = 'Start Recording Session';
  setHeaderStatus('idle');
}

function startNew() {
  cancelView();
}

// ── History ───────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const res = await fetch('/api/jobs');
    const jobs = await res.json();
    renderHistory(jobs);
  } catch (e) {
    console.warn('Failed to load history:', e);
  }
}

function renderHistory(jobs) {
  const list = document.getElementById('historyList');
  if (!jobs.length) {
    list.innerHTML = '<div class="empty-state">No sessions yet. Start your first recording above.</div>';
    return;
  }

  list.innerHTML = '';
  [...jobs].reverse().forEach(job => {
    const item = document.createElement('div');
    item.className = 'history-item';
    const created = new Date(job.created_at).toLocaleString();
    const meetingId = job.meeting_id || 'Parsing…';
    const sizeTxt = job.webm_size_mb ? ` · ${job.webm_size_mb} MB` : '';

    item.innerHTML = `
      <div class="history-info">
        <div class="history-id">Meeting: ${meetingId}</div>
        <div class="history-meta">${created}${sizeTxt}</div>
      </div>
      <div class="history-actions">
        <span class="status-badge ${job.status}">${job.status}</span>
        ${job.status === 'done' ? `<button class="btn-ghost" onclick="viewJob('${job.id}')">View →</button>` : ''}
      </div>
    `;
    list.appendChild(item);
  });
}

async function viewJob(jobId) {
  showJobPanel(jobId);
  document.getElementById('jobLabel').textContent = 'COMPLETE';

  const res  = await fetch(`/api/job/${jobId}`);
  const job  = await res.json();

  setActiveStep(job.status);
  setHeaderStatus(job.status === 'done' ? 'done' : 'error');

  (job.logs || []).forEach(l => appendLog(l));
  finishJob(job);
}

// ── Shake input ───────────────────────────────────────────────────────
function shakeInput(id) {
  const el = document.getElementById(id);
  el.style.animation = 'none';
  el.style.borderColor = 'var(--accent3)';
  setTimeout(() => el.style.borderColor = '', 800);
  el.focus();
}

// ── Toast ─────────────────────────────────────────────────────────────
function showToast(msg, type = 'info') {
  const toast = document.createElement('div');
  toast.style.cssText = `
    position:fixed; bottom:32px; right:32px; z-index:9999;
    background:var(--surface); border:1px solid ${type==='error'?'rgba(255,95,126,0.4)':'rgba(79,255,176,0.3)'};
    color:${type==='error'?'var(--accent3)':'var(--text)'};
    font-family:var(--mono); font-size:12px;
    padding:12px 18px; border-radius:8px;
    box-shadow:0 8px 32px rgba(0,0,0,0.4);
    animation: fadeUp 0.3s ease both;
    max-width:320px;
  `;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

// ── Icon helpers ──────────────────────────────────────────────────────
function docIcon() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"/></svg>`;
}

// ── Init ──────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  initWaveform();
  loadHistory();
  
  // Allow Enter key on zoom link field
  const zoomLinkInput = document.getElementById('zoomLink');
  if (zoomLinkInput) {
    zoomLinkInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') startJob();
    });
  }
});