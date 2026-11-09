"use strict";
// Motor HMI mobile view: sign in, live status over Server-Sent Events, E-stop.

const $ = (id) => document.getElementById(id);
const HIST_S = 20;            // plot window
const STALE_MS = 2500;        // no update for this long = link lost
const FMT = {
  position: [4, ""], velocity: [3, ""], torque: [3, ""],
  voltage: [1, " V"], temperature: [1, " °C"], motor_temperature: [1, " °C"],
  power: [1, " W"], q_current: [2, " A"], position_error: [4, " rev"],
};
const UNITS = { position: "rev", velocity: "rev/s", torque: "N·m" };

let me = null, es = null, snap = null, selected = null, signal = "position";
let lastUpdate = 0, lastEstop = 0;
const hist = new Map();       // motor id -> [{t, position, velocity, torque}]

async function api(path, opts = {}) {
  const r = await fetch(path, Object.assign({ credentials: "same-origin", cache: "no-store" }, opts));
  let body = null;
  try { body = await r.json(); } catch (e) { /* empty body */ }
  return { ok: r.ok, status: r.status, body };
}

function show(view) {
  $("login-view").hidden = view !== "login";
  $("dash-view").hidden = view !== "dash";
  $("logout").hidden = view !== "dash";
}

function setLink(kind, text) {
  const p = $("link");
  p.className = "pill " + kind;
  p.textContent = text;
}

// ---- sign in / out ----------------------------------------------------------
async function boot() {
  const r = await api("/api/me");
  if (r.ok) { me = r.body; startDash(); }
  else { show("login"); setLink("", "Signed out"); $("user").focus(); }
}

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("login-btn");
  btn.disabled = true;
  $("login-error").textContent = "";
  try {
    const r = await api("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("user").value.trim(), password: $("pass").value }),
    });
    $("pass").value = "";
    if (r.ok) { me = r.body; startDash(); }
    else { $("login-error").textContent = (r.body && r.body.error) || "Sign-in failed"; }
  } catch (e) {
    $("login-error").textContent = "Cannot reach the HMI laptop. Same Wi-Fi?";
  } finally {
    btn.disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  if (es) es.close();
  await api("/api/logout", { method: "POST" }).catch(() => {});
  me = null;
  signedOut("");
});

function signedOut(msg) {
  if (es) { es.close(); es = null; }
  show("login");
  setLink("", "Signed out");
  $("login-error").textContent = msg;
}

// ---- live data ----------------------------------------------------------------
function startDash() {
  show("dash");
  $("estop-dock").hidden = !me.can_estop;
  openStream();
}

function openStream() {
  if (es) es.close();
  setLink("", "Connecting");
  es = new EventSource("/api/stream");
  es.onmessage = (ev) => {
    snap = JSON.parse(ev.data);
    lastUpdate = Date.now();
    record(snap);
    render();
  };
  es.addEventListener("auth", () => signedOut("Your session ended. Sign in again."));
  es.onerror = async () => {
    setLink("lost", "Link lost");
    if (es && es.readyState === EventSource.CLOSED) {
      const r = await api("/api/me").catch(() => ({ status: 0 }));
      if (r.status === 401) signedOut("Your session ended. Sign in again.");
      else setTimeout(() => { if (me) openStream(); }, 3000);
    }
  };
}

function record(s) {
  const t = s.t;              // laptop clock: immune to a phone whose clock is off
  for (const m of s.motors) {
    if (!hist.has(m.id)) hist.set(m.id, []);
    const h = hist.get(m.id);
    h.push({ t, position: m.position, velocity: m.velocity, torque: m.torque });
    while (h.length && t - h[0].t > HIST_S) h.shift();
  }
}

setInterval(() => {
  if ($("dash-view").hidden) return;
  const age = Date.now() - lastUpdate;
  const stale = !lastUpdate || age > STALE_MS;
  $("motor-card").classList.toggle("stale", stale);
  if (stale && lastUpdate) setLink("lost", "No data");
  $("age").textContent = lastUpdate ? (stale ? "Last update " + Math.round(age / 1000) + " s ago." : "") : "";
}, 500);

// ---- rendering -------------------------------------------------------------------
function fmt(name, v) {
  if (v === null || v === undefined) return "-";
  const [d, u] = FMT[name] || [3, ""];
  let x = v.toFixed(d);
  if (/^-0(\.0+)?$/.test(x)) x = x.slice(1);     // no "-0.000"
  return x + u;
}

function motorState(m) {
  if (!m.comm_ok) return ["NO COMM", "nocomm"];
  if (m.tripped) return ["TRIPPED", "trip"];
  if (m.armed) return ["ENABLED", "armed"];
  return ["DISABLED", ""];
}

function render() {
  const s = snap;
  if (!s) return;
  if (s.estop) setLink("stopped", "E-STOP");
  else if (s.connected) setLink("live", "Live");
  else setLink("lost", "Bus off");

  // banner: E-stop first, then active trips, then no bus
  const active = s.alarms.filter((a) => a.active && (a.severity === "TRIP" || a.severity === "ESTOP"));
  const b = $("banner");
  b.className = "banner";
  if (s.estop) { b.textContent = "E-STOP ACTIVE. Reset it at the HMI laptop."; b.hidden = false; }
  else if (active.length) { b.textContent = active.length + (active.length > 1 ? " active alarms" : " active alarm"); b.hidden = false; }
  else if (!s.connected) { b.className = "banner warn"; b.textContent = "The HMI is not connected to the motors."; b.hidden = false; }
  else b.hidden = true;

  // motor tabs
  const ids = s.motors.map((m) => m.id);
  if (!ids.includes(selected)) selected = ids.length ? ids[0] : null;
  const tabs = $("motor-tabs");
  tabs.replaceChildren(...s.motors.map((m) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = m.id === selected ? "on" : "";
    const dot = document.createElement("span");
    dot.className = "dot " + motorState(m)[1];
    btn.append(dot, document.createTextNode("ID " + m.id));
    btn.addEventListener("click", () => { selected = m.id; render(); });
    return btn;
  }));
  tabs.hidden = s.motors.length < 2;

  const m = s.motors.find((x) => x.id === selected);
  const st = $("m-state");
  if (!m) {
    st.textContent = s.connected ? "NO MOTORS" : "NO BUS";
    st.className = "state";
    $("m-mode").textContent = s.link || "";
    $("m-reason").hidden = true;
    for (const k of Object.keys(FMT)) $("v-" + k).textContent = "-";
  } else {
    const [label, cls] = motorState(m);
    st.textContent = label;
    st.className = "state " + cls;
    $("m-mode").textContent = "ID " + m.id + (m.command ? " · " + m.command : "") + (m.state ? " · " + m.state : "");
    const reason = m.tripped ? m.trip_reason : (m.fault ? m.fault_text || ("fault " + m.fault) : "");
    $("m-reason").textContent = reason;
    $("m-reason").hidden = !reason;
    for (const k of Object.keys(FMT)) $("v-" + k).textContent = fmt(k, m[k]);
  }
  drawPlot();
  renderAlarms(s.alarms);
}

function renderAlarms(alarms) {
  const ul = $("alarms");
  if (!alarms.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No alarms";
    ul.replaceChildren(li);
    return;
  }
  ul.replaceChildren(...alarms.map((a) => {
    const li = document.createElement("li");
    li.className = !a.active ? "cleared" : (a.severity === "TRIP" || a.severity === "ESTOP") ? "trip"
      : a.severity === "WARNING" ? "warn" : "";
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = new Date(a.raised_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const mm = document.createElement("span");
    mm.className = "m";
    mm.textContent = a.motor === null ? "All" : "ID " + a.motor;
    const msg = document.createElement("span");
    msg.className = "msg";
    msg.textContent = a.message + (a.active ? "" : " (cleared)");
    li.append(t, mm, msg);
    return li;
  }));
}

function drawPlot() {
  const c = $("plot");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const css = getComputedStyle(document.documentElement);
  const grid = css.getPropertyValue("--grid"), muted = css.getPropertyValue("--muted"), line = css.getPropertyValue("--plot");
  const pts = (hist.get(selected) || []).filter((p) => p[signal] !== null && p[signal] !== undefined);
  const all = hist.get(selected) || [];
  const now = all.length ? all[all.length - 1].t : 0;
  let lo = Infinity, hi = -Infinity;
  for (const p of pts) { lo = Math.min(lo, p[signal]); hi = Math.max(hi, p[signal]); }
  if (!pts.length) { lo = -1; hi = 1; }
  if (hi - lo < 1e-6) { const mid = (hi + lo) / 2; lo = mid - 0.5; hi = mid + 0.5; }
  const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
  const L = 52, R = 6, T = 6, B = 18, pw = w - L - R, ph = h - T - B;
  g.font = "11px system-ui, sans-serif"; g.fillStyle = muted; g.strokeStyle = grid; g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = T + ph * i / 4, v = hi - (hi - lo) * i / 4;
    g.beginPath(); g.moveTo(L, y); g.lineTo(w - R, y); g.stroke();
    g.textAlign = "right"; g.textBaseline = "middle"; g.fillText(v.toFixed(Math.abs(hi - lo) < 1 ? 3 : 2), L - 6, y);
  }
  g.textBaseline = "top";
  for (const s of [-20, -10, 0]) {
    g.textAlign = s === 0 ? "right" : s === -20 ? "left" : "center";
    g.fillText(s === 0 ? "now" : s + " s", L + pw * (1 + s / HIST_S), h - B + 4);
  }
  g.strokeStyle = line; g.lineWidth = 2; g.lineJoin = "round"; g.beginPath();
  pts.forEach((p, i) => {
    const x = L + pw * (1 - (now - p.t) / HIST_S), y = T + ph * (hi - p[signal]) / (hi - lo);
    if (i) g.lineTo(x, y); else g.moveTo(x, y);
  });
  g.stroke();
  $("plot-range").textContent = signal[0].toUpperCase() + signal.slice(1) + " (" + UNITS[signal] + "), last " + HIST_S + " s";
}

for (const b of document.querySelectorAll("#plot-sel button")) {
  b.addEventListener("click", () => {
    signal = b.dataset.sig;
    for (const x of document.querySelectorAll("#plot-sel button")) x.classList.toggle("on", x === b);
    drawPlot();
  });
}
window.addEventListener("resize", drawPlot);

// ---- E-stop: fires on touch-down, not on release ----------------------------------------
async function estop(ev) {
  ev.preventDefault();
  const now = Date.now();
  if (now - lastEstop < 400) return;
  lastEstop = now;
  const btn = $("estop");
  btn.classList.add("sent");
  btn.textContent = "E-STOP SENT";
  let ok = false;
  try {
    const r = await api("/api/estop", { method: "POST", headers: { "X-CSRF-Token": me.csrf } });
    ok = r.ok;
  } catch (e) { ok = false; }
  if (!ok) {
    const b = $("banner");
    b.className = "banner";
    b.textContent = "E-STOP NOT CONFIRMED. Use the laptop or the hardware E-stop!";
    b.hidden = false;
    btn.textContent = "E-STOP (retry)";
  } else {
    setTimeout(() => { btn.textContent = "E-STOP"; btn.classList.remove("sent"); }, 1500);
  }
}
$("estop").addEventListener("pointerdown", estop);
$("estop").addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") estop(ev); });

boot();
