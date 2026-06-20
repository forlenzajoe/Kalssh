#!/usr/bin/env bash
# One-shot setup for the Kalshi edge watcher on a fresh Ubuntu/Debian server.
# Run it from INSIDE the project directory (where this repo was cloned):
#     cd ~/Kalssh && bash deploy/bootstrap.sh
#
# It installs Python, creates a virtualenv, installs dependencies, and registers
# the watcher as a 24/7 auto-restarting systemd service. It does NOT create your
# .env — you add that (with your Kalshi key) before or after, then restart the
# service. The watcher runs in LIVE mode and only sends notifications; it never
# places trades.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(whoami)"
SERVICE_NAME="kalshi-watcher"

echo "==> Project dir: $PROJECT_DIR   user: $RUN_USER"

echo "==> Installing system packages (python3, venv, pip, git)..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "==> Creating virtualenv and installing dependencies..."
cd "$PROJECT_DIR"
python3 -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt cryptography

echo "==> Ensuring data dir exists..."
mkdir -p data

if [ ! -f .env ]; then
  echo "==> NOTE: no .env found. Create it with your Kalshi key before the watcher"
  echo "    can see live prices. Template:"
  echo "      cp .env.example .env  &&  nano .env"
  echo "    (set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH; put the .pem on this server)"
fi

echo "==> Installing systemd service '$SERVICE_NAME'..."
TMP_UNIT="$(mktemp)"
sed -e "s|__DIR__|$PROJECT_DIR|g" -e "s|__USER__|$RUN_USER|g" \
    "$PROJECT_DIR/deploy/kalshi-watcher.service" > "$TMP_UNIT"
sudo cp "$TMP_UNIT" "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "$TMP_UNIT"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "==> Done. The watcher is installed and will run 24/7 (auto-restart on reboot)."
echo "    Status:   sudo systemctl status $SERVICE_NAME"
echo "    Logs:     journalctl -u $SERVICE_NAME -f"
echo "    Restart:  sudo systemctl restart $SERVICE_NAME   (after editing .env)"
