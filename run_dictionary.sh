#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Lewis & Short Latin Dictionary — startup script
# Run this from the Dictionaries folder, or from anywhere if you set
# DICT_DIR below to the correct path.
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/lewis_short_app.py"

# Check for Flask
if ! python3 -c "import flask" 2>/dev/null; then
  echo "Flask is not installed. Installing now …"
  pip3 install flask --break-system-packages
fi

echo "Starting Lewis & Short Dictionary …"
echo "Open  http://localhost:5050  in your browser."
echo "Press Ctrl-C to stop."
echo ""

python3 "$APP"
