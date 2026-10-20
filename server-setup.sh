#!/bin/bash
# Ticker Tycoon - server setup (run this ON THE SERVER MAC, from inside the TickerTycoon folder):
#
#     cd ~/Desktop/TickerTycoon && bash server-setup.sh
#
# What it does:
#   1. Stops SchoolMarket for good (its background service is disabled and moved aside, NOT deleted).
#   2. Installs Ticker Tycoon into ~/TickerTycoon-server (background apps can't read Desktop/Documents).
#   3. Runs it on port 5558 in the background, starting automatically at login and restarting if it crashes.
#   4. Points https://schoolmarket.win at it through your existing Cloudflare tunnel.
# Run it again any time to update the server to the latest code. Player accounts and progress are kept.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/TickerTycoon-server"
APPDIR="$HOME/Library/Application Support/TickerTycoon"
VENV="$APPDIR/venv"
PORT=5558
DOMAIN="schoolmarket.win"
LABEL="win.schoolmarket.tickertycoon"
AG="$HOME/Library/LaunchAgents"
ok()   { printf "  \033[32mOK\033[0m  %s\n" "$1"; }
warn() { printf "  \033[33m!!\033[0m  %s\n" "$1"; }
step() { printf "\n\033[1m%s\033[0m\n" "$1"; }

step "1/5  Stopping SchoolMarket"
mkdir -p "$AG"
found=0
for f in "$AG"/*.plist; do
  [ -e "$f" ] || continue
  if grep -qi "schoolmarket" "$f" && [ "$(basename "$f")" != "$LABEL.plist" ]; then
    launchctl unload -w "$f" 2>/dev/null || true
    mkdir -p "$AG/disabled"
    mv "$f" "$AG/disabled/"
    ok "Disabled background service $(basename "$f") (moved to ~/Library/LaunchAgents/disabled)"
    found=1
  fi
done
launchctl unload "$AG/$LABEL.plist" 2>/dev/null || true   # our own old copy, if updating
pids="$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$pids" ]; then
  # whatever owns the port may be restarted by a background service under another name: find it by its folder
  for p in $pids; do
    cwd="$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    if [ -n "$cwd" ] && [ "$cwd" != "/" ] && [ "$cwd" != "$HOME" ]; then
      for f in "$AG"/*.plist; do
        [ -e "$f" ] || continue
        b="$(basename "$f" | tr 'A-Z' 'a-z')"
        if grep -qF "$cwd" "$f" && [ "$(basename "$f")" != "$LABEL.plist" ] && ! grep -q cloudflared "$f"; then
          if [[ "$b" == *scholars* && "$b" != *schoolmarket* ]]; then
            warn "Port $PORT is used by $(basename "$f") (Scholars). Left it alone; stop SchoolMarket inside Scholars by hand."
            continue
          fi
          launchctl unload -w "$f" 2>/dev/null || true
          mkdir -p "$AG/disabled"; mv "$f" "$AG/disabled/"
          ok "Disabled background service $(basename "$f") that ran from $cwd"
          found=1
        fi
      done
    fi
  done
  for p in $pids; do
    echo "      stopping $(ps -o command= -p "$p" | cut -c1-80) (pid $p)"
    kill "$p" 2>/dev/null || true
  done
  sleep 2
  pids="$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null || true)"
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
  ok "Port $PORT is free"
else
  ok "Nothing was running on port $PORT"
fi
[ "$found" = 0 ] && echo "      (no SchoolMarket background service found; if you start it by hand, don't anymore)"

step "2/5  Installing Ticker Tycoon into $DEST"
mkdir -p "$DEST" "$APPDIR" "$DEST/logs"
rsync -a --delete --exclude ".venv" --exclude "logs" --exclude "game.db*" --exclude ".env" --exclude "__pycache__" "$SRC/" "$DEST/"
if [ ! -f "$DEST/.env" ]; then
  if [ -f "$SRC/.env" ]; then cp "$SRC/.env" "$DEST/.env"; else cp "$DEST/.env.example" "$DEST/.env"; fi
fi
grep -q "^PORT=" "$DEST/.env" && sed -i '' "s/^PORT=.*/PORT=$PORT/" "$DEST/.env" || echo "PORT=$PORT" >> "$DEST/.env"
if ! grep -q "^SECRET_KEY=..*" "$DEST/.env"; then
  sed -i '' '/^SECRET_KEY=/d' "$DEST/.env"
  echo "SECRET_KEY=$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 64)" >> "$DEST/.env"
fi
grep -q "^SECURE_COOKIES=" "$DEST/.env" || echo "SECURE_COOKIES=1" >> "$DEST/.env"
ok "Code copied (your .env with API keys is kept)"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 "$HOME/.homebrew/bin/python3" /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then PY="$(command -v "$c")"; break; fi
done
[ -z "$PY" ] && { warn "Python 3.9+ not found. Install it with:  brew install python   then run this again."; exit 1; }
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip -q
"$VENV/bin/python" -m pip install -r "$DEST/requirements.txt" -q
ok "Python packages installed ($("$VENV/bin/python" --version))"

step "3/5  Starting Ticker Tycoon in the background"
cat > "$AG/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$VENV/bin/python</string><string>app.py</string></array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DEST/logs/server.log</string>
  <key>StandardErrorPath</key><string>$DEST/logs/server.log</string>
</dict></plist>
PLIST
launchctl load -w "$AG/$LABEL.plist"
for i in $(seq 1 30); do
  sleep 1
  curl -s -m 2 "http://localhost:$PORT/api/health" | grep -q "Ticker Tycoon" && break
done
if curl -s -m 2 "http://localhost:$PORT/api/health" | grep -q "Ticker Tycoon"; then
  ok "Ticker Tycoon is running on http://localhost:$PORT"
else
  warn "It didn't answer yet. Check:  tail -40 \"$DEST/logs/server.log\""
fi

step "4/5  Pointing $DOMAIN at Ticker Tycoon (Cloudflare tunnel)"
CFG="$HOME/.cloudflared/config.yml"
if [ ! -f "$CFG" ]; then
  warn "No tunnel config at $CFG on this Mac. Run this script on the Mac that runs the Cloudflare tunnel."
else
  cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"
  RESULT="$("$VENV/bin/python" - "$CFG" "$DOMAIN" "$PORT" <<'PYEOF'
import re, sys
path, domain, port = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path).read().split("\n")
want = "http://localhost:%s" % port
changed, found, i = False, False, 0
while i < len(lines):
    m = re.match(r"^(\s*)-\s*hostname:\s*['\"]?(?:www\.)?" + re.escape(domain) + r"['\"]?\s*$", lines[i])
    if m:
        found = True
        j = i + 1
        while j < len(lines) and not re.match(r"^\s*-\s", lines[j]):
            sm = re.match(r"^(\s*)service:\s*(\S+)", lines[j])
            if sm and sm.group(2) != want:
                lines[j] = "%sservice: %s" % (sm.group(1), want)
                changed = True
            j += 1
        i = j
        continue
    i += 1
if not found:  # add the domain just before the catch-all rule
    for k, l in enumerate(lines):
        if re.match(r"^\s*-\s*service:\s*http_status", l):
            ind = re.match(r"^(\s*)", l).group(1)
            lines[k:k] = ["%s- hostname: %s" % (ind, domain), "%s  service: %s" % (ind, want),
                          "%s- hostname: www.%s" % (ind, domain), "%s  service: %s" % (ind, want)]
            changed = True
            break
open(path, "w").write("\n".join(lines))
print(("added" if not found else "updated" if changed else "same") + "|" +
      next((l.split(":", 1)[1].strip() for l in lines if l.strip().startswith("tunnel:")), ""))
PYEOF
)"
  STATE="${RESULT%%|*}"; TUNNEL="${RESULT##*|}"
  case "$STATE" in
    same)    ok "$DOMAIN already points to port $PORT, nothing to change" ;;
    updated) ok "$DOMAIN now points to port $PORT (backup of the old config saved next to it)" ;;
    added)   ok "Added $DOMAIN to the tunnel config"
             CF="$(command -v cloudflared || true)"
             if [ -n "$CF" ] && [ -n "$TUNNEL" ]; then
               "$CF" tunnel route dns "$TUNNEL" "$DOMAIN" 2>&1 | tail -1 || true
               "$CF" tunnel route dns "$TUNNEL" "www.$DOMAIN" 2>&1 | tail -1 || true
             else
               warn "Run once:  cloudflared tunnel route dns <your-tunnel-name> $DOMAIN"
             fi ;;
    *)       warn "Couldn't read the tunnel config. Make sure $DOMAIN points to http://localhost:$PORT in $CFG" ;;
  esac
  if [ "$STATE" != "same" ]; then   # restart the tunnel so it picks up the new config
    T="$(grep -il "tunnel" "$AG"/*.plist 2>/dev/null | xargs grep -l "cloudflared" 2>/dev/null | head -1 || true)"
    if [ -n "$T" ]; then
      launchctl unload "$T" 2>/dev/null || true; launchctl load -w "$T"; ok "Restarted the tunnel ($(basename "$T"))"
    elif pgrep -x cloudflared >/dev/null; then
      warn "Restart cloudflared so it reads the new config (quit it and start it again)."
    fi
  fi
fi

step "5/5  Checking https://$DOMAIN"
sleep 5
code="$(curl -s -o /dev/null -w "%{http_code}" -m 10 "https://$DOMAIN/api/health" || true)"
if [ "$code" = "200" ]; then
  ok "https://$DOMAIN is live. Open it and create your account (the first account is the admin)."
else
  warn "https://$DOMAIN answered '$code'. The tunnel can take a minute; try again shortly:"
  echo "      curl -s https://$DOMAIN/api/health"
fi

echo
echo "Server logs:   tail -f \"$DEST/logs/server.log\""
echo "Stop it:       launchctl unload ~/Library/LaunchAgents/$LABEL.plist"
echo "Update later:  cd \"$SRC\" && bash server-setup.sh"
echo "Tip: keep this Mac awake (System Settings > Energy > Prevent automatic sleeping)."
