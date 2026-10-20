#!/bin/bash
# Ticker Tycoon: collects server info (no passwords or keys) into server-check.txt, then closes.
cd "$(dirname "$0")"
OUT="server-check.txt"
{
  echo "== date"; date
  echo "== host"; scutil --get ComputerName 2>/dev/null; sw_vers 2>/dev/null | tr '\n' ' '; echo
  echo "== python"; for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 python3; do command -v $c >/dev/null && echo "$c -> $($c --version 2>&1)"; done
  echo "== cloudflared"; command -v cloudflared; cloudflared --version 2>/dev/null; pgrep -lf cloudflared
  echo "== listening ports 5555-5560"
  for p in 5555 5556 5557 5558 5559 5560; do
    for pid in $(lsof -ti tcp:$p -sTCP:LISTEN 2>/dev/null); do
      echo "port $p pid $pid: $(ps -o command= -p $pid | cut -c1-160)"
      echo "   cwd: $(lsof -a -p $pid -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    done
  done
  echo "== LaunchAgents"; ls -1 ~/Library/LaunchAgents 2>/dev/null
  for f in ~/Library/LaunchAgents/*.plist; do
    if grep -qiE "schoolmarket|scholars|cloudflared|tunnel|5558|tycoon" "$f" 2>/dev/null; then
      echo "--- $f"; plutil -p "$f" 2>/dev/null | grep -iE "label|ProgramArguments|=> \"/|WorkingDirectory|python|server|tunnel" | head -12
    fi
  done
  echo "== cloudflared config (no secrets)"
  for f in ~/.cloudflared/config.yml ~/.cloudflared/config.yaml /etc/cloudflared/config.yml /opt/homebrew/etc/cloudflared/config.yml; do
    [ -f "$f" ] && { echo "--- $f"; grep -vi "secret\|token" "$f"; }
  done
  ls -1 ~/.cloudflared 2>/dev/null | sed 's/^/   file: /'
  echo "== schoolmarket folders"; ls -d ~/SchoolMarket* ~/*/SchoolMarket* ~/Desktop/*/SchoolMarket* 2>/dev/null
  echo "== public check"; curl -s -o /dev/null -w "schoolmarket.win -> %{http_code}\n" -m 10 https://schoolmarket.win/
  curl -s -m 10 https://schoolmarket.win/api/health | head -c 200; echo
  echo "== disk"; df -h ~ | tail -1
} > "$OUT" 2>&1
echo "Saved $(pwd)/$OUT"
sleep 2
exit 0
