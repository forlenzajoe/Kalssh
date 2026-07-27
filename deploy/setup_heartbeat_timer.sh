#!/usr/bin/env bash
# Adds a daily liveness report so you can tell "no edge found" apart from
# "silently broken". Sends an OK summary when healthy and an urgent push when
# scans have stalled.
#
# Run once:  bash deploy/setup_heartbeat_timer.sh
# Check:     systemctl list-timers kalshi-heartbeat.timer
# Test now:  systemctl start kalshi-heartbeat.service
# Logs:      journalctl -u kalshi-heartbeat -n 30 --no-pager
# Remove:    systemctl disable --now kalshi-heartbeat.timer

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || { echo "No venv python at $PY (run deploy/bootstrap.sh first)."; exit 1; }

cat > /etc/systemd/system/kalshi-heartbeat.service <<EOF
[Unit]
Description=Kalshi weather: daily liveness report to phone
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$DIR
# Exits 1 when stalled; that is a report, not a service failure.
ExecStart=-$PY -m src.cli health
User=root
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/kalshi-heartbeat.timer <<'EOF'
[Unit]
Description=Send the Kalshi liveness report once a day

[Timer]
OnCalendar=*-*-* 13:00:00 UTC
# Fire on boot too, so a restarted server confirms itself promptly.
OnBootSec=10min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kalshi-heartbeat.timer

echo
echo "Heartbeat installed. Next runs:"
systemctl list-timers kalshi-heartbeat.timer --no-pager
echo
echo "Sending one now to verify..."
systemctl start kalshi-heartbeat.service
sleep 4
journalctl -u kalshi-heartbeat -n 15 --no-pager
