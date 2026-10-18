"use strict";
// Browser side. One WebSocket (api/ws) carries status and commands as JSON and
// always works through a reverse proxy. Audio comes over WebRTC (Opus) when
// the browser can reach the scanner directly; if no WebRTC path forms within
// a few seconds (e.g. a phone on mobile data behind a proxy), audio is sent
// over the WebSocket instead as IMA ADPCM and played with Web Audio.

const $ = (id) => document.getElementById(id);
const audio = $("audio");
const METER_MAX_DB = 40;
const WEBRTC_WAIT_MS = 5000;     // give up on WebRTC after this long
const AUDIO_RATE = 16000;

let ws = null;
let pc = null;
let transport = null;            // "webrtc" | "ws" | null while deciding
let channels = [];
let status = {};
let editing = null;              // id of the channel row being edited
let reconnectDelay = 1000;

// ---- connection --------------------------------------------------------------

function connect() {
  setConn("Connecting…");
  const url = new URL("api/ws", location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => { reconnectDelay = 1000; startAudio(); };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") onMessage(JSON.parse(e.data));
    else playAdpcm(e.data);
  };
  ws.onclose = () => { stopAudio(); scheduleReconnect(); };
}

let reconnectTimer = null;
function scheduleReconnect() {
  if (reconnectTimer) return;
  setConn("Disconnected – retrying", "bad");
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 15000);
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  else toast("Not connected to the scanner");
}

function setConn(text, cls) {
  const el = $("conn");
  el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}

function showTransport() {
  const how = { webrtc: "WebRTC", ws: "WebSocket" }[transport];
  setConn(how ? `Connected · ${how}` : "Connected · starting audio…", "ok");
}

// ---- audio path: WebRTC first, WebSocket as fallback ---------------------------

async function startAudio() {
  stopAudio();
  transport = null;
  showTransport();
  const myPc = pc = new RTCPeerConnection({ iceServers: [] });
  myPc.addTransceiver("audio", { direction: "recvonly" });
  myPc.ontrack = (e) => {
    audio.srcObject = e.streams[0] || new MediaStream([e.track]);
    playAudio();
  };
  myPc.onconnectionstatechange = () => {
    if (myPc !== pc) return;
    if (myPc.connectionState === "connected") {
      transport = "webrtc";
      showTransport();
    } else if (["failed", "disconnected", "closed"].includes(myPc.connectionState)) {
      useWebSocketAudio();
    }
  };
  setTimeout(() => { if (myPc === pc && transport === null) useWebSocketAudio(); }, WEBRTC_WAIT_MS);
  try {
    await myPc.setLocalDescription(await myPc.createOffer());
    await iceGatheringDone(myPc);
    const res = await fetch("api/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: myPc.localDescription.sdp, type: myPc.localDescription.type }),
    });
    if (!res.ok) throw new Error(`server answered ${res.status}`);
    await myPc.setRemoteDescription(await res.json());
  } catch (err) {
    console.warn("WebRTC unavailable, using WebSocket audio", err);
    if (myPc === pc) useWebSocketAudio();
  }
}

function iceGatheringDone(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    pc.addEventListener("icegatheringstatechange", () => {
      if (pc.iceGatheringState === "complete") resolve();
    });
    setTimeout(resolve, 2000);   // host candidates arrive quickly
  });
}

function useWebSocketAudio() {
  if (transport === "ws") return;
  closePc();
  transport = "ws";
  ensureAudioGraph();
  send({ type: "audio", transport: "ws" });
  showTransport();
  playAudio();
}

function closePc() {
  const old = pc;
  pc = null;
  try { old && old.close(); } catch (_) {}
}

function stopAudio() {
  closePc();
  transport = null;
  nextTime = 0;
}

// ---- WebSocket audio: IMA ADPCM decode + scheduled playback ---------------------
// Played through a MediaStream into the <audio> element rather than straight
// to the speakers: one volume control for both paths, and on iPhones it still
// plays with the silent switch on.

let audioCtx = null;
let streamDest = null;
let nextTime = 0;

function ensureAudioGraph() {
  if (audioCtx) {
    audio.srcObject = streamDest.stream;
    return;
  }
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  streamDest = audioCtx.createMediaStreamDestination();
  audio.srcObject = streamDest.stream;
}

const ADPCM_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8];
const ADPCM_STEP = [
  7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
  50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
  253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
  1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
  3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
  11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
  32767];

// Chunk: int16 LE predictor, uint8 step index, 1 spare byte, then two
// samples per byte, low nibble first (see app/adpcm.py).
function decodeAdpcm(buf) {
  const bytes = new Uint8Array(buf);
  const view = new DataView(buf);
  let pred = view.getInt16(0, true);
  let idx = bytes[2];
  const out = new Float32Array((bytes.length - 4) * 2);
  let o = 0;
  for (let i = 4; i < bytes.length; i++) {
    for (const code of [bytes[i] & 0x0f, bytes[i] >> 4]) {
      const step = ADPCM_STEP[idx];
      let diff = step >> 3;
      if (code & 4) diff += step;
      if (code & 2) diff += step >> 1;
      if (code & 1) diff += step >> 2;
      pred += code & 8 ? -diff : diff;
      if (pred > 32767) pred = 32767; else if (pred < -32768) pred = -32768;
      idx += ADPCM_INDEX[code];
      if (idx < 0) idx = 0; else if (idx > 88) idx = 88;
      out[o++] = pred / 32768;
    }
  }
  return out;
}

function playAdpcm(buf) {
  if (transport !== "ws" || !audioCtx) return;
  const samples = decodeAdpcm(buf);
  const now = audioCtx.currentTime;
  if (nextTime < now + 0.05) nextTime = now + 0.25;   // (re)start with a small buffer
  if (nextTime > now + 1.0) return;                   // too far behind: skip to catch up
  const b = audioCtx.createBuffer(1, samples.length, AUDIO_RATE);
  b.getChannelData(0).set(samples);
  const src = audioCtx.createBufferSource();
  src.buffer = b;
  src.connect(streamDest);
  src.start(nextTime);
  nextTime += samples.length / AUDIO_RATE;
}

// ---- playback permission & volume ------------------------------------------------

function playAudio() {
  const waits = [];
  if (audioCtx && audioCtx.state !== "running") waits.push(audioCtx.resume());
  if (audio.srcObject) waits.push(audio.play());
  Promise.all(waits)
    .then(() => { $("unmute").hidden = !(audioCtx && audioCtx.state !== "running"); })
    .catch(() => { $("unmute").hidden = false; });   // browser wants a tap/click first
}
$("unmute").addEventListener("click", playAudio);

function loadVolume() {
  let v = 80, muted = false;
  try {
    v = Number(localStorage.getItem("volume") ?? 80);
    muted = localStorage.getItem("muted") === "1";
  } catch (_) {}
  $("volume").value = v;
  audio.muted = muted;
  applyVolume();
}
function applyVolume() {
  const v = Number($("volume").value);
  audio.volume = v / 100;
  $("volval").textContent = audio.muted ? "muted" : v + "%";
  $("btn-mute").textContent = audio.muted || v === 0 ? "🔇" : "🔊";
  try {
    localStorage.setItem("volume", String(v));
    localStorage.setItem("muted", audio.muted ? "1" : "0");
  } catch (_) {}
}
$("volume").addEventListener("input", () => { audio.muted = false; applyVolume(); });
$("btn-mute").addEventListener("click", () => { audio.muted = !audio.muted; applyVolume(); });

// ---- controls ----------------------------------------------------------------

$("btn-start").addEventListener("click", () => { playAudio(); send({ type: "command", action: "start" }); });
$("btn-pause").addEventListener("click", () => { playAudio(); send({ type: "command", action: "pause" }); });
$("btn-stop").addEventListener("click", () => send({ type: "command", action: "stop" }));

$("addform").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  const hz = mhzToHz(f.freq.value);
  if (hz === null) return toast("Enter the frequency in MHz, e.g. 446.00625");
  send({ type: "channel.add", channel: { name: f.name.value, freq: hz, bw: Number(f.bw.value), enabled: true } });
  f.reset();
  f.name.focus();
});

function mhzToHz(text) {
  const v = Number(String(text).trim().replace(",", "."));
  return Number.isFinite(v) && v > 0 ? Math.round(v * 1e6) : null;
}
const fmtMHz = (hz) => (hz / 1e6).toFixed(5) + " MHz";

// ---- messages ----------------------------------------------------------------

function onMessage(msg) {
  if (msg.type === "status") { status = msg; renderStatus(); renderLevels(); }
  else if (msg.type === "channels") { channels = msg.channels; renderChannels(); }
  else if (msg.type === "error") toast(msg.message);
}

function renderStatus() {
  const s = status;
  const receiving = s.active && s.open;
  let label = { stopped: "STOPPED", scanning: "SCANNING", paused: "PAUSED" }[s.state] || "—";
  if (s.state === "scanning" && s.active) label = receiving ? "RECEIVING" : "WAITING";
  if (s.state === "paused" && receiving) label = "PAUSED · RECEIVING";
  $("state").textContent = label;
  $("state").className = "state" + (receiving ? " rx" : "");
  $("device").textContent = s.device
    ? [s.device, s.serial && `SN ${s.serial}`, `${(s.sample_rate / 1e6).toFixed(3)} MS/s`].filter(Boolean).join(" · ")
    : "";
  $("chname").textContent = s.active ? s.active.name : (s.state === "scanning" ? "…" : " ");
  $("chfreq").textContent = s.active ? fmtMHz(s.active.freq) : " ";
  const lvl = s.active && s.levels ? s.levels[s.active.id] : null;
  $("snr").textContent = lvl ? `SNR ${lvl.snr.toFixed(1)} dB · squelch ${lvl.open_db.toFixed(1)} dB` : "";
  $("meterbar").style.width = (lvl ? pct(lvl.snr) : 0) + "%";
  $("metermark").style.left = (lvl ? pct(lvl.open_db) : 0) + "%";
  $("error").hidden = !s.error;
  $("error").textContent = s.error || "";
  $("btn-start").classList.toggle("active", s.state === "scanning");
  $("btn-pause").classList.toggle("active", s.state === "paused");
  $("btn-stop").classList.toggle("active", s.state === "stopped");
  document.querySelectorAll("#chlist tr").forEach((tr) => {
    tr.classList.toggle("current", !!(s.active && tr.dataset.id === s.active.id));
  });
}

const pct = (db) => Math.max(0, Math.min(100, (db / METER_MAX_DB) * 100));

function renderLevels() {
  const levels = status.levels || {};
  const oor = new Set(status.out_of_range || []);
  for (const ch of channels) {
    const cell = document.querySelector(`#chlist tr[data-id="${ch.id}"] td.level`);
    if (!cell) continue;
    if (oor.has(ch.id)) { cell.innerHTML = '<span class="note">outside receiver range</span>'; continue; }
    const l = levels[ch.id];
    if (!l || l.snr < -90) { cell.innerHTML = ""; continue; }
    cell.innerHTML = `<div class="lvl" title="SNR ${l.snr} dB, squelch opens at ${l.open_db} dB">` +
      `<i class="${l.open ? "open" : ""}" style="width:${pct(l.snr)}%"></i>` +
      `<b style="left:${pct(l.open_db)}%"></b></div>`;
  }
}

function renderChannels() {
  const tbody = $("chlist");
  tbody.innerHTML = "";
  if (!channels.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">No channels yet – add one below.</td></tr>';
  }
  for (const ch of channels) {
    const tr = document.createElement("tr");
    tr.dataset.id = ch.id;
    if (!ch.enabled) tr.classList.add("disabled");
    if (editing === ch.id) {
      // One cell across the row, so it also works when columns are hidden on phones.
      tr.innerHTML = `
        <td colspan="6"><div class="editform">
          <input class="e-name" maxlength="40" placeholder="Channel name" aria-label="Channel name">
          <input class="e-freq" inputmode="decimal" placeholder="MHz" aria-label="Frequency in MHz">
          <select class="e-bw" aria-label="Bandwidth"><option value="12500">12.5 kHz</option><option value="25000">25 kHz</option></select>
          <span class="editbuttons"><button class="e-save">Save</button><button class="e-cancel">Cancel</button></span>
        </div></td>`;
      tr.querySelector(".e-name").value = ch.name;
      tr.querySelector(".e-freq").value = (ch.freq / 1e6).toFixed(5);
      tr.querySelector(".e-bw").value = String(ch.bw);
      tr.querySelector(".e-save").onclick = () => {
        const hz = mhzToHz(tr.querySelector(".e-freq").value);
        if (hz === null) return toast("Enter the frequency in MHz");
        send({ type: "channel.update", id: ch.id, changes: {
          name: tr.querySelector(".e-name").value, freq: hz, bw: Number(tr.querySelector(".e-bw").value) } });
        editing = null;
      };
      tr.querySelector(".e-cancel").onclick = () => { editing = null; renderChannels(); };
    } else {
      tr.innerHTML = `
        <td><input type="checkbox" title="Include in scan"></td>
        <td class="name"></td>
        <td class="freq col-freq"></td>
        <td class="col-bw">${ch.bw === 25000 ? "25k" : "12.5k"}</td>
        <td class="level"></td>
        <td class="actions">
          <button class="small listen" title="Hold on this channel">Listen</button>
          <button class="small edit">Edit</button>
          <button class="small danger delete">Delete</button>
        </td>`;
      tr.querySelector(".name").textContent = ch.name;
      tr.querySelector(".freq").textContent = fmtMHz(ch.freq);
      const cb = tr.querySelector("input[type=checkbox]");
      cb.checked = ch.enabled;
      cb.onchange = () => send({ type: "channel.update", id: ch.id, changes: { enabled: cb.checked } });
      tr.querySelector(".listen").onclick = () => { playAudio(); send({ type: "hold", id: ch.id }); };
      tr.querySelector(".listen").disabled = !ch.enabled;
      tr.querySelector(".edit").onclick = () => { editing = ch.id; renderChannels(); };
      const del = tr.querySelector(".delete");
      del.onclick = () => {
        // Two-step delete instead of confirm() dialogs.
        if (del.dataset.armed) return send({ type: "channel.delete", id: ch.id });
        del.dataset.armed = "1";
        del.textContent = "Sure?";
        setTimeout(() => { delete del.dataset.armed; del.textContent = "Delete"; }, 3000);
      };
    }
    tbody.appendChild(tr);
  }
  renderStatus();
  renderLevels();
}

let toastTimer = null;
function toast(text) {
  let el = document.querySelector(".toast");
  if (!el) { el = document.createElement("div"); el.className = "toast"; document.body.appendChild(el); }
  el.textContent = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 4000);
}

loadVolume();
connect();
