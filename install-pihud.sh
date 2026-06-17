#!/bin/bash
# install-pihud.sh - install the Pi 3B+ dual-display HUD as a hardened systemd service.
# Idempotent: safe to re-run. Run as your user (uses sudo) or as root.
#
#   chmod +x install-pihud.sh
#   ./install-pihud.sh 2>&1 | tee install-pihud.log
#
# Files expected alongside this script: pihud.py  pi_displays.py  ollama-hud-run.py

set -euo pipefail
export LC_ALL=C LANG=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

log()  { echo -e "\n\033[1;32m[+]\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*" >&2; }
fail() { echo -e "\033[1;31m[x]\033[0m $*" >&2; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
SVC_USER=huddisp
GRP=pihud
APP_DIR=/opt/pihud
NEED_REBOOT=0

if [[ $EUID -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi
$SUDO -v 2>/dev/null || true

REALUSER="${SUDO_USER:-$USER}"
[[ "$REALUSER" == "root" ]] && warn "Could not detect a non-root invoking user; FIFO group membership may need a manual gpasswd."
REALHOME="$(getent passwd "$REALUSER" | cut -d: -f6 || true)"

for f in pihud.py pi_displays.py ollama-hud-run.py; do
    [[ -f "$SRC/$f" ]] || fail "missing $f next to installer"
done

# ─── 1. Packages (apt, no pip) ───────────────────────────────────
log "Installing packages…"
$SUDO apt-get update -y
$SUDO apt-get install -y --no-install-recommends \
    python3 python3-pil python3-psutil python3-spidev \
    python3-gpiozero python3-lgpio python3-smbus \
    fonts-dejavu-core i2c-tools wireless-tools

# ─── 2. Service identity (least privilege) ───────────────────────
log "Creating group/user…"
getent group "$GRP" >/dev/null || $SUDO groupadd --system "$GRP"
if ! id "$SVC_USER" >/dev/null 2>&1; then
    $SUDO useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin --groups spi,i2c,gpio,"$GRP" "$SVC_USER"
else
    $SUDO usermod -aG spi,i2c,gpio,"$GRP" "$SVC_USER"
fi
# let the human write the AI FIFO
if [[ "$REALUSER" != "root" ]]; then
    $SUDO gpasswd -a "$REALUSER" "$GRP" >/dev/null || warn "add $REALUSER to $GRP failed"
fi

# ─── 3. Enable SPI + I2C (config.txt) ────────────────────────────
log "Ensuring SPI + I2C buses are enabled…"
CONFIG=""
for p in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$p" ]] && { CONFIG="$p"; break; }
done
[[ -z "$CONFIG" ]] && fail "config.txt not found"
$SUDO cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d-%H%M%S)"
add_param() {
    $SUDO grep -qxF "$1" "$CONFIG" || { echo "$1" | $SUDO tee -a "$CONFIG" >/dev/null; NEED_REBOOT=1; }
}
add_param "dtparam=spi=on"
add_param "dtparam=i2c_arm=on"

# ─── 4. Install application ───────────────────────────────────────
log "Installing application to $APP_DIR…"
$SUDO install -d -m 0755 "$APP_DIR"
$SUDO install -m 0644 "$SRC/pihud.py" "$APP_DIR/pihud.py"
$SUDO install -m 0644 "$SRC/pi_displays.py" "$APP_DIR/pi_displays.py"
$SUDO install -m 0755 "$SRC/ollama-hud-run.py" /usr/local/bin/ollama-hud-run
$SUDO install -d -m 0755 /etc/pihud
if [[ ! -f /etc/pihud/pihud.toml ]]; then
    $SUDO tee /etc/pihud/pihud.toml >/dev/null <<'EOF'
# Optional overrides for /opt/pihud/pihud.py defaults. Calibrate the light band:
# cover the OPT3002 -> note the value; phone torch on it -> note the value.
# light_min_nwcm2 = 10.0
# light_max_nwcm2 = 300000.0
# enable_keys = true
EOF
fi

# ─── 5. systemd unit (hardened, least privilege) ─────────────────
log "Installing systemd unit…"
$SUDO tee /etc/systemd/system/pihud.service >/dev/null <<'EOF'
[Unit]
Description=Pi dual-display HUD (OLED + 2.7" e-ink)
After=multi-user.target

[Service]
Type=simple
User=huddisp
Group=huddisp
SupplementaryGroups=spi i2c gpio pihud
RuntimeDirectory=pihud
RuntimeDirectoryMode=0751
ExecStartPre=/bin/sh -c 'test -p /run/pihud/ai.fifo || { mkfifo -m 0660 /run/pihud/ai.fifo && chgrp pihud /run/pihud/ai.fifo; }'
WorkingDirectory=/opt/pihud
ExecStart=/usr/bin/python3 /opt/pihud/pihud.py
Restart=always
RestartSec=3

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/run/pihud
PrivateTmp=yes
ProtectControlGroups=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_NETLINK AF_INET AF_INET6
IPAddressDeny=any
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
DevicePolicy=closed
DeviceAllow=/dev/spidev0.0 rw
DeviceAllow=/dev/spidev0.1 rw
DeviceAllow=/dev/i2c-1 rw
DeviceAllow=/dev/gpiochip0 rw
DeviceAllow=/dev/gpiochip1 rw
DeviceAllow=/dev/gpiomem rw

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable pihud.service >/dev/null

# ─── 6. Route `ollama run` through the HUD wrapper (per-user) ─────
log "Wiring 'ollama run' to the HUD wrapper for $REALUSER…"
if [[ -n "$REALHOME" && -d "$REALHOME" ]]; then
    BRC="$REALHOME/.bashrc"
    if ! grep -q "pihud ollama wrapper" "$BRC" 2>/dev/null; then
        $SUDO tee -a "$BRC" >/dev/null <<'EOF'

# >>> pihud ollama wrapper >>>
ollama() {
    if [ "$1" = "run" ]; then shift; command ollama-hud-run "$@";
    else command ollama "$@"; fi
}
# <<< pihud ollama wrapper <<<
EOF
        $SUDO chown "$REALUSER":"$REALUSER" "$BRC" 2>/dev/null || true
    fi
fi

# ─── 7. Start ─────────────────────────────────────────────────────
if [[ "$NEED_REBOOT" -eq 1 ]]; then
    warn "SPI/I2C were just enabled — reboot before the service can use them."
else
    log "Starting service…"
    $SUDO systemctl restart pihud.service || warn "start failed; check: journalctl -u pihud -e"
fi

# ─── Done ─────────────────────────────────────────────────────────
log "Done."
echo "  Smoke test : ./smoke-test-pihud.sh        (run with sudo for the panel stages)"
echo "  Live logs  : journalctl -u pihud -f"
echo "  Control    : sudo systemctl {status,restart,stop} pihud"
echo "  AI to e-ink: open a NEW shell, then:  ollama run qwenfast 'capital of sweden'"
[[ "$NEED_REBOOT" -eq 1 ]] && echo "  >>> sudo reboot first (buses enabled) <<<"
echo "  Re-login (or 'newgrp pihud') so your shell picks up the '$GRP' group."
