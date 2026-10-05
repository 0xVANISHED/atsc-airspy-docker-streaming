#!/usr/bin/env python3
"""Dumb-simple web TV for the LAN.

  /                    one page: channels with reception, optional player, rescan
  /api/state           JSON for that page
  /api/events          the same, pushed live (Server-Sent Events) whenever it changes
  POST /api/rescan     ask the tuner for a fresh channel scan
  /live/<number>.mp4   e.g. /live/8.1.mp4: the channel transcoded for browsers

Browsers can't play broadcast MPEG-2/AC-3, so each viewer gets an ffmpeg that
pulls the channel from Tvheadend and re-encodes it to H.264/AAC fragmented MP4,
at most 720p (on the GPU via VAAPI when /dev/dri is there, otherwise libx264).
The ffmpeg dies when the browser disconnects, which frees the tuner. Opening a
new channel kills that browser's previous stream first, so switching to a
channel on another RF channel retunes the Airspy instead of finding it busy.

After every finished scan (from the page, the CLI, or the tuner's start-up
scan) this runs atsc/tvh_sync.py, so Tvheadend and the page follow by
themselves.
"""
import json, os, re, subprocess, sys, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TVH = os.environ.get("TVH_URL", "http://127.0.0.1:9981")
TUNER = os.environ.get("TUNER_URL", "http://127.0.0.1:5600")
PORT = int(os.environ.get("PORT", "80"))
ATSC = os.environ.get("ATSC_DIR", "/atsc")            # the repo's atsc/ directory (read-only)
CONFIG = os.path.join(ATSC, "config")
HEIGHT = int(os.environ.get("HEIGHT", "720"))          # max output height
BITRATE = os.environ.get("BITRATE", "3M")
RENDER = "/dev/dri/renderD128"

procs, procs_lock = {}, threading.Lock()   # client IP -> its ffmpeg
live = {"json": None, "seq": 0, "listeners": 0}    # latest state for /api/events
live_cond = threading.Condition()
sync = {"running": False, "finished": None, "ok": None, "output": "", "for_scan": None}


def auth_code():
    with open(os.path.join(CONFIG, "viewer.json")) as f:
        return json.load(f)["auth"]


def get_json(url, data=None, timeout=3):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:      # e.g. 409 "scan already running" still has a JSON body
        return json.load(e)


def run_sync(scan_finished):
    """Make Tvheadend match the scan that just finished (atsc/tvh_sync.py)."""
    sync.update(running=True, for_scan=scan_finished)
    print("webtv: scan finished, syncing Tvheadend", file=sys.stderr, flush=True)
    try:
        p = subprocess.run([sys.executable, os.path.join(ATSC, "tvh_sync.py")],
                           capture_output=True, text=True, timeout=300)
        sync.update(ok=p.returncode == 0, output=(p.stdout + p.stderr).strip())
    except Exception as e:
        sync.update(ok=False, output=str(e))
    sync.update(running=False, finished=time.time())
    print(f"webtv: sync {'done' if sync['ok'] else 'FAILED'}\n{sync['output']}", file=sys.stderr, flush=True)


def sync_watcher():
    """Sync after every scan the tuner finishes (and once at start-up)."""
    while True:
        try:
            scan = get_json(f"{TUNER}/status")["scan"]
            if not scan["running"] and scan["finished"] and scan["finished"] != sync["for_scan"]:
                run_sync(scan["finished"])
        except Exception:
            pass
        time.sleep(2)


def state():
    """Channels Tvheadend offers, grouped with the last scan, plus tuner and sync."""
    out = {"channels": [], "scan": None, "tuner": None, "sync": dict(sync), "error": None, "auth": None}
    try:
        with open(os.path.join(CONFIG, "channels.json")) as f:
            out["scan"] = json.load(f)
    except (OSError, ValueError):
        pass
    rf_of = {s["number"]: c["rf"] for c in (out["scan"] or {}).get("channels", [])
             for s in c.get("services", [])}
    try:
        out["auth"] = auth_code()
        with urllib.request.urlopen(f"{TVH}/playlist/auth/channels.m3u?auth={out['auth']}", timeout=5) as r:
            m3u = r.read().decode()
        for num, name in re.findall(r'tvg-chno="([^"]+)",(.*)', m3u):
            out["channels"].append({"number": num, "name": name.strip(), "rf": rf_of.get(num)})
        out["channels"].sort(key=lambda c: [int(x) for x in c["number"].split(".") if x.isdigit()])
    except Exception as e:
        out["error"] = f"can't list channels from Tvheadend: {e}"
    try:
        out["tuner"] = get_json(f"{TUNER}/status")
    except Exception:
        pass
    return out


def state_loop():
    """Build the state once a second while pages are open; wake them on change."""
    while True:
        with live_cond:
            live_cond.wait_for(lambda: live["listeners"] > 0)
        js = json.dumps(state())
        with live_cond:
            if js != live["json"]:
                live.update(json=js, seq=live["seq"] + 1)
                live_cond.notify_all()
        time.sleep(1)


def ffmpeg_cmd(number):
    src = f"{TVH}/stream/channelnumber/{number}?auth={auth_code()}&profile=pass"
    # Half-second fragments and 1 s keyframes so data reaches the browser evenly.
    out = ["-map", "0:v:0", "-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
           "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
           "-frag_duration", "500000", "-flush_packets", "1", "pipe:1"]
    # -rw_timeout: give up after 15 s without data (no lock, tuner busy) so a
    # stalled ffmpeg can't keep holding a Tvheadend subscription (and the tuner)
    # nice 19: the ATSC decoder on the same machine must always win the CPU.
    # Broadcast reception has errors: drop packets the stream itself flags as
    # corrupt (a garbled header can otherwise abort the GPU decoder) and keep
    # decoding past errors, like VLC does.
    base = ["nice", "-n", "19", "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-threads", "2", "-rw_timeout", "15000000",
            "-fflags", "+discardcorrupt+genpts", "-err_detect", "ignore_err"]
    rate = ["-b:v", BITRATE, "-maxrate", BITRATE, "-bufsize", BITRATE, "-g", "30"]
    if os.path.exists(RENDER):
        return base + ["-hwaccel", "vaapi", "-hwaccel_device", RENDER, "-hwaccel_output_format", "vaapi",
                       "-i", src, "-vf", f"deinterlace_vaapi=auto=1,scale_vaapi=w=-2:h=min({HEIGHT}\\,ih)",
                       "-c:v", "h264_vaapi"] + rate + out
    return base + ["-i", src, "-vf", f"yadif=deint=interlaced,scale=-2:min({HEIGHT}\\,ih)",
                   "-c:v", "libx264", "-preset", "veryfast"] + rate + out


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web TV</title>
<style>
  :root { color-scheme: light dark; --bg:#f6f6f4; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff; --line:#ddd;
          --accent:#2457d6; --bad:#b3261e; }
  @media (prefers-color-scheme: dark) { :root { --bg:#151514; --fg:#ececea; --muted:#9a9a94; --card:#20201e;
          --line:#33332f; --accent:#7da2ff; --bad:#ff8a80; } }
  * { box-sizing:border-box; }
  body { margin:0; font:16px/1.4 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  main { max-width:1100px; margin:0 auto; padding:16px; }
  header { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:12px; }
  h1 { font-size:20px; margin:0; flex:1; }
  h2 { font-size:15px; margin:22px 0 8px; color:var(--muted); }
  .toggle { display:flex; align-items:center; gap:8px; font-size:14px; cursor:pointer; user-select:none; }
  .toggle input { width:20px; height:20px; }
  video { width:100%; aspect-ratio:16/9; background:#000; border-radius:8px; display:block; }
  #msg { min-height:1.4em; margin:8px 0 2px; color:var(--muted); } #msg.bad { color:var(--bad); }
  #tuner { font-size:14px; color:var(--muted); display:flex; align-items:center; flex-wrap:wrap; gap:4px 8px; }
  .rf { margin:14px 0 6px; font-size:13px; color:var(--muted); display:flex; align-items:center; flex-wrap:wrap; gap:4px 8px; }
  .chs { display:grid; grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); gap:8px; }
  button, .chs a { font:inherit; padding:10px 12px; min-height:44px; border-radius:8px; border:1px solid var(--line);
           background:var(--card); color:var(--fg); cursor:pointer; text-align:left; text-decoration:none;
           display:flex; align-items:center; gap:6px; }
  button:hover, .chs a:hover { border-color:var(--accent); }
  .chs .on { background:var(--accent); border-color:var(--accent); color:#fff; }
  .chs b { font-variant-numeric:tabular-nums; }
  .chs .nm { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #rescan { width:100%; justify-content:center; font-size:18px; font-weight:600; padding:14px; margin-top:4px;
            background:var(--accent); border-color:var(--accent); color:#fff; }
  #rescan:disabled { opacity:.6; cursor:wait; }
  #rescanmsg { font-size:13px; color:var(--muted); margin:6px 0 0; white-space:pre-wrap; }
  table { width:100%; border-collapse:collapse; font-size:14px; background:var(--card); border-radius:8px; overflow:hidden; }
  th, td { text-align:left; padding:8px; border-bottom:1px solid var(--line); vertical-align:top; }
  td.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .muted { color:var(--muted); } .bad { color:var(--bad); }
  .wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; }
  .q { display:inline-flex; align-items:center; gap:5px; font-size:13px; font-weight:600; white-space:nowrap; }
  .chs .q { margin-left:auto; }
  .q i { width:12px; height:12px; border-radius:50%; background:currentColor; box-shadow:0 0 0 1px rgba(0,0,0,.15); }
  .q-green { color:#1e9e4a; } .q-yellow { color:#c99a00; } .q-orange { color:#ef7d00; } .q-red { color:#d93025; }
  .chs .on .q { color:#fff; }
  .legend { font-size:12px; color:var(--muted); margin-top:10px; display:flex; flex-wrap:wrap; gap:4px 12px; }
  .legend .q { font-weight:400; }
  @media (max-width:600px) {
    main { padding:12px; } h1 { font-size:18px; }
    .chs { grid-template-columns:1fr 1fr; }
    th:nth-child(4), td:nth-child(4) { display:none; }   /* ripple column */
  }
</style></head>
<body><main>
<header>
  <h1>Web TV</h1>
  <label class="toggle"><input type="checkbox" id="usePlayer"> Play in this page</label>
</header>
<div id="player">
  <video id="v" controls playsinline preload="auto"></video>
  <div id="msg">Pick a channel.</div>
</div>
<div id="tuner"></div>

<h2>Channels</h2>
<div id="list"></div>
<div class="legend"><span>Reception:</span>
  <span class="q q-green"><i></i>99–100% clean</span><span class="q q-yellow"><i></i>95–98% some glitches</span>
  <span class="q q-orange"><i></i>80–94% frequent glitches</span><span class="q q-red"><i></i>below 80% / no signal</span>
  <span>live while tuned, otherwise from the last scan</span></div>
<p class="muted" style="font-size:13px" id="hint"></p>

<h2>Scan</h2>
<button id="rescan">Rescan channels</button>
<div id="rescanmsg"></div>
<div class="wrap" style="margin-top:10px"><table id="scan"></table></div>
</main>
<script>
const $ = id => document.getElementById(id);
const v = $("v"), msg = $("msg"), usePlayer = $("usePlayer");
let current = null, last = null, wantPlay = false, retries = 0;
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function say(text, bad) { msg.textContent = text; msg.className = bad ? "bad" : ""; }

// Player is optional (per device). Off: channels open the broadcast stream in
// the device's own player app (e.g. VLC), which also covers iPhones.
let pref = null; try { pref = localStorage.getItem("player"); } catch (e) {}
usePlayer.checked = pref ? pref === "on" : !matchMedia("(max-width: 600px)").matches;
function stopPlayer() { wantPlay = false; current = null; v.removeAttribute("src"); v.load(); }
function applyPlayer() {
  $("player").style.display = usePlayer.checked ? "" : "none";
  $("hint").textContent = usePlayer.checked
    ? "Channels on the RF channel being watched switch instantly; another RF channel retunes the receiver (a few seconds)."
    : "Tap a channel to open it in your video player app (e.g. VLC). Turn on “Play in this page” to watch here.";
  if (!usePlayer.checked && current) stopPlayer();
  render();
}
usePlayer.onchange = () => { try { localStorage.setItem("player", usePlayer.checked ? "on" : "off"); } catch (e) {} applyPlayer(); };

function directUrl(n) { return `http://${location.hostname}:9981/stream/channelnumber/${encodeURIComponent(n)}?auth=${last.auth}`; }

// Live stream, so play only with ~2 s buffered (like VLC's network cache) and
// re-buffer to 2 s after a stall instead of stuttering on a sliver of data.
const CUSHION = 2;
function ahead() { const b = v.buffered; return b.length ? b.end(b.length - 1) - v.currentTime : 0; }
function maybeStart() {
  if (!wantPlay || !v.paused || ahead() < CUSHION) return;
  if (ahead() > CUSHION + 6) v.currentTime = v.buffered.end(v.buffered.length - 1) - CUSHION;  // fell behind live
  v.play().catch(() => {});
}
v.addEventListener("progress", maybeStart);
v.addEventListener("canplay", maybeStart);
v.addEventListener("waiting", () => { if (wantPlay && current) { v.pause(); say(`Buffering ${current.number} ${current.name}…`); } });
v.addEventListener("playing", () => { retries = 0; current && say(`Playing ${current.number} ${current.name}`); });
// The stream can end on a reception dropout; reconnect a few times by itself.
function retry() {
  if (!wantPlay || !current || retries >= 5) return false;
  retries++;
  say(`Reconnecting ${current.number} ${current.name}…`);
  const ch = current;
  setTimeout(() => { if (wantPlay && current === ch) v.src = `/live/${encodeURIComponent(ch.number)}.mp4?r=${Date.now()}`; }, 1500);
  return true;
}
v.addEventListener("ended", retry);
v.addEventListener("pause", () => { if (wantPlay && !v.ended) setTimeout(maybeStart, 250); });
v.addEventListener("error", () => current && v.getAttribute("src") && !retry() &&
  say(`Couldn't play ${current.number} ${current.name}: no signal, the receiver is busy, or this browser can't play the stream (turn the player off to use an app).`, true));

function play(ch) {
  const t = last && last.tuner;
  if (t && t.scan && t.scan.running) { say("A channel scan is running; try again when it's done.", true); return; }
  if (t && t.rf && ch.rf && t.rf !== ch.rf && t.clients > 0 && !(current && current.rf === t.rf)) {
    say(`The receiver is busy on RF ${t.rf} (someone else is watching). Pick a channel on RF ${t.rf}, or wait.`, true);
    return;
  }
  current = ch; wantPlay = true; retries = 0;
  say(`Tuning ${ch.number} ${ch.name}…`);
  v.src = `/live/${encodeURIComponent(ch.number)}.mp4`;
  render();
}

// Reception = 100 - packet error %: 100% is a perfect signal.
function badge(errors, source) {
  const none = errors == null || errors >= 100;
  const pct = none ? 0 : Math.max(0, Math.round(100 - errors));
  const cls = none ? "q-red" : pct >= 99 ? "q-green" : pct >= 95 ? "q-yellow" : pct >= 80 ? "q-orange" : "q-red";
  const label = none ? "no signal" : `${pct}%`;
  return `<span class="q ${cls}" title="Reception ${label} (${source})"><i></i>${label}</span>`;
}
function rfErrors(rf) {
  const t = last && last.tuner;
  if (t && t.rf != null && String(t.rf) === String(rf) && t.tuned_for_s >= 3)
    return [t.locked ? t.errors_pct : null, "live"];
  const s = ((last && last.scan || {}).channels || []).find(c => String(c.rf) === String(rf));
  return [s ? s.errors_pct : null, "last scan"];
}

function render() {
  if (!last) return;
  const t = last.tuner, scanning = !!(t && t.scan && t.scan.running), syncing = last.sync.running;
  $("tuner").innerHTML = !t ? "Receiver: not reachable" : scanning ? "Receiver: scanning for channels" :
    t.rf == null ? "Receiver: idle (starts when someone watches)" :
    `Receiver: RF ${t.rf} · ${t.locked ? "locked" : "no lock"} · ${t.clients} stream(s) ` +
    (t.tuned_for_s >= 3 ? badge(t.locked ? t.errors_pct : null, "live") : "");

  const groups = {};
  for (const c of last.channels) (groups[c.rf ?? "?"] ||= []).push(c);
  const scanned = Object.fromEntries(((last.scan || {}).channels || []).map(c => [c.rf, c]));
  $("list").innerHTML = last.error ? `<p class="bad">${esc(last.error)}</p>` :
    !last.channels.length ? `<p class="muted">No channels yet${scanning ? " (scan in progress)" : ""}.</p>` :
    Object.keys(groups).map(rf => {
      const s = scanned[rf], tuned = t && String(t.rf) === rf, [err, src] = rfErrors(rf);
      return `<div class="rf">RF ${esc(rf)}${s ? ` · ${s.freq_mhz} MHz` : ""}${tuned ? " · tuned" : ""}${badge(err, src)}</div><div class="chs">` +
        groups[rf].map(c => {
          const inner = `<b>${esc(c.number)}</b><span class="nm">${esc(c.name)}</span>${badge(err, src)}`;
          return usePlayer.checked
            ? `<button data-n="${esc(c.number)}" class="${current && current.number === c.number ? "on" : ""}">${inner}</button>`
            : `<a href="${esc(directUrl(c.number))}">${inner}</a>`;
        }).join("") + "</div>";
    }).join("");
  for (const b of document.querySelectorAll("#list button"))
    b.onclick = () => play(last.channels.find(c => c.number === b.dataset.n));

  const btn = $("rescan"), sc = t && t.scan;
  btn.disabled = scanning || syncing || !t;
  btn.textContent = scanning
    ? `Scanning… ${sc.phase === "identify" ? "identifying stations" : "checking frequencies"}${sc.rf ? ` · RF ${sc.rf}` : ""} (${sc.done + 1}/${sc.total || "?"})`
    : syncing ? "Updating channel list…" : "Rescan channels";
  $("rescanmsg").textContent = scanning || syncing ? "TV is paused while scanning (about 2 minutes)." :
    sc && sc.error ? `Last scan failed: ${sc.error}` :
    last.sync.ok === false ? `Updating Tvheadend failed:\n${last.sync.output}` : "";

  const scan = last.scan, offered = new Set(last.channels.map(c => c.rf));
  $("scan").innerHTML = !scan ? `<tr><td class="muted">No scan yet.</td></tr>` :
    `<tr><th>RF</th><th>Reception</th><th>Stations</th><th class="n">Ripple</th><th>Status</th></tr>` +
    scan.channels.map(c => {
      const names = (c.services || []).map(s => `${s.number} ${s.name}`).join(", ");
      let status;
      if (c.kind !== "atsc1") status = `<span class="muted">ATSC 3.0? (can't decode)</span>`;
      else if (offered.has(c.rf)) status = "available";
      else if ((c.errors_pct ?? 100) >= 100) status = `<span class="bad">no lock${(c.ripple_db || 0) > 10 ? " (multipath)" : ""}</span>`;
      else status = `<span class="bad">too weak</span>`;
      return `<tr><td>${c.rf}<div class="muted" style="font-size:12px">${c.freq_mhz} MHz</div></td>` +
        `<td>${c.kind === "atsc1" ? badge(c.errors_pct, "last scan") : "–"}</td>` +
        `<td>${esc(names) || '<span class="muted">–</span>'}</td>` +
        `<td class="n">${c.ripple_db != null ? c.ripple_db.toFixed(1) + " dB" : "–"}</td><td>${status}</td></tr>`;
    }).join("") + `<tr><td colspan="5" class="muted">Scanned ${esc(scan.scanned)} at gain ${scan.gain}</td></tr>`;
}

$("rescan").onclick = async () => {
  if (!confirm("Rescan all channels? TV stops for about 2 minutes.")) return;
  if (current) stopPlayer();
  $("rescan").disabled = true;
  try { await fetch("/api/rescan", {method: "POST"}); } catch (e) {}
  refresh();
};

// Live updates pushed by the server (Server-Sent Events; the browser reconnects
// by itself). Polling is only a fallback for browsers without EventSource.
async function refresh() {
  try { last = await (await fetch("/api/state")).json(); render(); } catch (e) {}
}
applyPlayer(); refresh();
if (window.EventSource) {
  new EventSource("/api/events").onmessage = e => { last = JSON.parse(e.data); render(); };
} else {
  setInterval(refresh, 3000);
}
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_body(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self.send_body(200, "text/html; charset=utf-8", PAGE.encode())
        if path == "/api/state":
            return self.send_body(200, "application/json", json.dumps(state()).encode())
        if path == "/api/events":
            return self.events()
        m = re.fullmatch(r"/live/(\d+(?:\.\d+)?)\.mp4", path)
        if not m:
            return self.send_body(404, "text/plain", b"not found\n")
        self.stream(m.group(1))

    def events(self):
        """Server-Sent Events: push the state whenever it changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with live_cond:
            live["listeners"] += 1
            live_cond.notify_all()
        seen = -1
        try:
            while True:
                with live_cond:
                    live_cond.wait_for(lambda: live["seq"] != seen, timeout=15)
                    js, seq = live["json"], live["seq"]
                if seq != seen and js:
                    self.wfile.write(f"data: {js}\n\n".encode())
                    seen = seq
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with live_cond:
                live["listeners"] -= 1

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/rescan":
            return self.send_body(404, "text/plain", b"not found\n")
        try:
            r = get_json(f"{TUNER}/scan", data=b"", timeout=10)
        except Exception as e:
            r = {"started": False, "error": str(e)}
        print(f"webtv: {self.client_address[0]} asked for a rescan: {'started' if r.get('started') else 'refused'}",
              file=sys.stderr, flush=True)
        self.send_body(202 if r.get("started") else 409, "application/json", json.dumps(r).encode())

    def stream(self, number):
        ip = self.client_address[0]
        with procs_lock:
            old = procs.pop(ip, None)
        if old:                              # this browser switched channel: free the tuner first
            old.kill()
            old.wait()
        try:
            p = subprocess.Popen(ffmpeg_cmd(number), stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        except OSError as e:
            return self.send_body(503, "text/plain", f"can't start ffmpeg: {e}\n".encode())
        with procs_lock:
            procs[ip] = p
        print(f"webtv: {ip} watching {number}", file=sys.stderr, flush=True)
        try:
            first = p.stdout.read1(65536)
            if not first:                    # Tvheadend refused (no signal / tuner busy / scanning)
                return self.send_body(503, "text/plain", b"channel unavailable\n")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(first)
            while chunk := p.stdout.read1(65536):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            p.kill()
            p.wait()
            with procs_lock:
                if procs.get(ip) is p:
                    del procs[ip]
            print(f"webtv: {ip} stopped {number}", file=sys.stderr, flush=True)


def main():
    threading.Thread(target=sync_watcher, daemon=True).start()
    threading.Thread(target=state_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    print(f"webtv: listening on port {PORT} ({'VAAPI' if os.path.exists(RENDER) else 'software'} transcoding,"
          f" up to {HEIGHT}p)", file=sys.stderr, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
