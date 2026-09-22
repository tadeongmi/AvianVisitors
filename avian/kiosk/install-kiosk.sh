#!/usr/bin/env bash
# Set up the PNG wall display for browsers too old to run the collage itself.
#
# The Pi renders the real site with headless Chromium and writes a PNG into the
# Caddy webroot; the panel just shows that picture. Built for an iPad 4 (Safari
# 10), which cannot lay out the collage stylesheets, but it suits any old tablet
# or e-ink browser.
set -euo pipefail

STATION_DIR=${STATION_DIR:-$HOME/BirdNET-Pi}
FRAME_DIR=$STATION_DIR/frame
KIOSK_SRC=$STATION_DIR/avian/kiosk
VENV=${VENV:-$STATION_DIR/frame/.venv-kiosk}
CONF=/etc/avian-kiosk.conf

# iPad 4 portrait is 768x1024 CSS px at 2x -> a 1536x2048 PNG, pixel-exact.
KIOSK_WIDTH=${KIOSK_WIDTH:-768}
KIOSK_HEIGHT=${KIOSK_HEIGHT:-1024}
KIOSK_DSF=${KIOSK_DSF:-2}
KIOSK_URL=${KIOSK_URL:-http://127.0.0.1/}
KIOSK_INTERVAL=${KIOSK_INTERVAL:-10min}
KIOSK_TITLE=${KIOSK_TITLE:-}
KIOSK_SUBTITLE=${KIOSK_SUBTITLE:-}
KIOSK_BIRD_NAMES=${KIOSK_BIRD_NAMES:-0}

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -d "$FRAME_DIR" ] || die "no frame/ in $STATION_DIR -- is the station installed?"
[ -f "$KIOSK_SRC/index.html" ] || die "no kiosk page at $KIOSK_SRC/index.html"

say "Locating the Caddy webroot"
CADDYFILE=$(ls /etc/caddy/Caddyfile 2>/dev/null || true)
[ -n "$CADDYFILE" ] || die "no /etc/caddy/Caddyfile"
WEBROOT=$(sudo awk '/^[[:space:]]*root[[:space:]]+\*[[:space:]]+/ {print $3; exit}' "$CADDYFILE")
[ -n "$WEBROOT" ] && [ -d "$WEBROOT" ] || die "could not read a usable webroot from $CADDYFILE"
echo "webroot: $WEBROOT"

say "Creating the kiosk directory"
sudo install -d -o "$USER" -g "$USER" -m 0755 "$WEBROOT/kiosk"
install -m 0644 "$KIOSK_SRC/index.html" "$WEBROOT/kiosk/index.html"

say "Building the screenshot virtualenv"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-dev
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q playwright

say "Providing a Chromium for Playwright"
# Playwright has no Chromium build for every ARM64 distribution, and
# `install-deps` does not know every Debian release. Try its own browser first,
# then fall back to the distro package via AVIAN_CHROMIUM.
CHROMIUM_ENV=""
if sudo "$VENV/bin/playwright" install-deps chromium >/dev/null 2>&1 \
   && "$VENV/bin/playwright" install chromium >/dev/null 2>&1 \
   && "$VENV/bin/python" - <<'PY' >/dev/null 2>&1
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch().close()
PY
then
  echo "using Playwright's bundled Chromium"
else
  echo "bundled Chromium unusable here; falling back to the distro package"
  sudo apt-get install -y -qq chromium || sudo apt-get install -y -qq chromium-browser
  SYS_CHROMIUM=$(command -v chromium || command -v chromium-browser || true)
  [ -n "$SYS_CHROMIUM" ] || die "no system chromium available either"
  "$VENV/bin/python" - "$SYS_CHROMIUM" <<'PY' >/dev/null 2>&1 || die "system chromium failed to launch under Playwright"
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch(executable_path=sys.argv[1]).close()
PY
  echo "using $SYS_CHROMIUM"
  CHROMIUM_ENV="AVIAN_CHROMIUM=$SYS_CHROMIUM"
fi

say "Writing $CONF"
sudo tee "$CONF" >/dev/null <<CONFEOF
# Wall-display render settings. Edit, then: sudo systemctl restart avian-kiosk.timer
KIOSK_URL=$KIOSK_URL
KIOSK_WIDTH=$KIOSK_WIDTH
KIOSK_HEIGHT=$KIOSK_HEIGHT
KIOSK_DSF=$KIOSK_DSF
KIOSK_TITLE=$KIOSK_TITLE
KIOSK_SUBTITLE=$KIOSK_SUBTITLE
# Show common species names beside each bird (1 = on, 0 = off).
KIOSK_BIRD_NAMES=$KIOSK_BIRD_NAMES
$CHROMIUM_ENV
CONFEOF
sudo chmod 0644 "$CONF"

say "Installing the render helper"
sudo tee /usr/local/bin/avian-kiosk-shot >/dev/null <<HELPEOF
#!/usr/bin/env bash
# Render the collage to a PNG and swap it into the webroot atomically, so the
# panel never fetches a half-written file.
set -euo pipefail
: "\${KIOSK_URL:=http://127.0.0.1/}"
: "\${KIOSK_WIDTH:=768}"
: "\${KIOSK_HEIGHT:=1024}"
: "\${KIOSK_DSF:=2}"
out=$WEBROOT/kiosk/frame.png
tmp=\$(mktemp "$WEBROOT/kiosk/.frame.XXXXXX.png")
trap 'rm -f "\$tmp"' EXIT
args=(--url "\$KIOSK_URL" --out "\$tmp"
      --width "\$KIOSK_WIDTH" --height "\$KIOSK_HEIGHT" --dsf "\$KIOSK_DSF")
[ -n "\${KIOSK_TITLE:-}" ] && args+=(--title "\$KIOSK_TITLE")
[ -n "\${KIOSK_SUBTITLE:-}" ] && args+=(--subtitle "\$KIOSK_SUBTITLE")
case "\${KIOSK_BIRD_NAMES:-0}" in 1|true|yes|on) args+=(--bird-names) ;; esac
cd "$FRAME_DIR"
# shoot.py intermittently fails with "frame labels missing for: <species>"
# when --bird-names is on. The cause is upstream and not understood here;
# an identical re-run succeeds. On a timer-driven wall display a single
# flaky render would leave a stale PNG up for the whole interval, so retry
# rather than give up.
attempt=1
until "$VENV/bin/python" "$FRAME_DIR/shoot.py" "\${args[@]}"; do
  if [ "\$attempt" -ge 3 ]; then
    echo "render failed after \$attempt attempts" >&2
    exit 1
  fi
  echo "render attempt \$attempt failed; retrying" >&2
  attempt=\$((attempt + 1))
  sleep 5
done
chmod 0644 "\$tmp"
mv -f "\$tmp" "\$out"
trap - EXIT
HELPEOF
sudo chmod 0755 /usr/local/bin/avian-kiosk-shot

say "Installing the systemd units"
sudo tee /etc/systemd/system/avian-kiosk.service >/dev/null <<UNITEOF
[Unit]
Description=Render the AvianVisitors collage to a PNG for the wall display
Wants=network-online.target
After=network-online.target caddy.service

[Service]
Type=oneshot
User=$USER
EnvironmentFile=-$CONF
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/local/bin/avian-kiosk-shot
Nice=10
TimeoutStartSec=300
UNITEOF

sudo tee /etc/systemd/system/avian-kiosk.timer >/dev/null <<TIMEREOF
[Unit]
Description=Refresh the AvianVisitors wall-display PNG

[Timer]
OnBootSec=3min
OnActiveSec=1min
OnUnitActiveSec=$KIOSK_INTERVAL
Persistent=true

[Install]
WantedBy=timers.target
TIMEREOF

sudo systemctl daemon-reload
sudo systemctl enable --now avian-kiosk.timer

say "Rendering the first frame (this can take a minute)"
sudo systemctl start avian-kiosk.service
[ -s "$WEBROOT/kiosk/frame.png" ] || die "no frame.png was produced; see: journalctl -u avian-kiosk.service"

say "Done"
echo "Panel URL:  http://$(hostname -I | awk '{print $1}')/kiosk/"
echo "Frame PNG:  $WEBROOT/kiosk/frame.png ($(stat -c%s "$WEBROOT/kiosk/frame.png") bytes)"
echo "Refresh:    every $KIOSK_INTERVAL (systemctl list-timers avian-kiosk.timer)"
