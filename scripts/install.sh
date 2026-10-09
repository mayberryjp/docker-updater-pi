#!/usr/bin/env bash
# DockerGo installer for Raspberry Pi 4 + GeeekPi 3.5" SPI TFT (ILI9486 / XPT2046).
# Run as root on Raspberry Pi OS (Bookworm, 64-bit Lite recommended):
#     sudo bash scripts/install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

BOOT_CFG=/boot/firmware/config.txt
[ -f "$BOOT_CFG" ] || BOOT_CFG=/boot/config.txt
CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt

CONFIG_DIR=/boot/firmware/dockergo
[ -d /boot/firmware ] || CONFIG_DIR=/boot/dockergo

echo "==> Installing OS packages"
apt-get update
apt-get install -y python3-pip python3-pil python3-numpy fonts-dejavu-core \
                   network-manager git curl

echo "==> Ensuring Docker is installed"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

echo "==> Installing DockerGo (global, no venv)"
pip3 install --break-system-packages --no-cache-dir "$REPO_DIR"
pip3 install --break-system-packages --no-cache-dir --no-deps --force-reinstall "$REPO_DIR"

echo "==> Enabling the 3.5\" SPI panel overlay in $BOOT_CFG"
# GeeekPi / Waveshare high-speed 3.5" (MHS / MPI3501) uses the 'mhs35' overlay.
# If your panel stays white, run the vendor driver instead (goodtft LCD35-show:
#   git clone https://github.com/goodtft/LCD-show && sudo ./LCD-show/MHS35-show)
# and adjust/remove the lines below. For a plain ILI9486 panel, try 'tft35a'.
if ! grep -q "DockerGo 3.5" "$BOOT_CFG"; then
  cat >> "$BOOT_CFG" <<'EOF'

# --- DockerGo 3.5" SPI TFT ---
dtparam=spi=on
dtoverlay=mhs35:rotate=90
EOF
fi

echo "==> Seamless-boot tweaks (quiet console, no blanking, hidden cursor)"
if ! grep -q "consoleblank=0" "$CMDLINE"; then
  sed -i '1 s/$/ consoleblank=0 logo.nologo vt.global_cursor_default=0/' "$CMDLINE"
fi

echo "==> Installing config to $CONFIG_DIR"
install -d "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.json" ]; then
  cp "$REPO_DIR/config.example.json" "$CONFIG_DIR/config.json"
  echo "    wrote $CONFIG_DIR/config.json  (edit your sites / Wi-Fi here)"
fi

echo "==> Installing automatic GitHub update service"
UPDATE_ENV=/etc/default/dockergo-update
escaped_repo_dir=${REPO_DIR//\\/\\\\}
escaped_repo_dir=${escaped_repo_dir//\"/\\\"}
printf 'DOCKERGO_REPO_DIR="%s"\n' "$escaped_repo_dir" > "$UPDATE_ENV"
chmod 0644 "$UPDATE_ENV"
install -m 0755 "$REPO_DIR/scripts/update.sh" /usr/local/sbin/dockergo-update
install -m 0755 "$REPO_DIR/scripts/dockergo-wireguard-dispatcher" \
  /etc/NetworkManager/dispatcher.d/90-dockergo-wireguard
install -m 0644 "$REPO_DIR/systemd/dockergo-update.service" \
  /etc/systemd/system/dockergo-update.service
install -m 0644 "$REPO_DIR/systemd/dockergo-update.timer" \
  /etc/systemd/system/dockergo-update.timer

echo "==> Installing + enabling the systemd service"
SERVICE=/etc/systemd/system/dockergo.service
sed "s#/boot/firmware/dockergo/config.json#$CONFIG_DIR/config.json#" \
    "$REPO_DIR/systemd/dockergo.service" > "$SERVICE"
systemctl daemon-reload
systemctl enable --now dockergo-update.timer
systemctl enable dockergo.service

echo
echo "Done. Edit $CONFIG_DIR/config.json, then reboot:  sudo reboot"
echo "Follow logs with:  journalctl -u dockergo -f"
