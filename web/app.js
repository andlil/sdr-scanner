"use strict";
// Browser side: one WebRTC connection carries the audio (Opus track) and a
// "control" data channel with JSON status/commands.

const $ = (id) => document.getElementById(id);
const audio = $("audio");
const METER_MAX_DB = 40;

let pc = null;
let dc = null;
let channels = [];
let status = {};
let editing = null;          // id of the channel row being edited
let reconnectDelay = 1000;

// ---- connection --------------------------------------------------------------

async function connect() {
  setConn("Connecting…");
  pc = new RTCPeerConnection({ iceServers: [] });
  pc.addTransceiver("audio", { direction: "recvonly" });
  pc.ontrack = (e) => {
    audio.srcObject = e.streams[0] || new MediaStream([e.track]);
    playAudio();
  };
  dc = pc.createDataChannel("control");
  dc.onopen = () => { setConn("Connected", "ok"); reconnectDelay = 1000; };
  dc.onmessage = (e) => onMessage(JSON.parse(e.data));
  dc.onclose = () => scheduleReconnect();
  pc.onconnectionstatechange = () => {
    if (["failed", "disconnected", "closed"].includes(pc.connectionState)) scheduleReconnect();
  };

  try {
    await pc.setLocalDescription(await pc.createOffer());
    await iceGatheringDone(pc);
    const res = await fetch("/api/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
    });
    if (!res.ok) throw new Error(`server answered ${res.status}`);
    await pc.setRemoteDescription(await res.json());
  } catch (err) {
    console.error(err);
    scheduleReconnect();
  }
}

function iceGatheringDone(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { if (pc.iceGatheringState === "complete") resolve(); };
    pc.addEventListener("icegatheringstatechange", done);
    setTimeout(resolve, 2000);   // LAN host candidates arrive quickly
  });
}

let reconnectTimer = null;
function scheduleReconnect() {
  if (reconnectTimer) return;
  setConn("Disconnected – retrying", "bad");
  try { pc && pc.close(); } catch (_) {}
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 15000);
}

function send(msg) {
  if (dc && dc.readyState === "open") dc.send(JSON.stringify(msg));
  else toast("Not connected to the scanner");
}

function setConn(text, cls) {
  const el = $("conn");
  el.textContent = text;
  el.className = "pill" + (cls ? " " + cls : "");
}

// ---- audio & volume ----------------------------------------------------------

function playAudio() {
  audio.play().then(() => { $("unmute").hidden = true; })
    .catch(() => { $("unmute").hidden = false; });   // browser wants a click first
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
  $("device").textContent = s.device ? `${s.device} · ${(s.sample_rate / 1e6).toFixed(3)} MS/s` : "";
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
      tr.innerHTML = `
        <td></td>
        <td><input class="e-name" maxlength="40"></td>
        <td><input class="e-freq" inputmode="decimal"></td>
        <td><select class="e-bw"><option value="12500">12.5k</option><option value="25000">25k</option></select></td>
        <td></td>
        <td class="actions"><button class="small e-save">Save</button><button class="small e-cancel">Cancel</button></td>`;
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
        <td class="freq"></td>
        <td>${ch.bw === 25000 ? "25k" : "12.5k"}</td>
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
