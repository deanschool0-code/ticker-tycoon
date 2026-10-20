# Add Ticker Tycoon to Matt (port 5557)

Three small pieces: a backend route that starts the game, the tile HTML, and the JS that shows live status.

## 1. Backend (Matt's Flask server, e.g. `server.py` / `app.py`)

```python
import os, subprocess, requests
from flask import jsonify

TICKER_TYCOON_DIR = os.path.expanduser("~/Desktop/TickerTycoon")
TICKER_TYCOON_URL = "http://localhost:5558"

@app.route("/api/apps/tickertycoon/status")
def tickertycoon_status():
    try:
        r = requests.get(TICKER_TYCOON_URL + "/api/health", timeout=1.5)
        return jsonify({"running": r.ok, "url": TICKER_TYCOON_URL})
    except Exception:
        return jsonify({"running": False, "url": TICKER_TYCOON_URL})

@app.route("/api/apps/tickertycoon/start", methods=["POST"])
def tickertycoon_start():
    if not os.path.isdir(TICKER_TYCOON_DIR):
        return jsonify({"ok": False, "error": "~/Desktop/TickerTycoon not found"}), 404
    log = open(os.path.join(TICKER_TYCOON_DIR, "server.log"), "a")
    subprocess.Popen(["bash", "run.sh"], cwd=TICKER_TYCOON_DIR, stdout=log, stderr=log,
                     start_new_session=True)  # keeps running if Matt restarts
    return jsonify({"ok": True, "url": TICKER_TYCOON_URL})
```

## 2. Tile HTML (inside Matt's app grid on Home)

```html
<div class="app-tile" id="tile-tickertycoon">
  <div class="app-tile-icon" style="background:linear-gradient(135deg,#1f2a44,#0d1220)">
    <svg viewBox="0 0 64 64" width="28" height="28">
      <rect x="14" y="37" width="8" height="10" rx="2" fill="#ff453a"/>
      <rect x="28" y="28" width="8" height="14" rx="2" fill="#30d158"/>
      <rect x="42" y="18" width="8" height="18" rx="2" fill="#30d158"/>
      <path d="M41 11 l5 -4 l5 4" fill="none" stroke="#ffd60a" stroke-width="3" stroke-linecap="round"/>
    </svg>
  </div>
  <div class="app-tile-body">
    <div class="app-tile-name">Ticker Tycoon</div>
    <div class="app-tile-sub"><span class="tt-dot"></span><span id="tt-status">Checking...</span> &middot; :5558</div>
  </div>
  <button class="app-tile-btn" id="tt-start">Start</button>
</div>
<style>
  .tt-dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:#6b6b76; margin-right:6px; }
  .tt-dot.on { background:#30d158; box-shadow:0 0 8px rgba(48,209,88,.6); }
</style>
```

## 3. JS (Matt's `static/app.js`)

```javascript
async function ttRefresh() {
  try {
    const s = await (await fetch('/api/apps/tickertycoon/status')).json();
    document.querySelector('#tile-tickertycoon .tt-dot').classList.toggle('on', s.running);
    document.getElementById('tt-status').textContent = s.running ? 'Running' : 'Stopped';
    document.getElementById('tt-start').textContent = s.running ? 'Open' : 'Start';
  } catch (e) { /* Matt offline */ }
}
document.getElementById('tt-start').addEventListener('click', async () => {
  const btn = document.getElementById('tt-start');
  if (btn.textContent === 'Open') { window.open('http://localhost:5558', '_blank'); return; }
  btn.textContent = 'Starting...'; btn.disabled = true;
  await fetch('/api/apps/tickertycoon/start', { method: 'POST' });
  for (let i = 0; i < 60; i++) {           // first start installs packages, can take ~1 min
    await new Promise(r => setTimeout(r, 1500));
    await ttRefresh();
    if (btn.textContent === 'Open') { window.open('http://localhost:5558', '_blank'); break; }
  }
  btn.disabled = false;
});
ttRefresh();
setInterval(ttRefresh, 10000);
```

If Matt keeps its apps in a list in `config.py`, the equivalent entry is:

```python
{"id": "tickertycoon", "name": "Ticker Tycoon", "port": 5558,
 "path": "~/Desktop/TickerTycoon", "start": "bash run.sh", "health": "/api/health"}
```
