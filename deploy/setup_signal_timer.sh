#!/usr/bin/env bash
# Adds a 4-hourly job on this server that scans for MODEL-based buy signals,
# pushes the high-conviction ones to your phone, and settles finished paper
# trades. This is separate from kalshi-watcher.service, which runs continuously
# and only alerts on the much rarer model-free (locked / arb) edges.
#
# Run once:  bash deploy/setup_signal_timer.sh
# Check:     systemctl list-timers kalshi-signals.timer
# Logs:      journalctl -u kalshi-signals -n 50 --no-pager
# Remove:    systemctl disable --now kalshi-signals.timer

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || { echo "No venv python at $PY (run deploy/bootstrap.sh first)."; exit 1; }

cat > /etc/systemd/system/kalshi-signals.service <<EOF
[Unit]
Description=Kalshi weather: log paper signals, push high-conviction ones, settle results
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=$PY -m src.cli scan --paper
ExecStart=$PY -m src.cli autosettle
User=root
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/kalshi-signals.timer <<'EOF'
[Unit]
Description=Run the Kalshi signal scan every 4 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=4h
# Catch up after downtime rather than silently skipping a window.
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kalshi-signals.timer

echo
echo "Timer installed. Next runs:"
systemctl list-timers kalshi-signals.timer --no-pager
echo
echo "Running it once now to verify..."
systemctl start kalshi-signals.service
sleep 5
journalctl -u kalshi-signals -n 20 --no-pager
