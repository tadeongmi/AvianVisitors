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

# iPad 4 landscape is 1024x768 CSS px at 2x -> a 2048x1536 PNG, pixel-exact.
# For a portrait panel, swap them (768x1024).
KIOSK_WIDTH=${KIOSK_WIDTH:-1024}
KIOSK_HEIGHT=${KIOSK_HEIGHT:-768}
KIOSK_DSF=${KIOSK_DSF:-2}
KIOSK_URL=${KIOSK_URL:-http://127.0.0.1/}
# How often to look for new detections, and the longest a frame may stand
# without any (the recent window still rolls birds off on its own).
KIOSK_INTERVAL=${KIOSK_INTERVAL:-2min}
KIOSK_MAX_AGE=${KIOSK_MAX_AGE:-600}
KIOSK_SHOOT_ARGS=${KIOSK_SHOOT_ARGS:-}
KIOSK_TITLE=${KIOSK_TITLE:-}
KIOSK_BIRD_NAMES=${KIOSK_BIRD_NAMES:-0}
# The panel's lettered header (site name, "heard in the last ..."), CSS px.
KIOSK_HEAD_PX=${KIOSK_HEAD_PX:-96}
# How much of the frame the birds claim, relative to the site's own collage.
KIOSK_FILL=${KIOSK_FILL:-2}
# Time windows the header toggle offers (hours, or "all"), and the one it opens on.
KIOSK_WINDOWS=${KIOSK_WINDOWS:-1 12 24 168 all}
KIOSK_DEFAULT_WINDOW=${KIOSK_DEFAULT_WINDOW:-24}

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
# shoot.py composes the PNG itself with Pillow (same pin as
# frame/requirements-shoot.txt).
"$VENV/bin/pip" install -q playwright "Pillow>=9.2,<12"

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
# Header name; empty uses the station's site name.
KIOSK_TITLE=$KIOSK_TITLE
KIOSK_HEAD_PX=$KIOSK_HEAD_PX
KIOSK_FILL=$KIOSK_FILL
KIOSK_WINDOWS="$KIOSK_WINDOWS"
KIOSK_DEFAULT_WINDOW=$KIOSK_DEFAULT_WINDOW
# Show common species names beside each bird (1 = on, 0 = off).
KIOSK_BIRD_NAMES=$KIOSK_BIRD_NAMES
# Re-render at least this often (seconds) even with no new detections.
KIOSK_MAX_AGE=$KIOSK_MAX_AGE
# Extra shoot.py flags, after the defaults so they win (e.g. --collage-vh 70).
KIOSK_SHOOT_ARGS="$KIOSK_SHOOT_ARGS"
$CHROMIUM_ENV
CONFEOF
sudo chmod 0644 "$CONF"

say "Installing the render helper"
sudo tee /usr/local/bin/avian-kiosk-shot >/dev/null <<HELPEOF
#!/usr/bin/env bash
# Render the collage once per time window the panel offers, each as a PNG plus
# a frame-<window>.json saying where each bird landed (the panel's tap
# targets), and swap them into the webroot atomically, so the panel never
# fetches a half-written file. The panel letters its own header, so the PNGs
# are birds only and KIOSK_HEAD_PX shorter than the screen.
set -euo pipefail
: "\${KIOSK_URL:=http://127.0.0.1/}"
: "\${KIOSK_WIDTH:=1024}"
: "\${KIOSK_HEIGHT:=768}"
: "\${KIOSK_DSF:=2}"
: "\${KIOSK_MAX_AGE:=600}"
: "\${KIOSK_HEAD_PX:=96}"
: "\${KIOSK_FILL:=2}"
: "\${KIOSK_WINDOWS:=1 12 24 168 all}"
: "\${KIOSK_DEFAULT_WINDOW:=24}"
dir=$WEBROOT/kiosk
out=\$dir/frame.png
map=\$dir/frame.json
# The timer fires every few minutes; only render when there is something new:
# a detection (the database changed), a settings edit, or a frame past
# KIOSK_MAX_AGE. Delete .rendered-for (or set KIOSK_FORCE=1) to render regardless.
stamp_file=\$dir/.rendered-for
stamp="\$(stat -c %Y "$STATION_DIR/scripts/birds.db" 2>/dev/null || echo 0) \$(stat -c %Y "$CONF" 2>/dev/null || echo 0)"
if [ "\${KIOSK_FORCE:-0}" != 1 ] && [ -s "\$out" ] && [ -s "\$map" ] \
   && [ "\$(cat "\$stamp_file" 2>/dev/null)" = "\$stamp" ] \
   && [ \$(( \$(date +%s) - \$(stat -c %Y "\$out") )) -lt "\$KIOSK_MAX_AGE" ]; then
  exit 0
fi
case " \$KIOSK_WINDOWS " in
  *" \$KIOSK_DEFAULT_WINDOW "*) ;;
  *) echo "KIOSK_DEFAULT_WINDOW=\$KIOSK_DEFAULT_WINDOW is not in KIOSK_WINDOWS" >&2; exit 1 ;;
esac
tmp=\$(mktemp -d "\$dir/.render.XXXXXX")
trap 'rm -rf "\$tmp"' EXIT
args=(--url "\$KIOSK_URL" --width "\$KIOSK_WIDTH" --height "\$((KIOSK_HEIGHT - KIOSK_HEAD_PX))"
      --dsf "\$KIOSK_DSF" --no-head --fill "\$KIOSK_FILL" --collage-vh 96 --mat 0.02)
# shoot.py's defaults are tuned for a portrait frame. On a landscape panel,
# spread the cluster to the panel's full width and height.
if [ "\$KIOSK_WIDTH" -gt "\$KIOSK_HEIGHT" ]; then
  args+=(--cluster-xbias 1.5 --cluster-ybias 1.5)
fi
# The panel letters the header from frame.json: the station's site name, or
# KIOSK_TITLE when set.
[ -n "\${KIOSK_TITLE:-}" ] && args+=(--title "\$KIOSK_TITLE")
case "\${KIOSK_BIRD_NAMES:-0}" in 1|true|yes|on) args+=(--bird-names) ;; esac
# shellcheck disable=SC2206  # word-split on purpose: a list of flags
[ -n "\${KIOSK_SHOOT_ARGS:-}" ] && args+=(\$KIOSK_SHOOT_ARGS)
cd "$FRAME_DIR"
render() {
  local win=\$1 hours=\$1 attempt=1
  [ "\$win" = all ] && hours=1000000
  # shoot.py intermittently fails with "frame labels missing for: <species>"
  # when --bird-names is on; an identical re-run succeeds. Retry rather than
  # leave a stale PNG up for the whole interval.
  until "$VENV/bin/python" "$FRAME_DIR/shoot.py" "\${args[@]}" --window-hours "\$hours" \
          --out "\$tmp/frame-\$win.png" --hotspots "\$tmp/frame-\$win.json"; do
    if [ "\$attempt" -ge 3 ]; then
      echo "render of window \$win failed after \$attempt attempts" >&2
      return 1
    fi
    echo "render of window \$win attempt \$attempt failed; retrying" >&2
    attempt=\$((attempt + 1))
    sleep 5
  done
}
ok=()
for win in \$KIOSK_WINDOWS; do
  # A window that will not render keeps its previous frame; the others still update.
  render "\$win" && ok+=("\$win") || true
done
[ \${#ok[@]} -gt 0 ] || { echo "no window rendered" >&2; exit 1; }
chmod 0644 "\$tmp"/frame-*
for win in "\${ok[@]}"; do
  # PNG first: the panel polls the JSON and then fetches the PNG it describes.
  mv -f "\$tmp/frame-\$win.png" "\$dir/frame-\$win.png"
  mv -f "\$tmp/frame-\$win.json" "\$dir/frame-\$win.json"
done
# frame.png/frame.json stay the default window, for panels that predate the
# window toggle (and for the freshness check above).
if [ -s "\$dir/frame-\$KIOSK_DEFAULT_WINDOW.png" ]; then
  cp -f "\$dir/frame-\$KIOSK_DEFAULT_WINDOW.png" "\$tmp/frame.png"
  cp -f "\$dir/frame-\$KIOSK_DEFAULT_WINDOW.json" "\$tmp/frame.json"
  mv -f "\$tmp/frame.png" "\$out"
  mv -f "\$tmp/frame.json" "\$map"
fi
printf '{"windows":[%s],"default":"%s","head_px":%d}\n' \
  "\$(printf '"%s",' \$KIOSK_WINDOWS | sed 's/,\$//')" "\$KIOSK_DEFAULT_WINDOW" "\$KIOSK_HEAD_PX" \
  > "\$tmp/windows.json"
chmod 0644 "\$tmp/windows.json"
mv -f "\$tmp/windows.json" "\$dir/windows.json"
# Only a full set counts as current; a partial one is retried next tick.
[ \${#ok[@]} -eq \$(echo \$KIOSK_WINDOWS | wc -w) ] && echo "\$stamp" > "\$stamp_file"
exit 0
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
