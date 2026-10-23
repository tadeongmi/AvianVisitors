#!/usr/bin/env bash
# Generate illustrations automatically for every newly detected species.
#
# Installs avian-autogen.timer: every 10 minutes it finds detected species with
# no artwork and runs autogen.py, which renders them with Gemini (the same path
# as the Atlas "generate" button), cuts them with BiRefNet, and registers the
# collage masks. Needs GEMINI_API_KEY in birdnet.conf. See README.md.
set -euo pipefail

STATION_DIR=${STATION_DIR:-$HOME/BirdNET-Pi}
AUTOGEN_SRC=$STATION_DIR/avian/autogen
VENV=$STATION_DIR/avian/scripts/.venv
WEB_USER=${WEB_USER:-caddy}
STATE_DIR=/var/lib/avian-autogen
LOCK=/run/lock/avian-generation.lock

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -f "$AUTOGEN_SRC/autogen.py" ] || die "no $AUTOGEN_SRC/autogen.py -- is the station installed?"
id "$WEB_USER" >/dev/null 2>&1 || die "web user '$WEB_USER' does not exist"
station_group=$(id -gn "$USER")

say "Building the image-pipeline virtualenv"
sudo apt-get install -y -qq python3-venv
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$STATION_DIR/avian/scripts/requirements.txt" scipy

say "Keeping the generation lock across reboots"
# /run is a tmpfs. The installer and updater create this lock, but nothing
# recreated it at boot, so the Atlas "generate" button (and this timer) failed
# with "generation lock unavailable" after any restart until the next update.
echo "f $LOCK 0660 root $station_group -" \
  | sudo tee /etc/tmpfiles.d/avian-generation-lock.conf >/dev/null
sudo systemd-tmpfiles --create /etc/tmpfiles.d/avian-generation-lock.conf

say "Installing the systemd units"
sudo tee /etc/systemd/system/avian-autogen.service >/dev/null <<UNITEOF
[Unit]
Description=Generate AvianVisitors illustrations for newly detected species
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$WEB_USER
Group=$WEB_USER
UMask=0002
WorkingDirectory=$STATION_DIR
StateDirectory=avian-autogen
# BiRefNet (~1 GB) downloads here on the first run.
Environment=U2NET_HOME=$STATE_DIR/u2net HOME=$STATE_DIR OMP_NUM_THREADS=4
ExecStart=$VENV/bin/python $AUTOGEN_SRC/autogen.py
Nice=10
TimeoutStartSec=2h
# BiRefNet peaks around 14 GB on CPU; keep it from crowding the station.
MemoryMax=16G
UNITEOF

sudo tee /etc/systemd/system/avian-autogen.timer >/dev/null <<UNITEOF
[Unit]
Description=Check for new species needing illustrations every 10 minutes

[Timer]
OnCalendar=*:0/10
Persistent=true

[Install]
WantedBy=timers.target
UNITEOF

sudo systemctl daemon-reload
sudo systemctl enable --now avian-autogen.timer

say "Installing the avian-regional review command"
# Review decisions live in autogen's state dir, so the tool runs as the web user.
sudo tee /usr/local/bin/avian-regional >/dev/null <<WRAPEOF
#!/usr/bin/env bash
# Review this station's own illustrations and export them as a catalog bundle.
# See avian/regional/README.md.
set -euo pipefail
if [ "\$(id -un)" != "$WEB_USER" ]; then
  exec sudo -u "$WEB_USER" -- "\$0" "\$@"
fi
exec "$VENV/bin/python" "$STATION_DIR/avian/regional/regional.py" "\$@"
WRAPEOF
sudo chmod 0755 /usr/local/bin/avian-regional

if ! sudo grep -qE '^GEMINI_API_KEY=.+' "$STATION_DIR/birdnet.conf"; then
  echo "note: no GEMINI_API_KEY in birdnet.conf yet; the timer idles until one is set"
fi

say "Done"
echo "Timer:  systemctl list-timers avian-autogen.timer"
echo "Log:    journalctl -u avian-autogen"
echo "Run now: sudo systemctl start avian-autogen.service"
