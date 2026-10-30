#!/usr/bin/env bash
# Update PaperTrader on the VM: pull the code, refresh the venv, run the tests, reload the units.
#   sudo bash /opt/papertrader/deploy/oracle/update.sh
# Timers keep their enabled / disabled state. PAPER TRADING ONLY.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/papertrader}"
SVC_USER="${SVC_USER:-papertrader}"
BRANCH="${BRANCH:-main}"
UNIT_DIR=/etc/systemd/system

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

echo "==> Pulling $BRANCH"
sudo -u "$SVC_USER" git -C "$APP_DIR" pull --ff-only origin "$BRANCH"

echo "==> Installing requirements"
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR" --no-deps

echo "==> Tests"
if ! sudo -u "$SVC_USER" bash -c "cd '$APP_DIR' && .venv/bin/python -m pytest -q"; then
  echo "Tests FAILED: stopping the intraday timer so broken code does not trade (paper)." >&2
  systemctl stop papertrader-intraday.timer || true
  exit 1
fi

echo "==> Reloading systemd units"
for unit in papertrader-intraday.service papertrader-intraday.timer papertrader-daily.service papertrader-daily.timer; do
  install -m 644 "$APP_DIR/deploy/oracle/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
for t in papertrader-intraday.timer papertrader-daily.timer; do
  if systemctl is-enabled --quiet "$t"; then
    systemctl restart "$t"
  fi
done

systemctl list-timers 'papertrader-*' --no-pager || true
echo "Updated to $(sudo -u "$SVC_USER" git -C "$APP_DIR" log -1 --format='%h %s')."
