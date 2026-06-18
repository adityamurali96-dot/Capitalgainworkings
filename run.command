#!/bin/bash
# Double-click on Mac to launch cg-engine. First run creates a venv and installs deps.
cd "$(dirname "$0")"
PY=$(command -v python3 || echo /usr/bin/python3)
if [ ! -d ".venv" ]; then
  "$PY" -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi
echo "Starting cg-engine — open http://127.0.0.1:5000"
( sleep 2 && open http://127.0.0.1:5000 ) &
./.venv/bin/python app.py
