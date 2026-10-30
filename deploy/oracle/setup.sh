#!/usr/bin/env bash
# PaperTrader server setup for an Oracle Cloud Always Free VM. PAPER TRADING ONLY.
#
# Tested target: Ubuntu 22.04 / 24.04 on VM.Standard.A1.Flex (Ampere, arm64) or
# VM.Standard.E2.1.Micro (amd64, 1 GB RAM). Run it once, as the default "ubuntu" user:
#
#   curl -fsSL https://raw.githubusercontent.com/aeturijayasimhayadav2004/papertrader/main/deploy/oracle/setup.sh -o setup.sh
#   sudo bash setup.sh
#
# Safe to run again (it updates instead of reinstalling). What it does:
#   1. installs git, Python >= 3.11 (deadsnakes PPA on 22.04), unattended-upgrades
#   2. adds 2 GB swap when the VM has under 2 GB RAM (E2.1.Micro)
#   3. creates the service user "papertrader" (no login shell, no sudo)
#   4. clones the repo into /opt/papertrader and builds a venv there
#   5. creates /opt/papertrader/.env from env.example (chmod 600) if it is missing
#   6. installs the systemd units (timers are NOT enabled: you test first, see docs/SERVER.md)
#   7. installs /usr/local/bin/papertrader-status (health check)
#
# The system clock stays in UTC on purpose. The timers name Asia/Kolkata themselves
# and the bot computes IST internally, so nothing depends on the server's time zone.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/aeturijayasimhayadav2004/papertrader.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/papertrader}"
SVC_USER="${SVC_USER:-papertrader}"
UNIT_DIR=/etc/systemd/system

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$*"; }

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git ca-certificates curl software-properties-common unattended-upgrades tzdata

# ---------------------------------------------------------------- Python >= 3.11
PY=""
for cand in python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$(command -v "$cand")"
    break
  fi
done
if [[ -z "$PY" ]]; then
  log "Python 3.11 not found: adding the deadsnakes PPA (Ubuntu 22.04)"
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
  apt-get install -y python3.11 python3.11-venv python3.11-dev
  PY="$(command -v python3.11)"
else
  ver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  apt-get install -y "python${ver}-venv" || apt-get install -y python3-venv
fi
log "Using $PY ($("$PY" --version))"

# ---------------------------------------------------------------- swap on small VMs
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [[ $mem_mb -lt 2000 ]] && ! swapon --show | grep -q '/swapfile'; then
  log "Only ${mem_mb} MB RAM: adding a 2 GB swap file"
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------------------------------------------------------------- automatic security updates
log "Turning on unattended security upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades || true

# ---------------------------------------------------------------- service user and code
if ! id "$SVC_USER" >/dev/null 2>&1; then
  log "Creating service user $SVC_USER"
  useradd --system --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi

if [[ -d "$APP_DIR/.git" ]]; then
  log "Updating the code in $APP_DIR"
  sudo -u "$SVC_USER" git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  sudo -u "$SVC_USER" git -C "$APP_DIR" checkout --quiet "$BRANCH"
  sudo -u "$SVC_USER" git -C "$APP_DIR" pull --ff-only --quiet origin "$BRANCH"
else
  log "Cloning $REPO_URL into $APP_DIR"
  if [[ -d "$APP_DIR" ]] && [[ -n "$(ls -A "$APP_DIR")" ]]; then
    echo "$APP_DIR exists, is not empty and is not a git checkout. Move it away and run again." >&2
    exit 1
  fi
  mkdir -p "$APP_DIR"
  chown "$SVC_USER:$SVC_USER" "$APP_DIR"
  sudo -u "$SVC_USER" git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

log "Building the virtual environment"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  sudo -u "$SVC_USER" "$PY" -m venv "$APP_DIR/.venv"
fi
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR" --no-deps
sudo -u "$SVC_USER" mkdir -p "$APP_DIR/data/agent" "$APP_DIR/logs"

if [[ ! -f "$APP_DIR/.env" ]]; then
  log "Creating $APP_DIR/.env from env.example (fill it in: sudo -u $SVC_USER nano $APP_DIR/.env)"
  install -m 600 -o "$SVC_USER" -g "$SVC_USER" "$APP_DIR/deploy/oracle/env.example" "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"
chown "$SVC_USER:$SVC_USER" "$APP_DIR/.env"

# ---------------------------------------------------------------- systemd
log "Installing systemd units (not enabled yet)"
for unit in papertrader-intraday.service papertrader-intraday.timer papertrader-daily.service papertrader-daily.timer; do
  install -m 644 "$APP_DIR/deploy/oracle/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$UNIT_DIR/papertrader-intraday.service" "$UNIT_DIR/papertrader-intraday.timer" || true
  echo "Next intraday runs (IST):"
  # Fails loudly if a timer line is not valid systemd syntax on this systemd version.
  for spec in 'Mon..Fri *-*-* 09:15/5:30 Asia/Kolkata' 'Mon..Fri *-*-* 10..14:00/5:30 Asia/Kolkata'               'Mon..Fri *-*-* 15:00,05,10,15,20,25,30:30 Asia/Kolkata' 'Mon..Fri *-*-* 16:45:00 Asia/Kolkata'; do
    systemd-analyze calendar --iterations=2 "$spec" || echo "WARNING: bad OnCalendar: $spec"
  done
fi

cat > /usr/local/bin/papertrader-status <<EOF
#!/usr/bin/env bash
# Health check: last cycle, open paper positions, today's P&L, timer state.
cd "$APP_DIR" && sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/python" scripts/intraday_loop.py --status "\$@"
echo
systemctl list-timers 'papertrader-*' --no-pager || true
EOF
chmod 755 /usr/local/bin/papertrader-status

log "Running the test suite once"
sudo -u "$SVC_USER" bash -c "cd '$APP_DIR' && .venv/bin/python -m pytest -q" || echo "Tests failed: do not enable the timer until they pass."

cat <<EOF

Done. PaperTrader is in $APP_DIR, owned by $SVC_USER. PAPER TRADING ONLY.

Next steps (docs/SERVER.md has the details):
  1. Put your secrets in the .env file:
       sudo -u $SVC_USER nano $APP_DIR/.env
  2. Dry run (writes nothing real):
       cd $APP_DIR && sudo -u $SVC_USER .venv/bin/python scripts/intraday_loop.py --once --dry-run
  3. Turn on the 5-minute intraday timer (Mon-Fri 09:15-15:30 IST):
       sudo systemctl enable --now papertrader-intraday.timer
  4. Watch it:
       papertrader-status
       journalctl -u papertrader-intraday.service -n 50 --no-pager
  5. Updates later:
       sudo bash $APP_DIR/deploy/oracle/update.sh

Optional: papertrader-daily.timer (16:45 IST) is a backup for the GitHub Actions daily run.
Both are idempotent, so running both is safe, but GitHub Actions stays the primary.
  sudo systemctl enable --now papertrader-daily.timer
EOF
