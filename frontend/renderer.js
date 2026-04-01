const WS_URL = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;
const RECONNECT_DELAY = 2000;
const TARGET_SAMPLE_RATE = 16000;
const BUFFER_SIZE = 4096;

let ws = null;
let timerInterval = null;
let callSeconds = 0;
let autoScroll = true;
let interimTurnEl = null;
let lastFinalEl = null;
let lastFinalSpeaker = null;
let roleplaying = false;
let transcriptLog = [];
let userRequestedStop = false;  // only true when user clicks End Call

// ── Audio state ──────────────────────────────────────────────────────────
let micStream = null;
let displayStream = null;
let micContext = null;
let displayContext = null;
let micProcessor = null;
let displayProcessor = null;

// ── WebSocket ─────────────────────────────────────────────────────────────

function connect() {
  ws = new WebSocket(WS_URL);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => console.log("[ws] connected");

  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      try { handleMessage(JSON.parse(e.data)); }
      catch (err) { console.error("[ws] parse error", err); }
    }
  };

  ws.onclose = () => {
    console.log("[ws] disconnected, reconnecting...");
    setTimeout(connect, RECONNECT_DELAY);
  };

  ws.onerror = (err) => console.error("[ws] error", err);
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

function sendAudioChunk(label, int16Array) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const payload = new Uint8Array(1 + int16Array.byteLength);
  payload[0] = label;
  payload.set(new Uint8Array(int16Array.buffer), 1);
  ws.send(payload.buffer);
}

// ── Browser Audio Capture ────────────────────────────────────────────────

function float32ToInt16(float32Array) {
  const int16 = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i++) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return int16;
}

function resample(float32, fromRate, toRate) {
  if (fromRate === toRate) return float32;
  const ratio = fromRate / toRate;
  const newLen = Math.round(float32.length / ratio);
  const result = new Float32Array(newLen);
  for (let i = 0; i < newLen; i++) {
    const srcIdx = i * ratio;
    const idx = Math.floor(srcIdx);
    const frac = srcIdx - idx;
    const a = float32[idx] || 0;
    const b = float32[idx + 1] || 0;
    result[i] = a + frac * (b - a);
  }
  return result;
}

async function requestMic() {
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    document.getElementById("mic-status").textContent = "Microphone ready";
    document.getElementById("mic-status").className = "permission-status granted";
    document.getElementById("grant-mic-btn").textContent = "Mic Granted";
    document.getElementById("grant-mic-btn").disabled = true;
    document.getElementById("grant-mic-btn").classList.add("granted");
    checkStartReady();
  } catch (err) {
    console.error("[audio] mic error:", err);
    document.getElementById("mic-status").textContent = "Mic access denied";
    document.getElementById("mic-status").className = "permission-status denied";
  }
}

async function requestDisplayAudio() {
  try {
    displayStream = await navigator.mediaDevices.getDisplayMedia({
      audio: true,
      video: true, // Chrome requires video for getDisplayMedia
    });
    // Stop video track — we only need audio
    displayStream.getVideoTracks().forEach(t => t.stop());
    if (displayStream.getAudioTracks().length === 0) {
      document.getElementById("display-status").textContent = "No audio selected — make sure to check 'Share audio'";
      document.getElementById("display-status").className = "permission-status denied";
      displayStream = null;
      return;
    }
    document.getElementById("display-status").textContent = "Customer audio ready";
    document.getElementById("display-status").className = "permission-status granted";
    document.getElementById("grant-display-btn").textContent = "Audio Shared";
    document.getElementById("grant-display-btn").disabled = true;
    document.getElementById("grant-display-btn").classList.add("granted");
    // If the shared tab/screen is closed, reset
    displayStream.getAudioTracks()[0].onended = () => {
      displayStream = null;
      document.getElementById("display-status").textContent = "Audio source ended";
      document.getElementById("display-status").className = "permission-status denied";
      document.getElementById("grant-display-btn").textContent = "Share Audio Source";
      document.getElementById("grant-display-btn").disabled = false;
      document.getElementById("grant-display-btn").classList.remove("granted");
      checkStartReady();
    };
    checkStartReady();
  } catch (err) {
    console.error("[audio] display error:", err);
    document.getElementById("display-status").textContent = "Share cancelled or denied";
    document.getElementById("display-status").className = "permission-status denied";
  }
}

async function startAudioCapture(micOnly) {
  // Mic capture — use AudioWorklet if available, fallback to ScriptProcessor
  if (micStream) {
    micContext = new AudioContext();
    const micSource = micContext.createMediaStreamSource(micStream);
    const micRate = micContext.sampleRate;
    console.log(`[audio] mic sample rate: ${micRate}`);

    if (micContext.audioWorklet) {
      try {
        await micContext.audioWorklet.addModule("pcm-worklet.js");
        const workletNode = new AudioWorkletNode(micContext, "pcm-capture", {
          parameterData: { targetRate: TARGET_SAMPLE_RATE },
        });
        workletNode.port.onmessage = (e) => {
          const float32 = e.data;
          const resampled = resample(float32, micRate, TARGET_SAMPLE_RATE);
          const int16 = float32ToInt16(resampled);
          sendAudioChunk(0x00, int16);
        };
        micSource.connect(workletNode);
        workletNode.connect(micContext.destination);
        micProcessor = workletNode;
        console.log("[audio] mic using AudioWorklet");
      } catch (err) {
        console.warn("[audio] AudioWorklet failed, falling back to ScriptProcessor:", err);
        micProcessor = micContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
        micProcessor.onaudioprocess = (e) => {
          const float32 = e.inputBuffer.getChannelData(0);
          const resampled = resample(float32, micRate, TARGET_SAMPLE_RATE);
          const int16 = float32ToInt16(resampled);
          sendAudioChunk(0x00, int16);
        };
        micSource.connect(micProcessor);
        micProcessor.connect(micContext.destination);
      }
    } else {
      micProcessor = micContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
      micProcessor.onaudioprocess = (e) => {
        const float32 = e.inputBuffer.getChannelData(0);
        const resampled = resample(float32, micRate, TARGET_SAMPLE_RATE);
        const int16 = float32ToInt16(resampled);
        sendAudioChunk(0x00, int16);
      };
      micSource.connect(micProcessor);
      micProcessor.connect(micContext.destination);
    }
  }

  // Display/loopback capture (skip for practice/roleplay)
  if (!micOnly && displayStream && displayStream.getAudioTracks().length > 0) {
    displayContext = new AudioContext();
    const displaySource = displayContext.createMediaStreamSource(displayStream);
    const displayRate = displayContext.sampleRate;
    console.log(`[audio] display sample rate: ${displayRate}`);

    if (displayContext.audioWorklet) {
      try {
        await displayContext.audioWorklet.addModule("pcm-worklet.js");
        const workletNode = new AudioWorkletNode(displayContext, "pcm-capture", {
          parameterData: { targetRate: TARGET_SAMPLE_RATE },
        });
        workletNode.port.onmessage = (e) => {
          const float32 = e.data;
          const resampled = resample(float32, displayRate, TARGET_SAMPLE_RATE);
          const int16 = float32ToInt16(resampled);
          sendAudioChunk(0x01, int16);
        };
        displaySource.connect(workletNode);
        workletNode.connect(displayContext.destination);
        displayProcessor = workletNode;
        console.log("[audio] display using AudioWorklet");
      } catch (err) {
        console.warn("[audio] AudioWorklet failed for display, falling back:", err);
        displayProcessor = displayContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
        displayProcessor.onaudioprocess = (e) => {
          const float32 = e.inputBuffer.getChannelData(0);
          const resampled = resample(float32, displayRate, TARGET_SAMPLE_RATE);
          const int16 = float32ToInt16(resampled);
          sendAudioChunk(0x01, int16);
        };
        displaySource.connect(displayProcessor);
        displayProcessor.connect(displayContext.destination);
      }
    } else {
      displayProcessor = displayContext.createScriptProcessor(BUFFER_SIZE, 1, 1);
      displayProcessor.onaudioprocess = (e) => {
        const float32 = e.inputBuffer.getChannelData(0);
        const resampled = resample(float32, displayRate, TARGET_SAMPLE_RATE);
        const int16 = float32ToInt16(resampled);
        sendAudioChunk(0x01, int16);
      };
      displaySource.connect(displayProcessor);
      displayProcessor.connect(displayContext.destination);
    }
  }
}

function stopAudioCapture() {
  if (micProcessor) { micProcessor.disconnect(); micProcessor = null; }
  if (displayProcessor) { displayProcessor.disconnect(); displayProcessor = null; }
  if (micContext) { micContext.close(); micContext = null; }
  if (displayContext) { displayContext.close(); displayContext = null; }
  // Don't stop the streams themselves — user may want to start another session
}

// ── Setup ─────────────────────────────────────────────────────────────────

function checkStartReady() {
  document.getElementById("start-btn").disabled = !micStream || !displayStream;
  document.getElementById("practice-btn").disabled = !micStream;
}

document.getElementById("grant-mic-btn").addEventListener("click", requestMic);
document.getElementById("grant-display-btn").addEventListener("click", requestDisplayAudio);

let _pendingAudioMode = null; // "live" or "practice" — start capture after server confirms

document.getElementById("start-btn").addEventListener("click", () => {
  if (!micStream || !displayStream) return;
  document.getElementById("setup-status").textContent = "Connecting...";
  _pendingAudioMode = "live";
  send({ action: "start" });
});

document.getElementById("end-btn").addEventListener("click", () => {
  userRequestedStop = true;
  send({ action: "stop" });
  stopAudioCapture();
});

document.getElementById("practice-btn").addEventListener("click", () => {
  if (!micStream) return;
  document.getElementById("setup-status").textContent = "Starting practice session...";
  _pendingAudioMode = "practice";
  send({ action: "start_roleplay" });
});

// ── Message Handler ───────────────────────────────────────────────────────

function handleMessage(msg) {
  switch (msg.type) {
    case "status":          handleStatus(msg.state); break;
    case "transcript":      handleTranscript(msg); break;
    case "coaching":        handleCoaching(msg); break;
    case "score_update":    handleScoreUpdate(msg); break;
    case "roleplay_mode":   handleRoleplayMode(msg.active); break;
    case "roleplay_speech": handleRoleplaySpeech(msg); break;
    case "call_guidance":      handleCallGuidance(msg); break;
    case "checklist_update":   handleChecklistUpdate(msg); break;
  }
}

// ── Status ────────────────────────────────────────────────────────────────

async function handleStatus(state) {
  const pill = document.getElementById("status-pill");
  const statusText = document.getElementById("status-text");

  if (state === "recording") {
    showMainScreen();
    pill.classList.remove("processing");
    statusText.textContent = "LIVE";
    // Start audio capture now that server has queues ready
    if (_pendingAudioMode) {
      await startAudioCapture(_pendingAudioMode === "practice");
      console.log(`[audio] capture started (${_pendingAudioMode} mode)`);
      _pendingAudioMode = null;
    }
  } else if (state === "processing") {
    pill.classList.add("processing");
    statusText.textContent = "THINKING";
  } else if (state === "idle") {
    if (userRequestedStop && transcriptLog.length > 0) {
      showCallEndedScreen();
      userRequestedStop = false;
    } else if (transcriptLog.length > 0) {
      // Unexpected idle (server restart, WS reconnect) — don't kill the session.
      // Try to restart so the rep doesn't lose their call.
      console.log("[ws] unexpected idle during active call — attempting to restart session");
      _pendingAudioMode = "live";
      send({ action: "start" });
    } else {
      showSetupScreen();
    }
  }
}

function showMainScreen() {
  document.getElementById("setup-screen").style.display = "none";
  document.getElementById("main-screen").style.display = "flex";
  document.getElementById("call-timer-display").style.display = "flex";
  startTimer();
}

function showCallEndedScreen() {
  document.getElementById("main-screen").style.display = "none";
  document.getElementById("setup-screen").style.display = "none";
  document.getElementById("call-ended-screen").style.display = "flex";
  document.getElementById("call-timer-display").style.display = "none";
  const h = String(Math.floor(callSeconds / 3600)).padStart(2, "0");
  const m = String(Math.floor((callSeconds % 3600) / 60)).padStart(2, "0");
  const s = String(callSeconds % 60).padStart(2, "0");
  document.getElementById("call-ended-duration").textContent = `Duration: ${h}:${m}:${s}`;
  stopTimer();
  stopAudioCapture();
  showCoachingIdle();
}

function showSetupScreen() {
  document.getElementById("main-screen").style.display = "none";
  document.getElementById("call-ended-screen").style.display = "none";
  document.getElementById("setup-screen").style.display = "flex";
  document.getElementById("call-timer-display").style.display = "none";
  stopTimer();
  stopAudioCapture();
  resetTranscript();
  showCoachingIdle();
  checkStartReady();
  userRequestedStop = false;
}

// ── Timer ─────────────────────────────────────────────────────────────────

function startTimer() {
  callSeconds = 0;
  clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    callSeconds++;
    const h = String(Math.floor(callSeconds / 3600)).padStart(2, "0");
    const m = String(Math.floor((callSeconds % 3600) / 60)).padStart(2, "0");
    const s = String(callSeconds % 60).padStart(2, "0");
    document.getElementById("timer").textContent = `${h}:${m}:${s}`;
  }, 1000);
}

function stopTimer() {
  clearInterval(timerInterval);
  document.getElementById("timer").textContent = "00:00:00";
}

// ── Transcript ────────────────────────────────────────────────────────────

function handleTranscript(msg) {
  const { speaker, text, is_final } = msg;
  const body = document.getElementById("transcript-body");

  if (!is_final) {
    if (lastFinalSpeaker && lastFinalSpeaker !== speaker) {
      lastFinalEl = null;
      lastFinalSpeaker = null;
    }
    if (!interimTurnEl || interimTurnEl.dataset.speaker !== speaker) {
      if (interimTurnEl) body.removeChild(interimTurnEl);
      interimTurnEl = createTurnEl(speaker, text, true);
      body.appendChild(interimTurnEl);
    } else {
      interimTurnEl.querySelector(".turn-text").textContent = text;
    }
  } else {
    if (interimTurnEl && interimTurnEl.dataset.speaker === speaker) {
      if (lastFinalEl && lastFinalSpeaker === speaker) {
        const textEl = lastFinalEl.querySelector(".turn-text");
        textEl.textContent = textEl.textContent + " " + text;
        body.removeChild(interimTurnEl);
        if (transcriptLog.length && transcriptLog[transcriptLog.length - 1].speaker === speaker) {
          transcriptLog[transcriptLog.length - 1].text += " " + text;
        }
      } else {
        interimTurnEl.querySelector(".turn-text").textContent = text;
        interimTurnEl.querySelector(".turn-text").classList.remove("interim");
        lastFinalEl = interimTurnEl;
        lastFinalSpeaker = speaker;
        transcriptLog.push({ speaker, text });
      }
      interimTurnEl = null;
    } else if (lastFinalEl && lastFinalSpeaker === speaker) {
      lastFinalEl.querySelector(".turn-text").textContent += " " + text;
      if (transcriptLog.length && transcriptLog[transcriptLog.length - 1].speaker === speaker) {
        transcriptLog[transcriptLog.length - 1].text += " " + text;
      }
    } else {
      const el = createTurnEl(speaker, text, false);
      body.appendChild(el);
      lastFinalEl = el;
      lastFinalSpeaker = speaker;
      transcriptLog.push({ speaker, text });
    }
  }

  if (autoScroll) body.scrollTop = body.scrollHeight;
}

function createTurnEl(speaker, text, isInterim) {
  const div = document.createElement("div");
  div.className = "transcript-turn";
  div.dataset.speaker = speaker;

  const label = document.createElement("div");
  label.className = `turn-speaker ${speaker}`;
  label.textContent = speaker === "rep" ? "REP" : "CUSTOMER";

  const bubble = document.createElement("div");
  bubble.className = `turn-text ${speaker}${isInterim ? " interim" : ""}`;
  bubble.textContent = text;

  div.appendChild(label);
  div.appendChild(bubble);
  return div;
}

function resetTranscript() {
  document.getElementById("transcript-body").innerHTML = "";
  interimTurnEl = null;
  lastFinalEl = null;
  lastFinalSpeaker = null;
  transcriptLog = [];
  window.speechSynthesis.cancel();
}

document.getElementById("transcript-body")?.addEventListener("scroll", (e) => {
  const el = e.target;
  autoScroll = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
});

// ── Coaching ──────────────────────────────────────────────────────────────

function handleCoaching(msg) {
  if (!msg.triggered) return;

  document.getElementById("coaching-idle").style.display = "none";
  const content = document.getElementById("coaching-content");
  content.style.display = "flex";

  document.getElementById("objection-type").textContent = msg.objection_type || "Objection";
  document.getElementById("objection-summary").textContent = msg.objection_summary || "";

  const list = document.getElementById("suggestions-list");
  list.innerHTML = "";

  (msg.suggestions || []).forEach((s) => {
    const card = document.createElement("div");
    card.className = "suggestion-card";
    const labelEl = document.createElement("div");
    labelEl.className = "suggestion-label";
    labelEl.textContent = s.label || "SUGGESTION";
    const textEl = document.createElement("div");
    textEl.className = "suggestion-text";
    textEl.textContent = s.text;
    card.appendChild(labelEl);
    card.appendChild(textEl);
    list.appendChild(card);
  });

  const transList = document.getElementById("transitions-list");
  transList.innerHTML = "";
  (msg.transitions || []).forEach((t) => {
    const el = document.createElement("div");
    el.className = "transition-card";
    el.textContent = t;
    transList.appendChild(el);
  });
}

function handleScoreUpdate(msg) {
  const { score, feedback, breakdown, session_avg } = msg;

  const badge = document.getElementById("session-score-badge");
  const avgEl = document.getElementById("session-score-avg");
  badge.style.display = "flex";
  avgEl.textContent = session_avg;
  badge.className = "session-score-badge " + scoreClass(session_avg);

  const card = document.getElementById("score-card");
  card.style.display = "flex";
  card.className = "score-card " + scoreClass(score);

  document.getElementById("score-card-value").textContent = score;
  document.getElementById("score-card-feedback").textContent = feedback;

  const bars = document.getElementById("score-card-bars");
  bars.innerHTML = "";
  const labels = { verbiage: "Verbiage", handling: "Handling", closing: "Closing" };
  const maxes = { verbiage: 35, handling: 40, closing: 25 };
  Object.entries(breakdown || {}).forEach(([key, val]) => {
    const max = maxes[key] || 35;
    const pct = Math.round((val / max) * 100);
    const row = document.createElement("div");
    row.className = "score-bar-row";
    row.innerHTML = `
      <span class="score-bar-label">${labels[key] || key}</span>
      <div class="score-bar-track">
        <div class="score-bar-fill ${scoreClass(pct)}" style="width:${pct}%"></div>
      </div>
      <span class="score-bar-num">${val}/${max}</span>`;
    bars.appendChild(row);
  });
}

function scoreClass(score) {
  if (score >= 80) return "score-green";
  if (score >= 60) return "score-yellow";
  return "score-red";
}

function handleRoleplayMode(active) {
  roleplaying = active;
  const badge = document.getElementById("practice-badge");
  if (badge) badge.style.display = active ? "flex" : "none";
  if (!active) {
    window.speechSynthesis.cancel();
    if (_currentAudio) { _currentAudio.pause(); _currentAudio = null; }
  }
}

let _currentAudio = null;

function handleRoleplaySpeech(msg) {
  if (!roleplaying) return;

  if (_currentAudio) {
    _currentAudio.pause();
    _currentAudio = null;
    send({ action: "tts_playing", active: false });
  }
  window.speechSynthesis.cancel();

  if (msg.audio_b64) {
    const audio = new Audio(`data:audio/mpeg;base64,${msg.audio_b64}`);
    _currentAudio = audio;
    send({ action: "tts_playing", active: true });
    audio.onended = () => {
      _currentAudio = null;
      send({ action: "tts_playing", active: false });
    };
    audio.play().catch(err => {
      console.error("[tts] play error", err);
      send({ action: "tts_playing", active: false });
    });
    return;
  }

  const utter = new SpeechSynthesisUtterance(msg.text || msg);
  utter.rate = 0.95;
  utter.pitch = 1.05;
  const voices = window.speechSynthesis.getVoices();
  const preferred = voices.find(v => v.lang.startsWith("en") && v.name.toLowerCase().includes("female"))
    || voices.find(v => v.lang.startsWith("en") && !v.name.toLowerCase().includes("male"))
    || voices.find(v => v.lang.startsWith("en"))
    || voices[0];
  if (preferred) utter.voice = preferred;
  window.speechSynthesis.speak(utter);
}

const STAGE_ORDER = ["intro", "discovery", "collect_info", "build_system", "recap", "closing"];

// ── Stage Checklist ──────────────────────────────────────────────────────
const STAGE_CHECKLIST = {
  discovery: [
    { key: "why_security",      label: "What has you looking into security?" },
    { key: "had_system_before",  label: "Have you ever had a security system before?" },
    { key: "who_protecting",     label: "Who all are we looking to protect?" },
  ],
  collect_info: [
    { key: "full_name",     label: "Full name" },
    { key: "phone_number",  label: "Phone number" },
    { key: "email",         label: "Email" },
    { key: "address",       label: "Address" },
  ],
  build_system: [
    { key: "door_sensors",   label: "Door sensors" },
    { key: "window_sensors", label: "Window sensors" },
    { key: "extra_equip",    label: "Motion / glass break / CO detector" },
    { key: "indoor_camera",  label: "Free indoor camera" },
    { key: "outdoor_camera", label: "Outdoor / doorbell camera" },
    { key: "panel_hub",      label: "Panel, hub, & cellular backup" },
    { key: "yard_sign",      label: "Yard sign, stickers, & smartphone access" },
  ],
  closing: [
    { key: "no_contract",   label: "No contract — month to month" },
    { key: "monthly_price", label: "Monthly monitoring ($29.99 → $32.99)" },
    { key: "equip_total",   label: "Equipment total & discounts" },
    { key: "trial_60",      label: "60-day risk-free trial" },
  ],
};

let _currentChecklist = {};  // key → boolean

function renderChecklist(stage) {
  const container = document.getElementById("stage-checklist");
  const items = STAGE_CHECKLIST[stage];
  if (!items) {
    container.style.display = "none";
    return;
  }
  container.innerHTML = "";
  container.style.display = "flex";

  items.forEach(({ key, label }) => {
    const checked = !!_currentChecklist[key];
    const row = document.createElement("label");
    row.className = "checklist-item" + (checked ? " checked" : "");

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = checked;
    cb.addEventListener("change", () => {
      const nowChecked = cb.checked;
      _currentChecklist[key] = nowChecked;
      row.classList.toggle("checked", nowChecked);
      // Tell backend the rep toggled this topic
      send({ action: "toggle_topic", topic: key, checked: nowChecked });
    });

    const lbl = document.createElement("span");
    lbl.className = "checklist-label";
    lbl.textContent = label;

    row.appendChild(cb);
    row.appendChild(lbl);
    container.appendChild(row);
  });
}

function handleChecklistUpdate(msg) {
  // Backend sends { type: "checklist_update", topics: { key: bool, ... } }
  if (msg.topics) {
    Object.assign(_currentChecklist, msg.topics);
    // Re-render if checklist is visible
    const container = document.getElementById("stage-checklist");
    if (container.style.display !== "none" && container.children.length > 0) {
      // Update checkboxes without full re-render
      Array.from(container.children).forEach((row) => {
        const cb = row.querySelector("input[type=checkbox]");
        if (!cb) return;
        const key = Object.keys(STAGE_CHECKLIST).reduce((found, stage) => {
          if (found) return found;
          const items = STAGE_CHECKLIST[stage];
          const idx = Array.from(container.children).indexOf(row);
          return items && items[idx] ? items[idx].key : null;
        }, null);
        if (key && _currentChecklist[key] !== undefined) {
          cb.checked = _currentChecklist[key];
          row.classList.toggle("checked", _currentChecklist[key]);
        }
      });
    }
  }
}

let _currentCallStage = null;

function handleCallGuidance(msg) {
  const { call_stage, opener, next_step } = msg;

  if (call_stage) {
    _currentCallStage = call_stage;
    STAGE_ORDER.forEach((stage) => {
      const el = document.getElementById(`stage-${stage}`);
      if (!el) return;
      el.classList.remove("stage-active", "stage-done");
      const idx = STAGE_ORDER.indexOf(stage);
      const activeIdx = STAGE_ORDER.indexOf(call_stage);
      if (idx < activeIdx) el.classList.add("stage-done");
      else if (idx === activeIdx) el.classList.add("stage-active");
    });
    // Render checklist for this stage
    renderChecklist(call_stage);
  }

  if (opener) {
    const openerEl = document.getElementById("opener-text");
    openerEl.textContent = opener;
    document.getElementById("opener-card").style.display = "flex";
    document.getElementById("next-step-card").style.display = "none";
  }

  if (next_step) {
    const nextStepEl = document.getElementById("next-step-text");
    nextStepEl.textContent = next_step;
    document.getElementById("next-step-card").style.display = "flex";
  }
}

function showCoachingIdle() {
  document.getElementById("coaching-idle").style.display = "flex";
  document.getElementById("coaching-content").style.display = "none";
  document.getElementById("suggestions-list").innerHTML = "";
  document.getElementById("transitions-list").innerHTML = "";
  document.getElementById("score-card").style.display = "none";
  document.getElementById("stage-checklist").style.display = "none";
  _currentChecklist = {};
  _currentCallStage = null;
}

// ── Transcript Download ───────────────────────────────────────────────────

function downloadTranscript() {
  if (!transcriptLog.length) return;
  const now = new Date();
  const stamp = now.toISOString().slice(0, 19).replace(/[T:]/g, "-");
  const lines = transcriptLog.map(t =>
    `${t.speaker.toUpperCase()}: ${t.text}`
  );
  const content = `Call Transcript — ${now.toLocaleString()}\n${"=".repeat(50)}\n\n${lines.join("\n\n")}`;
  const blob = new Blob([content], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `transcript-${stamp}.txt`;
  a.click();
  URL.revokeObjectURL(url);
}

document.getElementById("download-transcript-btn")?.addEventListener("click", downloadTranscript);

document.getElementById("back-to-setup-btn")?.addEventListener("click", () => {
  showSetupScreen();
});

// ── Init ──────────────────────────────────────────────────────────────────

connect();
