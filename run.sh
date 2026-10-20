#!/usr/bin/env bash
# Ticker Tycoon - one-command start:  bash run.sh
# Creates a virtual environment, installs requirements, starts the server on port 5558 and opens your browser.
set -e
cd "$(dirname "$0")"
PORT="${PORT:-5558}"

# Already running? Just open it.
if curl -s -m 2 "http://localhost:$PORT/api/health" | grep -q "Ticker Tycoon"; then
  echo "Ticker Tycoon is already running on http://localhost:$PORT"
  open "http://localhost:$PORT" 2>/dev/null || true
  exit 0
fi

# The Python environment lives OUTSIDE this folder: Desktop/Documents are often synced to iCloud,
# and thousands of synced library files make Python start extremely slowly (or hang).
if [ "$(uname)" = "Darwin" ]; then
  APPDIR="$HOME/Library/Application Support/TickerTycoon"
else
  APPDIR="$HOME/.tickertycoon"
fi
VENV="${TT_VENV:-$APPDIR/venv}"
mkdir -p "$APPDIR"

# Clean up an old in-folder environment from an earlier version of this script
if [ -d .venv ]; then
  echo "Removing old .venv from this folder (moved to $VENV)..."
  rm -rf .venv || true
fi

# Pick the newest Python 3.9+ available (Homebrew first, then system)
PY=""
for c in python3.13 python3.12 python3.11 python3.10 \
         /opt/homebrew/bin/python3 "$HOME/.homebrew/bin/python3" /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9+ is required. Install it with:  brew install python"
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating Python environment with $($PY --version)..."
  "$PY" -m venv "$VENV"
fi

if [ ! -f "$VENV/.installed" ] || [ requirements.txt -nt "$VENV/.installed" ]; then
  echo "Installing requirements (first run takes a minute or two)..."
  "$VENV/bin/python" -m pip install --upgrade pip -q
  "$VENV/bin/python" -m pip install -r requirements.txt -q
  touch "$VENV/.installed"
fi

[ -f .env ] || cp .env.example .env

echo ""
echo "  Ticker Tycoon -> http://localhost:$PORT   (Ctrl+C to stop)"
echo ""
( for i in $(seq 1 240); do
    sleep 0.5
    if curl -s -m 1 "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
      open "http://localhost:$PORT" 2>/dev/null || true; break
    fi
  done ) &
exec "$VENV/bin/python" app.py
